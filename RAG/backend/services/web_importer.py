"""URL 网页导入服务：抓取（httpx）→ 提取标题/正文 → 生成文件名

安全与健壮性约束（路由层 / 本模块双层校验）：
- 仅 http/https（validate_public_url 校验 scheme + netloc）
- SSRF 防护（P0-2）：初始 URL 与每一跳重定向目标（最多 5 跳）都解析 DNS 逐
  个校验 IP，禁止私网/环回/链路本地/保留地址段（127.0.0.0/8、10.0.0.0/8、
  172.16.0.0/12、192.168.0.0/16、169.254.0.0/16、0.0.0.0/8、::1、fc00::/7、
  fe80::/10 等）；DNS 解析失败/无法判断 → 拒绝更安全；
  内网资源请先下载后上传（前端提示同文案）
- GET 超时 30s（连接/读取），follow_redirects=False 手动逐跳校验后跟随
- User-Agent 设浏览器 UA（部分站点防爬拦截默认 UA）
- 响应体流式读取，累计 > 5MB 立即拒绝（不整包载入内存）
- 4xx/5xx / 超时 / 网络错误 → 抛 WebFetchError（中文消息，路由层转 400）
- 正文提取不引入重型库：正则去 script/style 等标签 + 去标签 +
  html.unescape 反转义 + 多空行压缩（项目无 BeautifulSoup 依赖）
"""
from __future__ import annotations

import asyncio
import html as html_mod
import ipaddress
import logging
import re
import socket
from typing import List, Tuple
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# 抓取约束
_FETCH_TIMEOUT = 30.0          # 秒
_MAX_BODY_BYTES = 5 * 1024 * 1024  # 5MB
_MAX_REDIRECTS = 5             # 手动跟随重定向上限
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 禁止访问的地址段（SSRF 防护：私网/环回/链路本地/保留地址，命中即拒绝）
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),       # 本网络/未指定
    ipaddress.ip_network("10.0.0.0/8"),      # 私网 A 类
    ipaddress.ip_network("127.0.0.0/8"),     # 环回
    ipaddress.ip_network("169.254.0.0/16"),  # 链路本地
    ipaddress.ip_network("172.16.0.0/12"),   # 私网 B 类
    ipaddress.ip_network("192.168.0.0/16"),  # 私网 C 类
    ipaddress.ip_network("100.64.0.0/10"),   # CGN 共享地址
    ipaddress.ip_network("198.18.0.0/15"),   # 基准测试保留
    ipaddress.ip_network("224.0.0.0/4"),     # 组播
    ipaddress.ip_network("240.0.0.0/4"),     # 保留
    ipaddress.ip_network("::1/128"),         # IPv6 环回
    ipaddress.ip_network("::/128"),          # IPv6 未指定
    ipaddress.ip_network("fc00::/7"),        # IPv6 唯一本地地址
    ipaddress.ip_network("fe80::/10"),       # IPv6 链路本地
    ipaddress.ip_network("2001:db8::/32"),   # 文档示例地址
]

# 文件名约束
_MAX_FILENAME_CHARS = 80       # 标题截断长度
# 文件名非法字符（跨平台路径/控制字符，统一替换为下划线）
_ILLEGAL_CHARS_RE = re.compile(r'[\\/:*?"<>|\r\n\t]')

# 需要整体剔除内容的标签（含其中嵌套标签与文本）
_SKIP_TAG_RE = re.compile(
    r"(?is)<(script|style|noscript|svg|head|iframe|template)\b.*?</\1>")
# 其余标签剥除（保留文本）
_STRIP_TAG_RE = re.compile(r"(?s)<[^>]+>")
# 多空行压缩为至多一个空行
_BLANK_LINES_RE = re.compile(r"\n{3,}")


class WebFetchError(Exception):
    """网页抓取/提取失败（消息为中文，路由层转 400）"""


def extract_page(html_text: str) -> Tuple[str, str]:
    """从 HTML 提取 (title, 正文纯文本)

    - title: <title> 优先，缺省取第一个 <h1>，都没有返回 ""
    - 正文: 剔除 script/style/noscript/svg/head 等标签内容 → 剥除其余标签 →
      html.unescape 反转义 → 行 strip + 多空行压缩为单空行
    """
    title = ""
    m = re.search(r"<title\b[^>]*>(.*?)</title>", html_text, re.I | re.S)
    if m:
        title = _strip_inner(m.group(1))
    if not title:
        m = re.search(r"<h1\b[^>]*>(.*?)</h1>", html_text, re.I | re.S)
        if m:
            title = _strip_inner(m.group(1))
    body = _SKIP_TAG_RE.sub(" ", html_text)
    body = _STRIP_TAG_RE.sub(" ", body)
    text = html_mod.unescape(body)
    lines = [ln.strip() for ln in text.splitlines()]
    text = _BLANK_LINES_RE.sub("\n\n", "\n".join(ln for ln in lines if ln))
    return title, text.strip()


def _strip_inner(s: str) -> str:
    """标签内文本提纯：剥内部标签 + 反转义 + 去空白"""
    s = _STRIP_TAG_RE.sub(" ", s)
    s = html_mod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def build_filename(title: str, url: str, existing: List[str]) -> str:
    """网页文档文件名 = {标题}.md；标题为空用域名兜底；重名自动加序号

    - 标题超长截断 80 字符；非法字符（路径分隔符等）替换为下划线
    - 与 existing（同知识库已用 original_name 列表）冲突时追加 (1)、(2)...
    """
    base = (title or "").strip()
    if not base:
        host = (urlparse(url).netloc or "网页").split("@")[-1]
        base = host or "网页"
    base = _ILLEGAL_CHARS_RE.sub("_", base).strip(" ._")
    if not base:
        base = "网页"
    if len(base) > _MAX_FILENAME_CHARS:
        base = base[:_MAX_FILENAME_CHARS]
    name = f"{base}.md"
    if name not in existing:
        return name
    i = 1
    while f"{base}({i}).md" in existing:
        i += 1
    return f"{base}({i}).md"


def _is_blocked_ip(ip_str: str) -> bool:
    """单个 IP 是否命中禁止段（IPv4-mapped IPv6 先还原为 IPv4 再判断）"""
    try:
        addr = ipaddress.ip_address(ip_str.split("%")[0])
    except ValueError:
        return True  # 无法解析的地址一律视为不可信，拒绝
    # IPv4-mapped IPv6（::ffff:192.168.1.1）还原为 IPv4 再判断，防绕过
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _PRIVATE_NETWORKS)


async def validate_public_url(url: str) -> str:
    """SSRF 防护：校验 URL 目标为公网地址（仅 http/https）

    - 解析 host 的全部 A/AAAA 记录，任一命中私网/环回/链路本地/保留段 → 拒绝
    - DNS 解析失败/无记录/无法判断 → 拒绝（更安全）
    - 返回规范化 url；非法抛 WebFetchError（中文消息，路由层转 400）
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise WebFetchError("仅支持 http/https 网址导入")
    host = parsed.hostname
    if not host:
        raise WebFetchError("无法访问该网址：URL 缺少主机名")
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, parsed.port or 80)
    except OSError:
        raise WebFetchError("无法访问该网址：域名解析失败")
    if not infos:
        raise WebFetchError("无法访问该网址：域名解析失败")
    for info in infos:
        ip = info[4][0] if len(info) > 4 else ""
        if not ip or _is_blocked_ip(ip):
            raise WebFetchError("不允许访问内网地址（内网资源请先下载后上传）")
    return url


async def fetch_webpage(url: str) -> Tuple[str, str]:
    """GET url → (title, 正文纯文本)；失败抛 WebFetchError（中文消息）

    SSRF 防护（P0-2）：初始 URL 与每一跳重定向目标（最多 5 跳）逐跳调用
    validate_public_url 校验，任一跳目标为内网/环回/保留地址或 DNS 解析失败
    → 拒绝（400）；请求用 follow_redirects=False 手动跟随，保证重定向目标
    同样经过校验后才发起请求。
    """
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(_FETCH_TIMEOUT),
                follow_redirects=False,
                headers={"User-Agent": _BROWSER_UA}) as client:
            current = await validate_public_url(url)
            redirects = 0
            chunks: List[bytes] = []
            while True:
                async with client.stream("GET", current) as resp:
                    if resp.status_code >= 400:
                        raise WebFetchError(
                            f"无法访问该网址：HTTP {resp.status_code}")
                    if resp.is_redirect:
                        location = resp.headers.get("location", "").strip()
                        if not location:
                            # 重定向状态但无 location：按普通响应处理
                            location = None
                        else:
                            redirects += 1
                            if redirects > _MAX_REDIRECTS:
                                raise WebFetchError(
                                    "无法访问该网址：重定向次数过多")
                            # 重定向目标先校验（相对地址拼全）再跟随
                            next_url = str(httpx.URL(current).join(location))
                            await validate_public_url(next_url)
                            current = next_url
                            continue
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_BODY_BYTES:
                            raise WebFetchError(
                                "网页内容超过 5MB 限制，拒绝导入")
                        chunks.append(chunk)
                    break
    except WebFetchError:
        raise
    except httpx.TimeoutException:
        raise WebFetchError("无法访问该网址：请求超时（30 秒），请稍后重试")
    except httpx.HTTPError as e:
        raise WebFetchError(
            f"无法访问该网址：网络错误（{e.__class__.__name__}）") from e
    html_text = b"".join(chunks).decode("utf-8", errors="replace")
    title, text = extract_page(html_text)
    if not text:
        raise WebFetchError("网页无可提取的文本内容（可能为纯 JS 渲染页面）")
    logger.info("网页抓取成功: %s（%s，%d 字符）", title or url, url, len(text))
    return title, text

"""P0-2 URL 导入 SSRF 防护测试

validate_public_url：仅 http/https；DNS 解析全部 A/AAAA 记录，任一命中私网/
环回/链路本地/保留段（127.0.0.0/8、10.0.0.0/8、172.16.0.0/12、192.168.0.0/16、
169.254.0.0/16、0.0.0.0/8、::1、fc00::/7、fe80::/10 等）→ 拒绝；DNS 解析失败
→ 拒绝更安全。
fetch_webpage：follow_redirects=False 手动逐跳校验（最多 5 跳），任一跳目标
非法 → WebFetchError（路由层转 400）。
"""
from __future__ import annotations

import asyncio
from urllib.parse import urljoin

import pytest

from backend.services.web_importer import (WebFetchError, fetch_webpage,
                                           validate_public_url)


def _blocked(url: str) -> str:
    """断言 URL 校验被拒绝并返回错误消息"""
    with pytest.raises(WebFetchError) as ei:
        asyncio.run(validate_public_url(url))
    return str(ei.value)


class TestValidatePublicUrl:

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/x",           # 环回
        "http://localhost/x",           # localhost 解析为环回
        "http://10.0.0.5/x",            # 私网 A 类
        "http://172.16.0.1/x",          # 私网 B 类
        "http://172.31.255.255/x",      # 私网 B 类边界
        "http://192.168.1.1/x",         # 私网 C 类
        "http://169.254.1.1/x",         # 链路本地
        "http://0.0.0.0/x",             # 本网络
        "http://[::1]/x",               # IPv6 环回
        "http://[fc00::1]/x",           # IPv6 唯一本地
        "http://[fe80::1]/x",           # IPv6 链路本地
    ])
    def test_private_and_loopback_blocked(self, url):
        """私网/环回/链路本地/保留地址 → 拒绝（不允许访问内网地址）"""
        msg = _blocked(url)
        assert "不允许访问内网地址" in msg

    def test_invalid_scheme_blocked(self):
        """非 http/https scheme → 拒绝"""
        msg = _blocked("ftp://example.com/file")
        assert "仅支持 http/https" in msg

    def test_dns_failure_blocked(self, monkeypatch):
        """DNS 解析失败 → 拒绝（更安全）"""
        def fail(host, port, *a, **k):
            raise OSError("Name or service not known")
        monkeypatch.setattr("backend.services.web_importer.socket.getaddrinfo",
                            fail)
        msg = _blocked("http://nonexistent-domain-xyz.invalid/")
        assert "域名解析失败" in msg

    def test_internal_domain_blocked(self, monkeypatch):
        """域名解析出内网 IP（mock DNS）→ 拒绝"""
        def fake(host, port, *a, **k):
            return [(2, 1, 6, "", ("10.0.0.1", port))]
        monkeypatch.setattr("backend.services.web_importer.socket.getaddrinfo",
                            fake)
        msg = _blocked("http://intranet.example.com/")
        assert "不允许访问内网地址" in msg

    def test_mixed_records_blocked(self, monkeypatch):
        """多 A 记录含任一内网 IP → 拒绝（如 DNS rebinding 场景）"""
        def fake(host, port, *a, **k):
            return [
                (2, 1, 6, "", ("93.184.216.34", port)),   # 公网
                (10, 1, 6, "", ("10.0.0.1", port)),   # 内网
            ]
        monkeypatch.setattr("backend.services.web_importer.socket.getaddrinfo",
                            fake)
        msg = _blocked("http://evil.example.com/")
        assert "不允许访问内网地址" in msg

    def test_public_domain_ok(self, monkeypatch):
        """公网域名（mock DNS）→ 通过并原样返回"""
        def fake(host, port, *a, **k):
            return [(2, 1, 6, "", ("93.184.216.34", port))]
        monkeypatch.setattr("backend.services.web_importer.socket.getaddrinfo",
                            fake)
        url = "http://example.com/page"
        assert asyncio.run(validate_public_url(url)) == url


# ---------------- fetch_webpage 逐跳校验 ----------------

class _FakeResp:
    def __init__(self, status_code, headers, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    @property
    def is_redirect(self):
        return (self.status_code in (301, 302, 303, 307, 308)
                and bool(self.headers.get("location")))

    async def aiter_bytes(self):
        yield self._body


class _StreamCM:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    """按调用顺序依次返回 responses；记录每次请求的 URL"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url):
        self.calls.append(url)
        status, headers, body = self._responses.pop(0)
        return _StreamCM(_FakeResp(status, headers, body))


class _FakeHttpx:
    """fake httpx 模块（仅 fetch_webpage 用到的部分）"""

    class Timeout:
        def __init__(self, *a, **k):
            pass

    class HTTPError(Exception):
        pass

    class TimeoutException(Exception):
        pass

    class URL:
        def __init__(self, url):
            self._url = url

        def join(self, other):
            return urljoin(self._url, other)

    AsyncClient = _FakeClient


def _public_dns(host, port, *a, **k):
    """mock DNS：example.com 等域名 → 公网 IP；IP 字面量原样返回"""
    mapping = {"example.com": "93.184.216.34",
               "redirect.example.com": "93.184.216.34"}
    ip = mapping.get(host, host)
    return [(2, 1, 6, "", (ip, port))]


def _install_fakes(monkeypatch, responses):
    """替换 web_importer 的 httpx 与 DNS，返回 fake client（可断言请求序列）"""
    client = _FakeClient(responses)
    monkeypatch.setattr("backend.services.web_importer.httpx", _FakeHttpx)
    monkeypatch.setattr(_FakeHttpx, "AsyncClient", lambda **k: client)
    monkeypatch.setattr("backend.services.web_importer.socket.getaddrinfo",
                        _public_dns)
    return client


class TestFetchWebpageSSRF:

    def test_redirect_to_internal_blocked(self, monkeypatch):
        """重定向目标为内网 IP → 拒绝（逐跳校验）"""
        client = _install_fakes(monkeypatch, [
            (302, {"location": "http://192.168.1.1/evil"}, b""),
            (200, {}, b"<html><body>should not reach</body></html>"),
        ])
        with pytest.raises(WebFetchError) as ei:
            asyncio.run(fetch_webpage("http://example.com/page"))
        assert "不允许访问内网地址" in str(ei.value)
        # 只发起了第一跳请求，未请求内网目标
        assert client.calls == ["http://example.com/page"]

    def test_redirect_to_internal_domain_blocked(self, monkeypatch):
        """重定向目标域名解析为内网（mock DNS）→ 拒绝"""
        _install_fakes(monkeypatch, [
            (302, {"location": "http://intranet.example.com/evil"}, b""),
        ])

        def fake(host, port, *a, **k):
            if host == "intranet.example.com":
                return [(2, 1, 6, "", ("10.0.0.8", port))]
            return _public_dns(host, port)
        monkeypatch.setattr("backend.services.web_importer.socket.getaddrinfo",
                            fake)
        with pytest.raises(WebFetchError) as ei:
            asyncio.run(fetch_webpage("http://example.com/page"))
        assert "不允许访问内网地址" in str(ei.value)

    def test_public_redirect_chain_success(self, monkeypatch):
        """重定向链全部公网 → 跟随并成功抓取"""
        client = _install_fakes(monkeypatch, [
            (302, {"location": "/page2"}, b""),
            (301, {"location": "http://redirect.example.com/final"}, b""),
            (200, {},
             ("<html><title>SSRF 测试</title><body>你好，世界！"
              "<script>evil()</script></body></html>").encode("utf-8")),
        ])
        title, text = asyncio.run(fetch_webpage("http://example.com/start"))
        assert title == "SSRF 测试"
        assert "你好，世界！" in text and "evil()" not in text
        assert client.calls == ["http://example.com/start",
                                "http://example.com/page2",
                                "http://redirect.example.com/final"]

    def test_too_many_redirects_blocked(self, monkeypatch):
        """超过 5 跳重定向 → 拒绝"""
        responses = [(302, {"location": f"/step{i}"}, b"")
                     for i in range(6)]
        _install_fakes(monkeypatch, responses)
        with pytest.raises(WebFetchError) as ei:
            asyncio.run(fetch_webpage("http://example.com/start"))
        assert "重定向次数过多" in str(ei.value)

    def test_invalid_scheme_blocked_at_first_hop(self, monkeypatch):
        """初始 URL scheme 非法 → 拒绝（fetch 链路同样防护）"""
        _install_fakes(monkeypatch, [])
        with pytest.raises(WebFetchError) as ei:
            asyncio.run(fetch_webpage("file:///etc/passwd"))
        assert "仅支持 http/https" in str(ei.value)

    def test_http_error_400(self, monkeypatch):
        """目标 4xx → 400 中文错误（原有行为保持）"""
        _install_fakes(monkeypatch, [(404, {}, b"")])
        with pytest.raises(WebFetchError) as ei:
            asyncio.run(fetch_webpage("http://example.com/missing"))
        assert "HTTP 404" in str(ei.value)

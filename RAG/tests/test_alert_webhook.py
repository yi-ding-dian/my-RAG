"""告警 webhook 推送测试：平台识别 / 钉钉加签 / 响应处理 / 不阻塞

背景：`notify_health` 灯色变化时除了落本地 `[ALERT]` 日志，还会推送到群机器人
（钉钉/企微/飞书）。本文件验证推送链路的**纯逻辑**部分——真实 HTTP 用 fake
client 替换，绝不在测试里发真消息。

**「消息必须含关键词」是本文件最关键的约束**：钉钉「自定义关键词」模式下，
消息不含关键词会被直接拒收（实测返回 errcode 310000 "关键词不匹配"）。红灯与
恢复消息都得带上，否则"故障恢复通知"发不出去。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import urllib.parse

import pytest

from backend.config import settings as config_settings
from backend.logger import alert as alert_mod
from backend.logger.alert import (_alert_text, _build_payload, _push_webhook,
                                  _schedule_webhook, _signed_dingtalk_url)

DINGTALK = "https://oapi.dingtalk.com/robot/send?access_token=abc"
WECOM = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
FEISHU = "https://open.feishu.cn/open-apis/bot/v2/hook/abc"


class TestWebhookDisabledInTests:
    """★ 防回归：测试环境绝不能加载真实 webhook"""

    def test_webhook_url_blank(self):
        """conftest 置空 ALERT_WEBHOOK_URL（.env 里有真实地址）

        不置空的话，凡触发 `notify_health` 的用例（logs overview/health/ack
        那些）都会向真实群推送消息——跑一次测试就刷屏一次。
        """
        assert config_settings.ALERT_WEBHOOK_URL == "", \
            "测试环境不应加载真实 webhook（检查 conftest 是否置空该环境变量）"
        assert config_settings.ALERT_WEBHOOK_SECRET == ""


class TestAlertText:
    """消息文本：红灯与恢复**都必须含「告警」**"""

    def test_red_contains_keyword(self):
        assert "告警" in _alert_text(True, "近 60 分钟有 3 条未确认的系统级故障")

    def test_recovery_contains_keyword(self):
        """★ 恢复消息也要含关键词

        钉钉关键词模式下不含关键词的消息发不出去——若漏了这条，用户会看到
        "故障通知能收到、恢复通知收不到"，还以为是没恢复。
        """
        assert "告警" in _alert_text(False, "近 60 分钟无系统级故障")

    def test_carries_summary(self):
        text = _alert_text(True, "摘要内容在这里")
        assert "摘要内容在这里" in text
        assert "红灯" in text

    def test_recovery_wording(self):
        text = _alert_text(False, "摘要")
        assert "恢复" in text and "红灯" not in text


class TestBuildPayload:
    """按域名识别平台（换平台不用改代码）"""

    def test_dingtalk_structure(self, monkeypatch):
        monkeypatch.setattr(config_settings, "ALERT_WEBHOOK_SECRET", "")
        url, payload = _build_payload(DINGTALK, "文本")
        assert payload == {"msgtype": "text", "text": {"content": "文本"}}
        assert "sign=" not in url, "未配密钥时不应加签"

    def test_dingtalk_signed_when_secret_set(self, monkeypatch):
        monkeypatch.setattr(config_settings, "ALERT_WEBHOOK_SECRET", "SECtest")
        url, payload = _build_payload(DINGTALK, "文本")
        assert "timestamp=" in url and "sign=" in url
        assert payload["msgtype"] == "text"

    def test_wecom_structure(self):
        """企业微信与钉钉同构（msgtype/text.content）"""
        url, payload = _build_payload(WECOM, "文本")
        assert payload == {"msgtype": "text", "text": {"content": "文本"}}
        assert url == WECOM, "企微无需签名，URL 不应被改"

    def test_feishu_structure(self):
        """飞书字段名不同：msg_type + content.text（下划线、多一层嵌套）"""
        url, payload = _build_payload(FEISHU, "文本")
        assert payload == {"msg_type": "text", "content": {"text": "文本"}}

    def test_unknown_platform_falls_back(self):
        url, payload = _build_payload("https://example.com/hook", "文本")
        assert payload == {"msgtype": "text", "text": {"content": "文本"}}


class TestDingtalkSign:
    """钉钉加签算法"""

    def test_sign_format_and_value(self, monkeypatch):
        monkeypatch.setattr(alert_mod.time, "time", lambda: 1700000000.0)
        secret = "SECabc123"
        url = _signed_dingtalk_url(DINGTALK, secret)
        ts = "1700000000000"
        # 独立算一遍期望值：sign = urlencode(base64(HMAC-SHA256(ts\n secret, secret)))
        string_to_sign = f"{ts}\n{secret}"
        digest = hmac.new(secret.encode("utf-8"),
                          string_to_sign.encode("utf-8"),
                          hashlib.sha256).digest()
        expect = urllib.parse.quote_plus(base64.b64encode(digest))
        assert f"timestamp={ts}" in url, "应带上毫秒时间戳"
        assert f"sign={expect}" in url, "签名值应为 urlencode 后的 base64"

    def test_sign_appended_with_correct_separator(self, monkeypatch):
        """URL 已有 query 时用 & 追加"""
        monkeypatch.setattr(alert_mod.time, "time", lambda: 1700000000.0)
        url = _signed_dingtalk_url(DINGTALK, "SECx")
        assert url.startswith(DINGTALK + "&timestamp=")

    def test_sign_appended_when_no_query(self, monkeypatch):
        monkeypatch.setattr(alert_mod.time, "time", lambda: 1700000000.0)
        url = _signed_dingtalk_url("https://oapi.dingtalk.com/robot/send", "SECx")
        assert "?timestamp=" in url


class _FakeResponse:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class _FakeClient:
    """记录 post 调用并返回预置响应的 fake httpx.AsyncClient"""

    def __init__(self, response: _FakeResponse, recorder: list, **kw):
        self._response = response
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self._recorder.append((url, json))
        return self._response


@pytest.fixture
def fake_http(monkeypatch):
    """返回 prepare(response) -> 调用记录列表"""
    posted: list = []

    def _prepare(response: _FakeResponse):
        monkeypatch.setattr(
            "httpx.AsyncClient",
            lambda **kw: _FakeClient(response, posted, **kw))
        return posted

    return _prepare


class TestPushWebhook:
    """推送与响应处理（HTTP 全 fake，不发真消息）"""

    def test_skipped_when_url_blank(self, monkeypatch):
        monkeypatch.setattr(config_settings, "ALERT_WEBHOOK_URL", "")
        asyncio.run(_push_webhook(True, "摘要"))     # 不应抛异常

    def test_success_dingtalk(self, monkeypatch, fake_http):
        monkeypatch.setattr(config_settings, "ALERT_WEBHOOK_URL", DINGTALK)
        monkeypatch.setattr(config_settings, "ALERT_WEBHOOK_SECRET", "")
        posted = fake_http(_FakeResponse(200, {"errcode": 0, "errmsg": "ok"}))
        asyncio.run(_push_webhook(True, "摘要"))
        assert len(posted) == 1
        url, payload = posted[0]
        assert payload["msgtype"] == "text"
        assert "告警" in payload["text"]["content"]

    def test_errcode_nonzero_logged(self, monkeypatch, fake_http, caplog):
        """钉钉返回 errcode != 0（如关键词不匹配）→ 记 warning，不抛"""
        monkeypatch.setattr(config_settings, "ALERT_WEBHOOK_URL", DINGTALK)
        fake_http(_FakeResponse(200, {"errcode": 310000,
                                      "errmsg": "关键词不匹配"}))
        import logging
        with caplog.at_level(logging.WARNING, logger="backend.alert"):
            asyncio.run(_push_webhook(True, "摘要"))
        assert any("未送达" in r.getMessage() for r in caplog.records)

    def test_feishu_code_nonzero_logged(self, monkeypatch, fake_http, caplog):
        """飞书用 code 字段（不是 errcode）"""
        monkeypatch.setattr(config_settings, "ALERT_WEBHOOK_URL", FEISHU)
        fake_http(_FakeResponse(200, {"code": 19001, "msg": "param invalid"}))
        import logging
        with caplog.at_level(logging.WARNING, logger="backend.alert"):
            asyncio.run(_push_webhook(True, "摘要"))
        assert any("未送达" in r.getMessage() for r in caplog.records)

    def test_http_error_logged(self, monkeypatch, fake_http, caplog):
        monkeypatch.setattr(config_settings, "ALERT_WEBHOOK_URL", DINGTALK)
        fake_http(_FakeResponse(500, None, "server error"))
        import logging
        with caplog.at_level(logging.WARNING, logger="backend.alert"):
            asyncio.run(_push_webhook(True, "摘要"))
        assert any("未送达" in r.getMessage() for r in caplog.records)

    def test_network_exception_swallowed(self, monkeypatch, caplog):
        """网络异常绝不上抛（推送是尽力而为，不能影响健康检查接口）"""
        monkeypatch.setattr(config_settings, "ALERT_WEBHOOK_URL", DINGTALK)

        class _Boom:
            def __init__(self, **kw):
                raise RuntimeError("connection refused")

        monkeypatch.setattr("httpx.AsyncClient", _Boom)
        import logging
        with caplog.at_level(logging.WARNING, logger="backend.alert"):
            asyncio.run(_push_webhook(True, "摘要"))
        assert any("推送失败" in r.getMessage() for r in caplog.records)


class TestScheduleWebhook:
    """调度：不阻塞调用方"""

    def test_skipped_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(config_settings, "ALERT_WEBHOOK_URL", "")
        calls: list = []

        async def _fake(is_red, summary):
            calls.append((is_red, summary))

        monkeypatch.setattr(alert_mod, "_push_webhook", _fake)

        async def _run():
            _schedule_webhook(True, "摘要")
            await asyncio.sleep(0.01)

        asyncio.run(_run())
        assert calls == [], "未配置 webhook 时不应调度"

    def test_schedules_when_configured(self, monkeypatch):
        monkeypatch.setattr(config_settings, "ALERT_WEBHOOK_URL", DINGTALK)
        calls: list = []

        async def _fake(is_red, summary):
            calls.append((is_red, summary))

        monkeypatch.setattr(alert_mod, "_push_webhook", _fake)

        async def _run():
            _schedule_webhook(True, "摘要")
            await asyncio.sleep(0.01)      # 让后台 task 跑起来

        asyncio.run(_run())
        assert calls == [(True, "摘要")]

    def test_no_event_loop_does_not_raise(self, monkeypatch):
        """同步上下文调用（无事件循环）→ 静默跳过，不抛 RuntimeError"""
        monkeypatch.setattr(config_settings, "ALERT_WEBHOOK_URL", DINGTALK)
        _schedule_webhook(True, "摘要")     # 不应抛异常

"""Tests for media-limit reject reply (_maybe_media_reject in app.py).

Verifies that when ``NormalizedEvent.skipped_media`` is non-empty, the adapter
sends exactly one fused reject reply to the originating chat via the relay's
``send_reject_message`` (with reply_to pointing at the original message),
and that the global / per-group toggle gates this behavior.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import aiohttp
import pytest

from onebot_adapter.app import AdapterService, _render_media_reject_details
from onebot_adapter.config import AdapterConfig, ConfigStore, GroupConfig
from onebot_adapter.relay.protocol import NormalizedEvent


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEBOT_ADAPTER_CONFIG", str(tmp_path / "cfg.json"))
    store = ConfigStore(AdapterConfig(
        onebot_reverse_ws_port=28870,
        hermes_ws_port=28871,
        webui_port=28872,
        self_id="123",
        hermes_ws_token="rejecttest",
        onebot_ws_token="obtest",
        onebot_mode="reverse",
    ))
    svc = AdapterService(store)
    svc._session = aiohttp.ClientSession()
    svc._init_components()
    yield svc
    if svc._onebot_forward:
        await svc._onebot_forward.stop()
    if svc._session and not svc._session.closed:
        await svc._session.close()


def _event(
    skipped: list[dict],
    *,
    msg_id: str = "100",
    chat_id: str = "group:42",
    chat_type: str = "group",
) -> NormalizedEvent:
    return NormalizedEvent(
        message_id=msg_id,
        chat_id=chat_id,
        chat_type=chat_type,  # type: ignore[arg-type]
        user_id="u1",
        user_name="Alice",
        text="hello",
        skipped_media=skipped,
    )


# ── _render_media_reject_details ─────────────────────────────────────────


def test_render_details_count_limit():
    skipped = [
        {"kind": "image", "idx": 3, "name": "", "reason": "超出数量限制", "detail": "已下载2个达到上限2"},
        {"kind": "image", "idx": 4, "name": "", "reason": "超出数量限制", "detail": "已下载2个达到上限2"},
    ]
    out = _render_media_reject_details(skipped)
    assert "[图3]: 超出数量限制:已下载2个达到上限2" in out
    assert "[图4]: 超出数量限制:已下载2个达到上限2" in out


def test_render_details_size_and_file_name():
    skipped = [
        {"kind": "image", "idx": 1, "name": "", "reason": "文件大小8MB超过限制5MB", "detail": ""},
        {"kind": "file", "idx": 2, "name": "report.pdf", "reason": "下载失败", "detail": "timeout"},
    ]
    out = _render_media_reject_details(skipped)
    lines = out.splitlines()
    assert lines[0] == "[图1]: 文件大小8MB超过限制5MB"
    assert lines[1] == "[文件2:report.pdf]: 下载失败:timeout"


def test_render_details_unknown_kind():
    skipped = [{"kind": "mystery", "idx": 1, "name": "", "reason": "x", "detail": ""}]
    assert _render_media_reject_details(skipped) == "[媒体1]: x"


# ── gating: empty / disabled / no relay ──────────────────────────────────


async def test_empty_skipped_no_call(service):
    service._relay.send_reject_message = AsyncMock(return_value=True)
    await service._maybe_media_reject(_event([]))
    service._relay.send_reject_message.assert_not_called()


async def test_disabled_no_call(service):
    service.store.patch(media_limit_reject_enabled=False)
    service._relay.send_reject_message = AsyncMock(return_value=True)
    await service._maybe_media_reject(_event([
        {"kind": "image", "idx": 1, "name": "", "reason": "超出数量限制", "detail": ""},
    ]))
    service._relay.send_reject_message.assert_not_called()


# ── enabled: group + DM ─────────────────────────────────────────────────


async def test_enabled_group_sends_one_fused_reply(service):
    service._relay.send_reject_message = AsyncMock(return_value=True)
    skipped = [
        {"kind": "image", "idx": 3, "name": "", "reason": "超出数量限制", "detail": "已下载2个达到上限2"},
        {"kind": "image", "idx": 4, "name": "", "reason": "文件大小8MB超过限制5MB", "detail": ""},
    ]
    await service._maybe_media_reject(_event(skipped, msg_id="100", chat_id="group:42"))
    service._relay.send_reject_message.assert_called_once()
    kwargs = service._relay.send_reject_message.call_args.kwargs
    assert kwargs["chat_id"] == "group:42"
    assert kwargs["reply_to"] == "100"
    msg = kwargs["message"]
    # Default template variables substituted
    assert "2" in msg  # {skipped_count}
    assert "[图3]: 超出数量限制:已下载2个达到上限2" in msg
    assert "[图4]: 文件大小8MB超过限制5MB" in msg


async def test_enabled_dm_sends_reply(service):
    service._relay.send_reject_message = AsyncMock(return_value=True)
    await service._maybe_media_reject(_event(
        [{"kind": "image", "idx": 1, "name": "", "reason": "下载失败", "detail": "timeout"}],
        msg_id="200", chat_id="100", chat_type="dm",
    ))
    service._relay.send_reject_message.assert_called_once()
    kwargs = service._relay.send_reject_message.call_args.kwargs
    assert kwargs["chat_id"] == "100"
    assert kwargs["reply_to"] == "200"
    assert "[图1]: 下载失败:timeout" in kwargs["message"]


async def test_send_failure_swallown(service):
    service._relay.send_reject_message = AsyncMock(side_effect=RuntimeError("boom"))
    # Must not raise
    await service._maybe_media_reject(_event([
        {"kind": "image", "idx": 1, "name": "", "reason": "超出数量限制", "detail": ""},
    ]))


# ── per-group override ──────────────────────────────────────────────────


async def test_group_override_false_blocks(service):
    service.store.patch(
        media_limit_reject_enabled=True,
        groups={"42": GroupConfig(group_id="42", media_limit_reject_enabled=False).to_dict()},
    )
    service._relay.send_reject_message = AsyncMock(return_value=True)
    await service._maybe_media_reject(_event(
        [{"kind": "image", "idx": 1, "name": "", "reason": "超出数量限制", "detail": ""}],
        chat_id="group:42",
    ))
    service._relay.send_reject_message.assert_not_called()


async def test_group_override_true_enables_when_global_off(service):
    service.store.patch(
        media_limit_reject_enabled=False,
        groups={"42": GroupConfig(group_id="42", media_limit_reject_enabled=True).to_dict()},
    )
    service._relay.send_reject_message = AsyncMock(return_value=True)
    await service._maybe_media_reject(_event(
        [{"kind": "image", "idx": 1, "name": "", "reason": "超出数量限制", "detail": ""}],
        chat_id="group:42",
    ))
    service._relay.send_reject_message.assert_called_once()


async def test_group_none_follows_global(service):
    service.store.patch(
        media_limit_reject_enabled=True,
        groups={"42": GroupConfig(group_id="42", media_limit_reject_enabled=None).to_dict()},
    )
    service._relay.send_reject_message = AsyncMock(return_value=True)
    await service._maybe_media_reject(_event(
        [{"kind": "image", "idx": 1, "name": "", "reason": "超出数量限制", "detail": ""}],
        chat_id="group:42",
    ))
    service._relay.send_reject_message.assert_called_once()

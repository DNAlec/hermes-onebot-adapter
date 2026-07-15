"""Tests for the Hermes relay WS send dispatch."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from onebot_adapter.config import AdapterConfig
from onebot_adapter.relay.hermes_ws import HermesRelayServer
from onebot_adapter.relay.protocol import (
    api_call_message,
    result_message,
    send_message,
)


def _make_relay_app(cfg: AdapterConfig | None = None):
    mock_api = MagicMock()
    mock_api.send_group_msg = AsyncMock(return_value={"message_id": "g1"})
    mock_api.send_private_msg = AsyncMock(return_value={"message_id": "p1"})
    mock_api.upload_group_file = AsyncMock(return_value=None)
    mock_api.upload_private_file = AsyncMock(return_value=None)
    if cfg is None:
        cfg = AdapterConfig(hermes_ws_token="testtoken", hermes_ws_path="/hermes")
    relay = HermesRelayServer(cfg, mock_api, adapter_version="0.1.0-test", onebot_connected_fn=lambda: True)
    import aiohttp.web

    app = aiohttp.web.Application()
    relay.add_routes(app)
    return app, mock_api, relay


async def test_send_text_private():
    app, mock_api, _ = _make_relay_app()
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                ready = await ws.receive_json(timeout=2)
                assert ready["type"] == "ready"
                await ws.send_json(send_message("send_text", "r1", "100", content="hello"))
                result = await ws.receive_json(timeout=2)
                assert result == result_message("r1", True, message_id="p1")
            mock_api.send_private_msg.assert_awaited_once_with(100, [{"type": "text", "data": {"text": "hello"}}])
    finally:
        await server.close()


async def test_send_text_group_with_reply():
    app, mock_api, _ = _make_relay_app()
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)
                await ws.send_json(send_message("send_text", "r2", "group:42", content="hi", reply_to="99"))
                result = await ws.receive_json(timeout=2)
                assert result["success"] is True
            call_args = mock_api.send_group_msg.await_args
            assert call_args.args[0] == 42
            segs = call_args.args[1]
            assert segs[0]["type"] == "reply"
            assert segs[1]["type"] == "text"
    finally:
        await server.close()


async def test_send_image_via_url():
    """send_image with image_url forwards the URL directly to OneBot."""
    app, mock_api, _ = _make_relay_app()
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)
                await ws.send_json(
                    send_message("send_image", "r3", "group:42", image_url="http://x/1.jpg", caption="cap")
                )
                result = await ws.receive_json(timeout=2)
                assert result["success"] is True
            segs = mock_api.send_group_msg.await_args.args[1]
            assert segs[0]["type"] == "image"
            assert segs[0]["data"]["file"] == "http://x/1.jpg"
            assert segs[1]["type"] == "text"
    finally:
        await server.close()


async def test_send_image_via_local_path():
    """send_image with image_url as a local path — forwarded to OneBot as-is."""
    app, mock_api, _ = _make_relay_app()
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)
                await ws.send_json(
                    send_message("send_image", "r3b", "group:42", image_url="/tmp/test.jpg")
                )
                result = await ws.receive_json(timeout=2)
                assert result["success"] is True
            segs = mock_api.send_group_msg.await_args.args[1]
            assert segs[0]["type"] == "image"
            assert segs[0]["data"]["file"] == "/tmp/test.jpg"
    finally:
        await server.close()


async def test_send_document_via_file_path():
    """send_document with file_path — forwarded to OneBot upload API."""
    app, mock_api, _ = _make_relay_app()
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)
                await ws.send_json(
                    send_message(
                        "send_document", "r5", "group:42",
                        file_path="/tmp/report.pdf", filename="report.pdf",
                    )
                )
                result = await ws.receive_json(timeout=5)
                assert result["success"] is True
            mock_api.upload_group_file.assert_awaited_once()
            args = mock_api.upload_group_file.await_args.args
            assert args[0] == 42
            assert args[1] == "/tmp/report.pdf"
            assert args[2] == "report.pdf"
    finally:
        await server.close()


async def test_send_image_with_reply_to():
    """send_image with reply_to — prepends a reply segment before the image."""
    app, mock_api, _ = _make_relay_app()
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)
                await ws.send_json(
                    send_message("send_image", "r3c", "group:42",
                                  image_url="http://x/1.jpg", caption="cap", reply_to="99")
                )
                result = await ws.receive_json(timeout=2)
                assert result["success"] is True
            segs = mock_api.send_group_msg.await_args.args[1]
            assert segs[0]["type"] == "reply"
            assert segs[0]["data"]["id"] == "99"
            assert segs[1]["type"] == "image"
            assert segs[2]["type"] == "text"
    finally:
        await server.close()


async def test_send_voice_with_caption_and_reply_to():
    """send_voice with caption and reply_to — prepends reply, appends caption text."""
    app, mock_api, _ = _make_relay_app()
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)
                await ws.send_json(
                    send_message("send_voice", "r4", "group:42",
                                  audio_path="/tmp/a.ogg", caption="voice caption",
                                  reply_to="55")
                )
                result = await ws.receive_json(timeout=2)
                assert result["success"] is True
            segs = mock_api.send_group_msg.await_args.args[1]
            assert segs[0]["type"] == "reply"
            assert segs[0]["data"]["id"] == "55"
            assert segs[1]["type"] == "record"
            assert segs[1]["data"]["file"] == "/tmp/a.ogg"
            assert segs[2]["type"] == "text"
            assert segs[2]["data"]["text"] == "voice caption"
    finally:
        await server.close()


async def test_send_video_with_reply_to():
    """send_video with reply_to — prepends a reply segment before the video."""
    app, mock_api, _ = _make_relay_app()
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)
                await ws.send_json(
                    send_message("send_video", "r4b", "group:42",
                                  video_path="/tmp/v.mp4", caption="vid cap", reply_to="77")
                )
                result = await ws.receive_json(timeout=2)
                assert result["success"] is True
            segs = mock_api.send_group_msg.await_args.args[1]
            assert segs[0]["type"] == "reply"
            assert segs[0]["data"]["id"] == "77"
            assert segs[1]["type"] == "video"
            assert segs[2]["type"] == "text"
    finally:
        await server.close()


async def test_send_document_with_caption_and_reply_to():
    """send_document with caption+reply_to — uploads file then sends follow-up text."""
    app, mock_api, _ = _make_relay_app()
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)
                await ws.send_json(
                    send_message("send_document", "r5b", "group:42",
                                  file_path="/tmp/report.pdf", filename="report.pdf",
                                  caption="doc caption", reply_to="88")
                )
                result = await ws.receive_json(timeout=5)
                assert result["success"] is True
            # File upload
            mock_api.upload_group_file.assert_awaited_once()
            # Follow-up caption text with reply segment
            assert mock_api.send_group_msg.await_count == 1
            segs = mock_api.send_group_msg.await_args.args[1]
            assert segs[0]["type"] == "reply"
            assert segs[0]["data"]["id"] == "88"
            assert segs[1]["type"] == "text"
            assert segs[1]["data"]["text"] == "doc caption"
    finally:
        await server.close()


async def test_api_call_passthrough():
    app, mock_api, _ = _make_relay_app()
    mock_api.call = AsyncMock(return_value={"data": {"group_name": "Test"}})
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)

                await ws.send_json(api_call_message("get_group_info", "r6", {"group_id": 42}))
                result = await ws.receive_json(timeout=2)
                assert result["success"] is True
                assert result["data"]["group_name"] == "Test"
    finally:
        await server.close()


async def test_ping_pong():
    app, _, _ = _make_relay_app()
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)
                await ws.send_json({"type": "ping", "v": 1})
                pong = await ws.receive_json(timeout=2)
                assert pong["type"] == "pong"
    finally:
        await server.close()


async def test_unauthorized_rejected():
    app, _, _ = _make_relay_app()
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await client.ws_connect("/hermes?token=wrong")
    finally:
        await server.close()


# ── send_text content is plain text (no {@QQ号} marker parsing) ───────────


async def test_send_text_at_marker_passed_through_as_plain_text():
    """{@QQ号} markers are NOT parsed — they are sent as literal text.

    Outbound @ mentions must go through the onebot_send_message tool with
    proper OneBot at segments; the send_text path treats content as plain
    text and does not split it into at + text segments.
    """
    app, mock_api, _ = _make_relay_app()
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)
                await ws.send_json(send_message("send_text", "r1", "group:42", content="{@123456} 你好"))
                result = await ws.receive_json(timeout=2)
                assert result["success"] is True
            segs = mock_api.send_group_msg.await_args.args[1]
            # Single text segment, marker preserved literally (no at segment).
            assert len(segs) == 1
            assert segs[0]["type"] == "text"
            assert segs[0]["data"]["text"] == "{@123456} 你好"
    finally:
        await server.close()


async def test_send_text_without_at_markers_is_plain_text():
    """Content without markers should produce a single text segment."""
    app, mock_api, _ = _make_relay_app()
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)
                await ws.send_json(send_message("send_text", "r1", "100", content="just plain text"))
                result = await ws.receive_json(timeout=2)
                assert result["success"] is True
            segs = mock_api.send_private_msg.await_args.args[1]
            assert len(segs) == 1
            assert segs[0]["type"] == "text"
            assert segs[0]["data"]["text"] == "just plain text"
    finally:
        await server.close()


# ── Send dedup (Gateway send_text timeout-retry mitigation) ──────────────


async def test_send_text_dedup_on_retry():
    """Same-content send_text within TTL returns cached result without re-sending."""
    app, mock_api, _ = _make_relay_app()
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)
                await ws.send_json(send_message("send_text", "r1", "group:42", content="hello"))
                r1 = await ws.receive_json(timeout=2)
                assert r1["success"] is True
                assert r1["message_id"] == "g1"
                # Retry with a fresh req_id but identical payload — mirrors the
                # Gateway's behaviour when the adapter's result frame times out.
                await ws.send_json(send_message("send_text", "r2", "group:42", content="hello"))
                r2 = await ws.receive_json(timeout=2)
                assert r2["success"] is True
                assert r2["message_id"] == "g1"  # cached msg_id
            # Only one actual send_group_msg call despite two send frames.
            mock_api.send_group_msg.assert_awaited_once()
    finally:
        await server.close()


async def test_send_text_dedup_disabled():
    """With dedup disabled, identical sends both reach the OneBot API."""
    cfg = AdapterConfig(
        hermes_ws_token="testtoken", hermes_ws_path="/hermes", send_dedup_enabled=False,
    )
    app, mock_api, _ = _make_relay_app(cfg)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)
                await ws.send_json(send_message("send_text", "r1", "group:42", content="hello"))
                await ws.receive_json(timeout=2)
                await ws.send_json(send_message("send_text", "r2", "group:42", content="hello"))
                await ws.receive_json(timeout=2)
            assert mock_api.send_group_msg.await_count == 2
    finally:
        await server.close()


async def test_send_text_dedup_expired():
    """After TTL expires, an identical send reaches the OneBot API again."""
    cfg = AdapterConfig(
        hermes_ws_token="testtoken", hermes_ws_path="/hermes",
        send_dedup_ttl_seconds=0.1,
    )
    app, mock_api, _ = _make_relay_app(cfg)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)
                await ws.send_json(send_message("send_text", "r1", "group:42", content="hello"))
                await ws.receive_json(timeout=2)
                await asyncio.sleep(0.2)  # exceed TTL
                await ws.send_json(send_message("send_text", "r2", "group:42", content="hello"))
                await ws.receive_json(timeout=2)
            assert mock_api.send_group_msg.await_count == 2
    finally:
        await server.close()


async def test_send_text_dedup_different_content():
    """Different content produces different fingerprints, so both sends go through."""
    app, mock_api, _ = _make_relay_app()
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)
                await ws.send_json(send_message("send_text", "r1", "group:42", content="hello"))
                await ws.receive_json(timeout=2)
                await ws.send_json(send_message("send_text", "r2", "group:42", content="world"))
                await ws.receive_json(timeout=2)
            assert mock_api.send_group_msg.await_count == 2
    finally:
        await server.close()


# ── Adapter-side send concurrency limit (semaphore) ─────────────────────


async def test_send_api_semaphore_limits_concurrent_calls():
    """The adapter limits concurrent OneBot API send calls to
    _MAX_CONCURRENT_SENDS (default 2) to prevent NapCat WS serialization
    from inflating latency past the plugin's 30s _RESULT_TIMEOUT.
    """
    app, mock_api, relay = _make_relay_app()
    # Track max concurrent calls
    concurrent = [0]
    max_concurrent = [0]

    original_send = mock_api.send_group_msg

    async def tracking_send(*args, **kwargs):
        concurrent[0] += 1
        max_concurrent[0] = max(max_concurrent[0], concurrent[0])
        await asyncio.sleep(0.05)  # simulate slow NapCat
        concurrent[0] -= 1
        return await original_send(*args, **kwargs)

    mock_api.send_group_msg = tracking_send

    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)
                # Fire 4 concurrent sends with different content (no dedup)
                for i in range(4):
                    await ws.send_json(send_message(
                        "send_text", f"r{i}", "group:42", content=f"msg{i}",
                    ))
                # Receive all 4 results
                for _ in range(4):
                    res = await ws.receive_json(timeout=5)
                    assert res["success"] is True
        # Max concurrent calls should not exceed _MAX_CONCURRENT_SENDS (2)
        assert max_concurrent[0] <= relay._MAX_CONCURRENT_SENDS, (
            f"max concurrent={max_concurrent[0]} exceeds limit {relay._MAX_CONCURRENT_SENDS}"
        )
    finally:
        await server.close()


async def test_send_api_semaphore_releases_on_success():
    """After a send completes, the semaphore slot is released and available
    for the next send (no leak)."""
    app, mock_api, relay = _make_relay_app()
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            async with client.ws_connect("/hermes?token=testtoken") as ws:
                await ws.receive_json(timeout=2)
                # Send 3 sequential sends — all should succeed
                for i in range(3):
                    await ws.send_json(send_message(
                        "send_text", f"r{i}", "group:42", content=f"msg{i}",
                    ))
                    res = await ws.receive_json(timeout=2)
                    assert res["success"] is True
            assert mock_api.send_group_msg.await_count == 3
    finally:
        await server.close()

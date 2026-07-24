import time

from aiohttp.test_utils import TestClient, TestServer
from conftest import make_session_token

from onebot_adapter.app import AdapterService
from onebot_adapter.config import AdapterConfig, ConfigStore
from onebot_adapter.relay.protocol import NormalizedEvent
from onebot_adapter.usage_stats import UsageStatsStore


def _event(
    *,
    group_id: str | None = "42",
    user_id: str = "100",
    user_name: str = "Alice",
    notice: bool = False,
) -> NormalizedEvent:
    return NormalizedEvent(
        message_id="1",
        chat_id=f"group:{group_id}" if group_id else user_id,
        chat_type="group" if group_id else "dm",
        user_id=user_id,
        user_name=user_name,
        text="hello",
        chat_name=f"{group_id}(Test)" if group_id else user_name,
        is_system_notice=notice,
    )


async def test_usage_store_records_duplicates_and_filters(tmp_path):
    store = UsageStatsStore(tmp_path / "usage.sqlite3")
    await store.start()
    try:
        await store.record(_event())
        await store.record(_event())
        await store.record(_event(group_id=None, user_id="200", user_name="Bob", notice=True))
        now = time.time()
        all_stats = await store.query(
            start=now - 60,
            end=now + 60,
            scope="all",
            group_id=None,
            user_id=None,
            bucket="hour",
            tz_offset_minutes=480,
        )
        assert all_stats["summary"] == {"total": 3, "active_groups": 1, "active_users": 2}
        assert all_stats["top_groups"][0]["count"] == 2
        assert all_stats["top_users"][0]["id"] == "100"

        dm_stats = await store.query(
            start=now - 60,
            end=now + 60,
            scope="dm",
            group_id=None,
            user_id="200",
            bucket="day",
            tz_offset_minutes=480,
        )
        assert dm_stats["summary"]["total"] == 1
        assert dm_stats["top_groups"] == []

        dimensions = await store.dimensions(now - 60, now + 60)
        assert dimensions["groups"] == [{"id": "42", "name": "Test"}]
        assert {row["id"] for row in dimensions["users"]} == {"100", "200"}
    finally:
        await store.close()


async def test_usage_store_prune_and_clear(tmp_path, monkeypatch):
    store = UsageStatsStore(tmp_path / "usage.sqlite3", retention_days=2)
    await store.start()
    try:
        monkeypatch.setattr("onebot_adapter.usage_stats.time.time", lambda: 1_000_000.0)
        await store.record(_event())
        monkeypatch.setattr("onebot_adapter.usage_stats.time.time", lambda: 1_000_000.0 + 3 * 86400)
        assert await store.prune() == 1
        await store.record(_event(group_id=None))
        assert await store.clear() == 1
        assert await store.dimensions(0, 2_000_000) == {"groups": [], "users": []}
    finally:
        await store.close()


async def test_service_counts_before_relay_and_respects_switch(tmp_path):
    cfg = AdapterConfig(onebot_ws_token="a", hermes_ws_token="b", usage_stats_enabled=True)
    service = AdapterService(ConfigStore(cfg))
    usage = UsageStatsStore(tmp_path / "usage.sqlite3")
    await usage.start()

    class BrokenRelay:
        has_clients = False

        async def push_event(self, event):
            raise RuntimeError("delivery failed")

    service._usage_stats = usage
    service._relay = BrokenRelay()
    try:
        try:
            await service._on_onebot_event(_event())
        except RuntimeError:
            pass
        service.store.patch(usage_stats_enabled=False)
        try:
            await service._on_onebot_event(_event())
        except RuntimeError:
            pass
        now = time.time()
        stats = await usage.query(
            start=now - 60, end=now + 60, scope="all", group_id=None, user_id=None,
            bucket="day", tz_offset_minutes=0,
        )
        assert stats["summary"]["total"] == 1
    finally:
        await usage.close()


async def test_usage_api_query_validation_and_clear(tmp_path):
    token = "secret"
    cfg = AdapterConfig(
        onebot_ws_token="a", hermes_ws_token="b", webui_token=token,
        webui_token_lifetime_hours=24,
    )
    service = AdapterService(ConfigStore(cfg))
    usage = UsageStatsStore(tmp_path / "usage.sqlite3")
    await usage.start()
    service._state["usage_stats"] = usage
    await usage.record(_event())
    server = TestServer(service.build_webui_app())
    await server.start_server()
    client = TestClient(server)
    auth = {"Authorization": f"Bearer {make_session_token(token, 0)}"}
    try:
        now = time.time()
        resp = await client.get(
            f"/api/usage/stats?start={now - 60}&end={now + 60}&scope=group&bucket=hour"
            "&tz_offset_minutes=480",
            headers=auth,
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["enabled"] is True
        assert body["summary"]["total"] == 1

        dimensions = await client.get(
            f"/api/usage/dimensions?start={now - 60}&end={now + 60}", headers=auth,
        )
        assert dimensions.status == 200
        assert (await dimensions.json())["groups"][0]["id"] == "42"

        invalid = await client.get("/api/usage/stats?scope=bad", headers=auth)
        assert invalid.status == 400
        unauthenticated = await client.delete("/api/usage")
        assert unauthenticated.status == 401
        cleared = await client.delete("/api/usage", headers=auth)
        assert cleared.status == 200
        assert (await cleared.json())["deleted"] == 1
    finally:
        await client.close()
        await server.close()
        await usage.close()


def test_usage_config_defaults_and_validation():
    cfg = AdapterConfig(onebot_ws_token="a", hermes_ws_token="b")
    assert cfg.usage_stats_enabled is True
    assert cfg.usage_stats_retention_days == 365
    cfg.usage_stats_retention_days = 0
    assert "usage_stats_retention_days must be at least 1" in cfg.validate()

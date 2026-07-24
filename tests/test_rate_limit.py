from __future__ import annotations

from onebot_adapter.app import AdapterService
from onebot_adapter.config import AdapterConfig, ConfigStore, GroupConfig
from onebot_adapter.rate_limit import MessageRateLimiter
from onebot_adapter.relay.protocol import NormalizedEvent


def _config(**overrides) -> AdapterConfig:
    values = {
        "onebot_ws_token": "onebot",
        "hermes_ws_token": "hermes",
        "rate_limit_enabled": True,
    }
    values.update(overrides)
    return AdapterConfig(**values)


def _event(*, user_id: str = "100", group_id: str | None = "42", eligible: bool = True):
    return NormalizedEvent(
        message_id="9",
        chat_id=f"group:{group_id}" if group_id else user_id,
        chat_type="group" if group_id else "dm",
        user_id=user_id,
        user_name="User",
        text="hello",
        rate_limit_eligible=eligible,
    )


async def test_sliding_window_expires():
    limiter = MessageRateLimiter()
    cfg = _config(global_rate_limit_messages=2, global_rate_limit_window_seconds=10)

    assert (await limiter.check(cfg, user_id="1", group_id=None, now=0)).allowed
    assert (await limiter.check(cfg, user_id="2", group_id=None, now=1)).allowed
    blocked = await limiter.check(cfg, user_id="3", group_id=None, now=2)
    assert not blocked.allowed
    assert blocked.scope == "global"
    assert blocked.retry_after == 8
    assert (await limiter.check(cfg, user_id="3", group_id=None, now=10)).allowed


async def test_token_bucket_refills_smoothly():
    limiter = MessageRateLimiter()
    cfg = _config(
        user_rate_limit_algorithm="token_bucket",
        user_rate_limit_messages=2,
        user_rate_limit_window_seconds=10,
    )

    assert (await limiter.check(cfg, user_id="1", group_id=None, now=0)).allowed
    assert (await limiter.check(cfg, user_id="1", group_id=None, now=0)).allowed
    blocked = await limiter.check(cfg, user_id="1", group_id=None, now=0)
    assert not blocked.allowed
    assert blocked.retry_after == 5
    assert (await limiter.check(cfg, user_id="1", group_id=None, now=5)).allowed


async def test_scopes_are_atomic_and_user_scope_is_global():
    limiter = MessageRateLimiter()
    cfg = _config(
        group_rate_limit_messages=1,
        group_rate_limit_window_seconds=60,
        user_rate_limit_messages=2,
        user_rate_limit_window_seconds=60,
    )

    assert (await limiter.check(cfg, user_id="1", group_id="42", now=0)).allowed
    assert not (await limiter.check(cfg, user_id="1", group_id="42", now=1)).allowed
    # The rejected group message did not consume the user's second slot.
    assert (await limiter.check(cfg, user_id="1", group_id=None, now=2)).allowed
    assert not (await limiter.check(cfg, user_id="1", group_id="99", now=3)).allowed


async def test_group_override_changes_policy_for_one_group():
    limiter = MessageRateLimiter()
    cfg = _config(
        group_rate_limit_messages=1,
        group_rate_limit_window_seconds=60,
        groups={
            "42": GroupConfig(
                group_id="42",
                group_rate_limit_messages=2,
                group_rate_limit_window_seconds=60,
            ).to_dict(),
        },
    )

    assert (await limiter.check(cfg, user_id="1", group_id="42", now=0)).allowed
    assert (await limiter.check(cfg, user_id="2", group_id="42", now=1)).allowed
    assert not (await limiter.check(cfg, user_id="3", group_id="42", now=2)).allowed
    assert (await limiter.check(cfg, user_id="1", group_id="99", now=0)).allowed
    assert not (await limiter.check(cfg, user_id="2", group_id="99", now=1)).allowed


async def test_service_rejects_with_reply_and_exempts_admin_and_member_notice():
    cfg = _config(
        global_admins=["1"],
        user_rate_limit_messages=1,
        user_rate_limit_window_seconds=60,
        rate_limit_reject_message="{scope}:{retry_after}:{user_id}",
    )
    service = AdapterService(ConfigStore(cfg))
    pushed = []
    rejected = []

    class Relay:
        has_clients = False

        async def push_event(self, event):
            pushed.append(event)
            return "broadcast"

        async def send_reject_message(self, chat_id, message, reply_to=None):
            rejected.append((chat_id, message, reply_to))
            return True

    service._relay = Relay()
    await service._on_onebot_event(_event(user_id="2"))
    await service._on_onebot_event(_event(user_id="2"))
    await service._on_onebot_event(_event(user_id="1"))
    await service._on_onebot_event(_event(user_id="2", eligible=False))

    assert len(pushed) == 3
    assert rejected == [("group:42", "个人:60:2", "9")]


def test_rate_limit_config_validation_and_group_resolvers():
    cfg = _config(global_rate_limit_messages=1, global_rate_limit_window_seconds=0)
    assert "global_rate_limit_window_seconds must be positive when the limit is enabled" in cfg.validate()

    cfg = _config(
        group_rate_limit_messages=5,
        group_rate_limit_window_seconds=30,
        groups={
            "42": GroupConfig(
                group_id="42",
                group_rate_limit_algorithm="token_bucket",
                group_rate_limit_messages=2,
                group_rate_limit_window_seconds=10,
            ).to_dict(),
        },
    )
    assert cfg.validate() == []
    assert cfg.resolve_group_rate_limit_algorithm("42") == "token_bucket"
    assert cfg.resolve_group_rate_limit_messages("42") == 2
    assert cfg.resolve_group_rate_limit_window_seconds("42") == 10
    assert cfg.resolve_group_rate_limit_messages("99") == 5

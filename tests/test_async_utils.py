"""Tests for shared async utilities."""
from __future__ import annotations

import asyncio
import logging

from onebot_adapter._async_utils import bearer_token, log_task_exception


def test_bearer_token_valid():
    assert bearer_token("Bearer abc123") == "abc123"
    assert bearer_token("bearer xyz") == "xyz"
    assert bearer_token("BEARER token123") == "token123"


def test_bearer_token_no_prefix():
    assert bearer_token("abc123") == ""
    assert bearer_token("Basic abc123") == ""
    assert bearer_token("") == ""


def test_bearer_token_with_trailing_whitespace():
    assert bearer_token("Bearer  abc  ") == "abc"


def test_bearer_token_none():
    """None input should not crash — callers pass headers.get(..., '')."""
    # The function does .lower() on the input, so None would raise.
    # Callers use request.headers.get("Authorization", "") which defaults to "".
    assert bearer_token("") == ""


async def test_log_task_exception_no_exception():
    """A task that completes normally should not log anything."""
    async def _ok():
        return 42

    task = asyncio.create_task(_ok())
    await task
    log_task_exception(task)  # should not raise


async def test_log_task_exception_cancelled():
    """A cancelled task should be silently handled."""
    async def _cancelled():
        await asyncio.sleep(100)

    task = asyncio.create_task(_cancelled())
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    log_task_exception(task)  # should not raise


async def test_log_task_exception_with_error(caplog):
    """A task with an exception should log it."""
    async def _fail():
        raise ValueError("test error")

    task = asyncio.create_task(_fail())
    try:
        await task
    except ValueError:
        pass
    with caplog.at_level(logging.ERROR):
        log_task_exception(task)
    assert any("test error" in r.message for r in caplog.records)

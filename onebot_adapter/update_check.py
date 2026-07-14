import logging
import re
import time

import aiohttp
from packaging.version import InvalidVersion, Version

from onebot_adapter import __version__

logger = logging.getLogger(__name__)

_REPO = "DNAlec/hermes-onebot-adapter"
_TAGS_URL = f"https://api.github.com/repos/{_REPO}/tags?per_page=10"
_CHANGELOG_URL = f"https://github.com/{_REPO}/blob/main/CHANGELOG.md"
_CACHE_TTL = 3600

_cache: dict | None = None
_cache_at: float = 0.0

_TAG_RE = re.compile(r"^v?(\d+\.\d+\.\S+)$")
# Strip local-version suffixes that ``packaging.version.Version`` rejects:
#   ``.dev0+g123abc`` (setuptools-scm) and ``.dirty`` (dirty git checkout).
_DEV_SUFFIX_RE = re.compile(r"\.dev\d+.*$|\.dirty$")


def _parse_version(raw: str) -> Version | None:
    base = _DEV_SUFFIX_RE.sub("", raw)
    try:
        return Version(base)
    except InvalidVersion:
        return None


async def check_for_updates() -> dict:
    global _cache, _cache_at

    now = time.time()
    if _cache is not None and now - _cache_at < _CACHE_TTL:
        return _cache

    current = __version__
    result: dict = {
        "current_version": current,
        "latest_version": current,
        "has_update": False,
        "changelog_url": _CHANGELOG_URL,
    }

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"Accept": "application/vnd.github+json", "User-Agent": "hermes-onebot-adapter"},
        ) as session:
            async with session.get(_TAGS_URL) as resp:
                if resp.status != 200:
                    logger.warning("GitHub tags API returned %d", resp.status)
                    result["error"] = f"GitHub API returned {resp.status}"
                    _cache = result
                    _cache_at = now
                    return result
                tags = await resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch GitHub tags: %s", exc)
        result["error"] = str(exc)
        _cache = result
        _cache_at = now
        return result

    current_ver = _parse_version(current)
    if current_ver is None:
        logger.warning("Cannot parse current version %r for comparison", current)
        result["error"] = f"cannot parse current version: {current}"
        _cache = result
        _cache_at = now
        return result

    latest_ver: Version | None = None
    latest_tag: str = current
    for tag_obj in tags:
        tag = tag_obj.get("name", "")
        m = _TAG_RE.match(tag)
        if not m:
            continue
        raw_ver = m.group(1)
        try:
            ver = Version(raw_ver)
        except InvalidVersion:
            continue
        if latest_ver is None or ver > latest_ver:
            latest_ver = ver
            latest_tag = raw_ver

    if latest_ver is not None and latest_ver > current_ver:
        result["latest_version"] = latest_tag
        result["has_update"] = True

    _cache = result
    _cache_at = now
    return result

"""The running version (from pyproject.toml, bumped by release-please) and
the newest release on GitHub, for the bot's status."""

import logging
import re
import tomllib
from pathlib import Path

import aiohttp

PROJECT_URL = "https://github.com/teasit/discord-aoe2-matchbot"
LATEST_RELEASE_URL = (
    PROJECT_URL.replace("https://github.com/", "https://api.github.com/repos/")
    + "/releases/latest"
)
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
# <repo>/pyproject.toml, /app/pyproject.toml in Docker.
PYPROJECT_FILE = Path(__file__).resolve().parents[3] / "pyproject.toml"


def current_version() -> str:
    """The version in pyproject.toml, "unknown" if it can't be read."""
    try:
        with open(PYPROJECT_FILE, "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "unknown"


def parse_version(version: str) -> tuple[int, ...]:
    """ "v1.2.3" -> (1, 2, 3); () if there is no number."""
    return tuple(int(n) for n in re.findall(r"\d+", version.split("-")[0]))


def is_newer(latest: str, current: str) -> bool:
    latest_parts, current_parts = parse_version(latest), parse_version(current)
    return bool(latest_parts and current_parts) and latest_parts > current_parts


async def latest_release(
    session: aiohttp.ClientSession, logger: logging.Logger | None = None
) -> str | None:
    """Tag of the newest GitHub release (e.g. "v1.0.1"), None on errors."""
    try:
        async with session.get(
            LATEST_RELEASE_URL,
            timeout=REQUEST_TIMEOUT,
            headers={"Accept": "application/vnd.github+json"},
        ) as response:
            if response.status != 200:
                if logger:
                    logger.warning(
                        f"Update check failed: HTTP {response.status} "
                        f"from {LATEST_RELEASE_URL}"
                    )
                return None
            return (await response.json()).get("tag_name")
    except Exception as e:
        if logger:
            logger.warning(f"Update check failed: {e!r}")
        return None


if __name__ == "__main__":
    import asyncio

    async def main():
        async with aiohttp.ClientSession() as session:
            latest = await latest_release(session)
        current = current_version()
        print(
            f"current: {current}, latest: {latest}, newer: {is_newer(latest or '', current)}"
        )

    asyncio.run(main())

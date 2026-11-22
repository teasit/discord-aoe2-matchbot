"""Data the Relic Link API does not provide, from the public aoe2companion API:
map display names/images, Quick Play statistics, player colours and
match results."""

import asyncio
import collections
import logging
import re
import time
from datetime import datetime
from http import HTTPStatus

import aiohttp

from matchbot.rlink import LeaderboardStats

API_URL = "https://data.aoe2companion.com/api"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=5)
MAPS_CACHE_SECONDS = 24 * 60 * 60
QUICKPLAY_CACHE_SECONDS = 15 * 60
# aoe2companion allows 16 requests per 10 s (X-RateLimit-* headers) and
# answers 429 Too Many Requests above that.
RATE_LIMIT_REQUESTS = 15
RATE_LIMIT_WINDOW_SECONDS = 10
RATE_LIMIT_RETRIES = 3
# aoe2companion splits Quick Play into a 1v1 and a team (2v2-4v4) leaderboard.
QUICKPLAY_LEADERBOARD_IDS = ("qp_rm_1v1", "qp_rm_team")

# Lobby map file names that differ from aoe2companion's map names.
MAP_NAME_ALIASES = {"scandanavia": "scandinavia"}


def normalize_map_name(name: str) -> str:
    name = name.lower().removesuffix(".rms2").removesuffix(".rms")
    name = re.sub(r"^(rm|cm|rwm|sm|br)_", "", name)
    name = re.sub(r"[^a-z0-9]", "", name)
    return MAP_NAME_ALIASES.get(name, name)


class Aoe2CompanionClient:
    logger: logging.Logger

    def __init__(self, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger()
        self._maps: dict[str, dict] = {}
        self._maps_update = 0.0
        self._session: aiohttp.ClientSession | None = None
        self._request_times: collections.deque[float] = collections.deque()
        self._rate_limit_lock = asyncio.Lock()
        # profile_id -> (time, stats); None stats = no Quick Play games.
        self._quickplay_cache: dict[int, tuple[float, LeaderboardStats | None]] = {}

    async def _get(self, path: str, params: dict | None = None):
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            await self._wait_for_rate_limit()
            async with self._session.get(f"{API_URL}{path}", params=params) as response:
                if (
                    response.status != HTTPStatus.TOO_MANY_REQUESTS
                    or attempt == RATE_LIMIT_RETRIES
                ):
                    response.raise_for_status()
                    return await response.json()
                headers = response.headers
                retry_after = float(
                    headers.get("Retry-After")
                    or headers.get("X-RateLimit-Reset")
                    or RATE_LIMIT_WINDOW_SECONDS
                )
            self.logger.debug(
                f"aoe2companion rate limit hit, retrying {path} in {retry_after:.0f} s."
            )
            await asyncio.sleep(retry_after)

    async def _wait_for_rate_limit(self):
        """Sliding window: at most RATE_LIMIT_REQUESTS per window."""
        async with self._rate_limit_lock:
            now = time.monotonic()
            while (
                self._request_times
                and now - self._request_times[0] > RATE_LIMIT_WINDOW_SECONDS
            ):
                self._request_times.popleft()
            if len(self._request_times) >= RATE_LIMIT_REQUESTS:
                await asyncio.sleep(
                    RATE_LIMIT_WINDOW_SECONDS - (now - self._request_times[0]) + 0.1
                )
                self._request_times.popleft()
            self._request_times.append(time.monotonic())

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def get_map(self, map_name: str) -> dict | None:
        """{"mapName": ..., "imageUrl": ...} of a map (display or file
        name), if known."""
        if time.time() - self._maps_update > MAPS_CACHE_SECONDS:
            try:
                maps = await self._get("/maps", {"language": "en"})
                # Display names first, so file names (unique) win collisions.
                self._maps = {normalize_map_name(m["mapName"]): m for m in maps}
                self._maps |= {normalize_map_name(m["mapId"]): m for m in maps}
                self._maps_update = time.time()
            except Exception as e:
                self.logger.warning(f"Failed to load aoe2companion maps: {e}")
        return self._maps.get(normalize_map_name(map_name))

    async def _get_match(self, match_id: int) -> dict | None:
        try:
            return await self._get(f"/matches/{match_id}")
        except Exception:
            return None

    @staticmethod
    def _match_players(match: dict) -> list[dict]:
        return [
            player
            for team in match.get("teams") or []
            for player in team.get("players") or []
        ]

    async def get_player_colors(self, match_id: int) -> dict[int, int] | None:
        """In-game player numbers (colours 1-8) by profile id. Only known once
        the game started and aoe2companion picked it up, else None."""
        match = await self._get_match(match_id)
        if not match:
            return None
        colors = {
            player["profileId"]: player["color"]
            for player in self._match_players(match)
            if player.get("color")
        }
        return colors or None

    async def get_match_result(
        self, match_id: int
    ) -> tuple[int, dict[int, bool]] | None:
        """Finish timestamp and whether each player (by profile id) won.
        None until the game finished and its winners are known."""
        match = await self._get_match(match_id)
        if not match or not match.get("finished"):
            return None
        won = {
            player["profileId"]: player["won"]
            for player in self._match_players(match)
            if player.get("won") is not None
        }
        if not any(won.values()):
            return None
        finished = datetime.fromisoformat(match["finished"].replace("Z", "+00:00"))
        return int(finished.timestamp()), won

    async def get_quickplay_stats(
        self, profile_ids: list[int]
    ) -> dict[int, LeaderboardStats]:
        """Wins and losses summed over all Quick Play formats (1v1 and team),
        so their ratio is the win rate across all Quick Play games."""

        async def get_stats(profile_id: int) -> LeaderboardStats | None:
            cached = self._quickplay_cache.get(profile_id)
            if cached and time.time() - cached[0] < QUICKPLAY_CACHE_SECONDS:
                return cached[1]
            try:
                profile = await self._get(f"/profiles/{profile_id}")
            except Exception as e:
                self.logger.warning(f"Failed to load aoe2companion profile: {e}")
                return None
            leaderboards = [
                lb
                for lb in profile.get("leaderboards", [])
                if lb["leaderboardId"] in QUICKPLAY_LEADERBOARD_IDS
            ]
            stats = None
            if leaderboards:
                stats = LeaderboardStats(
                    wins=sum(lb["wins"] for lb in leaderboards),
                    losses=sum(lb["losses"] for lb in leaderboards),
                )
            self._quickplay_cache[profile_id] = (time.time(), stats)
            return stats

        stats = await asyncio.gather(*(get_stats(pid) for pid in profile_ids))
        return {
            pid: stat
            for pid, stat in zip(profile_ids, stats, strict=True)
            if stat is not None
        }

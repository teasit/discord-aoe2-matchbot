import asyncio
import base64
import json
import logging
import os
import struct
import sys
import time
import zipfile
import zlib
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path

import aiohttp
from mgz.fast.header import parse as mgz_parse
from steam.enums import EResult

from matchbot.steam import SteamClientWithout2FA, SteamThread

# https://wiki.librematch.org/rlink/usage
API_URL = "https://aoe-api.worldsedgelink.com"
API_OPEN_URL = "https://api.ageofempires.com/api/"
API_MAX_REQUESTS_PER_MINUTE = 200
API_RATE_LIMIT_EXCEEDED_STATUS_CODE = 429
API_QUERY_INTERVAL_SAFE_SECONDS = 1
API_AUTH_RECOMMENDED_INTERVAL = 3 * 60
# aiohttp's default (300 s) would stall the lobby scan on a hung connection.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


APP_TITLE = "age2"
APP_ID = 813780  # AoE2:DE
APP_CLIENT_LIB_VER = 190

# Advertisement descriptions (row[8]) of lobbies created by the matchmaker.
# A SESSION_MATCH_KEY lobby is created when a player enters the queue, an
# AUTOMATCH lobby once players were matched (before the game is launched).
LOBBY_DESCRIPTION_AUTOMATCH = "AUTOMATCH"
LOBBY_DESCRIPTION_QUEUE = "SESSION_MATCH_KEY"
GET_ADVERTISEMENTS_MAX_IDS = 100

# Keys of the lobby options (advertisement row[12], see
# parse_advertisement_options). The advertised map file name (row[11]) is
# unreliable: matchmade lobbies carry a map of the pool that is usually not
# the one played, custom lobbies always say "my map". The real map is the
# "location" option, the game's string id of the map name.
OPTION_MAP_LOCATION = "10"
OPTION_CUSTOM_MAP_FILE = "11"
# "0" while the matchmaker has not settled the lobby yet (the location is
# then provisional and may still change), "1" afterwards.
OPTION_SETTLED = "7"
MAP_LOCATION_CUSTOM = -2  # custom .rms, file name in OPTION_CUSTOM_MAP_FILE
PROXY_STEAM_USER_MAX_IDS = 50


class LobbyType(StrEnum):
    RANKED = "ranked"
    QUICKPLAY = "quickplay"
    EMPIRE_WARS = "empire_wars"
    CUSTOM = "custom"
    # Any other matchmaking queue (Deathmatch, Battle Royale, event queues).
    OTHER = "other"


# Match type ids (row[16]), see /community/leaderboard/getAvailableLeaderboards.
# Custom lobbies have match type 0.
MATCH_TYPE_ID_TO_LOBBY_TYPE = {
    **dict.fromkeys((6, 7, 8, 9), LobbyType.RANKED),  # RM 1v1-4v4
    **dict.fromkeys((66, 67, 68, 69), LobbyType.RANKED),  # RM (console)
    **dict.fromkeys((18, 19, 20, 21), LobbyType.QUICKPLAY),  # Quick Play RM
    **dict.fromkeys((26, 27, 28, 29), LobbyType.EMPIRE_WARS),  # EW 1v1-4v4
    **dict.fromkeys((86, 87, 88, 89), LobbyType.EMPIRE_WARS),  # EW (console)
}


@dataclass
class AppTicket:
    ticket: str
    last_update: int = field(
        default_factory=lambda: (
            int(asyncio.get_event_loop().time()) if asyncio.get_event_loop() else 0
        )
    )


@dataclass
class RelicLinkSession:
    session_id: str
    last_update: int = field(
        default_factory=lambda: (
            int(asyncio.get_event_loop().time()) if asyncio.get_event_loop() else 0
        )
    )
    call_counter: int = 0

    def refresh(self):
        self.last_update = int(asyncio.get_event_loop().time())
        self.call_counter += 1


class RelicLinkLoginError(Exception):
    pass


class RelicLinkAuthenticationError(Exception):
    pass


class RelicLinkNotLoggedInError(RelicLinkAuthenticationError):
    """Raised while waiting to retry a failed login."""


class RelicLinkAPIError(Exception):
    code: int
    message: str

    def __init__(self, message: str = "RelicLink API Error", code: int = -1):
        self.message = f"Relic Link API Error: {message}"
        self.code = code
        super().__init__(self.message)


@dataclass
class LeaderboardStats:
    rating: int = None
    wins: int = None
    losses: int = None
    # All-time highest rating on this leaderboard.
    highest_rating: int = None


@dataclass
class Profile:
    profile_id: int
    name: str
    steam_id: int | None = None

    @staticmethod
    def from_profile_info(item: list) -> "Profile":
        # [statgroup_id, profile_id, "/steam/<steam_id64>", metadata, alias, ...]
        platform_name = item[2] or ""
        steam_id = None
        if platform_name.startswith("/steam/"):
            steam_id = int(platform_name.removeprefix("/steam/"))
        return Profile(profile_id=item[1], name=item[4], steam_id=steam_id)


@dataclass
class UserStats:
    name: str
    steam_id: str
    profile_id: str
    solo: LeaderboardStats = None
    team: LeaderboardStats = None
    ew_solo: LeaderboardStats = None
    ew_team: LeaderboardStats = None
    # Not provided by Relic Link (Quick Play has no leaderboard there).
    quickplay: LeaderboardStats = None
    # In-game player number (colour 1-8), only known after the game started.
    color: int | None = None
    # Whether the player won, only known after the game finished.
    won: bool | None = None


@dataclass
class ObservableMatchPlayerInfo:
    stats: UserStats
    civilisation_id: str
    team: int


@dataclass
class ObservableMatch:
    match_id: str
    map_name: str
    teams: dict
    players: list[UserStats]
    game_type_id: int = field(default=1)
    game_mode_id: int = field(default=0)
    lobby_name: str = field(default="")
    is_ranked: int = field(default=0)
    rating_type_id: int = field(default=0)
    start_timestamp: int = field(default=0)
    finish_timestamp: int = field(default=0)
    server_region_code: str = field(default="")
    server_region_name: str = field(default="")
    spectator_delay: int = field(default=0)

    @property
    def num_players(self) -> int:
        return len(self.players)

    def __repr__(self):
        return json.dumps(asdict(self), indent=2)


@dataclass
class Lobby:
    """An advertisement as returned by getAdvertisements, started or not."""

    match_id: int
    description: str
    # Advertised map file name, unreliable (see OPTION_MAP_LOCATION).
    map_name: str
    profile_ids: list[int]
    teams: dict
    # String id of the map name in the game's string table, None if the
    # options do not say.
    map_location_id: int | None = field(default=None)
    # File name of a custom map (map_location_id == MAP_LOCATION_CUSTOM).
    custom_map_file: str = field(default="")
    # Whether the options (and so the map) are final, see OPTION_SETTLED.
    options_settled: bool = field(default=False)
    game_type_id: int = field(default=1)
    game_mode_id: int = field(default=0)
    rating_type_id: int = field(default=0)
    start_timestamp: int | None = field(default=None)
    server_region_code: str = field(default="")
    server_region_name: str = field(default="")
    spectator_delay: int = field(default=0)

    @property
    def is_automatch(self) -> bool:
        return self.description == LOBBY_DESCRIPTION_AUTOMATCH

    @property
    def is_queue(self) -> bool:
        return self.description == LOBBY_DESCRIPTION_QUEUE

    @property
    def lobby_type(self) -> LobbyType:
        # Matchmade lobbies carry their match type from creation on, even
        # while the AUTOMATCH description is not set yet.
        if self.game_mode_id == 0:
            return LobbyType.CUSTOM
        return MATCH_TYPE_ID_TO_LOBBY_TYPE.get(self.game_mode_id, LobbyType.OTHER)

    @property
    def started(self) -> bool:
        return self.start_timestamp is not None

    @property
    def map_settled(self) -> bool:
        return self.options_settled or self.started

    @staticmethod
    def from_advertisement(row: list) -> "Lobby":
        profile_ids = []
        teams = {}
        for player_data in row[17]:
            profile_id = player_data[1]
            profile_ids.append(profile_id)
            teams.setdefault(str(player_data[5]), []).append(profile_id)
        map_name = row[11].removesuffix(".rms2").removesuffix(".rms")
        options = parse_advertisement_options(row[12])
        try:
            map_location_id = int(options[OPTION_MAP_LOCATION])
        except (KeyError, ValueError):
            map_location_id = None
        return Lobby(
            match_id=row[0],
            description=row[8],
            map_name=map_name,
            profile_ids=profile_ids,
            teams=teams,
            map_location_id=map_location_id,
            custom_map_file=options.get(OPTION_CUSTOM_MAP_FILE, ""),
            options_settled=options.get(OPTION_SETTLED) == "1",
            game_type_id=row[7],
            game_mode_id=row[16],
            rating_type_id=row[13],
            start_timestamp=row[24],
            server_region_code=row[25],
            server_region_name=row[26],
            spectator_delay=row[21],
        )


def parse_advertisement_options(options: str) -> dict[str, str]:
    """Decodes the options of an advertisement: base64(zlib(JSON string of
    base64(count byte, then per option a little-endian uint32 length and
    "key:value")))). Returns {} for empty or malformed options."""
    try:
        text = json.loads(zlib.decompress(base64.b64decode(options)).rstrip(b"\0"))
        data = base64.b64decode(text)
        result = {}
        pos = 1
        while pos < len(data):
            (length,) = struct.unpack_from("<I", data, pos)
            pos += 4
            key, _, value = data[pos : pos + length].decode().partition(":")
            pos += length
            result[key] = value
        return result
    except Exception:
        return {}


@dataclass
class RecentMatch:
    match_id: str = field(default="")
    map_name: str = field(default="")
    time_start: int = field(default=0)
    time_end: int = field(default=0)
    winners_profile_ids: list[int] = field(default_factory=list)
    losers_profile_ids: list[int] = field(default_factory=list)


class RelicLinkClient:
    TICKET_AGE_MAX_SEC = 4 * 60
    LOGIN_RETRY_SECONDS = 10

    logger: logging.Logger

    _steam: SteamThread
    _app_ticket: AppTicket | None = None
    _reliclink_session: RelicLinkSession | None = None
    _session: aiohttp.ClientSession | None = None

    def __init__(
        self,
        steam: SteamThread,
        logger: logging.Logger | None = None,
    ):
        self._steam = steam
        self._steam_id = 0
        self._alias = ""
        self._app_ticket = AppTicket("", 0)
        self._last_login_attempt = float("-inf")
        self._session = None
        self.logger = logger or logging.getLogger()

    @property
    def steam(self) -> SteamThread:
        return self._steam

    async def _ensure_session(self):
        """Ensure aiohttp session is created."""
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
        return self._session

    async def close(self):
        """Close the aiohttp session."""
        if self._session:
            await self._session.close()
            self._session = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def login(self) -> str:
        """
        Authenticate with RelicLink and obtain a session ID.
        Returns the server response JSON on success.
        """
        self._last_login_attempt = time.monotonic()
        app_ticket = await self._authenticate()
        if not app_ticket:
            raise RelicLinkAuthenticationError(
                "Steam login failed, no app ticket for Relic Link."
            )

        auth = app_ticket.ticket
        steam_id = self._steam_id
        alias = self._alias
        url = f"{API_URL}/game/login/platformlogin"
        data = {
            "accountType": "STEAM",
            "activeMatchId": "-1",
            "alias": alias,
            "appID": str(APP_ID),
            "auth": auth,
            "callNum": "0",
            "clientLibVersion": str(APP_CLIENT_LIB_VER),
            "connect_id": "",
            "country": "DE",
            "installationType": "windows",
            "language": "en",
            "lastCallTime": str(asyncio.get_event_loop().time()),
            "macAddress": "DE-AD-D0-0D-00-00",
            "majorVersion": "4.0.0",
            "minorVersion": "0",
            "platformUserID": str(steam_id),
            "startGameToken": "",
            "syncHash": "[0,0]",
            "timeoutOverride": "0",
            "title": APP_TITLE,
        }
        session = await self._ensure_session()
        async with session.post(
            url,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
            },
        ) as request:
            result_text = await request.text()
            http_status = request.status

        if f"/steam/{steam_id}" not in result_text:
            self._app_ticket = None
            self._reliclink_session = None
            raise RelicLinkLoginError(
                f"Relic Link platformlogin failed (HTTP {http_status}, "
                f"result {result_text[:20]!r})."
            )

        result_json = json.loads(result_text)
        status_code = result_json[0]
        session_id = result_json[1]

        if status_code != 0:
            self._app_ticket = None
            self._reliclink_session = None
            raise RelicLinkLoginError(
                f"Relic Link platformlogin failed with result code {status_code}."
            )

        self._reliclink_session = RelicLinkSession(session_id)

        # Note: Steam client only required for generation of authentication token
        await self._steam.run(SteamClientWithout2FA.logout)

        return result_json

    async def logout(self):
        await self._steam.run(SteamClientWithout2FA.logout)
        self._app_ticket = None
        self._reliclink_session = None

    async def _ensure_logged_in(self):
        """Logs in when there is no valid session. After a failed login the
        next attempt waits LOGIN_RETRY_SECONDS instead of retrying on every
        API call."""
        if self._reliclink_session is not None and self._ticket_is_valid():
            return
        since_last_attempt = time.monotonic() - self._last_login_attempt
        if (
            self._reliclink_session is None
            and since_last_attempt < self.LOGIN_RETRY_SECONDS
        ):
            raise RelicLinkNotLoggedInError(
                "Not logged in to Relic Link, next login attempt in "
                f"{self.LOGIN_RETRY_SECONDS - since_last_attempt:.0f} s."
            )
        await self.login()

    def _ticket_is_valid(self) -> bool:
        if not self._app_ticket:
            return False
        ticket_age_sec = (
            int(asyncio.get_event_loop().time()) - self._app_ticket.last_update
        )
        return ticket_age_sec < self.TICKET_AGE_MAX_SEC

    async def _authenticate(self) -> AppTicket | None:
        result = await self._steam.run(self._request_app_ticket)
        if result is None:
            return None
        ticket, self._steam_id, self._alias = result
        self._app_ticket = AppTicket(ticket, int(asyncio.get_event_loop().time()))
        return self._app_ticket

    @staticmethod
    def _request_app_ticket(
        steam_client: SteamClientWithout2FA,
    ) -> tuple[str, int, str] | None:
        """Encrypted app ticket (base64), SteamID64 and persona name of the
        Steam account, logging in first if needed. Blocking, runs in the
        Steam thread."""
        if not steam_client.logged_on and steam_client.login() != EResult.OK:
            return None
        response = steam_client.get_encrypted_app_ticket(
            app_id=APP_ID, userdata=b"RLINK"
        )
        if response is None:
            raise RelicLinkAuthenticationError("Steam sent no encrypted app ticket.")
        ticket_str = response.encrypted_app_ticket.SerializeToString(deterministic=True)
        ticket = base64.standard_b64encode(ticket_str).decode("utf-8")
        steam_user = steam_client.user
        alias = (steam_user.name if steam_user else None) or steam_client.username
        return ticket, steam_client.steam_id.as_64, alias

    async def get_match(self, match_id: int) -> dict:
        await self._ensure_logged_in()
        # https://wiki.librematch.org/rlink/game/advertisement/getadvertisements
        url = f"{API_URL}/game/advertisement/getadvertisements"
        session_id = self._reliclink_session.session_id
        call_counter = self._reliclink_session.call_counter
        last_update = self._reliclink_session.last_update

        params = {
            "callNum": str(call_counter),
            "connect_id": session_id,
            "lastCallTime": str(last_update),
            "match_ids": f"[{match_id}]",
            "sessionID": session_id,
        }
        session = await self._ensure_session()
        async with session.get(url, params=params) as request:
            result_json = await request.json()
        self._reliclink_session.refresh()
        return result_json

    async def get_advertisements(self, match_ids: list[int]) -> list[list]:
        """Advertisements for the given match ids, including lobbies that have
        not started yet (unlike findObservableAdvertisements)."""
        rows = []
        for i in range(0, len(match_ids), GET_ADVERTISEMENTS_MAX_IDS):
            chunk = match_ids[i : i + GET_ADVERTISEMENTS_MAX_IDS]
            for attempt in range(2):
                await self._ensure_logged_in()
                # https://wiki.librematch.org/rlink/game/advertisement/getadvertisements
                url = f"{API_URL}/game/advertisement/getAdvertisements"
                session_id = self._reliclink_session.session_id
                params = {
                    "callNum": str(self._reliclink_session.call_counter),
                    "connect_id": session_id,
                    "lastCallTime": str(self._reliclink_session.last_update),
                    "match_ids": json.dumps(chunk, separators=(",", ":")),
                    "sessionID": session_id,
                }
                session = await self._ensure_session()
                async with session.get(url, params=params) as request:
                    if request.status == HTTPStatus.UNAUTHORIZED and attempt == 0:
                        # Session expired, log in again and retry once.
                        self._app_ticket = None
                        continue
                    if request.status != HTTPStatus.OK:
                        text = await request.text()
                        raise RelicLinkAPIError(message=text, code=request.status)
                    result_json = await request.json()
                self._reliclink_session.refresh()
                status_code = result_json[0]
                if status_code != 0:
                    raise RelicLinkAPIError(
                        message="Error fetching advertisements", code=status_code
                    )
                rows.extend(result_json[1])
                break
        return rows

    async def get_lobbies_by_match_ids(self, match_ids: list[int]) -> list[Lobby]:
        rows = await self.get_advertisements(match_ids)
        return [Lobby.from_advertisement(row) for row in rows]

    async def get_match_from_lobby(self, lobby: Lobby) -> ObservableMatch:
        player_stats = await self.get_leaderboard_stat_groups_by_profile_ids(
            lobby.profile_ids
        )
        return ObservableMatch(
            match_id=lobby.match_id,
            map_name=lobby.map_name,
            game_type_id=lobby.game_type_id,
            lobby_name=lobby.description,
            rating_type_id=lobby.rating_type_id,
            game_mode_id=lobby.game_mode_id,
            start_timestamp=lobby.start_timestamp,
            server_region_code=lobby.server_region_code,
            server_region_name=lobby.server_region_name,
            spectator_delay=lobby.spectator_delay,
            teams=lobby.teams,
            players=player_stats,
        )

    @staticmethod
    async def get_newest_lobby_id() -> int | None:
        lobbies = await RelicLinkClient.get_lobbies()
        if not lobbies or not lobbies.get("matches"):
            return None
        return max(match["id"] for match in lobbies["matches"])

    async def get_replay_data(self, match_id: int, profile_id: int) -> dict:
        url = f"{API_OPEN_URL}/GameStats/AgeII/GetMatchReplay"

        params = {
            "matchId": match_id,
            "profileId": profile_id,
        }

        cache_dir = Path(".cache")
        cache_dir.mkdir(exist_ok=True, parents=True)

        zip_path = cache_dir / f"{match_id}.zip"
        savegame_path = None

        session = await self._ensure_session()
        async with session.get(url, params=params) as request:
            if request.status != 200:
                text = await request.text()
                raise RelicLinkAPIError(message=text, code=request.status)
            with open(zip_path, "wb") as f:
                f.write(await request.read())

        if not os.path.exists(zip_path):
            raise RelicLinkAPIError(
                message=f"Failed to download replay for match {match_id}",
                code=HTTPStatus.NOT_FOUND,
            )

        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(cache_dir)
            for file in zip_ref.namelist():
                if file.endswith(".aoe2record"):
                    savegame_path = cache_dir / file

        os.rename(savegame_path, cache_dir / f"{match_id}.aoe2record")
        os.remove(zip_path)

        savegame_path = cache_dir / f"{match_id}.aoe2record"
        with open(savegame_path, "rb") as f:
            match = mgz_parse(f)
        return match

    async def get_ticket(self) -> AppTicket:
        if not self._app_ticket:
            return await self._authenticate()
        return self._app_ticket

    async def find_profiles_by_platform_id(self, platform_ids: list[str]) -> list[str]:
        await self._ensure_logged_in()

        url = f"{API_URL}/game/account/FindProfilesByPlatformID"
        session_id = self._reliclink_session.session_id
        call_counter = self._reliclink_session.call_counter
        last_update = self._reliclink_session.last_update
        data = {
            "callNum": str(call_counter),
            "lastCallTime": str(last_update),
            "platformIDs": str(platform_ids).replace("'", '"'),
            "sessionID": session_id,
        }
        session = await self._ensure_session()
        async with session.post(
            url,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
            },
        ) as request:
            if request.status != 200:
                text = await request.text()
                raise RelicLinkAPIError(message=text, code=request.status)
            result_json = await request.json()
        self._reliclink_session.refresh()

        status_code = result_json[0]
        if status_code != 0:
            return []

        profiles = result_json[1]
        profile_ids = []
        for profile in profiles:
            profile_ids.append(profile[1])
        return profile_ids

    async def get_profiles(self, profile_ids: list[int]) -> list[Profile]:
        """Name and Steam ID of Relic Link profiles; unknown IDs are left out."""
        await self._ensure_logged_in()

        url = f"{API_URL}/game/Leaderboard/getStatGroupsByProfileIDs"
        session_id = self._reliclink_session.session_id
        params = {
            "callNum": str(self._reliclink_session.call_counter),
            "lastCallTime": str(self._reliclink_session.last_update),
            "profileIDs": json.dumps(profile_ids),
            "sessionID": session_id,
        }
        session = await self._ensure_session()
        async with session.get(url, params=params) as request:
            if request.status != 200:
                text = await request.text()
                raise RelicLinkAPIError(message=text, code=request.status)
            result_json = await request.json()
        self._reliclink_session.refresh()
        if result_json[0] != 0:
            return []
        return [Profile.from_profile_info(item) for item in result_json[2]]

    async def get_leaderboard_stat_groups_by_profile_ids(
        self, profile_ids: list[int]
    ) -> list[UserStats]:
        await self._ensure_logged_in()

        url = f"{API_URL}/game/Leaderboard/getStatGroupsByProfileIDs"
        session_id = self._reliclink_session.session_id
        call_counter = self._reliclink_session.call_counter
        last_update = self._reliclink_session.last_update
        params = {
            "callNum": str(call_counter),
            "lastCallTime": str(last_update),
            "profileIDs": str(profile_ids).replace("'", '"'),
            "sessionID": session_id,
        }
        session = await self._ensure_session()
        async with session.get(url, params=params) as request:
            if request.status != 200:
                text = await request.text()
                raise RelicLinkAPIError(message=text, code=request.status)
            result_json = await request.json()
        self._reliclink_session.refresh()

        status_code = result_json[0]
        if status_code != 0:
            raise RelicLinkAPIError(
                message="Error fetching leaderboard stat groups",
                code=status_code,
            )
        leaderboard_id_to_profile_id = {item[0]: item[4][0] for item in result_json[1]}
        profile_info = result_json[2]
        profile_id_to_name = {item[1]: item[4] for item in profile_info}

        user_stats = []
        for profile_id in profile_ids:
            name = profile_id_to_name.get(profile_id)
            user_stats.append(
                UserStats(
                    name=name,
                    profile_id=profile_id,
                    steam_id=None,
                )
            )

        ID_RANKED_SOLO = 3
        ID_RANKED_TEAM = 4
        ID_EMPIRE_WARS_SOLO = 13
        ID_EMPIRE_WARS_TEAM = 14
        leaderboard_stat_groups = result_json[3]
        for group in leaderboard_stat_groups:
            leaderboard_id = group[0]
            leaderboard_type_id = group[1]
            profile_id = leaderboard_id_to_profile_id.get(leaderboard_id)
            user_stats_idx = profile_ids.index(profile_id)
            rating = group[12]
            wins = group[2]
            loss = group[3]
            # [13] last match date, [14] highest rank, [15] highest rank level,
            # [16] highest rating (0 if never rated).
            highest_rating = group[16] if len(group) > 16 and group[16] > 0 else None
            stats = LeaderboardStats(
                rating=rating, wins=wins, losses=loss, highest_rating=highest_rating
            )
            if leaderboard_type_id == ID_RANKED_SOLO:
                user_stats[user_stats_idx].solo = stats
            elif leaderboard_type_id == ID_RANKED_TEAM:
                user_stats[user_stats_idx].team = stats
            elif leaderboard_type_id == ID_EMPIRE_WARS_SOLO:
                user_stats[user_stats_idx].ew_solo = stats
            elif leaderboard_type_id == ID_EMPIRE_WARS_TEAM:
                user_stats[user_stats_idx].ew_team = stats
            else:
                continue

        return user_stats

    async def get_recent_match_history(
        self, profile_ids: list[int]
    ) -> list[RecentMatch] | None:
        """Returns the latest match as the last item and the oldest match as the first item."""
        await self._ensure_logged_in()

        session_id = self._reliclink_session.session_id
        call_counter = self._reliclink_session.call_counter
        last_update = self._reliclink_session.last_update
        url = f"{API_URL}/game/Leaderboard/getRecentMatchHistory"
        params = {
            "callNum": str(call_counter),
            "connect_id": session_id,
            "lastCallTime": str(last_update),
            "sessionID": session_id,
            "profile_ids": str(profile_ids).replace("'", '"'),
            "title": APP_TITLE,
        }
        session = await self._ensure_session()
        async with session.get(url, params=params) as request:
            if request.status != 200:
                text = await request.text()
                raise RelicLinkAPIError(message=text, code=request.status)
            result_json = await request.json()
        self._reliclink_session.refresh()

        status_code = result_json[0]
        if status_code != 0:
            return []

        matches: list[RecentMatch] = []
        match_history_data = result_json[1]
        for match_data in match_history_data:
            match_id = match_data[0]
            is_duplicate = any(match_id == m.match_id for m in matches)
            if is_duplicate:
                continue
            map_name = match_data[2].strip(".rms").strip(".rms2")
            timestamp_start = match_data[8]
            timestamp_end = match_data[9]
            players_data = match_data[11]
            winner_ids = []
            loser_ids = []
            for player_data in players_data:
                profile_id = player_data[1]
                player_won = player_data[2]
                if player_won:
                    winner_ids.append(profile_id)
                else:
                    loser_ids.append(profile_id)
            match = RecentMatch(
                match_id=match_id,
                map_name=map_name,
                time_start=timestamp_start,
                time_end=timestamp_end,
                winners_profile_ids=winner_ids,
                losers_profile_ids=loser_ids,
            )
            matches.append(match)
        matches.sort(key=lambda x: x.time_start)
        return matches

    @staticmethod
    async def get_lobbies():
        url = f"{API_URL}/community/advertisement/findAdvertisements"
        async with (
            aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session,
            session.get(url, params={"title": APP_TITLE}) as response,
        ):
            if response.status != 200:
                return None
            return await response.json()

    @staticmethod
    async def get_steam_player_summaries(steam_ids: list[int]) -> list[dict]:
        """Steam player summaries (personaname, profileurl, ...) of Steam
        users that have an AoE2:DE profile, proxied by Relic Link so no Steam
        Web API key is needed. Unknown Steam ids are left out."""
        players = []
        url = f"{API_URL}/community/external/proxysteamuserrequest"
        async with aiohttp.ClientSession() as session:
            for i in range(0, len(steam_ids), PROXY_STEAM_USER_MAX_IDS):
                chunk = steam_ids[i : i + PROXY_STEAM_USER_MAX_IDS]
                params = {
                    "title": APP_TITLE,
                    "profileNames": json.dumps([f"/steam/{sid}" for sid in chunk]),
                    "request": "/ISteamUser/GetPlayerSummaries/v0002/",
                }
                async with session.get(url, params=params) as response:
                    if response.status != HTTPStatus.OK:
                        text = await response.text()
                        raise RelicLinkAPIError(message=text, code=response.status)
                    data = await response.json()
                if data["result"]["code"] != 0:
                    continue
                players.extend(data["steamResults"]["response"]["players"])
        return players


class LobbyScanner:
    """Finds lobbies as soon as they are created, before the game starts.

    Match ids are assigned sequentially to every lobby (custom lobbies, queue
    sessions and matchmade games), so scanning a window of ids around the
    newest known id with getAdvertisements reveals new lobbies within one
    scan interval. Matchmade games appear as AUTOMATCH lobbies roughly
    5-90 seconds before they are launched.

    The window only follows lobbies it finds, so after a stall (slow
    connection, blocking Steam login) the id counter may have run past it;
    the newest id is therefore looked up again every RESYNC_SECONDS.
    """

    IDS_BEHIND = 150
    IDS_AHEAD = 150
    RESYNC_SECONDS = 60

    def __init__(self, client: RelicLinkClient):
        self._client = client
        self._newest_match_id: int | None = None
        self._last_resync = float("-inf")

    @property
    def newest_match_id(self) -> int | None:
        return self._newest_match_id

    async def scan(self, extra_match_ids: list[int] | None = None) -> list[Lobby]:
        """Returns all lobbies in the current id window plus `extra_match_ids`."""
        if time.monotonic() - self._last_resync >= self.RESYNC_SECONDS:
            await self._resync()
        if self._newest_match_id is None:
            return []
        window = range(
            self._newest_match_id - self.IDS_BEHIND,
            self._newest_match_id + self.IDS_AHEAD,
        )
        match_ids = list(window)
        match_ids.extend(i for i in (extra_match_ids or []) if i not in window)
        lobbies = await self._client.get_lobbies_by_match_ids(match_ids)
        newest = max((lobby.match_id for lobby in lobbies), default=0)
        self._newest_match_id = max(self._newest_match_id, newest)
        return lobbies

    async def _resync(self):
        self._last_resync = time.monotonic()
        try:
            newest = await RelicLinkClient.get_newest_lobby_id()
        except Exception as e:
            self._client.logger.warning(f"Failed to look up the newest lobby id: {e!r}")
            return
        if newest is None:
            return
        if (
            self._newest_match_id is not None
            and newest > self._newest_match_id + self.IDS_AHEAD
        ):
            self._client.logger.warning(
                f"Lobby scan fell behind by {newest - self._newest_match_id} ids, "
                "resynced."
            )
        self._newest_match_id = max(self._newest_match_id or 0, newest)


async def main():
    """Prints lobbies of the given Steam users as soon as they appear."""
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(format="%(asctime)s | %(message)s", level=logging.INFO)
    logger = logging.getLogger()
    steam_ids = [int(arg) for arg in sys.argv[1:]]

    steam = SteamThread(
        os.environ["STEAM_USERNAME"], os.environ["STEAM_PASSWORD"], logger=logger
    )
    async with RelicLinkClient(steam, logger=logger) as rlink_client:
        profile_ids = await rlink_client.find_profiles_by_platform_id(steam_ids)
        logger.info("Profile IDs: %s", profile_ids)
        scanner = LobbyScanner(rlink_client)
        while True:
            for lobby in await scanner.scan():
                if any(pid in profile_ids for pid in lobby.profile_ids):
                    logger.info(lobby)
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())

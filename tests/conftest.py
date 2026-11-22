"""Fakes for Relic Link, aoe2companion and Discord, so the bot's logic runs
without network access or a Discord deployment."""

import base64
import json
import os
import struct
import tempfile
import zlib
from types import SimpleNamespace

# Before the bot module is imported: keep its data dir (map name cache,
# heartbeat) out of the repository.
os.environ["MATCHBOT_DATA_DIR"] = tempfile.mkdtemp(prefix="matchbot-test-")

import discord  # noqa: E402
import pytest  # noqa: E402

from matchbot import rlink  # noqa: E402
from matchbot.rlink import (  # noqa: E402
    LeaderboardStats,
    Lobby,
    ObservableMatch,
    Profile,
    UserStats,
)

ARABIA = 10875
ARCHIPELAGO = 10876
RANKED_1V1 = 6
RANKED_2V2 = 7
QUICKPLAY_1V1 = 18
EMPIRE_WARS_1V1 = 26
DEATHMATCH = 1  # any match type that is not mapped, i.e. LobbyType.OTHER
CUSTOM = 0


# --- Relic Link data --------------------------------------------------------


def encode_options(options: dict[str, str]) -> str:
    """Inverse of rlink.parse_advertisement_options."""
    data = bytes([len(options)])
    for key, value in options.items():
        entry = f"{key}:{value}".encode()
        data += struct.pack("<I", len(entry)) + entry
    text = json.dumps(base64.b64encode(data).decode())
    return base64.b64encode(zlib.compress(text.encode())).decode()


def advertisement_row(
    match_id: int,
    players: list[tuple[int, int]],
    *,
    description: str = rlink.LOBBY_DESCRIPTION_AUTOMATCH,
    game_mode_id: int = RANKED_1V1,
    map_location: int | None = ARABIA,
    settled: bool = True,
    custom_map_file: str | None = None,
    map_file: str = "arabia.rms",
    start: int | None = None,
    region: str = "westeurope",
) -> list:
    """A getAdvertisements row; `players` are (profile id, team) pairs."""
    options = {}
    if map_location is not None:
        options[rlink.OPTION_MAP_LOCATION] = str(map_location)
    if custom_map_file is not None:
        options[rlink.OPTION_CUSTOM_MAP_FILE] = custom_map_file
    options[rlink.OPTION_SETTLED] = "1" if settled else "0"
    row: list = [None] * 27
    row[0] = match_id
    row[7] = 1  # game type
    row[8] = description
    row[11] = map_file
    row[12] = encode_options(options)
    row[13] = 2  # rating type
    row[16] = game_mode_id
    row[17] = [[match_id, pid, -1, 0, 0, team] for pid, team in players]
    row[21] = 120  # spectator delay
    row[24] = start
    row[25] = region
    row[26] = "West Europe"
    return row


def make_lobby(match_id: int, players: list[tuple[int, int]], **kwargs) -> Lobby:
    return Lobby.from_advertisement(advertisement_row(match_id, players, **kwargs))


def stats(
    profile_id: int,
    name: str | None = None,
    solo: int | None = None,
    team: int | None = None,
    highest: int | None = None,
    wins: int = 10,
    losses: int = 10,
) -> UserStats:
    return UserStats(
        name=name or f"Player{profile_id}",
        steam_id=None,
        profile_id=profile_id,
        solo=LeaderboardStats(solo, wins, losses, highest) if solo else None,
        team=LeaderboardStats(team, wins, losses) if team else None,
    )


def make_match(
    match_id: int = 1000,
    players: list[UserStats] | None = None,
    teams: dict | None = None,
    game_mode_id: int = RANKED_1V1,
    start: int | None = None,
    map_name: str = "Arabia",
) -> ObservableMatch:
    players = players or [stats(1, solo=1000), stats(2, solo=1100)]
    if teams is None:
        teams = {str(i): [p.profile_id] for i, p in enumerate(players)}
    return ObservableMatch(
        match_id=match_id,
        map_name=map_name,
        teams=teams,
        players=players,
        game_mode_id=game_mode_id,
        start_timestamp=start,
        server_region_code="westeurope",
    )


class FakeRelicLink:
    """Stands in for RelicLinkClient in the bot."""

    def __init__(self):
        import logging

        self.logger = logging.getLogger("test")
        # SteamID64 -> Relic profile id
        self.steam_to_profile: dict[int, int] = {}
        self.profiles: dict[int, Profile] = {}
        # profile id -> UserStats template for get_match_from_lobby
        self.player_stats: dict[int, UserStats] = {}
        self.find_profiles_calls = 0
        self.fail_find_profiles: Exception | None = None

    def add_player(self, profile_id, steam_id=None, name=None, **stat_kwargs):
        name = name or f"Player{profile_id}"
        if steam_id:
            self.steam_to_profile[steam_id] = profile_id
        self.profiles[profile_id] = Profile(profile_id, name, steam_id)
        self.player_stats[profile_id] = stats(profile_id, name, **stat_kwargs)

    async def find_profiles_by_platform_id(self, steam_ids):
        self.find_profiles_calls += 1
        if self.fail_find_profiles:
            raise self.fail_find_profiles
        return [
            self.steam_to_profile[s] for s in steam_ids if s in self.steam_to_profile
        ]

    async def get_profiles(self, profile_ids):
        return [self.profiles[p] for p in profile_ids if p in self.profiles]

    async def get_match_from_lobby(self, lobby: Lobby) -> ObservableMatch:
        import copy

        players = [
            copy.deepcopy(self.player_stats.get(pid) or stats(pid))
            for pid in lobby.profile_ids
        ]
        return ObservableMatch(
            match_id=lobby.match_id,
            map_name=lobby.map_name,
            teams=lobby.teams,
            players=players,
            game_mode_id=lobby.game_mode_id,
            start_timestamp=lobby.start_timestamp,
            server_region_code=lobby.server_region_code,
        )

    async def login(self):
        pass

    async def logout(self):
        pass

    async def close(self):
        pass


class FakeScanner:
    """Stands in for LobbyScanner: returns whatever the test put in `lobbies`."""

    def __init__(self):
        self.lobbies: list[Lobby] = []
        self.extra_ids_calls: list[list[int]] = []
        self.error: Exception | None = None

    async def scan(self, extra_match_ids=None):
        self.extra_ids_calls.append(sorted(extra_match_ids or []))
        if self.error:
            raise self.error
        return list(self.lobbies)


class FakeAoe2Companion:
    def __init__(self):
        self.maps = {"Arabia": "https://img/arabia.png"}
        self.quickplay: dict[int, LeaderboardStats] = {}
        self.colors: dict[int, int] | None = None
        self.result: tuple[int, dict[int, bool]] | None = None
        # Set to an asyncio.Event to make Quick Play stats slow.
        self.quickplay_gate = None

    async def get_map(self, map_name):
        if map_name in self.maps:
            return {"mapName": map_name, "imageUrl": self.maps[map_name]}
        return None

    async def get_quickplay_stats(self, profile_ids):
        if self.quickplay_gate is not None:
            await self.quickplay_gate.wait()
        return {p: s for p, s in self.quickplay.items() if p in profile_ids}

    async def get_player_colors(self, match_id):
        return self.colors

    async def get_match_result(self, match_id):
        return self.result

    async def close(self):
        pass


# --- Discord ------------------------------------------------------------------


def forbidden() -> discord.Forbidden:
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no")


def view_text(view) -> str:
    """All text of a Components V2 view, for assertions."""
    return "\n".join(
        item.content for item in view.walk_children() if hasattr(item, "content")
    )


def view_colour(view) -> discord.Colour:
    return next(
        item.accent_colour
        for item in view.walk_children()
        if isinstance(item, discord.ui.Container)
    )


class FakeThread:
    def __init__(self, name):
        self.name = name
        self.sent: list[str] = []

    async def send(self, text):
        self.sent.append(text)


class FakeMessage:
    def __init__(self, channel=None, content=None, view=None):
        self.channel = channel
        self.content = content
        self.views = [view] if view is not None else []
        self.threads: list[FakeThread] = []
        self.replies: list[str] = []
        self.deleted = False

    @property
    def view(self):
        return self.views[-1]

    async def edit(self, view=None, **kwargs):
        self.views.append(view)

    async def create_thread(self, name):
        if self.channel is not None and self.channel.forbid_threads:
            raise forbidden()
        thread = FakeThread(name)
        self.threads.append(thread)
        return thread

    async def reply(self, text):
        self.replies.append(text)

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self, channel_id: int, forbid_threads=False, fail_send=False):
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self.messages: list[FakeMessage] = []
        self.forbid_threads = forbid_threads
        self.fail_send = fail_send

    def __str__(self):
        return f"channel-{self.id}"

    async def send(self, content=None, view=None):
        if self.fail_send:
            raise forbidden()
        message = FakeMessage(self, content, view)
        self.messages.append(message)
        return message


class FakeResponse:
    def __init__(self):
        self.done = False
        self.deferred = False
        self.sent: list[str] = []

    def is_done(self):
        return self.done

    async def defer(self):
        self.done = True
        self.deferred = True

    async def send_message(self, content, suppress_embeds=False):
        self.done = True
        self.sent.append(content)


class FakeFollowup:
    def __init__(self):
        self.messages: list[FakeMessage] = []

    async def send(self, content, suppress_embeds=False, wait=False):
        message = FakeMessage(content=content)
        self.messages.append(message)
        return message


class FakeInteraction:
    def __init__(self, channel: FakeChannel, command: str = "test", **options):
        self.channel = channel
        self.user = "tester#0001"
        self.command = SimpleNamespace(name=command)
        self.data = {"options": [{"name": k, "value": v} for k, v in options.items()]}
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.original_deleted = False

    @property
    def replies(self) -> list[str]:
        return self.response.sent + [m.content for m in self.followup.messages]

    @property
    def reply(self) -> str:
        assert len(self.replies) == 1, self.replies
        return self.replies[0]

    async def delete_original_response(self):
        self.original_deleted = True


# --- The bot ------------------------------------------------------------------


@pytest.fixture
def rlink_client():
    return FakeRelicLink()


@pytest.fixture
async def bot(tmp_path, monkeypatch, rlink_client):
    """An AoeMatchBot wired to fakes; channels 1 and 2 exist."""
    import aoe_matchbot
    from matchbot.config import MatchBotConfig

    # Keep replies, unless a test is about deleting them.
    monkeypatch.setattr(aoe_matchbot, "COMMAND_DELETE_AFTER_SECONDS", 0)
    bot = aoe_matchbot.AoeMatchBot(
        rlink_client=rlink_client,
        db_file=tmp_path / "db.json",
        config=MatchBotConfig(),
    )
    bot.lobby_scanner = FakeScanner()
    bot.aoe2companion = FakeAoe2Companion()
    bot.channels = {1: FakeChannel(1), 2: FakeChannel(2)}
    bot.get_channel = bot.channels.get
    yield bot
    for task in list(bot._background_tasks):
        task.cancel()


async def drain(bot):
    """Waits for the bot's background tasks (late edits, live updates)."""
    import asyncio

    while bot._background_tasks:
        await asyncio.gather(*list(bot._background_tasks))

import asyncio
import json
import logging
import os
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
from dotenv import load_dotenv

import matchbot.rlink as rlink
from matchbot.aoe2companion import Aoe2CompanionClient
from matchbot.config import MatchBotConfig
from matchbot.maps import MapNames
from matchbot.rlink import LobbyScanner, RelicLinkClient
from matchbot.steam import SteamThread
from matchbot.ui import (
    MatchState,
    build_match_view,
    elo_gap,
    escape_name,
    match_summary,
    smurf_gaps,
)
from matchbot.version import PROJECT_URL, current_version, is_newer, latest_release

APP_DIR = Path(__file__).resolve().parent.parent
# Everything the bot writes (database, logs) and optional user files
# (.env, config.yaml) live here. In Docker this is the single volume.
DATA_DIR = Path(os.environ.get("MATCHBOT_DATA_DIR", APP_DIR / "data"))
DATABASE_FILE = DATA_DIR / "cache" / "aoe_matchbot_database.json"
# Name before the project was renamed from "matchalyzer"; moved on startup.
LEGACY_DATABASE_FILE = DATA_DIR / "cache" / "aoe_matchalyzer_database.json"
# The game's string table (map names), downloaded from Steam.
GAME_STRINGS_FILE = DATA_DIR / "cache" / "key-value-strings-utf8.txt"
# Touched while the event loop runs; Docker's health check reads its age.
HEARTBEAT_FILE = DATA_DIR / "cache" / "heartbeat"
LOGS_DIR = DATA_DIR / "logs"
REQUIRED_ENV_VARS = ("DISCORD_TOKEN", "STEAM_USERNAME", "STEAM_PASSWORD")

# Environment variables take precedence; .env files only fill in missing ones.
ENV_FILES = (DATA_DIR / ".env", APP_DIR / ".env")
for env_file in ENV_FILES:
    load_dotenv(env_file)
STEAM_USERNAME = os.environ.get("STEAM_USERNAME")
STEAM_PASSWORD = os.environ.get("STEAM_PASSWORD")
TOKEN = os.environ.get("DISCORD_TOKEN")
# Slash command interactions (prompt and reply) are deleted after this many
# seconds to keep channels clean; 0 keeps them.
LOG_LEVEL = os.environ.get("MATCHBOT_LOG_LEVEL", "INFO").upper()
COMMAND_DELETE_AFTER_SECONDS = float(
    os.environ.get("MATCHBOT_COMMAND_DELETE_AFTER", "10")
)
# The process exits when the event loop has been blocked this long, so the
# container's restart policy starts it again; 0 disables the watchdog.
WATCHDOG_SECONDS = float(os.environ.get("MATCHBOT_WATCHDOG_SECONDS", "300"))
# Checks GitHub for a newer release and shows it in the bot's status; 0 = off.
UPDATE_CHECK = os.environ.get("MATCHBOT_UPDATE_CHECK", "1") != "0"


def find_config_file() -> Path | None:
    """MATCHBOT_CONFIG, else config.yaml in the data dir, else the default."""
    if os.environ.get("MATCHBOT_CONFIG"):
        return Path(os.environ["MATCHBOT_CONFIG"])
    for file in (DATA_DIR / "config.yaml", APP_DIR / "config.yaml"):
        if file.exists():
            return file
    return None


COMMAND_DESC = {
    "join": "Publishes match analysis to the channel.",
    "leave": "Stops publishing match analysis to the channel.",
    "add": "Tracks a player by SteamID64 or Relic Link profile ID.",
    "remove": "Stops tracking a player by SteamID64 or Relic Link profile ID.",
    "list": "Lists all tracked and banned players.",
    "set_elo_max_diff": "Warns in a thread when the Elo gap between teams exceeds this (0 = off).",
    "set_smurf_max_diff": "Warns in a thread when a player is this far below their all-time high 1v1 Elo (0 = off).",
    "ban": "Adds a player to the banlist by SteamID64 or Relic Link profile ID.",
    "unban": "Removes a player from the banlist by SteamID64 or Relic Link profile ID.",
}
PLAYER_ID_DESC = "SteamID64 (17 digits) or Relic Link profile ID"
# PROJECT_URL (matchbot.version) is appended to the application description
# (the bot's "About Me") on startup, so every instance links back to the project.
APP_DESCRIPTION_MAX_LENGTH = 400
STEAM_ID64_DIGITS = 17


@dataclass
class AoeMatchBotDatabase:
    channel_ids: list[int] = field(default_factory=list)
    steam_ids: list[int] = field(default_factory=list)
    match_ids_analysed: list[int] = field(default_factory=list)
    # Channel ID (as string, JSON keys) -> max Elo gap between teams.
    elo_max_diff: dict[str, int] = field(default_factory=dict)
    # Channel ID -> max gap between a player's all-time high and current
    # 1v1 RM rating (possible smurf).
    smurf_max_diff: dict[str, int] = field(default_factory=dict)
    # Relic Link profile ID (as string, JSON keys) ->
    # {"name": ..., "steam_id": ... | None, "comment": ...}
    banlist: dict[str, dict] = field(default_factory=dict)

    @staticmethod
    def load(file: str) -> "AoeMatchBotDatabase":
        if not Path(file).exists():
            return AoeMatchBotDatabase()
        with open(file) as f:
            data = json.load(f)
        return AoeMatchBotDatabase(**data)

    def save(self, file: str) -> None:
        file_path = Path(file)
        file_path.parent.mkdir(exist_ok=True, parents=True)
        with open(file, "w") as f:
            data = asdict(self)
            json.dump(data, f, indent=4)


@dataclass
class Announcement:
    """A published match post, updated as the match progresses."""

    match: rlink.ObservableMatch
    messages: list[discord.Message]
    matched_timestamp: int
    map_info: dict | None
    player_count: int
    state: MatchState = MatchState.MATCHED


class AoeMatchBot(discord.Client):
    MATCH_IDS_ANALYSED_CACHE_LEN = 20
    LOBBY_SCAN_INTERVAL_SECONDS = 3
    # Scans taking longer than this are logged as warnings.
    LOBBY_SCAN_SLOW_SECONDS = 10
    DISCORD_MESSAGE_MAX_LEN = 2000
    PLAYER_NUMBER_POLL_INTERVAL_SECONDS = 15
    PLAYER_NUMBER_POLL_ATTEMPTS = 20  # 5 minutes
    MATCH_RESULT_POLL_INTERVAL_SECONDS = 60
    MATCH_RESULT_POLL_ATTEMPTS = 4 * 60  # 4 hours
    # Posts don't wait longer than this for Quick Play stats; slower ones
    # (aoe2companion rate limit) are edited into the post afterwards.
    QUICKPLAY_STATS_WAIT_SECONDS = 3
    HEARTBEAT_INTERVAL_SECONDS = 30
    UPDATE_CHECK_INTERVAL_SECONDS = 60 * 60

    database_file: Path | None
    database: AoeMatchBotDatabase | None
    config: MatchBotConfig
    logger: logging.Logger | None
    analysis_task: asyncio.Task | None
    rlink_client: RelicLinkClient | None
    aoe2companion: Aoe2CompanionClient
    lobby_scanner: LobbyScanner | None
    authentication_task: asyncio.Task | None
    update_check_task: asyncio.Task | None
    tree: discord.app_commands.CommandTree | None

    def __init__(
        self,
        rlink_client: RelicLinkClient,
        db_file: str | Path = DATABASE_FILE,
        config: MatchBotConfig | None = None,
    ):
        discord.Client.__init__(
            self,
            intents=discord.Intents.default(),
            activity=discord.CustomActivity(name=f"v{current_version()}"),
        )
        self.logger = logging.getLogger("discord")
        self.version = current_version()
        # Newest GitHub release tag, once the update check found one.
        self.latest_release: str | None = None
        self.update_check_task = None
        self._http: aiohttp.ClientSession | None = None
        self.logger.info(f"discord-aoe2-matchbot v{self.version}")
        self.analysis_task = None
        self.rlink_client = rlink_client
        self.lobby_scanner = LobbyScanner(rlink_client)
        self.aoe2companion = Aoe2CompanionClient(logger=self.logger)
        self.map_names = MapNames(GAME_STRINGS_FILE, logger=self.logger)
        self._background_tasks: set[asyncio.Task] = set()
        self.authentication_task = None
        self.tree = discord.app_commands.CommandTree(self)

        self.config = config or MatchBotConfig()
        self.logger.info(
            "Tracked lobby types: " + ", ".join(sorted(self.config.tracked_lobby_types))
        )

        self.logger.info(f"Loading database from {db_file}...")
        self.database_file = Path(db_file)
        self.database = AoeMatchBotDatabase.load(db_file)
        self.logger.info("Database loaded.")

        @self.tree.command(name="join", description=COMMAND_DESC["join"])
        async def join_callback(interaction: discord.Interaction):
            return await self._join(interaction)

        @self.tree.command(name="leave", description=COMMAND_DESC["leave"])
        async def leave_callback(interaction: discord.Interaction):
            return await self._leave(interaction)

        @self.tree.command(name="add", description=COMMAND_DESC["add"])
        @discord.app_commands.describe(player_id=PLAYER_ID_DESC)
        async def add_callback(interaction: discord.Interaction, player_id: str):
            return await self._add_player(interaction, player_id)

        @self.tree.command(name="remove", description=COMMAND_DESC["remove"])
        @discord.app_commands.describe(player_id=PLAYER_ID_DESC)
        async def remove_callback(interaction: discord.Interaction, player_id: str):
            return await self._remove_player(interaction, player_id)

        @self.tree.command(
            name="set_elo_max_diff", description=COMMAND_DESC["set_elo_max_diff"]
        )
        async def set_elo_max_diff_callback(
            interaction: discord.Interaction, max_diff: int
        ):
            return await self._set_elo_max_diff(interaction, max_diff)

        @self.tree.command(
            name="set_smurf_max_diff", description=COMMAND_DESC["set_smurf_max_diff"]
        )
        async def set_smurf_max_diff_callback(
            interaction: discord.Interaction, max_diff: int
        ):
            return await self._set_smurf_max_diff(interaction, max_diff)

        @self.tree.command(name="ban", description=COMMAND_DESC["ban"])
        @discord.app_commands.describe(
            player_id=PLAYER_ID_DESC, comment="Reason, shown in the alert"
        )
        async def ban_callback(
            interaction: discord.Interaction, player_id: str, comment: str = ""
        ):
            return await self._ban(interaction, player_id, comment)

        @self.tree.command(name="unban", description=COMMAND_DESC["unban"])
        @discord.app_commands.describe(player_id=PLAYER_ID_DESC)
        async def unban_callback(interaction: discord.Interaction, player_id: str):
            return await self._unban(interaction, player_id)

        @self.tree.command(name="list", description=COMMAND_DESC["list"])
        async def list_callback(interaction: discord.Interaction):
            return await self._list_steam_ids(interaction)

    async def setup_hook(self):
        self._start_watchdog()

    def _start_watchdog(self):
        """Beats from the event loop and checks the beat from a thread: if the
        loop is blocked for WATCHDOG_SECONDS, logs where it hangs and exits."""
        last_beat = time.monotonic()
        loop_thread_id = threading.get_ident()

        async def beat():
            nonlocal last_beat
            while True:
                last_beat = time.monotonic()
                try:
                    HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
                    HEARTBEAT_FILE.touch()
                except OSError as e:
                    self.logger.warning(f"Could not touch {HEARTBEAT_FILE}: {e}")
                await asyncio.sleep(self.HEARTBEAT_INTERVAL_SECONDS)

        def watch():
            while True:
                time.sleep(self.HEARTBEAT_INTERVAL_SECONDS)
                blocked = time.monotonic() - last_beat
                if blocked < WATCHDOG_SECONDS:
                    continue
                frame = sys._current_frames().get(loop_thread_id)
                stack = "".join(traceback.format_stack(frame)) if frame else ""
                self.logger.critical(
                    f"Event loop blocked for {blocked:.0f} s, exiting.\n{stack}"
                )
                for handler in logging.getLogger().handlers:
                    handler.flush()
                os._exit(1)

        self._run_in_background(beat())
        if WATCHDOG_SECONDS > 0:
            threading.Thread(target=watch, name="watchdog", daemon=True).start()

    async def on_ready(self):
        self.logger.info(f"{self.user} ready.")
        await self.tree.sync()
        await self._ensure_project_link()
        await self._update_presence()
        await self._restart_authentication_task()
        await self._restart_analysis_task()
        if UPDATE_CHECK and (
            self.update_check_task is None or self.update_check_task.done()
        ):
            self.update_check_task = self.loop.create_task(self._update_check_task())

    def _status_text(self) -> str:
        """ "v1.0.0", or "v1.0.0 (v1.0.1 available)" when a newer release exists."""
        text = f"v{self.version}"
        if self.latest_release and is_newer(self.latest_release, self.version):
            text += f" ({self.latest_release} available)"
        return text

    async def _update_presence(self):
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.CustomActivity(name=self._status_text()),
        )

    async def _update_check_task(self):
        """Looks up the newest GitHub release every UPDATE_CHECK_INTERVAL_SECONDS
        and shows it in the status when it is newer than the running version."""
        while True:
            if self._http is None or self._http.closed:
                self._http = aiohttp.ClientSession()
            latest = await latest_release(self._http, self.logger)
            if latest and latest != self.latest_release:
                self.latest_release = latest
                if is_newer(latest, self.version):
                    self.logger.info(
                        f"Update available: {latest} (running v{self.version})"
                    )
                try:
                    await self._update_presence()
                except Exception as e:
                    self.logger.warning(f"Could not update the status: {e!r}")
            await asyncio.sleep(self.UPDATE_CHECK_INTERVAL_SECONDS)

    async def _ensure_project_link(self):
        """Appends PROJECT_URL to the bot's "About Me" unless it is already there."""
        try:
            app = await self.application_info()
            description = app.description or ""
            if PROJECT_URL in description:
                return
            suffix = f"\n\n{PROJECT_URL}" if description else PROJECT_URL
            keep = APP_DESCRIPTION_MAX_LENGTH - len(suffix)
            await app.edit(description=description[:keep].rstrip() + suffix)
            self.logger.info(f"Added {PROJECT_URL} to the application description.")
        except discord.HTTPException as e:
            self.logger.warning(f"Could not update the application description: {e}")

    async def on_resumed(self):
        self.logger.info(f"{self.user} resumed.")
        await self._update_presence()
        await self._restart_authentication_task()
        await self._restart_analysis_task()

    async def close(self):
        await self._save_database()
        await self.rlink_client.close()
        await self.aoe2companion.close()
        if self.update_check_task:
            self.update_check_task.cancel()
        if self._http:
            await self._http.close()
        await self.rlink_client.logout()
        await discord.Client.close(self)

    async def _restart_analysis_task(self):
        if (
            self.analysis_task
            and not self.analysis_task.cancelled()
            and not self.analysis_task.done()
        ):
            self.analysis_task.cancel()
        self.analysis_task = self.loop.create_task(self._analysis_task())
        self.logger.debug("Started analysis task.")

    async def _restart_authentication_task(self):
        if (
            self.authentication_task
            and not self.authentication_task.cancelled()
            and not self.authentication_task.done()
        ):
            self.authentication_task.cancel()
        self.authentication_task = self.loop.create_task(self._authentication_task())
        self.logger.debug("Started authentication task.")

    async def _authentication_task(self):
        while True:
            if self.map_names.needs_refresh:
                try:
                    await self.rlink_client.steam.run(self.map_names.refresh)
                except Exception as e:
                    self.logger.warning(f"Failed to refresh map names: {e!r}")
            try:
                await self.rlink_client.login()
                self.logger.debug("Logged in to Relic Link.")
            except Exception as e:
                self.logger.error(f"Relic Link login failed: {e!r}")
            await asyncio.sleep(rlink.API_AUTH_RECOMMENDED_INTERVAL)

    async def _analysis_task(self):
        profile_ids: list[int] = []
        profile_ids_steam_ids: list[int] = []
        # Custom lobbies of tracked players, watched until they start.
        pending_match_ids: set[int] = set()
        # Matchmade games announced before they started, watched until they
        # start or a player dodges.
        unstarted_announcements: dict[int, Announcement] = {}
        # When lobbies of tracked players were first seen, to log how long
        # they took to be posted.
        first_seen: dict[int, float] = {}
        while True:
            await asyncio.sleep(self.LOBBY_SCAN_INTERVAL_SECONDS)
            if not self.database.steam_ids:
                continue
            try:
                if profile_ids_steam_ids != self.database.steam_ids:
                    profile_ids = await self.rlink_client.find_profiles_by_platform_id(
                        self.database.steam_ids
                    )
                    profile_ids_steam_ids = list(self.database.steam_ids)
                scan_start = time.monotonic()
                lobbies = await self.lobby_scanner.scan(
                    list(pending_match_ids | unstarted_announcements.keys())
                )
                scan_seconds = time.monotonic() - scan_start
                if scan_seconds > self.LOBBY_SCAN_SLOW_SECONDS:
                    self.logger.warning(f"Lobby scan took {scan_seconds:.1f} s.")
                else:
                    self.logger.debug(
                        f"Lobby scan took {scan_seconds:.1f} s, {len(lobbies)} lobbies."
                    )
            except rlink.RelicLinkNotLoggedInError as e:
                # The failed login itself was already logged.
                self.logger.debug(str(e))
                continue
            except Exception as e:
                self.logger.error(f"Relic Link error: {e!r}")
                continue

            lobbies_by_id = {lobby.match_id: lobby for lobby in lobbies}
            now = time.time()
            for match_id, seen in list(first_seen.items()):
                if now - seen > 600:
                    del first_seen[match_id]
            # Forget custom lobbies that were closed without starting.
            pending_match_ids &= lobbies_by_id.keys()
            await self._update_unstarted_announcements(
                unstarted_announcements, lobbies_by_id
            )

            for lobby in lobbies:
                if lobby.is_queue:
                    continue
                if not any(pid in profile_ids for pid in lobby.profile_ids):
                    continue
                if lobby.lobby_type not in self.config.tracked_lobby_types:
                    continue
                if lobby.match_id in self.database.match_ids_analysed:
                    pending_match_ids.discard(lobby.match_id)
                    continue
                if lobby.match_id not in first_seen:
                    first_seen[lobby.match_id] = now
                    self.logger.debug(
                        f"Lobby {lobby.match_id} of a tracked player first seen "
                        f"({lobby.description or 'no description'}, "
                        f"map settled: {lobby.map_settled}, started: {lobby.started})."
                    )
                # Matchmade games are announced as soon as the players were
                # matched and the map is final (until then the lobby carries
                # a provisional one), custom lobbies only once the game
                # started.
                if not (lobby.is_automatch and lobby.map_settled or lobby.started):
                    pending_match_ids.add(lobby.match_id)
                    continue
                pending_match_ids.discard(lobby.match_id)

                try:
                    match = await self.rlink_client.get_match_from_lobby(lobby)
                except Exception as e:
                    self.logger.error(f"Relic Link error: {e}")
                    continue
                match.map_name = self.map_names.name(lobby)
                self.logger.debug(match)
                await self._cache_match_id(int(match.match_id))
                try:
                    announcement = await self._publish_match_analysis(
                        match, profile_ids
                    )
                    seen_seconds = time.time() - first_seen.pop(lobby.match_id, now)
                    self.logger.info(
                        f"Match {match.match_id} found "
                        f"({'started' if lobby.started else 'players matched, not started yet'}, "
                        f"first seen {seen_seconds:.0f} s before posting), "
                        f"posted to {len(announcement.messages)} channel(s):\n"
                        + match_summary(match, announcement.map_info)
                    )
                except Exception:
                    # Never let one bad post stop the analysis loop.
                    self.logger.exception(f"Failed to publish match {match.match_id}")
                    continue
                if lobby.started:
                    self._schedule_live_updates(announcement)
                else:
                    unstarted_announcements[lobby.match_id] = announcement

    async def _update_unstarted_announcements(
        self,
        announcements: dict[int, Announcement],
        lobbies_by_id: dict[int, rlink.Lobby],
    ):
        """Switches announced matches to live once they start, or to
        cancelled when a player dodged. Corrects the map should it still
        change after the announcement."""
        for match_id, announcement in list(announcements.items()):
            lobby = lobbies_by_id.get(match_id)
            map_changed = False
            if lobby:
                map_name = self.map_names.name(lobby)
                if map_name != announcement.match.map_name:
                    self.logger.info(
                        f"Map of match {match_id} changed from "
                        f"{announcement.match.map_name} to {map_name}."
                    )
                    announcement.match.map_name = map_name
                    announcement.map_info = await self.aoe2companion.get_map(map_name)
                    map_changed = True
            if lobby and lobby.started:
                del announcements[match_id]
                announcement.match.start_timestamp = lobby.start_timestamp
                self.logger.info(f"Match {match_id} started.")
                await self._update_announcement(announcement, MatchState.LIVE)
                self._schedule_live_updates(announcement)
                continue
            # A dodging player leaves the lobby, which is then closed.
            if lobby and len(lobby.profile_ids) >= announcement.player_count:
                if map_changed:
                    await self._update_announcement(announcement, announcement.state)
                continue
            del announcements[match_id]
            self.logger.info(f"Match {match_id} was dodged, marked as cancelled.")
            await self._update_announcement(announcement, MatchState.DODGED)

    async def _update_announcement(self, announcement: Announcement, state: MatchState):
        announcement.state = state
        view = build_match_view(
            announcement.match,
            state,
            announcement.matched_timestamp,
            announcement.map_info,
        )
        for message in announcement.messages:
            try:
                await message.edit(view=view)
            except Exception as e:
                self.logger.error(f"Failed to edit message: {e}")

    def _run_in_background(self, coroutine):
        # Keep a reference, asyncio only holds weak references to tasks.
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _schedule_live_updates(self, announcement: Announcement):
        """Adds the in-game player numbers to a live post as soon as
        aoe2companion knows them (shortly after the game started), then
        marks the winners and losers once the game finished."""
        match = announcement.match

        async def update_player_numbers():
            for _ in range(self.PLAYER_NUMBER_POLL_ATTEMPTS):
                await asyncio.sleep(self.PLAYER_NUMBER_POLL_INTERVAL_SECONDS)
                colors = await self.aoe2companion.get_player_colors(match.match_id)
                if colors:
                    self.logger.debug(
                        f"Adding player numbers to match {match.match_id}."
                    )
                    for player in match.players:
                        player.color = colors.get(player.profile_id)
                    await self._update_announcement(announcement, MatchState.LIVE)
                    return

        async def update_result():
            for _ in range(self.MATCH_RESULT_POLL_ATTEMPTS):
                await asyncio.sleep(self.MATCH_RESULT_POLL_INTERVAL_SECONDS)
                result = await self.aoe2companion.get_match_result(match.match_id)
                if result:
                    match.finish_timestamp, won = result
                    for player in match.players:
                        player.won = won.get(player.profile_id)
                    self.logger.info(f"Match {match.match_id} finished.")
                    await self._update_announcement(announcement, MatchState.FINISHED)
                    return
            self.logger.info(f"No result for match {match.match_id}.")

        async def update():
            await update_player_numbers()
            await update_result()

        self._run_in_background(update())

    async def _cache_match_id(self, match_id: int):
        self.database.match_ids_analysed.append(match_id)
        if len(self.database.match_ids_analysed) > self.MATCH_IDS_ANALYSED_CACHE_LEN:
            self.database.match_ids_analysed.pop(0)
        self.database.save(self.database_file)

    async def _publish_match_analysis(
        self, match: rlink.ObservableMatch, tracked_profile_ids: list[int]
    ) -> Announcement:
        # Data the Relic Link API does not provide.
        map_info = await self.aoe2companion.get_map(match.map_name)
        quickplay_task = asyncio.create_task(
            self.aoe2companion.get_quickplay_stats(
                [p.profile_id for p in match.players]
            )
        )

        def apply_quickplay_stats(stats: dict):
            for player in match.players:
                player.quickplay = stats.get(player.profile_id)

        try:
            apply_quickplay_stats(
                await asyncio.wait_for(
                    asyncio.shield(quickplay_task),
                    self.QUICKPLAY_STATS_WAIT_SECONDS,
                )
            )
            quickplay_task = None
        except TimeoutError:
            self.logger.debug(
                f"Quick Play stats for match {match.match_id} are slow, "
                "posting without them."
            )

        announcement = Announcement(
            match=match,
            messages=[],
            matched_timestamp=int(time.time()),
            map_info=map_info,
            player_count=len(match.players),
        )
        state = MatchState.LIVE if match.start_timestamp else MatchState.MATCHED
        announcement.state = state
        view = build_match_view(match, state, announcement.matched_timestamp, map_info)
        # Only warn when a tracked player is on the weaker side of the gap.
        gap = elo_gap(match)
        gap_diff = None
        if gap and any(p.profile_id in tracked_profile_ids for p in gap[1]):
            gap_diff = gap[0]
        ban_alerts = self._ban_alerts(match)
        # Tracked players dropping below their peak is not news.
        smurfs = [
            (player, gap)
            for player, gap in smurf_gaps(match)
            if player.profile_id not in tracked_profile_ids
        ]

        for channel_id in self.database.channel_ids:
            channel = self.get_channel(channel_id)
            if not channel:
                self.logger.warning(f"Channel ID {channel_id} not found.")
                continue
            try:
                message = await channel.send(view=view)
            except Exception as e:
                self.logger.error(f"Failed to post to channel {channel_id}: {e}")
                continue
            announcement.messages.append(message)
            alerts = list(ban_alerts)
            smurf_max_diff = self.database.smurf_max_diff.get(str(channel_id))
            smurf_alerts = [
                f"**{escape_name(player.name)}** is {gap} below their all-time "
                f"high 1v1 Elo ({player.solo.highest_rating} → {player.solo.rating})"
                for player, gap in smurfs
                if smurf_max_diff and gap > smurf_max_diff
            ]
            alerts += smurf_alerts
            max_diff = self.database.elo_max_diff.get(str(channel_id))
            if gap_diff and max_diff and gap_diff > max_diff:
                alerts.append(
                    f"Large Elo Gap (>{max_diff}) between highest-ranking players!"
                )
            if alerts:
                self.logger.info(
                    f"Alerts for match {match.match_id} in #{channel}: "
                    + " | ".join(a.replace("\n", " ") for a in alerts)
                )
                if ban_alerts:
                    thread_name = "⛔ Banned player in this match"
                elif smurf_alerts:
                    thread_name = "🕵️ Possible smurf in this match"
                else:
                    thread_name = f"⚠️ Large Elo Gap (>{max_diff})"
                await self._post_alerts(message, thread_name, alerts)

        if quickplay_task is not None:

            async def add_quickplay_stats():
                apply_quickplay_stats(await quickplay_task)
                self.logger.debug(f"Adding Quick Play stats to match {match.match_id}.")
                await self._update_announcement(announcement, announcement.state)

            self._run_in_background(add_quickplay_stats())

        self.logger.debug("Published match analysis.")
        return announcement

    def _ban_alerts(self, match: rlink.ObservableMatch) -> list[str]:
        alerts = []
        for player in match.players:
            entry = self.database.banlist.get(str(player.profile_id))
            if entry is None:
                continue
            alert = f"**{escape_name(player.name or entry['name'])}** is on the banlist"
            if entry.get("comment"):
                alert += f"\n> {entry['comment']}"
            alerts.append(alert)
        return alerts

    async def _post_alerts(
        self, message: discord.Message, thread_name: str, alerts: list[str]
    ):
        """Posts alerts in a thread on the match post."""
        text = "\n\n".join(alerts)
        try:
            thread = await message.create_thread(name=thread_name)
            await thread.send(text)
        except discord.Forbidden:
            self.logger.warning(
                "Missing permission to create threads, replying instead."
            )
            await message.reply(text)
        except Exception as e:
            self.logger.error(f"Failed to post alerts: {e}")

    async def _reply(self, interaction: discord.Interaction, content: str):
        """Answers a slash command and deletes the interaction (the reply,
        which also shows the user's command) after COMMAND_DELETE_AFTER_SECONDS."""
        options = " ".join(
            f"{o['name']}={o.get('value')}"
            for o in (interaction.data or {}).get("options", [])
        )
        command = f"/{interaction.command.name if interaction.command else '?'}"
        self.logger.info(
            f"{command} {options} by {interaction.user} in #{interaction.channel}: "
            + content.strip().replace("\n", " | ")[:300]
        )
        if interaction.response.is_done():
            message = await interaction.followup.send(
                content, suppress_embeds=True, wait=True
            )
        else:
            await interaction.response.send_message(content, suppress_embeds=True)
            message = None
        if COMMAND_DELETE_AFTER_SECONDS <= 0:
            return

        async def delete():
            await asyncio.sleep(COMMAND_DELETE_AFTER_SECONDS)
            self.logger.debug(f"Auto-deleting {command} interaction messages.")
            # A deferred command's original response is the "thinking"
            # message (or the first follow-up), so delete it in any case.
            for delete_message in (
                interaction.delete_original_response,
                message.delete if message else None,
            ):
                if delete_message is None:
                    continue
                try:
                    await delete_message()
                except discord.NotFound:
                    pass
                except Exception as e:
                    self.logger.warning(f"Failed to delete command reply: {e}")

        self._run_in_background(delete())

    async def _join(self, interaction: discord.Interaction):
        channel = interaction.channel
        if channel.id not in self.database.channel_ids:
            self.database.channel_ids.append(channel.id)
            await self._save_database()
            await self._reply(interaction, f"Joined {channel.mention}.")
        else:
            await self._reply(interaction, f"Already joined {channel.mention}.")

    async def _leave(self, interaction: discord.Interaction):
        channel = interaction.channel
        if channel.id in self.database.channel_ids:
            self.database.channel_ids.remove(channel.id)
            await self._save_database()
            await self._reply(interaction, f"Left channel '{channel.mention}'.")
        else:
            await self._reply(interaction, f"Never joined channel '{channel.mention}'.")

    async def _resolve_player(
        self, interaction: discord.Interaction, player_id: str
    ) -> rlink.Profile | None:
        """Looks up a player by SteamID64 (17 digits) or Relic Link profile ID
        (shorter); replies with the reason and returns None if not found."""
        player_id = player_id.strip()
        if not player_id.isdigit():
            await self._reply(
                interaction, "Please pass a SteamID64 or a Relic Link profile ID."
            )
            return None
        is_steam_id = len(player_id) == STEAM_ID64_DIGITS
        try:
            if is_steam_id:
                profile_ids = await self.rlink_client.find_profiles_by_platform_id(
                    [int(player_id)]
                )
            else:
                profile_ids = [int(player_id)]
            profiles = (
                await self.rlink_client.get_profiles(profile_ids) if profile_ids else []
            )
        except Exception as e:
            self.logger.error(f"Relic Link error: {e}")
            profiles = []
        if profiles:
            return profiles[0]
        if is_steam_id:
            reply = (
                f"SteamID64 {player_id} does not exist or has never played "
                "Age of Empires II: Definitive Edition."
            )
        else:
            reply = f"No Relic Link profile with ID {player_id} found."
        await self._reply(interaction, reply)
        return None

    async def _add_player(self, interaction: discord.Interaction, player_id: str):
        await interaction.response.defer()
        profile = await self._resolve_player(interaction, player_id)
        if not profile:
            return
        steam_id = profile.steam_id
        if not steam_id:
            await self._reply(
                interaction,
                f"**{escape_name(profile.name)}** (Relic ID {profile.profile_id}) "
                "has no Steam account and cannot be tracked.",
            )
            return
        players = await RelicLinkClient.get_steam_player_summaries([steam_id])
        if players:
            name = f"[{players[0]['personaname']}]({players[0]['profileurl']})"
        else:
            name = escape_name(profile.name)
        if steam_id not in self.database.steam_ids:
            self.database.steam_ids.append(steam_id)
            await self._save_database()
            await self._reply(
                interaction,
                f"Now tracking {name} (SteamID64 {steam_id}, "
                f"Relic ID {profile.profile_id})!",
            )
        else:
            await self._reply(
                interaction, f"{name} (SteamID64 {steam_id}) is already being tracked."
            )

    async def _remove_player(self, interaction: discord.Interaction, player_id: str):
        player_id = player_id.strip()
        if player_id.isdigit() and len(player_id) == STEAM_ID64_DIGITS:
            # No lookup needed, which also allows removing deleted accounts.
            steam_id = int(player_id)
        else:
            await interaction.response.defer()
            profile = await self._resolve_player(interaction, player_id)
            if not profile:
                return
            steam_id = profile.steam_id
        if steam_id in self.database.steam_ids:
            self.database.steam_ids.remove(steam_id)
            await self._save_database()
            await self._reply(interaction, f"Removed SteamID64 {steam_id}.")
        else:
            await self._reply(interaction, f"Player {player_id} is not tracked.")

    async def _list_steam_ids(self, interaction: discord.Interaction) -> list[int]:
        # Looking up many players can exceed the 3 s interaction deadline.
        await interaction.response.defer()
        lines = ["Tracked Steam Users:"]
        players = await RelicLinkClient.get_steam_player_summaries(
            self.database.steam_ids
        )
        players_by_id = {int(player["steamid"]): player for player in players}
        for steam_id in self.database.steam_ids:
            player = players_by_id.get(steam_id)
            if player:
                lines.append(
                    f"- {steam_id} ([{player['personaname']}]({player['profileurl']}))"
                )
            else:
                lines.append(f"- {steam_id}")
        if self.database.banlist:
            lines.append("Banned players:")
            for profile_id, entry in self.database.banlist.items():
                steam_id = entry.get("steam_id") or "no Steam account"
                comment = f": {entry['comment']}" if entry.get("comment") else ""
                lines.append(
                    f"- {steam_id} ({escape_name(entry['name'])}, "
                    f"Relic ID {profile_id}){comment}"
                )

        # Discord rejects messages longer than 2000 characters.
        messages = [""]
        for line in lines:
            if len(messages[-1]) + len(line) + 1 > self.DISCORD_MESSAGE_MAX_LEN:
                messages.append("")
            messages[-1] += line + "\n"
        for message in messages:
            await self._reply(interaction, message)

    async def _set_elo_max_diff(self, interaction: discord.Interaction, max_diff: int):
        channel = interaction.channel
        if max_diff <= 0:
            self.database.elo_max_diff.pop(str(channel.id), None)
            reply = f"Elo gap warnings disabled in {channel.mention}."
        else:
            self.database.elo_max_diff[str(channel.id)] = max_diff
            reply = (
                f"Matches with an Elo gap above {max_diff} between teams get "
                f"a warning thread in {channel.mention}."
            )
        await self._save_database()
        await self._reply(interaction, reply)

    async def _set_smurf_max_diff(
        self, interaction: discord.Interaction, max_diff: int
    ):
        channel = interaction.channel
        if max_diff <= 0:
            self.database.smurf_max_diff.pop(str(channel.id), None)
            reply = f"Smurf warnings disabled in {channel.mention}."
        else:
            self.database.smurf_max_diff[str(channel.id)] = max_diff
            reply = (
                f"Matches with a player more than {max_diff} below their "
                f"all-time high 1v1 Elo get a warning thread in {channel.mention}."
            )
        await self._save_database()
        await self._reply(interaction, reply)

    async def _ban(
        self, interaction: discord.Interaction, player_id: str, comment: str
    ):
        await interaction.response.defer()
        profile = await self._resolve_player(interaction, player_id)
        if not profile:
            return
        self.database.banlist[str(profile.profile_id)] = {
            "name": profile.name,
            "steam_id": profile.steam_id,
            "comment": comment,
        }
        await self._save_database()
        steam = (
            f"SteamID64 {profile.steam_id}" if profile.steam_id else "no Steam account"
        )
        await self._reply(
            interaction,
            f"Banned **{escape_name(profile.name)}** "
            f"(Relic ID {profile.profile_id}, {steam}).",
        )

    async def _unban(self, interaction: discord.Interaction, player_id: str):
        # Matched against the stored entries, so no lookup is needed.
        player_id = player_id.strip()
        banned = [
            profile_id
            for profile_id, entry in self.database.banlist.items()
            if player_id in (profile_id, str(entry.get("steam_id")))
        ]
        if not banned:
            await self._reply(interaction, f"Player {player_id} is not banned.")
            return
        names = [self.database.banlist.pop(pid)["name"] for pid in banned]
        await self._save_database()
        await self._reply(
            interaction,
            f"Unbanned **{escape_name(', '.join(names))}** ({player_id}).",
        )

    async def _load_database(self):
        if not self.database_file.exists():
            return
        try:
            self.database = AoeMatchBotDatabase.load(self.database_file)
            self.logger.info(f"Restored {len(self.database.channel_ids)} channels.")
        except Exception as e:
            self.logger.error(f"Failed to load channels: {e}")

    async def _save_database(self):
        try:
            self.database.save(self.database_file)
            self.logger.info(f"Saved {len(self.database.channel_ids)} channels")
        except Exception as e:
            self.logger.error(f"Failed to save channels: {e}")


if __name__ == "__main__":
    german_time = datetime.now(ZoneInfo("Europe/Berlin"))
    timestamp = german_time.strftime("%d_%m_%Y_%H_%M")
    log_file = LOGS_DIR / f"aoe_matchbot_{timestamp}.log"
    LOGS_DIR.mkdir(exist_ok=True, parents=True)
    # Log to stdout (docker logs) and to a file in the data dir.
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(filename=log_file, encoding="utf-8", mode="w"),
        ],
    )
    logger_discord = logging.getLogger("discord")

    if LEGACY_DATABASE_FILE.exists() and not DATABASE_FILE.exists():
        LEGACY_DATABASE_FILE.rename(DATABASE_FILE)
        logger_discord.info(f"Moved {LEGACY_DATABASE_FILE} to {DATABASE_FILE}.")

    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            f"Missing {', '.join(missing)}. Set them as environment variables "
            f"or in one of: {', '.join(map(str, ENV_FILES))}."
        )
    steam = SteamThread(STEAM_USERNAME, STEAM_PASSWORD, logger=logger_discord)
    rlink_client = RelicLinkClient(steam, logger=logger_discord)
    config = MatchBotConfig.load(find_config_file())
    bot = AoeMatchBot(rlink_client=rlink_client, config=config)
    # Logging is already set up above.
    bot.run(TOKEN, log_handler=None)

"""The analysis loop: which lobbies get posted when, and how posts follow the
match (matched -> live / dodged -> finished)."""

import asyncio

import discord
import pytest
from conftest import (
    ARABIA,
    ARCHIPELAGO,
    CUSTOM,
    DEATHMATCH,
    EMPIRE_WARS_1V1,
    QUICKPLAY_1V1,
    drain,
    make_lobby,
    view_colour,
    view_text,
)

from aoe_matchbot import AnalysisState
from matchbot import rlink
from matchbot.rlink import LobbyType

TRACKED_STEAM_ID = 76561198000000001
TRACKED = 101  # Relic profile id of the tracked player
OTHER = 202
START = 1_800_000_000


@pytest.fixture
def state():
    return AnalysisState()


@pytest.fixture
def live_updates(bot, monkeypatch):
    """Records announcements handed over to the live updates."""
    scheduled = []
    monkeypatch.setattr(bot, "_schedule_live_updates", scheduled.append)
    return scheduled


@pytest.fixture
def tracked(bot, rlink_client):
    rlink_client.add_player(TRACKED, steam_id=TRACKED_STEAM_ID, solo=1000)
    rlink_client.add_player(OTHER, solo=1100)
    bot.database.steam_ids = [TRACKED_STEAM_ID]
    bot.database.channel_ids = [1, 2]


def lobby(match_id=500, players=((TRACKED, 0), (OTHER, 1)), **kwargs):
    return make_lobby(match_id, list(players), **kwargs)


def posts(bot):
    return [m for channel in bot.channels.values() for m in channel.messages]


async def scan(bot, state, *lobbies):
    bot.lobby_scanner.lobbies = list(lobbies)
    await bot._analyse_lobbies(state)


async def test_nothing_tracked_does_not_scan(bot, state):
    await bot._analyse_lobbies(state)
    assert bot.lobby_scanner.extra_ids_calls == []


async def test_matched_lobby_is_posted_before_start(bot, state, tracked, live_updates):
    await scan(bot, state, lobby())
    assert len(posts(bot)) == 2  # one per joined channel
    text = view_text(posts(bot)[0].view)
    assert "### Ranked 1v1 · Arabia" in text
    assert "**Matched**" in text
    assert view_colour(posts(bot)[0].view) == discord.Colour.gold()
    assert bot.database.match_ids_analysed == [500]
    assert 500 in state.unstarted_announcements
    assert live_updates == []


async def test_profile_ids_are_looked_up_once(bot, state, tracked, rlink_client):
    await scan(bot, state)
    await scan(bot, state)
    assert rlink_client.find_profiles_calls == 1
    bot.database.steam_ids.append(76561198000000002)
    await scan(bot, state)
    assert rlink_client.find_profiles_calls == 2


async def test_unsettled_map_waits(bot, state, tracked):
    await scan(bot, state, lobby(settled=False, map_location=ARCHIPELAGO))
    assert posts(bot) == []
    assert state.pending_match_ids == {500}
    # Pending lobbies are scanned even if they leave the id window.
    await scan(bot, state, lobby(settled=True, map_location=ARABIA))
    assert bot.lobby_scanner.extra_ids_calls[-1] == [500]
    assert "Arabia" in view_text(posts(bot)[0].view)
    assert state.pending_match_ids == set()


async def test_lobby_without_automatch_description_waits(bot, state, tracked):
    await scan(bot, state, lobby(description=""))
    assert posts(bot) == []
    await scan(bot, state, lobby())
    assert len(posts(bot)) == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"description": rlink.LOBBY_DESCRIPTION_QUEUE},
        {"players": ((OTHER, 0), (303, 1))},  # no tracked player
        {"game_mode_id": EMPIRE_WARS_1V1},  # not tracked by default
        {"game_mode_id": DEATHMATCH},
    ],
)
async def test_ignored_lobbies(bot, state, tracked, kwargs):
    await scan(bot, state, lobby(**kwargs), lobby(**kwargs, start=START))
    assert posts(bot) == []
    assert state.pending_match_ids == set()


async def test_quickplay_is_tracked_by_default(bot, state, tracked):
    await scan(bot, state, lobby(game_mode_id=QUICKPLAY_1V1))
    assert "Quick Play 1v1" in view_text(posts(bot)[0].view)


async def test_configured_lobby_types(bot, state, tracked):
    bot.config.tracked_lobby_types = {LobbyType.EMPIRE_WARS}
    await scan(bot, state, lobby(1, game_mode_id=EMPIRE_WARS_1V1), lobby(2))
    assert len(posts(bot)) == 2
    assert all("Empire Wars 1v1" in view_text(m.view) for m in posts(bot))


async def test_lobby_is_posted_only_once(bot, state, tracked):
    await scan(bot, state, lobby())
    await scan(bot, state, lobby())
    assert len(posts(bot)) == 2
    # Also after a restart (fresh state), thanks to the database.
    await scan(bot, AnalysisState(), lobby(start=START))
    assert len(posts(bot)) == 2


async def test_matched_lobby_goes_live(bot, state, tracked, live_updates):
    await scan(bot, state, lobby())
    await scan(bot, state, lobby(start=START))
    for message in posts(bot):
        assert "**Live** since <t:1800000000:R>" in view_text(message.view)
        assert view_colour(message.view) == discord.Colour.green()
    assert state.unstarted_announcements == {}
    assert len(live_updates) == 1
    assert live_updates[0].match.start_timestamp == START


async def test_still_waiting_lobby_is_not_edited(bot, state, tracked):
    await scan(bot, state, lobby())
    await scan(bot, state, lobby())
    assert all(len(m.views) == 1 for m in posts(bot))


@pytest.mark.parametrize(
    "later", [(), (lobby(players=((TRACKED, 0),)),)], ids=["closed", "player left"]
)
async def test_dodged_lobby_is_cancelled(bot, state, tracked, live_updates, later):
    await scan(bot, state, lobby())
    await scan(bot, state, *later)
    for message in posts(bot):
        assert "**Cancelled**" in view_text(message.view)
        assert view_colour(message.view) == discord.Colour.red()
    assert state.unstarted_announcements == {}
    assert live_updates == []


async def test_map_change_after_post_is_edited(bot, state, tracked):
    await scan(bot, state, lobby(map_location=ARABIA))
    await scan(bot, state, lobby(map_location=ARCHIPELAGO))
    message = posts(bot)[0]
    assert len(message.views) == 2
    assert "### Ranked 1v1 · Archipelago" in view_text(message.view)
    assert "**Matched**" in view_text(message.view)


async def test_custom_lobby_is_posted_once_started(bot, state, tracked, live_updates):
    bot.config.tracked_lobby_types = {LobbyType.CUSTOM}
    custom = {"game_mode_id": CUSTOM, "description": "Come play", "settled": True}
    await scan(bot, state, lobby(**custom))
    assert posts(bot) == []
    assert state.pending_match_ids == {500}
    await scan(bot, state, lobby(**custom, start=START))
    assert "Custom Lobby" in view_text(posts(bot)[0].view)
    assert "**Live**" in view_text(posts(bot)[0].view)
    assert len(live_updates) == 1
    assert state.unstarted_announcements == {}


async def test_closed_custom_lobby_is_forgotten(bot, state, tracked):
    bot.config.tracked_lobby_types = {LobbyType.CUSTOM}
    await scan(bot, state, lobby(game_mode_id=CUSTOM, description="Come play"))
    assert state.pending_match_ids == {500}
    await scan(bot, state)
    assert state.pending_match_ids == set()
    assert bot.lobby_scanner.extra_ids_calls[-1] == [500]
    await scan(bot, state)
    assert bot.lobby_scanner.extra_ids_calls[-1] == []


async def test_lobby_seen_first_after_start_is_posted_live(
    bot, state, tracked, live_updates
):
    await scan(bot, state, lobby(start=START))
    assert "**Live**" in view_text(posts(bot)[0].view)
    assert len(live_updates) == 1


@pytest.mark.parametrize(
    "error", [rlink.RelicLinkNotLoggedInError("wait"), RuntimeError("offline")]
)
async def test_scan_errors_are_survived(bot, state, tracked, error):
    bot.lobby_scanner.error = error
    await scan(bot, state, lobby())
    assert posts(bot) == []
    bot.lobby_scanner.error = None
    await scan(bot, state, lobby())
    assert len(posts(bot)) == 2


async def test_profile_lookup_error_is_survived(bot, state, tracked, rlink_client):
    rlink_client.fail_find_profiles = RuntimeError("offline")
    await scan(bot, state, lobby())
    assert posts(bot) == []


async def test_match_lookup_error_skips_lobby(bot, state, tracked, rlink_client):
    async def fail(lobby):
        raise RuntimeError("offline")

    rlink_client.get_match_from_lobby = fail
    await scan(bot, state, lobby())
    assert posts(bot) == []
    assert bot.database.match_ids_analysed == []


async def test_publish_error_does_not_stop_other_lobbies(bot, state, tracked):
    original = bot._publish_match_analysis

    async def publish(match, tracked_profile_ids):
        if match.match_id == 1:
            raise RuntimeError("bad post")
        return await original(match, tracked_profile_ids)

    bot._publish_match_analysis = publish
    await scan(bot, state, lobby(1), lobby(2))
    assert len(posts(bot)) == 2


async def test_analysed_match_ids_are_capped(bot, state, tracked):
    bot.database.match_ids_analysed = list(range(1, 21))
    await scan(bot, state, lobby(500))
    assert len(bot.database.match_ids_analysed) == bot.MATCH_IDS_ANALYSED_CACHE_LEN
    assert bot.database.match_ids_analysed[-1] == 500
    assert 1 not in bot.database.match_ids_analysed


# --- live updates -------------------------------------------------------------


@pytest.fixture
def fast_polls(bot):
    bot.PLAYER_NUMBER_POLL_INTERVAL_SECONDS = 0
    bot.MATCH_RESULT_POLL_INTERVAL_SECONDS = 0
    bot.PLAYER_NUMBER_POLL_ATTEMPTS = 3
    bot.MATCH_RESULT_POLL_ATTEMPTS = 3


async def test_live_post_gets_colours_then_result(bot, state, tracked, fast_polls):
    bot.aoe2companion.colors = {TRACKED: 1, OTHER: 2}
    bot.aoe2companion.result = (START + 30 * 60, {TRACKED: True, OTHER: False})
    await scan(bot, state, lobby(start=START))
    await drain(bot)
    message = posts(bot)[0]
    # posted live, player numbers added, finished
    assert len(message.views) == 3
    assert "`1` [Player101]" in view_text(message.views[1])
    text = view_text(message.view)
    assert "**Finished**" in text and "after 30 min" in text
    assert ":crown: `1` [Player101]" in text
    assert ":skull: `2` [Player202]" in text
    assert view_colour(message.view) == discord.Colour.dark_grey()


async def test_live_post_without_result_stays_live(bot, state, tracked, fast_polls):
    await scan(bot, state, lobby(start=START))
    await drain(bot)
    message = posts(bot)[0]
    assert len(message.views) == 1
    assert "**Live**" in view_text(message.view)


async def test_analysis_task_runs_scans(bot, tracked, monkeypatch):
    bot.LOBBY_SCAN_INTERVAL_SECONDS = 0
    bot.lobby_scanner.lobbies = [lobby()]
    task = asyncio.create_task(bot._analysis_task())
    for _ in range(50):
        await asyncio.sleep(0)
        if len(bot.lobby_scanner.extra_ids_calls) >= 3:
            break
    task.cancel()
    assert len(bot.lobby_scanner.extra_ids_calls) >= 3
    assert len(posts(bot)) == 2

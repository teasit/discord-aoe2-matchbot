"""Posting match cards: channels, Quick Play stats and alert threads."""

import asyncio

import pytest
from conftest import FakeChannel, drain, make_match, stats, view_text

from matchbot.rlink import LeaderboardStats

TRACKED = 1


@pytest.fixture
def channels(bot):
    bot.database.channel_ids = [1, 2]
    return bot.channels


def two_v_two(tracked_rating=1000, mate=1050, opponents=(1500, 1400)):
    players = [
        stats(TRACKED, solo=tracked_rating),
        stats(2, solo=mate),
        stats(3, solo=opponents[0]),
        stats(4, solo=opponents[1]),
    ]
    return make_match(players=players, teams={"0": [1, 2], "1": [3, 4]})


async def test_posts_to_every_joined_channel(bot, channels):
    announcement = await bot._publish_match_analysis(make_match(), [TRACKED])
    assert [len(c.messages) for c in channels.values()] == [1, 1]
    assert announcement.messages == [c.messages[0] for c in channels.values()]
    assert announcement.player_count == 2
    assert announcement.map_info["imageUrl"] == "https://img/arabia.png"
    # No alerts configured.
    assert all(not m.threads for c in channels.values() for m in c.messages)


async def test_missing_and_failing_channels_are_skipped(bot, channels):
    bot.database.channel_ids = [1, 99, 3]
    bot.channels[3] = FakeChannel(3, fail_send=True)
    announcement = await bot._publish_match_analysis(make_match(), [TRACKED])
    assert len(announcement.messages) == 1


async def test_quickplay_stats_in_post(bot, channels):
    bot.aoe2companion.quickplay = {TRACKED: LeaderboardStats(wins=3, losses=1)}
    await bot._publish_match_analysis(make_match(), [TRACKED])
    assert "Q:75%" in view_text(channels[1].messages[0].view)


async def test_slow_quickplay_stats_are_edited_in(bot, channels):
    bot.QUICKPLAY_STATS_WAIT_SECONDS = 0.01
    bot.aoe2companion.quickplay = {TRACKED: LeaderboardStats(wins=1, losses=1)}
    bot.aoe2companion.quickplay_gate = asyncio.Event()
    await bot._publish_match_analysis(make_match(), [TRACKED])
    message = channels[1].messages[0]
    assert "Q:--%" in view_text(message.view)
    bot.aoe2companion.quickplay_gate.set()
    await drain(bot)
    assert len(message.views) == 2
    assert "Q:50%" in view_text(message.view)


async def test_elo_gap_alert_when_tracked_player_is_weaker(bot, channels):
    bot.database.elo_max_diff = {"1": 300}  # only channel 1
    await bot._publish_match_analysis(two_v_two(), [TRACKED])
    thread = channels[1].messages[0].threads[0]
    assert thread.name == "⚠️ Large Elo Gap (>300)"
    assert thread.sent == ["Large Elo Gap (>300) between highest-ranking players!"]
    assert channels[2].messages[0].threads == []


@pytest.mark.parametrize(
    "match, max_diff",
    [
        (two_v_two(), 500),  # gap 450 is below the threshold
        (two_v_two(tracked_rating=2000), 300),  # tracked player is stronger
    ],
)
async def test_no_elo_gap_alert(bot, channels, match, max_diff):
    bot.database.elo_max_diff = {"1": max_diff}
    await bot._publish_match_analysis(match, [TRACKED])
    assert channels[1].messages[0].threads == []


async def test_smurf_alert_for_untracked_players_only(bot, channels):
    bot.database.smurf_max_diff = {"1": 200, "2": 1000}
    match = make_match(
        players=[
            stats(TRACKED, name="Me", solo=1000, highest=2000),
            stats(2, name="Smurfy", solo=900, highest=1500),
        ]
    )
    await bot._publish_match_analysis(match, [TRACKED])
    thread = channels[1].messages[0].threads[0]
    assert thread.name == "🕵️ Possible smurf in this match"
    assert thread.sent == [
        "**Smurfy** is 600 below their all-time high 1v1 Elo (1500 → 900)"
    ]
    assert channels[2].messages[0].threads == []


async def test_ban_alert_in_every_channel(bot, channels):
    bot.database.banlist = {"2": {"name": "Old", "steam_id": None, "comment": "griefs"}}
    bot.database.elo_max_diff = {"1": 100}
    match = make_match(
        players=[stats(TRACKED, solo=1000), stats(2, name="Cheater", solo=1500)]
    )
    await bot._publish_match_analysis(match, [TRACKED])
    for channel in channels.values():
        thread = channel.messages[0].threads[0]
        assert thread.name == "⛔ Banned player in this match"
    text = channels[1].messages[0].threads[0].sent[0]
    assert "**Cheater** is on the banlist\n> griefs" in text
    # Further alerts of the channel go into the same thread.
    assert "Large Elo Gap (>100)" in text
    assert "Large Elo Gap" not in channels[2].messages[0].threads[0].sent[0]


async def test_alert_without_thread_permission_replies(bot, channels):
    bot.channels[1].forbid_threads = True
    bot.database.banlist = {"2": {"name": "B", "steam_id": None, "comment": ""}}
    await bot._publish_match_analysis(make_match(), [TRACKED])
    message = channels[1].messages[0]
    assert message.threads == []
    assert message.replies == ["**Player2** is on the banlist"]


async def test_update_announcement_survives_edit_errors(bot, channels):
    from matchbot.ui import MatchState

    announcement = await bot._publish_match_analysis(make_match(), [TRACKED])

    async def fail(**kwargs):
        raise RuntimeError("deleted")

    announcement.messages[0].edit = fail
    await bot._update_announcement(announcement, MatchState.LIVE)
    assert announcement.state == MatchState.LIVE
    assert "**Live**" in view_text(announcement.messages[1].view)

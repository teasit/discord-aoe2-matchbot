"""Rendering of match posts and the rating/smurf computations."""

import discord
import pytest
from conftest import (
    CUSTOM,
    EMPIRE_WARS_1V1,
    QUICKPLAY_1V1,
    RANKED_2V2,
    make_match,
    stats,
    view_colour,
    view_text,
)

from matchbot.rlink import LeaderboardStats
from matchbot.ui import (
    MatchState,
    build_match_view,
    elo_gap,
    escape_name,
    game_mode_name,
    match_summary,
    player_line,
    relevant_stats,
    smurf_gaps,
    stat_tags,
    status_line,
    team_rating,
)


def team_match(**kwargs):
    players = [
        stats(1, solo=1000, team=1200),
        stats(2, solo=1500, team=1300),
        stats(3, solo=1100, team=1250),
        stats(4, solo=1050, team=1150),
    ]
    teams = {"0": [1, 2], "1": [3, 4]}
    return make_match(players=players, teams=teams, game_mode_id=RANKED_2V2, **kwargs)


@pytest.mark.parametrize(
    "name, escaped",
    [
        ("Plain", "Plain"),
        (None, "Unknown"),
        ("**bold**", "∗∗bold∗∗"),
        ("[link](x)", "⁅link⁆(x)"),
        ("a|b`c~d\\e", "a∣bˋc∼d∖e"),
        ("_lead", "ˍlead"),
        ("snake_case", "snake_case"),
        ("double__under", "doubleˍˍunder"),
    ],
)
def test_escape_name(name, escaped):
    assert escape_name(name) == escaped


def test_game_mode_name():
    assert game_mode_name(make_match(game_mode_id=CUSTOM)) == "Custom Lobby"
    assert game_mode_name(make_match(game_mode_id=RANKED_2V2)) == "Ranked 2v2"
    assert game_mode_name(make_match(game_mode_id=999)) == "Unknown"


def test_relevant_stats_by_mode():
    player = stats(1, solo=1000, team=1200)
    player.ew_solo = LeaderboardStats(rating=900)
    player.ew_team = LeaderboardStats(rating=950)
    assert relevant_stats(make_match(), player).rating == 1000
    assert relevant_stats(team_match(), player).rating == 1200
    ew = make_match(game_mode_id=EMPIRE_WARS_1V1)
    assert relevant_stats(ew, player).rating == 900
    # Missing ladder falls back to the other RM ladder.
    assert relevant_stats(team_match(), stats(9, solo=1111)).rating == 1111


def test_team_rating_average_skips_unrated():
    match = team_match()
    assert team_rating(match, [match.players[0], match.players[1]]) == 1250
    assert team_rating(match, [stats(9)]) is None


def test_elo_gap_between_top_players():
    match = team_match()
    gap, weaker = elo_gap(match)
    # Team 0 top: 1500, team 1 top: 1100.
    assert gap == 400
    assert [p.profile_id for p in weaker] == [3, 4]


def test_elo_gap_unknown_without_ratings():
    match = make_match(players=[stats(1, solo=1000), stats(2)])
    assert elo_gap(match) is None
    assert elo_gap(make_match(players=[stats(1, solo=1)], teams={"0": [1]})) is None


def test_smurf_gaps_sorted_largest_first():
    match = make_match(
        players=[
            stats(1, solo=1000, highest=1300),
            stats(2, solo=1000, highest=1800),
            stats(3, solo=1000, highest=1000),
            stats(4, solo=1000),
        ],
        teams={"0": [1, 2], "1": [3, 4]},
    )
    assert [(p.profile_id, gap) for p, gap in smurf_gaps(match)] == [(2, 800), (1, 300)]


def test_stat_tags():
    player = stats(1, solo=660, team=11100, wins=7, losses=13)
    player.quickplay = LeaderboardStats(wins=1, losses=0)
    assert stat_tags(make_match(), player) == "R:0660 | T:9999 | E:---- | Q:99%"
    assert stat_tags(make_match(), stats(2)) == "R:---- | T:---- | E:---- | Q:--%"


def test_status_lines():
    match = make_match(start=1000)
    match.finish_timestamp = 1000 + 25 * 60
    assert "Matched** <t:50:R>" in status_line(match, MatchState.MATCHED, 50, 2000)
    assert "Live** since <t:1000:R>" in status_line(match, MatchState.LIVE, 50, 2000)
    assert "a player dodged <t:2000:R>" in status_line(
        match, MatchState.DODGED, 50, 2000
    )
    assert "after 25 min" in status_line(match, MatchState.FINISHED, 50, 2000)


def test_player_line_marks_winners_once_finished():
    winner = stats(1, name="Win_ner")
    winner.won = True
    winner.color = 3
    loser = stats(2)
    loser.won = False
    match = make_match(players=[winner, loser])
    line = player_line(match, winner, MatchState.FINISHED)
    assert line.startswith(
        ":crown: `3` [Win_ner](https://www.aoe2insights.com/user/relic/1/)"
    )
    assert player_line(match, loser, MatchState.FINISHED).startswith(":skull: ")
    assert not player_line(match, winner, MatchState.LIVE).startswith(":crown:")


@pytest.mark.parametrize(
    "state, colour",
    [
        (MatchState.MATCHED, discord.Colour.gold()),
        (MatchState.LIVE, discord.Colour.green()),
        (MatchState.DODGED, discord.Colour.red()),
        (MatchState.FINISHED, discord.Colour.dark_grey()),
    ],
)
async def test_build_match_view(state, colour):
    match = team_match(start=1000)
    view = build_match_view(
        match,
        state,
        matched_timestamp=900,
        map_info={"mapName": "Arabia", "imageUrl": "https://img/arabia"},
        now=2000,
    )
    text = view_text(view)
    assert "### Ranked 2v2 · Arabia" in text
    assert "westeurope" in text
    for player in match.players:
        assert f"[{player.name}]" in text
    assert "E: Empire Wars team" in text
    assert view_colour(view) == colour
    thumbnails = [
        i for i in view.walk_children() if isinstance(i, discord.ui.Thumbnail)
    ]
    assert thumbnails[0].media.url == "https://img/arabia"
    buttons = {
        b.label: b for b in view.walk_children() if isinstance(b, discord.ui.Button)
    }
    assert buttons["Spectate"].url.endswith("match_id=1000")
    assert buttons["Spectate"].disabled == (state == MatchState.DODGED)
    assert buttons["Insights"].url == "https://www.aoe2insights.com/match/1000/"


async def test_build_match_view_without_map_info():
    view = build_match_view(
        make_match(map_name="Megarandom", game_mode_id=QUICKPLAY_1V1), MatchState.LIVE
    )
    assert "### Quick Play 1v1 · Megarandom" in view_text(view)
    assert not [i for i in view.walk_children() if isinstance(i, discord.ui.Thumbnail)]


async def test_teams_are_separated():
    view = build_match_view(team_match(), MatchState.MATCHED)
    separators = [
        i for i in view.walk_children() if isinstance(i, discord.ui.Separator)
    ]
    assert len(separators) == 2


def test_match_summary():
    summary = match_summary(team_match(), {"mapName": "Arabia (img)"})
    assert summary.splitlines()[0] == "Ranked 2v2 · Arabia (img) · westeurope"
    assert "Team 1 (avg 1250)" in summary
    assert "Team 2 (avg 1200)" in summary
    assert "Player4 (Relic ID 4)" in summary

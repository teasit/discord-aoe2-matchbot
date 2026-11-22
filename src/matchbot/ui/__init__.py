"""Discord rendering of match posts (Components V2), no Discord state."""

import re
import time
from enum import StrEnum

import discord
from discord import ui

from matchbot.rlink import (
    MATCH_TYPE_ID_TO_LOBBY_TYPE,
    LeaderboardStats,
    LobbyType,
    ObservableMatch,
    UserStats,
)

REPLAY_REDIRECT_HANDLER_URL = "https://teasit.github.io/aoe2de-redirect"
AOE2INSIGHTS_URL = "https://www.aoe2insights.com"

# TODO: Extract this info instead from https://wiki.librematch.org/rlink/game/leaderboard/getavailableleaderboards
GAME_MODE_ID_TO_NAME = {
    6: "Ranked 1v1",
    7: "Ranked 2v2",
    8: "Ranked 3v3",
    9: "Ranked 4v4",
    18: "Quick Play 1v1",
    19: "Quick Play 2v2",
    20: "Quick Play 3v3",
    21: "Quick Play 4v4",
    26: "Empire Wars 1v1",
    27: "Empire Wars 2v2",
    28: "Empire Wars 3v3",
    29: "Empire Wars 4v4",
    66: "Ranked 1v1 (Console)",
    67: "Ranked 2v2 (Console)",
    68: "Ranked 3v3 (Console)",
    69: "Ranked 4v4 (Console)",
    86: "Empire Wars 1v1 (Console)",
    87: "Empire Wars 2v2 (Console)",
    88: "Empire Wars 3v3 (Console)",
    89: "Empire Wars 4v4 (Console)",
}


class MatchState(StrEnum):
    MATCHED = "matched"
    LIVE = "live"
    DODGED = "dodged"
    FINISHED = "finished"


STATE_TO_COLOUR = {
    MatchState.MATCHED: discord.Colour.gold(),
    MatchState.LIVE: discord.Colour.green(),
    MatchState.DODGED: discord.Colour.red(),
    MatchState.FINISHED: discord.Colour.dark_grey(),
}


# Discord ignores backslash escapes inside link text, so markdown characters
# in player names are replaced with look-alikes instead.
NAME_LOOKALIKES = str.maketrans(
    {"*": "∗", "`": "ˋ", "~": "∼", "|": "∣", "[": "⁅", "]": "⁆", "\\": "∖"}
)


def escape_name(name: str | None) -> str:
    name = (name or "Unknown").translate(NAME_LOOKALIKES)
    # Underscores only format at word boundaries or when doubled (__underline__).
    if "__" in name or re.search(r"(^|\W)_|_($|\W)", name):
        name = name.replace("_", "ˍ")
    return name


def game_mode_name(match: ObservableMatch) -> str:
    if match.game_mode_id == 0:
        return "Custom Lobby"
    return GAME_MODE_ID_TO_NAME.get(match.game_mode_id, "Unknown")


def is_team_game(match: ObservableMatch) -> bool:
    return any(len(pids) > 1 for pids in match.teams.values())


def relevant_stats(
    match: ObservableMatch, player: UserStats
) -> LeaderboardStats | None:
    """Stats of the ladder that fits the match: Empire Wars for Empire Wars
    games, Random Map otherwise (Quick Play has no official rating)."""
    team_game = is_team_game(match)
    lobby_type = MATCH_TYPE_ID_TO_LOBBY_TYPE.get(match.game_mode_id)
    if lobby_type == LobbyType.EMPIRE_WARS:
        return player.ew_team if team_game else player.ew_solo
    if team_game:
        return player.team or player.solo
    return player.solo or player.team


def team_players(match: ObservableMatch) -> dict[int, list[UserStats]]:
    players_by_id = {player.profile_id: player for player in match.players}
    return {
        int(team_num): [players_by_id[pid] for pid in pids if pid in players_by_id]
        for team_num, pids in sorted(match.teams.items(), key=lambda t: int(t[0]))
    }


def team_rating(match: ObservableMatch, players: list[UserStats]) -> int | None:
    ratings = [
        stats.rating
        for stats in (relevant_stats(match, p) for p in players)
        if stats and stats.rating
    ]
    return round(sum(ratings) / len(ratings)) if ratings else None


def elo_gap(match: ObservableMatch) -> tuple[int, list[UserStats]] | None:
    """Gap in 1v1 RM rating between the highest rated players of the teams,
    plus the players of the team whose highest rated player is lowest."""
    teams = team_players(match)
    if len(teams) < 2:
        return None
    top_ratings = []
    for players in teams.values():
        solo_ratings = [p.solo.rating for p in players if p.solo and p.solo.rating]
        if not solo_ratings:
            return None
        top_ratings.append((max(solo_ratings), players))
    lowest_rating, weaker_team = min(top_ratings, key=lambda t: t[0])
    highest_rating = max(rating for rating, _ in top_ratings)
    return highest_rating - lowest_rating, weaker_team


def smurf_gaps(match: ObservableMatch) -> list[tuple[UserStats, int]]:
    """Players below their all-time highest 1v1 RM rating, with how far below,
    largest gap first."""
    gaps = [
        (p, p.solo.highest_rating - p.solo.rating)
        for p in match.players
        if p.solo and p.solo.rating and p.solo.highest_rating
    ]
    return sorted(
        ((p, gap) for p, gap in gaps if gap > 0), key=lambda t: t[1], reverse=True
    )


def status_line(
    match: ObservableMatch, state: MatchState, matched_timestamp: int, now: int
) -> str:
    if state == MatchState.DODGED:
        return f":x: **Cancelled**, a player dodged <t:{now}:R>"
    if state == MatchState.MATCHED:
        return f":hourglass_flowing_sand: **Matched** <t:{matched_timestamp}:R>, starting soon"
    start = match.start_timestamp or now
    if state == MatchState.FINISHED:
        finish = match.finish_timestamp or now
        minutes = max(0, finish - start) // 60
        return f":checkered_flag: **Finished** <t:{finish}:R> after {minutes} min"
    return f":green_circle: **Live** since <t:{start}:R>"


def _rating_tag(letter: str, stats: LeaderboardStats | None) -> str:
    if not stats or not stats.rating:
        return f"{letter}:----"
    return f"{letter}:{min(stats.rating, 9999):04d}"


def _winrate_tag(letter: str, stats: LeaderboardStats | None) -> str:
    games = (stats.wins or 0) + (stats.losses or 0) if stats else 0
    if not games:
        return f"{letter}:--%"
    return f"{letter}:{min(round(stats.wins / games * 100), 99):02d}%"


def stat_tags(match: ObservableMatch, player: UserStats) -> str:
    """`R:0660 | T:1100 | E:0300 | Q:35%`"""
    ew = player.ew_team if is_team_game(match) else player.ew_solo
    return " | ".join(
        (
            _rating_tag("R", player.solo),
            _rating_tag("T", player.team),
            _rating_tag("E", ew),
            _winrate_tag("Q", player.quickplay),
        )
    )


def match_summary(match: ObservableMatch, map_info: dict | None = None) -> str:
    """Plain-text version of the match card, for logs."""
    map_name = map_info["mapName"] if map_info else match.map_name
    region = match.server_region_code or "unknown"
    lines = [f"{game_mode_name(match)} · {map_name} · {region}"]
    for team_num, players in team_players(match).items():
        rating = team_rating(match, players)
        average = f" (avg {rating})" if rating else ""
        lines.append(f"  Team {team_num + 1}{average}")
        for player in players:
            number = f"P{player.color} " if player.color else ""
            lines.append(
                f"    {stat_tags(match, player)}  {number}{player.name or 'Unknown'}"
                f" (Relic ID {player.profile_id})"
            )
    return "\n".join(lines)


def player_line(match: ObservableMatch, player: UserStats, state: MatchState) -> str:
    """The linked player name, with `R:0660 | T:1100 | E:0300 | Q:35%` below it
    (names are too long to share a row on mobile). Once the game finished a
    crown or skull in front of the name marks winners and losers."""
    tags = stat_tags(match, player)
    name = escape_name(player.name)
    profile_url = f"{AOE2INSIGHTS_URL}/user/relic/{player.profile_id}/"
    result = ""
    if state == MatchState.FINISHED and player.won is not None:
        result = ":crown: " if player.won else ":skull: "
    number = f"`{player.color}` " if player.color else ""
    return f"{result}{number}[{name}]({profile_url})\n`{tags}`"


def legend(match: ObservableMatch) -> str:
    size = "team" if is_team_game(match) else "1v1"
    return (
        "-# R: Ranked 1v1 · T: Ranked team · "
        f"E: Empire Wars {size} · Q: Quick Play win rate"
    )


def match_buttons(match: ObservableMatch, state: MatchState) -> ui.ActionRow:
    return ui.ActionRow(
        ui.Button(
            style=discord.ButtonStyle.link,
            label="Spectate",
            emoji="👁️",
            url=f"{REPLAY_REDIRECT_HANDLER_URL}?match_id={match.match_id}",
            disabled=state == MatchState.DODGED,
        ),
        ui.Button(
            style=discord.ButtonStyle.link,
            label="Insights",
            emoji="📊",
            url=f"{AOE2INSIGHTS_URL}/match/{match.match_id}/",
        ),
    )


def build_match_view(
    match: ObservableMatch,
    state: MatchState,
    matched_timestamp: int | None = None,
    map_info: dict | None = None,
    now: int | None = None,
) -> ui.LayoutView:
    """`map_info` is an aoe2companion map ({"mapName", "imageUrl"})."""
    now = now or int(time.time())
    matched_timestamp = matched_timestamp or now
    region = match.server_region_code or "unknown"
    map_name = map_info["mapName"] if map_info else match.map_name
    thumbnail_url = map_info.get("imageUrl") if map_info else None
    header = (
        f"### {game_mode_name(match)} · {map_name}\n-# :globe_with_meridians: {region}"
    )
    status = status_line(match, state, matched_timestamp, now)
    top = (
        ui.Section(header, status, accessory=ui.Thumbnail(thumbnail_url))
        if thumbnail_url
        else ui.TextDisplay(f"{header}\n{status}")
    )

    # Teams are separated by lines like the header, without team headers.
    items: list[ui.Item] = [top]
    for players in team_players(match).values():
        items.append(ui.Separator())
        items.append(
            ui.TextDisplay("\n".join(player_line(match, p, state) for p in players))
        )

    view = ui.LayoutView(timeout=None)
    view.add_item(
        ui.Container(
            *items,
            ui.TextDisplay(legend(match)),
            match_buttons(match, state),
            accent_colour=STATE_TO_COLOUR[state],
        )
    )
    return view

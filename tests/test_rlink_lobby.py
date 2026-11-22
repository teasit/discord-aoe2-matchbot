"""Parsing of Relic Link advertisements into lobbies."""

import base64

import pytest
from conftest import (
    ARABIA,
    CUSTOM,
    DEATHMATCH,
    EMPIRE_WARS_1V1,
    QUICKPLAY_1V1,
    RANKED_1V1,
    advertisement_row,
    encode_options,
    make_lobby,
)

from matchbot import rlink
from matchbot.rlink import Lobby, LobbyType, Profile, parse_advertisement_options


def test_parse_options_roundtrip():
    options = {"10": "10875", "7": "1", "11": "my_map.rms", "empty": ""}
    assert parse_advertisement_options(encode_options(options)) == options


def test_parse_options_value_with_colon():
    assert parse_advertisement_options(encode_options({"k": "a:b"})) == {"k": "a:b"}


@pytest.mark.parametrize(
    "options", ["", "not base64!", base64.b64encode(b"not zlib").decode(), None]
)
def test_parse_options_malformed_is_empty(options):
    assert parse_advertisement_options(options) == {}


def test_from_advertisement():
    row = advertisement_row(
        555, [(11, 0), (12, 0), (21, 1), (22, 1)], map_file="my_pool_map.rms2"
    )
    lobby = Lobby.from_advertisement(row)
    assert lobby.match_id == 555
    assert lobby.profile_ids == [11, 12, 21, 22]
    assert lobby.teams == {"0": [11, 12], "1": [21, 22]}
    assert lobby.map_name == "my_pool_map"
    assert lobby.map_location_id == ARABIA
    assert lobby.options_settled
    assert lobby.is_automatch
    assert not lobby.is_queue
    assert not lobby.started
    assert lobby.server_region_code == "westeurope"
    assert lobby.spectator_delay == 120


def test_from_advertisement_without_options():
    row = advertisement_row(1, [(1, 0)])
    row[12] = ""
    lobby = Lobby.from_advertisement(row)
    assert lobby.map_location_id is None
    assert not lobby.options_settled
    assert lobby.custom_map_file == ""


def test_from_advertisement_non_numeric_location():
    row = advertisement_row(1, [(1, 0)])
    row[12] = encode_options({rlink.OPTION_MAP_LOCATION: "abc"})
    assert Lobby.from_advertisement(row).map_location_id is None


def test_custom_map_file():
    lobby = make_lobby(
        1,
        [(1, 0)],
        map_location=rlink.MAP_LOCATION_CUSTOM,
        custom_map_file="Megarandom.rms",
    )
    assert lobby.map_location_id == rlink.MAP_LOCATION_CUSTOM
    assert lobby.custom_map_file == "Megarandom.rms"


@pytest.mark.parametrize(
    "game_mode_id, lobby_type",
    [
        (RANKED_1V1, LobbyType.RANKED),
        (9, LobbyType.RANKED),
        (66, LobbyType.RANKED),
        (QUICKPLAY_1V1, LobbyType.QUICKPLAY),
        (21, LobbyType.QUICKPLAY),
        (EMPIRE_WARS_1V1, LobbyType.EMPIRE_WARS),
        (89, LobbyType.EMPIRE_WARS),
        (CUSTOM, LobbyType.CUSTOM),
        (DEATHMATCH, LobbyType.OTHER),
        (12345, LobbyType.OTHER),
    ],
)
def test_lobby_type_by_match_type(game_mode_id, lobby_type):
    assert make_lobby(1, [(1, 0)], game_mode_id=game_mode_id).lobby_type == lobby_type


def test_lobby_type_ignores_description():
    # Matchmade lobbies briefly have no description before AUTOMATCH.
    lobby = make_lobby(1, [(1, 0)], description="", game_mode_id=RANKED_1V1)
    assert lobby.lobby_type == LobbyType.RANKED
    assert not lobby.is_automatch


def test_queue_lobby():
    lobby = make_lobby(1, [(1, 0)], description=rlink.LOBBY_DESCRIPTION_QUEUE)
    assert lobby.is_queue
    assert not lobby.is_automatch


@pytest.mark.parametrize(
    "settled, start, map_settled",
    [(False, None, False), (True, None, True), (False, 1700000000, True)],
)
def test_map_settled(settled, start, map_settled):
    lobby = make_lobby(1, [(1, 0)], settled=settled, start=start)
    assert lobby.map_settled is map_settled
    assert lobby.started is (start is not None)


def test_profile_from_profile_info():
    profile = Profile.from_profile_info([1, 42, "/steam/76561198000000001", "", "Bob"])
    assert profile == Profile(42, "Bob", 76561198000000001)
    assert Profile.from_profile_info([1, 43, "/xboxlive/123", "", "X"]).steam_id is None
    assert Profile.from_profile_info([1, 44, None, "", "Y"]).steam_id is None

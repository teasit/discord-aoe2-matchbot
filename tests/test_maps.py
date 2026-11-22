"""Map names from the game's string table."""

import time

import pytest
from conftest import ARABIA, ARCHIPELAGO, make_lobby

from matchbot import maps, rlink
from matchbot.maps import UNKNOWN_MAP, MapNames, parse_strings


@pytest.fixture
def map_names(tmp_path):
    return MapNames(tmp_path / "strings.txt")


def test_parse_strings():
    text = '10875 "Arabia"\n# comment\n10876   "Archi pelago"  \nbroken line\n1 ""\n'
    assert parse_strings(text) == {10875: "Arabia", 10876: "Archi pelago", 1: ""}


def test_bundled_names(map_names):
    assert map_names.name(make_lobby(1, [(1, 0)], map_location=ARABIA)) == "Arabia"
    assert (
        map_names.name(make_lobby(1, [(1, 0)], map_location=ARCHIPELAGO))
        == "Archipelago"
    )


def test_location_wins_over_advertised_file_name(map_names):
    lobby = make_lobby(1, [(1, 0)], map_location=ARABIA, map_file="arena.rms")
    assert map_names.name(lobby) == "Arabia"


def test_no_location_falls_back_to_file_name(map_names):
    lobby = make_lobby(1, [(1, 0)], map_location=None, map_file="arena.rms")
    assert map_names.name(lobby) == "arena"


@pytest.mark.parametrize(
    "custom_file, expected", [("Megarandom.rms2", "Megarandom"), (None, "my map")]
)
def test_custom_map(map_names, custom_file, expected):
    lobby = make_lobby(
        1,
        [(1, 0)],
        game_mode_id=0,
        map_location=rlink.MAP_LOCATION_CUSTOM,
        custom_map_file=custom_file,
        map_file="my map",
    )
    assert map_names.name(lobby) == expected


def test_unknown_location_requests_refresh(map_names):
    map_names._updated = time.time()
    assert not map_names.needs_refresh
    assert map_names.name(make_lobby(1, [(1, 0)], map_location=999999)) == UNKNOWN_MAP
    assert map_names.needs_refresh


def test_needs_refresh_when_old(map_names):
    assert map_names.needs_refresh  # never loaded from Steam
    map_names._updated = time.time()
    assert not map_names.needs_refresh


def test_failed_refresh_is_not_retried_immediately(map_names):
    map_names._last_refresh_attempt = time.time()
    assert not map_names.needs_refresh


class FakeSteam:
    def __init__(self, text: str):
        self.text = text
        self.requests = []

    def read_depot_file(self, app_id, path):
        self.requests.append((app_id, path))
        return ("﻿" + self.text).encode("utf-8")


def test_refresh_from_steam_and_cache(tmp_path):
    cache = tmp_path / "cache" / "strings.txt"
    map_names = MapNames(cache)
    map_names.name(make_lobby(1, [(1, 0)], map_location=999999))
    steam = FakeSteam('999999 "Brand New Map"\n10875 "Arabia (new)"\n')
    map_names.refresh(steam)
    assert steam.requests == [(rlink.APP_ID, maps.STRINGS_DEPOT_PATH)]
    assert (
        map_names.name(make_lobby(1, [(1, 0)], map_location=999999)) == "Brand New Map"
    )
    assert not map_names.needs_refresh
    # The cached table is used on the next start.
    assert cache.exists()
    reloaded = MapNames(cache)
    assert reloaded.name(make_lobby(1, [(1, 0)], map_location=ARABIA)) == "Arabia (new)"
    assert not reloaded.needs_refresh


def test_refresh_with_empty_file_fails(map_names):
    with pytest.raises(ValueError):
        map_names.refresh(FakeSteam("nothing useful"))


def test_broken_cache_file_is_ignored(tmp_path):
    cache = tmp_path / "strings.txt"
    cache.write_bytes(b"\xff\xfe\x00broken")
    map_names = MapNames(cache)
    assert map_names.name(make_lobby(1, [(1, 0)], map_location=ARABIA)) == "Arabia"

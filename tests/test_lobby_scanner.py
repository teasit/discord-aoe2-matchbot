"""LobbyScanner: finding new lobbies by scanning a window of sequential ids."""

import logging
import time

import pytest
from conftest import make_lobby

from matchbot.rlink import LobbyScanner, RelicLinkClient


class FakeClient:
    """Lobbies exist for the ids in `existing`; records the scanned ids."""

    def __init__(self, existing=()):
        self.logger = logging.getLogger("test")
        self.existing = set(existing)
        self.scanned: list[list[int]] = []

    async def get_lobbies_by_match_ids(self, match_ids):
        self.scanned.append(match_ids)
        return [make_lobby(i, [(i, 0)]) for i in match_ids if i in self.existing]


@pytest.fixture
def newest(monkeypatch):
    """The id findAdvertisements reports as newest; set `.value` in tests."""

    class Newest:
        value = 1000
        calls = 0
        error = None

    async def get_newest_lobby_id():
        Newest.calls += 1
        if Newest.error:
            raise Newest.error
        return Newest.value

    monkeypatch.setattr(RelicLinkClient, "get_newest_lobby_id", get_newest_lobby_id)
    return Newest


def window(newest_id):
    return list(
        range(newest_id - LobbyScanner.IDS_BEHIND, newest_id + LobbyScanner.IDS_AHEAD)
    )


async def test_scans_window_around_newest_id(newest):
    client = FakeClient(existing={990, 1005})
    scanner = LobbyScanner(client)
    lobbies = await scanner.scan()
    assert client.scanned == [window(1000)]
    assert [lobby.match_id for lobby in lobbies] == [990, 1005]


async def test_window_follows_found_lobbies(newest):
    client = FakeClient(existing={1100})
    scanner = LobbyScanner(client)
    await scanner.scan()
    assert scanner.newest_match_id == 1100
    await scanner.scan()
    # No resync within RESYNC_SECONDS; the window moved with the lobby.
    assert newest.calls == 1
    assert client.scanned[1] == window(1100)


async def test_window_never_moves_back(newest):
    client = FakeClient(existing={900})
    scanner = LobbyScanner(client)
    await scanner.scan()
    assert scanner.newest_match_id == 1000


async def test_extra_ids_outside_window_are_scanned_once(newest):
    client = FakeClient(existing={5, 1001})
    scanner = LobbyScanner(client)
    lobbies = await scanner.scan(extra_match_ids=[5, 1001])
    scanned = client.scanned[0]
    assert scanned[: len(window(1000))] == window(1000)
    # 1001 is in the window already, 5 is added.
    assert scanned[len(window(1000)) :] == [5]
    assert {lobby.match_id for lobby in lobbies} == {5, 1001}


async def test_no_newest_id_returns_nothing(newest):
    newest.value = None
    client = FakeClient()
    scanner = LobbyScanner(client)
    assert await scanner.scan() == []
    assert client.scanned == []


async def test_resync_failure_keeps_scanning(newest):
    client = FakeClient()
    scanner = LobbyScanner(client)
    await scanner.scan()
    newest.error = RuntimeError("offline")
    scanner._last_resync = float("-inf")
    await scanner.scan()
    assert client.scanned[-1] == window(1000)


async def test_resync_catches_up_after_stall(newest, caplog):
    client = FakeClient()
    scanner = LobbyScanner(client)
    await scanner.scan()
    # The id counter ran far past the window while the bot was stalled.
    newest.value = 5000
    scanner._last_resync = time.monotonic() - LobbyScanner.RESYNC_SECONDS
    with caplog.at_level(logging.WARNING):
        await scanner.scan()
    assert client.scanned[-1] == window(5000)
    assert "fell behind" in caplog.text


async def test_resync_never_moves_back(newest):
    client = FakeClient(existing={1120})
    scanner = LobbyScanner(client)
    await scanner.scan()
    newest.value = 1050
    scanner._last_resync = float("-inf")
    await scanner.scan()
    assert scanner.newest_match_id == 1120

"""aoe2companion client: maps, Quick Play stats, colours, results, rate limit."""

from datetime import UTC, datetime

import pytest

from matchbot import aoe2companion
from matchbot.aoe2companion import Aoe2CompanionClient, normalize_map_name

MAPS = [
    {"mapId": "rm_arabia", "mapName": "Arabia", "imageUrl": "https://img/arabia"},
    {"mapId": "rm_scandinavia", "mapName": "Scandinavia", "imageUrl": "s"},
    {"mapId": "rwm_amazon_tunnel", "mapName": "Amazon Tunnel", "imageUrl": "a"},
]


@pytest.fixture
def client():
    """A client whose API answers come from `client.responses[path]`."""
    client = Aoe2CompanionClient()
    client.responses = {}
    client.requests = []

    async def get(path, params=None):
        client.requests.append(path)
        response = client.responses[path]
        if isinstance(response, Exception):
            raise response
        return response

    client._get = get
    return client


@pytest.mark.parametrize(
    "name, normalized",
    [
        ("Arabia", "arabia"),
        ("rm_arabia.rms", "arabia"),
        ("Amazon Tunnel", "amazontunnel"),
        ("rwm_amazon_tunnel.rms2", "amazontunnel"),
        ("scandanavia", "scandinavia"),
    ],
)
def test_normalize_map_name(name, normalized):
    assert normalize_map_name(name) == normalized


async def test_get_map_by_display_or_file_name(client):
    client.responses["/maps"] = MAPS
    assert (await client.get_map("Arabia"))["imageUrl"] == "https://img/arabia"
    assert (await client.get_map("rwm_amazon_tunnel"))["mapName"] == "Amazon Tunnel"
    assert (await client.get_map("Scandanavia"))["mapName"] == "Scandinavia"
    assert await client.get_map("Unknown map") is None
    # The map list is cached.
    assert client.requests == ["/maps"]


async def test_get_map_failure_is_none(client):
    client.responses["/maps"] = RuntimeError("down")
    assert await client.get_map("Arabia") is None


async def test_quickplay_stats_summed_and_cached(client):
    client.responses["/profiles/1"] = {
        "leaderboards": [
            {"leaderboardId": "qp_rm_1v1", "wins": 3, "losses": 1},
            {"leaderboardId": "qp_rm_team", "wins": 7, "losses": 9},
            {"leaderboardId": "rm_1v1", "wins": 100, "losses": 100},
        ]
    }
    client.responses["/profiles/2"] = {
        "leaderboards": [{"leaderboardId": "rm_1v1", "wins": 1, "losses": 1}]
    }
    client.responses["/profiles/3"] = RuntimeError("down")
    result = await client.get_quickplay_stats([1, 2, 3])
    assert list(result) == [1]
    assert (result[1].wins, result[1].losses) == (10, 10)
    await client.get_quickplay_stats([1, 2])
    # Players without Quick Play games are cached too, failures are not.
    assert client.requests.count("/profiles/1") == 1
    assert client.requests.count("/profiles/2") == 1


MATCH = {
    "finished": "2026-11-22T12:30:00.000Z",
    "teams": [
        {"players": [{"profileId": 1, "color": 1, "won": True}]},
        {"players": [{"profileId": 2, "color": 2, "won": False}]},
    ],
}


async def test_player_colors(client):
    client.responses["/matches/5"] = MATCH
    assert await client.get_player_colors(5) == {1: 1, 2: 2}


async def test_player_colors_unknown(client):
    client.responses["/matches/5"] = {"teams": [{"players": [{"profileId": 1}]}]}
    assert await client.get_player_colors(5) is None
    client.responses["/matches/5"] = RuntimeError("404")
    assert await client.get_player_colors(5) is None


async def test_match_result(client):
    client.responses["/matches/5"] = MATCH
    finished, won = await client.get_match_result(5)
    assert finished == int(datetime(2026, 11, 22, 12, 30, tzinfo=UTC).timestamp())
    assert won == {1: True, 2: False}


@pytest.mark.parametrize(
    "match",
    [
        {**MATCH, "finished": None},
        # Finished, but no winner known yet.
        {**MATCH, "teams": [{"players": [{"profileId": 1, "won": None}]}]},
    ],
)
async def test_match_result_not_yet(client, match):
    client.responses["/matches/5"] = match
    assert await client.get_match_result(5) is None


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def get(self, url, params=None):
        self.urls.append(url)
        return self.responses.pop(0)


async def test_get_retries_after_rate_limit():
    client = Aoe2CompanionClient()
    client._session = FakeSession(
        [FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, {"ok": 1})]
    )
    assert await client._get("/x") == {"ok": 1}
    assert client._session.urls == [f"{aoe2companion.API_URL}/x"] * 2


async def test_get_gives_up_after_retries():
    client = Aoe2CompanionClient()
    client._session = FakeSession(
        [FakeResponse(429, headers={"Retry-After": "0"})]
        * (aoe2companion.RATE_LIMIT_RETRIES + 1)
    )
    with pytest.raises(RuntimeError, match="429"):
        await client._get("/x")


async def test_rate_limiter_waits_when_window_is_full(monkeypatch):
    monkeypatch.setattr(aoe2companion, "RATE_LIMIT_REQUESTS", 2)
    monkeypatch.setattr(aoe2companion, "RATE_LIMIT_WINDOW_SECONDS", 0.2)
    client = Aoe2CompanionClient()
    import time

    start = time.monotonic()
    for _ in range(3):
        await client._wait_for_rate_limit()
    # The third request had to wait for the first to leave the window.
    assert time.monotonic() - start >= 0.2
    assert len(client._request_times) == 2

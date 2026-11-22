"""RelicLinkClient request handling against a fake HTTP session."""

import asyncio
import json
import time

import pytest
from conftest import advertisement_row

from matchbot import rlink
from matchbot.rlink import (
    AppTicket,
    RelicLinkAPIError,
    RelicLinkClient,
    RelicLinkNotLoggedInError,
    RelicLinkSession,
)


class FakeHTTPResponse:
    def __init__(self, status=200, body=None):
        self.status = status
        self._body = body

    async def json(self):
        return self._body

    async def text(self):
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeHTTPSession:
    """Answers requests with `handler(method, url, params_or_data)`."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def _request(self, method, url, payload):
        self.calls.append((method, url, payload))
        return self.handler(method, url, payload)

    def get(self, url, params=None):
        return self._request("GET", url, params)

    def post(self, url, data=None, headers=None):
        return self._request("POST", url, data)

    async def close(self):
        pass


def logged_in_client(handler) -> tuple[RelicLinkClient, FakeHTTPSession]:
    client = RelicLinkClient(steam=None)
    session = FakeHTTPSession(handler)
    client._session = session
    client._reliclink_session = RelicLinkSession("session-id")
    client._app_ticket = AppTicket("ticket", int(asyncio.get_event_loop().time()))
    return client, session


async def test_get_advertisements_chunks_ids():
    def handler(method, url, params):
        ids = json.loads(params["match_ids"])
        assert len(ids) <= rlink.GET_ADVERTISEMENTS_MAX_IDS
        # Only even ids exist.
        rows = [advertisement_row(i, [(i, 0)]) for i in ids if i % 2 == 0]
        return FakeHTTPResponse(200, [0, rows])

    client, session = logged_in_client(handler)
    lobbies = await client.get_lobbies_by_match_ids(list(range(250)))
    assert len(session.calls) == 3
    assert [lobby.match_id for lobby in lobbies] == list(range(0, 250, 2))
    assert session.calls[0][2]["sessionID"] == "session-id"
    # Every call is counted for the session.
    assert client._reliclink_session.call_counter == 3


async def test_get_advertisements_relogs_once_on_401():
    responses = [FakeHTTPResponse(401, "expired"), FakeHTTPResponse(200, [0, []])]
    client, session = logged_in_client(lambda *a: responses.pop(0))
    logins = []

    async def login():
        logins.append(1)
        client._app_ticket = AppTicket("new", int(asyncio.get_event_loop().time()))

    client.login = login
    assert await client.get_advertisements([1]) == []
    assert logins == [1]
    assert len(session.calls) == 2


async def test_get_advertisements_second_401_raises():
    client, _ = logged_in_client(lambda *a: FakeHTTPResponse(401, "expired"))

    async def login():
        client._app_ticket = AppTicket("new", int(asyncio.get_event_loop().time()))

    client.login = login
    with pytest.raises(RelicLinkAPIError) as error:
        await client.get_advertisements([1])
    assert error.value.code == 401


async def test_get_advertisements_error_status_code():
    client, _ = logged_in_client(lambda *a: FakeHTTPResponse(200, [2, []]))
    with pytest.raises(RelicLinkAPIError):
        await client.get_advertisements([1])


async def test_get_advertisements_http_error():
    client, _ = logged_in_client(lambda *a: FakeHTTPResponse(500, "boom"))
    with pytest.raises(RelicLinkAPIError) as error:
        await client.get_advertisements([1])
    assert error.value.code == 500


async def test_failed_login_is_not_retried_immediately():
    client = RelicLinkClient(steam=None)
    attempts = []

    async def login():
        attempts.append(1)
        client._last_login_attempt = time.monotonic()
        raise rlink.RelicLinkLoginError("nope")

    client.login = login
    with pytest.raises(rlink.RelicLinkLoginError):
        await client.get_advertisements([1])
    # The next call within LOGIN_RETRY_SECONDS does not hammer the login.
    with pytest.raises(RelicLinkNotLoggedInError):
        await client.get_advertisements([1])
    assert attempts == [1]


async def test_find_profiles_by_platform_id():
    def handler(method, url, data):
        assert method == "POST"
        assert json.loads(data["platformIDs"]) == ["76561198000000001"]
        return FakeHTTPResponse(200, [0, [[1, 4242, "/steam/76561198000000001"]]])

    client, _ = logged_in_client(handler)
    assert await client.find_profiles_by_platform_id(["76561198000000001"]) == [4242]


async def test_find_profiles_unknown_is_empty():
    client, _ = logged_in_client(lambda *a: FakeHTTPResponse(200, [7]))
    assert await client.find_profiles_by_platform_id([1]) == []


def stat_groups_response():
    """Two players, 11 with RM 1v1/team/EW stats, 12 with RM 1v1 only."""

    def group(statgroup_id, leaderboard_type, wins, losses, rating, highest):
        row = [statgroup_id, leaderboard_type, wins, losses] + [0] * 13
        row[12] = rating
        row[16] = highest
        return row

    return [
        0,
        [[101, 0, 0, 0, [11]], [102, 0, 0, 0, [12]]],
        [
            [101, 11, "/steam/76561198000000011", "", "Alice"],
            [102, 12, "", "", "Bob"],
        ],
        [
            group(101, 3, 30, 20, 1500, 1600),
            group(101, 4, 5, 5, 1400, 0),
            group(101, 13, 1, 2, 1200, 1250),
            group(101, 14, 0, 0, 900, 900),
            group(102, 3, 1, 1, 800, 800),
            # Other leaderboards (e.g. Deathmatch) are ignored.
            group(102, 1, 9, 9, 2000, 2000),
        ],
    ]


async def test_get_leaderboard_stat_groups():
    client, _ = logged_in_client(
        lambda *a: FakeHTTPResponse(200, stat_groups_response())
    )
    alice, bob = await client.get_leaderboard_stat_groups_by_profile_ids([11, 12])
    assert alice.name == "Alice"
    assert (alice.solo.rating, alice.solo.wins, alice.solo.losses) == (1500, 30, 20)
    assert alice.solo.highest_rating == 1600
    # A highest rating of 0 means never rated.
    assert alice.team.highest_rating is None
    assert alice.ew_solo.rating == 1200
    assert alice.ew_team.rating == 900
    assert bob.name == "Bob"
    assert bob.solo.rating == 800
    assert bob.team is None and bob.ew_solo is None


async def test_get_profiles():
    client, _ = logged_in_client(
        lambda *a: FakeHTTPResponse(200, stat_groups_response())
    )
    profiles = await client.get_profiles([11, 12])
    assert [(p.profile_id, p.name, p.steam_id) for p in profiles] == [
        (11, "Alice", 76561198000000011),
        (12, "Bob", None),
    ]


async def test_get_match_from_lobby():
    client, _ = logged_in_client(
        lambda *a: FakeHTTPResponse(200, stat_groups_response())
    )
    lobby = rlink.Lobby.from_advertisement(
        advertisement_row(77, [(11, 0), (12, 1)], start=1700000000)
    )
    match = await client.get_match_from_lobby(lobby)
    assert match.match_id == 77
    assert match.teams == {"0": [11], "1": [12]}
    assert [p.name for p in match.players] == ["Alice", "Bob"]
    assert match.start_timestamp == 1700000000
    assert match.num_players == 2


async def test_get_newest_lobby_id(monkeypatch):
    async def get_lobbies():
        return {"matches": [{"id": 5}, {"id": 9}, {"id": 7}]}

    monkeypatch.setattr(RelicLinkClient, "get_lobbies", get_lobbies)
    assert await RelicLinkClient.get_newest_lobby_id() == 9


@pytest.mark.parametrize("lobbies", [None, {}, {"matches": []}])
async def test_get_newest_lobby_id_none(monkeypatch, lobbies):
    async def get_lobbies():
        return lobbies

    monkeypatch.setattr(RelicLinkClient, "get_lobbies", get_lobbies)
    assert await RelicLinkClient.get_newest_lobby_id() is None

"""config.yaml loading and version/update checks."""

import tomllib
from pathlib import Path

import pytest

from matchbot import version
from matchbot.config import ConfigError, MatchBotConfig
from matchbot.rlink import LobbyType

REPO = Path(__file__).resolve().parents[1]


# --- config -------------------------------------------------------------------


def test_config_defaults_without_file():
    assert MatchBotConfig.load(None).tracked_lobby_types == {
        LobbyType.RANKED,
        LobbyType.QUICKPLAY,
    }
    assert MatchBotConfig.load("/does/not/exist.yaml") == MatchBotConfig()


def test_config_empty_file(tmp_path):
    file = tmp_path / "config.yaml"
    file.write_text("")
    assert MatchBotConfig.load(file) == MatchBotConfig()


def test_config_lobby_types(tmp_path):
    file = tmp_path / "config.yaml"
    file.write_text("tracked_lobby_types: [custom, empire_wars, other]\n")
    assert MatchBotConfig.load(file).tracked_lobby_types == {
        LobbyType.CUSTOM,
        LobbyType.EMPIRE_WARS,
        LobbyType.OTHER,
    }


def test_config_invalid_lobby_type(tmp_path):
    file = tmp_path / "config.yaml"
    file.write_text("tracked_lobby_types: [ranked, deathmatch]\n")
    with pytest.raises(ConfigError, match="deathmatch"):
        MatchBotConfig.load(file)


def test_bundled_config_is_valid():
    assert MatchBotConfig.load(REPO / "config.yaml") == MatchBotConfig()


# --- version ------------------------------------------------------------------


def test_current_version_matches_pyproject():
    with open(REPO / "pyproject.toml", "rb") as f:
        expected = tomllib.load(f)["project"]["version"]
    assert version.current_version() == expected


def test_current_version_unreadable(monkeypatch, tmp_path):
    monkeypatch.setattr(version, "PYPROJECT_FILE", tmp_path / "missing.toml")
    assert version.current_version() == "unknown"


@pytest.mark.parametrize(
    "text, parsed",
    [
        ("v1.2.3", (1, 2, 3)),
        ("1.10.0", (1, 10, 0)),
        ("v2.0.0-rc.1", (2, 0, 0)),
        ("", ()),
    ],
)
def test_parse_version(text, parsed):
    assert version.parse_version(text) == parsed


@pytest.mark.parametrize(
    "latest, current, newer",
    [
        ("v1.0.1", "1.0.0", True),
        ("v1.10.0", "1.9.0", True),
        ("v2.0.0", "1.99.99", True),
        ("v1.0.0", "1.0.0", False),
        ("v0.9.0", "1.0.0", False),
        ("v1.0.0", "unknown", False),
        ("", "1.0.0", False),
    ],
)
def test_is_newer(latest, current, newer):
    assert version.is_newer(latest, current) is newer


def test_latest_release_url_derived_from_project_url():
    assert version.LATEST_RELEASE_URL == (
        "https://api.github.com/repos/teasit/discord-aoe2-matchbot/releases/latest"
    )


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    async def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, url, **kwargs):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.mark.parametrize(
    "response, expected",
    [
        (FakeResponse(200, {"tag_name": "v1.2.3"}), "v1.2.3"),
        (FakeResponse(404, {"message": "Not Found"}), None),
        (FakeResponse(200, ValueError("bad json")), None),
        (OSError("offline"), None),
    ],
)
async def test_latest_release(response, expected):
    assert await version.latest_release(FakeSession(response)) == expected

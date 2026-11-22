"""Slash commands, the database and the bot's status."""

import json

import pytest
from conftest import FakeChannel, FakeInteraction, drain

import aoe_matchbot
from aoe_matchbot import AoeMatchBotDatabase

STEAM_ID = 76561198000000001
STEAM_ID_2 = 76561198000000002
PROFILE = 4242


@pytest.fixture
def channel():
    return FakeChannel(1)


@pytest.fixture
def steam_summaries(monkeypatch):
    """Steam player summaries by SteamID64 (Relic's Steam proxy)."""
    summaries = {}

    async def get(steam_ids):
        return [summaries[s] for s in steam_ids if s in summaries]

    monkeypatch.setattr(aoe_matchbot.RelicLinkClient, "get_steam_player_summaries", get)
    return summaries


def interaction(channel, command="test", **options):
    return FakeInteraction(channel, command, **options)


def saved(bot) -> dict:
    return json.loads(bot.database_file.read_text())


def test_all_commands_registered(bot):
    assert {c.name for c in bot.tree.get_commands()} == {
        "join",
        "leave",
        "add",
        "remove",
        "list",
        "set_elo_max_diff",
        "set_smurf_max_diff",
        "ban",
        "unban",
    }
    for command in bot.tree.get_commands():
        assert command.description


async def test_command_callbacks_are_wired(bot, channel):
    i = interaction(channel, "join")
    await bot.tree.get_command("join").callback(i)
    assert bot.database.channel_ids == [1]


# --- join / leave ---------------------------------------------------------------


async def test_join_and_leave(bot, channel):
    i = interaction(channel)
    await bot._join(i)
    assert i.reply == "Joined <#1>."
    assert bot.database.channel_ids == [1]
    assert saved(bot)["channel_ids"] == [1]

    i = interaction(channel)
    await bot._join(i)
    assert i.reply == "Already joined <#1>."
    assert bot.database.channel_ids == [1]

    i = interaction(channel)
    await bot._leave(i)
    assert i.reply == "Left channel '<#1>'."
    assert saved(bot)["channel_ids"] == []

    i = interaction(channel)
    await bot._leave(i)
    assert i.reply == "Never joined channel '<#1>'."


# --- add / remove ---------------------------------------------------------------


async def test_add_by_steam_id(bot, channel, rlink_client, steam_summaries):
    rlink_client.add_player(PROFILE, steam_id=STEAM_ID, name="Bob")
    steam_summaries[STEAM_ID] = {
        "steamid": str(STEAM_ID),
        "personaname": "BobSteam",
        "profileurl": "https://steamcommunity.com/id/bob/",
    }
    i = interaction(channel)
    await bot._add_player(i, f" {STEAM_ID} ")
    assert i.response.deferred
    assert i.reply == (
        f"Now tracking [BobSteam](https://steamcommunity.com/id/bob/) "
        f"(SteamID64 {STEAM_ID}, Relic ID {PROFILE})!"
    )
    assert saved(bot)["steam_ids"] == [STEAM_ID]

    i = interaction(channel)
    await bot._add_player(i, str(STEAM_ID))
    assert "is already being tracked" in i.reply
    assert bot.database.steam_ids == [STEAM_ID]


async def test_add_by_profile_id(bot, channel, rlink_client, steam_summaries):
    rlink_client.add_player(PROFILE, steam_id=STEAM_ID, name="Bob_")
    i = interaction(channel)
    await bot._add_player(i, str(PROFILE))
    # Without a Steam summary the (escaped) Relic name is used.
    assert i.reply == f"Now tracking Bobˍ (SteamID64 {STEAM_ID}, Relic ID {PROFILE})!"
    assert bot.database.steam_ids == [STEAM_ID]


async def test_add_profile_without_steam(bot, channel, rlink_client, steam_summaries):
    rlink_client.add_player(PROFILE, steam_id=None, name="Xbox")
    i = interaction(channel)
    await bot._add_player(i, str(PROFILE))
    assert "has no Steam account and cannot be tracked" in i.reply
    assert bot.database.steam_ids == []


@pytest.mark.parametrize(
    "player_id, reply",
    [
        ("abc", "Please pass a SteamID64 or a Relic Link profile ID."),
        (
            str(STEAM_ID),
            f"SteamID64 {STEAM_ID} does not exist or has never played "
            "Age of Empires II: Definitive Edition.",
        ),
        ("999", "No Relic Link profile with ID 999 found."),
    ],
)
async def test_add_unknown_player(bot, channel, steam_summaries, player_id, reply):
    i = interaction(channel)
    await bot._add_player(i, player_id)
    assert i.reply == reply
    assert bot.database.steam_ids == []


async def test_add_survives_relic_errors(bot, channel, rlink_client):
    async def fail(profile_ids):
        raise RuntimeError("offline")

    rlink_client.get_profiles = fail
    i = interaction(channel)
    await bot._add_player(i, "999")
    assert i.reply == "No Relic Link profile with ID 999 found."


async def test_remove_by_steam_id_without_lookup(bot, channel, rlink_client):
    bot.database.steam_ids = [STEAM_ID, STEAM_ID_2]
    i = interaction(channel)
    await bot._remove_player(i, str(STEAM_ID))
    assert not i.response.deferred
    assert i.reply == f"Removed SteamID64 {STEAM_ID}."
    assert saved(bot)["steam_ids"] == [STEAM_ID_2]
    assert rlink_client.find_profiles_calls == 0


async def test_remove_by_profile_id(bot, channel, rlink_client):
    rlink_client.add_player(PROFILE, steam_id=STEAM_ID)
    bot.database.steam_ids = [STEAM_ID]
    i = interaction(channel)
    await bot._remove_player(i, str(PROFILE))
    assert i.reply == f"Removed SteamID64 {STEAM_ID}."
    assert bot.database.steam_ids == []


async def test_remove_untracked(bot, channel):
    i = interaction(channel)
    await bot._remove_player(i, str(STEAM_ID))
    assert i.reply == f"Player {STEAM_ID} is not tracked."


# --- list -----------------------------------------------------------------------


async def test_list(bot, channel, steam_summaries):
    bot.database.steam_ids = [STEAM_ID, STEAM_ID_2]
    steam_summaries[STEAM_ID] = {
        "steamid": str(STEAM_ID),
        "personaname": "Bob",
        "profileurl": "https://s/bob",
    }
    bot.database.banlist = {
        "7": {"name": "*Bad*", "steam_id": None, "comment": "griefing"},
        "8": {"name": "Worse", "steam_id": STEAM_ID_2, "comment": ""},
    }
    i = interaction(channel)
    await bot._list_steam_ids(i)
    assert i.reply.splitlines() == [
        "Tracked Steam Users:",
        f"- {STEAM_ID} ([Bob](https://s/bob))",
        f"- {STEAM_ID_2}",
        "Banned players:",
        "- no Steam account (∗Bad∗, Relic ID 7): griefing",
        f"- {STEAM_ID_2} (Worse, Relic ID 8)",
    ]


async def test_long_list_is_split(bot, channel, steam_summaries):
    bot.database.steam_ids = [STEAM_ID + n for n in range(200)]
    i = interaction(channel)
    await bot._list_steam_ids(i)
    assert len(i.replies) > 1
    assert all(len(reply) <= bot.DISCORD_MESSAGE_MAX_LEN for reply in i.replies)
    lines = "".join(i.replies).splitlines()
    assert len(lines) == 201


# --- thresholds -----------------------------------------------------------------


@pytest.mark.parametrize(
    "method, key",
    [("_set_elo_max_diff", "elo_max_diff"), ("_set_smurf_max_diff", "smurf_max_diff")],
)
async def test_set_and_disable_thresholds(bot, channel, method, key):
    i = interaction(channel)
    await getattr(bot, method)(i, 250)
    assert "250" in i.reply and "<#1>" in i.reply
    assert saved(bot)[key] == {"1": 250}

    i = interaction(channel)
    await getattr(bot, method)(i, 0)
    assert "disabled in <#1>" in i.reply
    assert saved(bot)[key] == {}


# --- ban / unban ----------------------------------------------------------------


async def test_ban_and_unban_by_profile_id(bot, channel, rlink_client):
    rlink_client.add_player(PROFILE, steam_id=STEAM_ID, name="Griefer")
    i = interaction(channel)
    await bot._ban(i, str(PROFILE), "smurfs")
    assert i.reply == f"Banned **Griefer** (Relic ID {PROFILE}, SteamID64 {STEAM_ID})."
    assert saved(bot)["banlist"] == {
        str(PROFILE): {"name": "Griefer", "steam_id": STEAM_ID, "comment": "smurfs"}
    }

    i = interaction(channel)
    await bot._unban(i, str(PROFILE))
    assert i.reply == f"Unbanned **Griefer** ({PROFILE})."
    assert saved(bot)["banlist"] == {}


async def test_ban_without_steam_and_unban_by_steam_id(bot, channel, rlink_client):
    rlink_client.add_player(PROFILE, steam_id=None, name="Console")
    i = interaction(channel)
    await bot._ban(i, str(PROFILE), "")
    assert "no Steam account" in i.reply

    bot.database.banlist["5"] = {"name": "Other", "steam_id": STEAM_ID, "comment": ""}
    i = interaction(channel)
    await bot._unban(i, str(STEAM_ID))
    assert i.reply == f"Unbanned **Other** ({STEAM_ID})."
    assert list(bot.database.banlist) == [str(PROFILE)]


async def test_ban_unknown_and_unban_not_banned(bot, channel):
    i = interaction(channel)
    await bot._ban(i, "999", "")
    assert i.reply == "No Relic Link profile with ID 999 found."
    assert bot.database.banlist == {}

    i = interaction(channel)
    await bot._unban(i, "999")
    assert i.reply == "Player 999 is not banned."


# --- replies ----------------------------------------------------------------------


async def test_replies_are_deleted_after_a_while(bot, channel, monkeypatch):
    monkeypatch.setattr(aoe_matchbot, "COMMAND_DELETE_AFTER_SECONDS", 0.01)
    i = interaction(channel, "join")
    await bot._join(i)
    await drain(bot)
    assert i.original_deleted


async def test_deferred_replies_delete_follow_ups(
    bot, channel, monkeypatch, steam_summaries
):
    monkeypatch.setattr(aoe_matchbot, "COMMAND_DELETE_AFTER_SECONDS", 0.01)
    i = interaction(channel, "list")
    await bot._list_steam_ids(i)
    await drain(bot)
    assert i.original_deleted
    assert all(m.deleted for m in i.followup.messages)


async def test_replies_are_kept_when_disabled(bot, channel):
    i = interaction(channel, "join")
    await bot._join(i)
    assert not bot._background_tasks
    assert not i.original_deleted


async def test_replies_are_logged(bot, channel, caplog):
    i = interaction(channel, "set_elo_max_diff", max_diff=300)
    with caplog.at_level("INFO", logger="discord"):
        await bot._set_elo_max_diff(i, 300)
    assert "/set_elo_max_diff max_diff=300 by tester#0001 in #channel-1" in caplog.text


# --- database and status ----------------------------------------------------------


def test_database_roundtrip(tmp_path):
    file = tmp_path / "cache" / "db.json"
    database = AoeMatchBotDatabase(
        channel_ids=[1],
        steam_ids=[STEAM_ID],
        match_ids_analysed=[5],
        elo_max_diff={"1": 300},
        smurf_max_diff={"1": 400},
        banlist={"7": {"name": "B", "steam_id": None, "comment": ""}},
    )
    database.save(file)
    assert AoeMatchBotDatabase.load(file) == database


def test_database_missing_file_is_empty(tmp_path):
    assert AoeMatchBotDatabase.load(tmp_path / "none.json") == AoeMatchBotDatabase()


def test_database_from_older_version_gets_defaults(tmp_path):
    file = tmp_path / "db.json"
    file.write_text(json.dumps({"channel_ids": [1], "steam_ids": [2]}))
    database = AoeMatchBotDatabase.load(file)
    assert database.channel_ids == [1]
    assert database.banlist == {} and database.smurf_max_diff == {}


async def test_bot_loads_existing_database(tmp_path, rlink_client):
    file = tmp_path / "db.json"
    AoeMatchBotDatabase(channel_ids=[9]).save(file)
    bot = aoe_matchbot.AoeMatchBot(rlink_client=rlink_client, db_file=file)
    assert bot.database.channel_ids == [9]


@pytest.mark.parametrize(
    "latest, status",
    [
        (None, "v1.0.0"),
        ("v1.0.0", "v1.0.0"),
        ("v0.9.0", "v1.0.0"),
        ("v1.0.1", "v1.0.0 (v1.0.1 available)"),
    ],
)
async def test_status_text(bot, latest, status):
    bot.version = "1.0.0"
    bot.latest_release = latest
    assert bot._status_text() == status

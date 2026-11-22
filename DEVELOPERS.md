# Developer guide

A short overview for developers. More detail (API quirks, data formats, runtime files) is in [CLAUDE.md](CLAUDE.md).

## How it works

```
Steam ──encrypted app ticket──▶ Relic Link login ──session──▶ LobbyScanner (every 3 s)
                                                                    │ lobbies of tracked players
                                                                    ▼
             aoe2companion ──map image, Quick Play stats, colours, result──▶ Discord post
```

1. **Login.** A Steam account that owns AoE2:DE gets an *encrypted app ticket* for the game. Relic Link (World's Edge, `aoe-api.worldsedgelink.com`) exchanges it for a session. The session expires after a few minutes, so the bot logs in again every 3 minutes. Nothing of the game is installed.
2. **Pre-match detection.** Match ids are sequential across all lobbies, and `getAdvertisements` returns lobbies that have not started yet. Scanning a window of ids around the newest one finds matchmade lobbies seconds to minutes before the game launches. That is why posts appear as *Matched*, before the game starts.
3. **Enrichment.** Relic Link provides players, ratings and the lobby options; the real map is decoded from those options. [aoe2companion](https://data.aoe2companion.com/api) adds what Relic lacks: map images, Quick Play win rates, in-game player colours and match results.
4. **Posting.** Every match is one Discord message (a Components V2 card) that is edited as the match goes from *Matched* to *Live* (or *Dodged*) to *Finished*.

API references: [Relic Link community wiki](https://wiki.librematch.org/rlink/community/start), [OpenAPI spec](https://github.com/librematch/librematch-rlink_client/blob/main/openapi.yaml).

## Modules

| Path | Purpose |
|---|---|
| `src/aoe_matchbot.py` | Entry point: Discord client, slash commands, the login and scan loops, the post lifecycle and the JSON database. |
| `matchbot/rlink` | Async Relic Link client: login, `LobbyScanner`, parsing of the positional JSON lobbies and options, player stats. |
| `matchbot/aoe2companion` | Client for the public aoe2companion API, with caching and a rate limiter (16 requests / 10 s). |
| `matchbot/steam` | Steam login without 2FA (for the app ticket) and download of single files from the game's depot. |
| `matchbot/maps` | Map string id → name, from the game's string table (downloaded from Steam, refreshed daily) with a bundled fallback. |
| `matchbot/ui` | Renders the match card from a match and its state; no Discord state. |
| `matchbot/config` | Loads `config.yaml` (which lobby types are posted). |

Modules are imported as `matchbot.*` because the bot runs from `src/`; there is no package install.

## Run locally

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp /path/to/.env data/.env   # DISCORD_TOKEN, STEAM_USERNAME, STEAM_PASSWORD
uv run src/aoe_matchbot.py
```

Credentials are read from environment variables, then `data/.env`, then `.env` in the repo root. Optional environment variables:

| Variable | Default |
|---|---|
| `MATCHBOT_DATA_DIR` | `data/` (cache, logs, optional `.env` and `config.yaml`) |
| `MATCHBOT_CONFIG` | `config.yaml` in the data dir, else the repo root |
| `MATCHBOT_LOG_LEVEL` | `INFO` |
| `MATCHBOT_COMMAND_DELETE_AFTER` | `10` seconds (`0` keeps command replies) |
| `MATCHBOT_UPDATE_CHECK` | `1`; hourly check for a newer GitHub release, shown in the bot's status (`0` = off) |
| `MATCHBOT_WATCHDOG_SECONDS` | `300` seconds; the bot exits when its event loop is blocked this long, so Docker restarts it (`0` = off) |

Debug helpers:

```bash
PYTHONPATH=src uv run src/matchbot/rlink/__init__.py <steam_id64>...  # print lobbies of these players
PYTHONPATH=src uv run src/matchbot/maps/__init__.py                   # regenerate map_names.json
```

## Build the Docker image

```bash
docker compose up --build                                  # builds from ./Dockerfile, mounts ./data
# or
docker build -t discord-aoe2-matchbot .
docker run -d --name matchbot -v "$PWD/data:/app/data" discord-aoe2-matchbot
```

Put `.env` (and optionally `config.yaml`) into `./data`. CI builds the image for every PR and pushes `ghcr.io/teasit/discord-aoe2-matchbot` on pushes to `main` (`:main`) and for releases (`:latest`, `:X.Y.Z`, `:X.Y`, `:X`), so `:latest` is always the newest release.

## Releases

Releases are made by [release-please](https://github.com/googleapis/release-please) (`.github/workflows/release-please.yml`, config in `release-please-config.json`). Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `feat!:` …): on every push to `main` it opens or updates a release PR that bumps the version in `pyproject.toml` and `uv.lock` and updates `CHANGELOG.md`. Merging that PR tags `vX.Y.Z`, creates the GitHub release and publishes the versioned Docker image.

## Conventions

Tests live in `tests/` and run with `uv run pytest`; they use fakes for Discord, Steam, Relic Link and aoe2companion, so no credentials or network access are needed. CI runs them for every PR, push to `main` and release, before the Docker image is built. Linting and formatting use [Ruff](https://docs.astral.sh/ruff/) (config in `pyproject.toml`, installed by `uv sync` as a dev dependency): run `uv run ruff check --fix . && uv run ruff format .` before committing. The devcontainer formats, sorts imports and applies safe fixes on save. Types are checked with Pylance in standard mode.

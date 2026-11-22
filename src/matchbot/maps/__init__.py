"""Names of the maps of lobbies.

The map file name a Relic Link advertisement carries is unreliable, the real
map is the location option of the lobby: the id of the map name in the
game's string table (see `rlink.OPTION_MAP_LOCATION`). That table is read
from the game files on Steam (the account owns the game, nothing is
installed) and refreshed daily, so maps added by patches resolve without a
code change. `map_names.json` is a bundled copy of the map names in it for
when Steam is not reachable.
"""

import json
import logging
import re
import sys
import time
from pathlib import Path

from matchbot.rlink import APP_ID, MAP_LOCATION_CUSTOM, Lobby
from matchbot.steam import SteamClientWithout2FA

STRINGS_DEPOT_PATH = "resources/en/strings/key-value/key-value-strings-utf8.txt"
BUNDLED_FILE = Path(__file__).with_name("map_names.json")
# String ids of map names (standard, real world and later DLC maps, the
# "Custom Map Pool" of custom lobbies), only used to keep the bundled table
# small.
MAP_STRING_ID_RANGES = (
    range(10875, 11050),
    range(11367, 11368),
    range(13540, 13560),
    range(301100, 301300),
)
REFRESH_SECONDS = 24 * 60 * 60
# An unknown location id means the bundled/cached table is older than the
# game, refresh then, but not more often than this.
UNKNOWN_REFRESH_SECONDS = 60 * 60
UNKNOWN_MAP = "Unknown map"

STRING_LINE = re.compile(r'^(\d+)\s+"(.*)"\s*$')


def parse_strings(text: str) -> dict[int, str]:
    """Parses a key-value-strings file: `<id> "<text>"` per line."""
    strings = {}
    for line in text.splitlines():
        match = STRING_LINE.match(line)
        if match:
            strings[int(match[1])] = match[2]
    return strings


class MapNames:
    logger: logging.Logger

    def __init__(self, cache_file: Path, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger()
        self._cache_file = cache_file
        with open(BUNDLED_FILE, encoding="utf-8") as f:
            self._names = {int(k): v for k, v in json.load(f).items()}
        self._updated = float("-inf")
        self._last_refresh_attempt = float("-inf")
        self._unknown_seen = False
        if cache_file.exists():
            try:
                self._names |= parse_strings(cache_file.read_text(encoding="utf-8"))
                self._updated = cache_file.stat().st_mtime
            except Exception as e:
                self.logger.warning(f"Failed to load map names from {cache_file}: {e}")

    def name(self, lobby: Lobby) -> str:
        """Display name of the map of the lobby."""
        if lobby.map_location_id is None:
            # Lobby without options, nothing better known.
            return lobby.map_name
        if lobby.map_location_id == MAP_LOCATION_CUSTOM:
            file = lobby.custom_map_file or lobby.map_name
            return file.removesuffix(".rms2").removesuffix(".rms")
        name = self._names.get(lobby.map_location_id)
        if name:
            return name
        if not self._unknown_seen:
            self.logger.warning(
                f"Unknown map location id {lobby.map_location_id} "
                f"(match {lobby.match_id}), refreshing map names."
            )
        self._unknown_seen = True
        return UNKNOWN_MAP

    @property
    def needs_refresh(self) -> bool:
        now = time.time()
        if now - self._updated > REFRESH_SECONDS:
            return now - self._last_refresh_attempt > UNKNOWN_REFRESH_SECONDS
        return (
            self._unknown_seen
            and now - self._last_refresh_attempt > UNKNOWN_REFRESH_SECONDS
        )

    def refresh(self, steam_client: SteamClientWithout2FA):
        """Reads the game's current string table from Steam. Blocking (the
        steam library is synchronous), takes a few seconds."""
        self._last_refresh_attempt = time.time()
        text = steam_client.read_depot_file(APP_ID, STRINGS_DEPOT_PATH).decode(
            "utf-8-sig"
        )
        strings = parse_strings(text)
        if not strings:
            raise ValueError(f"No strings in {STRINGS_DEPOT_PATH}.")
        self._names |= strings
        self._updated = time.time()
        self._unknown_seen = False
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._cache_file.write_text(text, encoding="utf-8")
        self.logger.info(f"Loaded {len(strings)} game strings for map names.")


def main():
    """Regenerates the bundled map_names.json from the game files on Steam,
    or from a key-value-strings-utf8.txt given as argument."""
    import os

    from dotenv import load_dotenv

    if len(sys.argv) > 1:
        text = Path(sys.argv[1]).read_text(encoding="utf-8-sig")
    else:
        load_dotenv()
        steam_client = SteamClientWithout2FA(
            os.environ["STEAM_USERNAME"], os.environ["STEAM_PASSWORD"]
        )
        text = steam_client.read_depot_file(APP_ID, STRINGS_DEPOT_PATH).decode(
            "utf-8-sig"
        )
    names = {
        str(string_id): name
        for string_id, name in sorted(parse_strings(text).items())
        if name and any(string_id in ids for ids in MAP_STRING_ID_RANGES)
    }
    with open(BUNDLED_FILE, "w", encoding="utf-8") as f:
        json.dump(names, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"Wrote {len(names)} map names to {BUNDLED_FILE}.")


if __name__ == "__main__":
    main()

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from matchbot.rlink import LobbyType


class ConfigError(Exception):
    pass


@dataclass
class MatchBotConfig:
    tracked_lobby_types: set[LobbyType] = field(
        default_factory=lambda: {LobbyType.RANKED, LobbyType.QUICKPLAY}
    )

    @staticmethod
    def load(file: str | Path | None) -> "MatchBotConfig":
        """Loads the YAML config, using defaults for a missing file or key."""
        config = MatchBotConfig()
        if file is None or not Path(file).exists():
            return config
        with open(file) as f:
            data = yaml.safe_load(f) or {}

        lobby_types = data.get("tracked_lobby_types")
        if lobby_types is not None:
            valid = [lobby_type.value for lobby_type in LobbyType]
            try:
                config.tracked_lobby_types = {LobbyType(t) for t in lobby_types}
            except ValueError as e:
                raise ConfigError(
                    f"{file}: invalid tracked_lobby_types entry ({e}), "
                    f"valid values are {valid}."
                ) from e
        return config

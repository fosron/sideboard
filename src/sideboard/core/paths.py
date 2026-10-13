"""Resolution of the private data locations.

Defaults (per the plan, section 7):

* config: ``~/.config/sideboard/config.toml``
* database: ``~/Library/Application Support/Sideboard/sideboard.sqlite3``
* logs: ``~/Library/Logs/Sideboard/``

Each has an explicit override, by environment variable or CLI flag.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ENV_CONFIG", "ENV_DATA_DIR", "ENV_LOG_DIR", "Paths", "default_paths"]

ENV_CONFIG = "SIDEBOARD_CONFIG"
ENV_DATA_DIR = "SIDEBOARD_DATA_DIR"
ENV_LOG_DIR = "SIDEBOARD_LOG_DIR"

APP_DIR_NAME = "Sideboard"


@dataclass(frozen=True, slots=True)
class Paths:
    config_file: Path
    data_dir: Path
    log_dir: Path
    database_file: Path

    def all(self) -> tuple[Path, ...]:
        return (self.config_file, self.data_dir, self.log_dir, self.database_file)


def default_paths(
    *,
    home: Path | None = None,
    config_file: str | os.PathLike[str] | None = None,
    data_dir: str | os.PathLike[str] | None = None,
    log_dir: str | os.PathLike[str] | None = None,
) -> Paths:
    base = home if home is not None else Path.home()

    resolved_config = (
        Path(config_file).expanduser()
        if config_file
        else Path(os.environ.get(ENV_CONFIG, base / ".config" / "sideboard" / "config.toml"))
    ).expanduser()

    default_data = base / "Library" / "Application Support" / APP_DIR_NAME
    resolved_data = Path(data_dir or os.environ.get(ENV_DATA_DIR, default_data)).expanduser()

    default_logs = base / "Library" / "Logs" / APP_DIR_NAME
    resolved_logs = Path(log_dir or os.environ.get(ENV_LOG_DIR, default_logs)).expanduser()

    return Paths(
        config_file=resolved_config,
        data_dir=resolved_data,
        log_dir=resolved_logs,
        database_file=resolved_data / "sideboard.sqlite3",
    )

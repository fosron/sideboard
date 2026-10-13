"""Shared test fixtures.

Default tests use fixtures, fake clocks and synthetic data. No test in this directory
touches the public internet or a private user file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sideboard.core.clock import FakeClock
from sideboard.core.paths import Paths, default_paths
from sideboard.core.storage import CORE_MIGRATIONS, Database


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 13, 12, 0, tzinfo=UTC))


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    resolved = default_paths(
        home=tmp_path,
        config_file=tmp_path / "config" / "config.toml",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    resolved.config_file.parent.mkdir(parents=True, exist_ok=True)
    return resolved


@pytest.fixture
async def database(paths: Paths) -> AsyncIterator[Database]:
    database = await Database.open(paths.database_file)
    await database.initialize(CORE_MIGRATIONS)
    try:
        yield database
    finally:
        await database.aclose()

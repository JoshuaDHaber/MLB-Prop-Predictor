"""Runtime settings, read from CLI flags, environment variables and an optional ``.env`` file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal ``.env`` loader (KEY=VALUE lines). Existing environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "mlb-prop-predictor"


@dataclass(frozen=True)
class Settings:
    date: str
    season: int
    odds_api_key: str | None
    use_odds: bool
    odds_regions: str = "us"
    odds_bookmakers: str | None = None
    park_factor_seasons: int = 3


# Cache lifetimes (seconds). Odds are cached so re-running a report
# does not spend more of the free Odds API quota.
TTL_SCHEDULE = 10 * 60
TTL_STATS = 6 * 60 * 60
TTL_PAST_SEASON = 30 * 24 * 60 * 60
TTL_STATCAST = 12 * 60 * 60
TTL_WEATHER = 60 * 60
TTL_ODDS = 30 * 60

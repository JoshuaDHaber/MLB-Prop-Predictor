"""Offline demo: serves canned API responses for a fictional slate (see scripts/build_demo_fixtures.py)."""

from __future__ import annotations

from importlib import resources
from typing import Any

from mlb_prop_predictor.http import Response

DEMO_DATE = "2026-07-04"
DEMO_SEASON = 2026


def _read(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8")


class DemoClient:
    """HttpClient that answers from bundled fixtures instead of the network."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str, params: dict[str, Any] | None = None, ttl: int = 0) -> Response:
        params = params or {}
        self.calls.append(url)
        if url.endswith("/schedule"):
            return Response(_read("schedule_recent.json" if "startDate" in params else "schedule.json"))
        if url.endswith("/people"):
            group = "pitching" if "pitching" in str(params.get("hydrate", "")) else "hitting"
            return Response(_read(f"people_{group}.json"))
        if "/people/" in url:
            return Response('{"stats": []}')
        if "/teams/" in url and url.endswith("/stats") and not url.endswith("/teams/stats"):
            return Response('{"stats": []}')
        if url.endswith("/teams/stats"):
            return Response(_read(f"team_stats_{params.get('group', 'hitting')}.json"))
        if url.endswith("/teams"):
            return Response(_read("teams.json"))
        if "baseballsavant" in url:
            return Response(_read(f"savant_{params.get('type', 'batter')}.csv"))
        if "open-meteo" in url:
            return Response(_read("weather.json"))
        if url.endswith("/events"):
            return Response(_read("odds_events.json"))
        if "/events/" in url and url.endswith("/odds"):
            event_id = url.rsplit("/", 2)[-2]
            return Response(
                _read(f"odds_{event_id}.json"),
                headers={"x-requests-remaining": "500 (demo)", "x-requests-last": "0"},
            )
        raise KeyError(f"demo client has no fixture for {url}")

"""Player prop lines from The Odds API (https://the-odds-api.com).

Only two markets are requested -- ``batter_home_runs`` and ``pitcher_strikeouts`` -- to
stay inside the free plan. Each game costs (markets x regions) credits, so a 15-game
slate is about 30 credits. Listing events is free. Responses are cached for 30 minutes.
"""

from __future__ import annotations

import contextlib
import logging
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from mlb_prop_predictor.config import TTL_ODDS
from mlb_prop_predictor.domain import Game, PropPrice
from mlb_prop_predictor.http import HttpClient, HttpError

log = logging.getLogger(__name__)

BASE = "https://api.the-odds-api.com/v4/sports/baseball_mlb"
HR_MARKET = "batter_home_runs"
K_MARKET = "pitcher_strikeouts"
MARKETS = (HR_MARKET, K_MARKET)

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def normalize_name(name: str) -> str:
    """'José Ramírez Jr.' -> 'jose ramirez'. Used to join sportsbook names to MLB names."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z\s]", " ", text)
    parts = [p for p in text.split() if p not in _SUFFIXES]
    return " ".join(parts)


@dataclass
class OddsBook:
    """Prop prices keyed by (market, normalized player name)."""

    prices: dict[tuple[str, str], list[PropPrice]] = field(default_factory=lambda: defaultdict(list))
    credits_remaining: str | None = None
    credits_used_this_run: int = 0
    error: str | None = None

    def lookup(self, market: str, player_name: str) -> list[PropPrice]:
        return self.prices.get((market, normalize_name(player_name)), [])


def _slate_window(day: str) -> tuple[str, str]:
    # A US "game day" runs from roughly 10:00 UTC to 10:00 UTC the next morning.
    start = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=timezone.utc)
    start += timedelta(hours=10)
    end = start + timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)


def match_event(game: Game, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the sportsbook event for an MLB game by team nickname and start time."""
    # Doubleheader games are usually 3+ hours apart, so keep the window tight.
    best, best_gap = None, timedelta(hours=2)
    for ev in events:
        home, away = ev.get("home_team", ""), ev.get("away_team", "")
        if not (home.endswith(game.home.short_name) and away.endswith(game.away.short_name)):
            continue
        try:
            when = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        gap = abs(when - game.start_time)
        if gap < best_gap:
            best, best_gap = ev, gap
    return best


def parse_event_odds(payload: dict[str, Any], book: OddsBook) -> None:
    for bm in payload.get("bookmakers", []):
        title = bm.get("title") or bm.get("key", "?")
        for market in bm.get("markets", []):
            key = market.get("key")
            if key not in MARKETS:
                continue
            for oc in market.get("outcomes", []):
                player = oc.get("description")
                price = oc.get("price")
                if not player or price is None:
                    continue
                book.prices[(key, normalize_name(player))].append(
                    PropPrice(
                        bookmaker=title, side=oc.get("name", ""), point=oc.get("point"), price=int(price)
                    )
                )


def get_prop_odds(
    client: HttpClient,
    api_key: str,
    day: str,
    games: list[Game],
    regions: str = "us",
    bookmakers: str | None = None,
) -> OddsBook:
    book = OddsBook()
    start, end = _slate_window(day)
    try:
        events = client.get(
            f"{BASE}/events",
            {"apiKey": api_key, "commenceTimeFrom": start, "commenceTimeTo": end, "dateFormat": "iso"},
            ttl=TTL_ODDS,
        ).json()
    except (HttpError, ValueError) as exc:
        book.error = f"could not list events: {exc}"
        log.warning("Odds API: %s", book.error)
        return book

    for game in games:
        event = match_event(game, events if isinstance(events, list) else [])
        if event is None:
            log.info("no sportsbook event found for %s", game.label)
            continue
        params = {
            "apiKey": api_key,
            "markets": ",".join(MARKETS),
            "oddsFormat": "american",
            "dateFormat": "iso",
        }
        # The API accepts either regions or a bookmaker list; bookmakers lets you cap cost.
        if bookmakers:
            params["bookmakers"] = bookmakers
        else:
            params["regions"] = regions
        try:
            resp = client.get(f"{BASE}/events/{event['id']}/odds", params, ttl=TTL_ODDS)
        except HttpError as exc:
            log.warning("Odds API request failed for %s: %s", game.label, exc)
            if "401" in str(exc) or "429" in str(exc):
                book.error = str(exc)
                break
            continue
        parse_event_odds(resp.json(), book)
        if resp.from_cache:
            continue
        if "x-requests-remaining" in resp.headers:
            book.credits_remaining = resp.headers["x-requests-remaining"]
        with contextlib.suppress(ValueError):
            book.credits_used_this_run += int(resp.headers.get("x-requests-last", 0))
    return book

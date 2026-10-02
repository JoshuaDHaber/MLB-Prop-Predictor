"""Shared math: regression to the mean, the log5 matchup formula, odds conversions."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from mlb_prop_predictor.domain import PropPrice


@dataclass(frozen=True)
class LeagueRates:
    """League-wide per-PA rates for the season, computed from team totals at run time."""

    hr_per_pa: float = 0.030
    k_per_pa: float = 0.222
    barrels_per_pa: float = 0.060

    @property
    def hr_per_barrel(self) -> float:
        return self.hr_per_pa / self.barrels_per_pa if self.barrels_per_pa > 0 else 0.5


def regress(successes: float, trials: float, prior_rate: float, prior_n: float) -> float:
    """Empirical-Bayes shrinkage: add ``prior_n`` trials at ``prior_rate`` to the observed record.

    ``prior_n`` is roughly the sample size at which the stat is half signal, half noise.
    """
    return (successes + prior_rate * prior_n) / (trials + prior_n)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def odds_ratio(p: float) -> float:
    p = clamp(p, 1e-9, 1 - 1e-9)
    return p / (1 - p)


def from_odds_ratio(o: float) -> float:
    return o / (1 + o)


def log5(batter: float, pitcher: float, league: float) -> float:
    """Bill James' log5 / odds-ratio method for a batter-vs-pitcher event probability.

    Combines the batter's rate and the pitcher's rate relative to league average:
    odds(matchup) = odds(batter) * odds(pitcher) / odds(league).
    """
    return from_odds_ratio(odds_ratio(batter) * odds_ratio(pitcher) / odds_ratio(league))


def scale_probability(p: float, factor: float) -> float:
    """Apply a multiplicative environment factor (park, weather) on the odds scale so p stays in (0, 1)."""
    return from_odds_ratio(odds_ratio(p) * factor)


def american_to_prob(price: int) -> float:
    return 100 / (price + 100) if price > 0 else -price / (-price + 100)


def american_to_decimal(price: int) -> float:
    return 1 + (price / 100 if price > 0 else 100 / -price)


def prob_to_american(p: float) -> int:
    p = clamp(p, 1e-6, 1 - 1e-6)
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def expected_value(p: float, price: int) -> float:
    """Expected profit per 1 unit staked at American ``price`` if the true probability is ``p``."""
    return p * (american_to_decimal(price) - 1) - (1 - p)


def poisson_binomial(probs: Sequence[float]) -> list[float]:
    """Distribution of the number of successes in independent trials with different probabilities."""
    dist = [1.0]
    for p in probs:
        nxt = [0.0] * (len(dist) + 1)
        for k, mass in enumerate(dist):
            nxt[k] += mass * (1 - p)
            nxt[k + 1] += mass * p
        dist = nxt
    return dist


def normal_weights(center: float, sd: float, values: Sequence[int]) -> list[float]:
    raw = [math.exp(-0.5 * ((v - center) / sd) ** 2) for v in values]
    total = sum(raw)
    return [r / total for r in raw]


@dataclass(frozen=True)
class MarketView:
    """Consensus market probability and best available price for one side of a prop."""

    fair_prob: float | None  # vig-removed where both sides are quoted, else raw implied
    devigged: bool
    best_price: int | None
    best_book: str | None
    books: int


def market_view(prices: list[PropPrice], side: str, point: float | None) -> MarketView:
    """Summarize one side (e.g. 'Over' 0.5) across books, removing vig where the book quotes both sides."""
    other = {"Over": "Under", "Under": "Over", "Yes": "No", "No": "Yes"}.get(side)
    by_book: dict[str, dict[str, int]] = {}
    for pp in prices:
        if point is not None and pp.point is not None and abs(pp.point - point) > 1e-9:
            continue
        by_book.setdefault(pp.bookmaker, {})[pp.side] = pp.price

    fair, raw = [], []
    best_price, best_book = None, None
    for book, sides in by_book.items():
        if side not in sides:
            continue
        price = sides[side]
        if best_price is None or american_to_decimal(price) > american_to_decimal(best_price):
            best_price, best_book = price, book
        p_side = american_to_prob(price)
        raw.append(p_side)
        if other in sides:
            p_other = american_to_prob(sides[other])
            fair.append(p_side / (p_side + p_other))

    if fair:
        return MarketView(sum(fair) / len(fair), True, best_price, best_book, len(raw))
    if raw:
        return MarketView(sum(raw) / len(raw), False, best_price, best_book, len(raw))
    return MarketView(None, False, None, None, 0)

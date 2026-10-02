"""End-to-end run: fetch the slate and inputs, project every matchup, attach market prices."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from mlb_prop_predictor.config import Settings
from mlb_prop_predictor.domain import Counts, Game, Player, StatcastLine, TeamSide, Weather
from mlb_prop_predictor.http import HttpClient
from mlb_prop_predictor.models.common import (
    LeagueRates,
    MarketView,
    expected_value,
    market_view,
    prob_to_american,
)
from mlb_prop_predictor.models.home_runs import project_home_run
from mlb_prop_predictor.models.park import ParkFactor, compute_park_factors, roof_closed, temperature_factor
from mlb_prop_predictor.models.strikeouts import project_strikeouts
from mlb_prop_predictor.sources import mlb_stats, savant, weather
from mlb_prop_predictor.sources.odds import HR_MARKET, K_MARKET, OddsBook, get_prop_odds

log = logging.getLogger(__name__)


@dataclass
class GameContext:
    label: str
    start_time: str
    venue: str
    park_hr: float
    park_k: float
    temp_f: float | None
    roof_closed: bool
    hr_env: float
    away_lineup: str
    home_lineup: str


@dataclass
class HrRow:
    game: str
    team: str
    opponent: str
    player: str
    player_id: int
    bats: str
    slot: int | None
    lineup_status: str
    pitcher: str
    pitcher_hand: str
    prob: float
    fair_odds: int
    expected_pa: float
    batter_hr_rate: float
    pitcher_hr_rate: float
    park_hr: float
    hr_env: float
    best_price: int | None = None
    best_book: str | None = None
    market_prob: float | None = None
    market_devigged: bool = False
    edge: float | None = None
    ev: float | None = None


@dataclass
class KRow:
    game: str
    team: str
    opponent: str
    pitcher: str
    pitcher_id: int
    throws: str
    opp_lineup_status: str
    expected_bf: float
    expected_k: float
    k_rate: float
    line: float
    p_over: float
    p_under: float
    line_source: str  # "market" or "model"
    over_price: int | None = None
    over_book: str | None = None
    under_price: int | None = None
    under_book: str | None = None
    market_over_prob: float | None = None
    pick: str | None = None  # "Over" / "Under" side with the better expected value
    edge: float | None = None
    ev: float | None = None


@dataclass
class Report:
    date: str
    season: int
    generated_at: str
    games: list[GameContext] = field(default_factory=list)
    home_runs: list[HrRow] = field(default_factory=list)
    strikeouts: list[KRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    odds_credits_used: int = 0
    odds_credits_remaining: str | None = None


# -- input assembly ----------------------------------------------------------


def _league_rates(
    team_counts: dict[int, dict[str, Counts]], batter_sc: dict[int, StatcastLine]
) -> LeagueRates:
    total = Counts()
    for c in team_counts.values():
        for key in ("bat_h", "bat_a"):
            if key in c:
                total = total + c[key]
    defaults = LeagueRates()
    if total.opp < 2000:  # too early in the season to trust: keep long-run defaults
        hr, k = defaults.hr_per_pa, defaults.k_per_pa
    else:
        hr, k = total.hr / total.opp, total.k / total.opp

    barrels = sum(s.barrels for s in batter_sc.values())
    implied_pa = sum(s.barrels / s.barrels_per_pa for s in batter_sc.values() if s.barrels_per_pa > 0)
    brl = barrels / implied_pa if barrels >= 200 and implied_pa > 0 else defaults.barrels_per_pa
    return LeagueRates(hr_per_pa=hr, k_per_pa=k, barrels_per_pa=brl)


def _team_total(team_counts: dict[int, dict[str, Counts]], team_id: int, prefix: str) -> Counts | None:
    c = team_counts.get(team_id)
    if not c or f"{prefix}_h" not in c or f"{prefix}_a" not in c:
        return None
    return c[f"{prefix}_h"] + c[f"{prefix}_a"]


def _park_factors(client: HttpClient, season: int, n_seasons: int) -> tuple[dict[int, ParkFactor], dict]:
    seasons = []
    current_counts: dict[int, dict[str, Counts]] = {}
    for s in range(season - n_seasons + 1, season + 1):
        venues = mlb_stats.get_team_venues(client, s, season)
        counts = mlb_stats.get_team_home_away(client, s, season, venues.keys())
        seasons.append((counts, venues))
        if s == season:
            current_counts = counts
    current_venues = seasons[-1][1] if seasons else {}
    return compute_park_factors(seasons, current_venues), current_counts


# -- market attachment -------------------------------------------------------


def _attach_hr_market(row: HrRow, odds: OddsBook | None) -> None:
    if odds is None:
        return
    view: MarketView = market_view(odds.lookup(HR_MARKET, row.player), "Over", 0.5)
    if view.best_price is None:
        return
    row.best_price, row.best_book = view.best_price, view.best_book
    row.market_prob, row.market_devigged = view.fair_prob, view.devigged
    if view.fair_prob is not None:
        row.edge = row.prob - view.fair_prob
    row.ev = expected_value(row.prob, view.best_price)


def _main_line(points: list[float]) -> float | None:
    if not points:
        return None
    return max(set(points), key=lambda p: (points.count(p), -abs(p - 5.5)))


def _nearest_half(x: float) -> float:
    return math.floor(x) + 0.5


def _attach_k_market(row: KRow, odds: OddsBook | None, proj) -> None:
    if odds is None:
        return
    prices = odds.lookup(K_MARKET, row.pitcher)
    line = _main_line([p.point for p in prices if p.point is not None])
    if line is None:
        return
    row.line, row.line_source = line, "market"
    row.p_over, row.p_under = proj.prob_over(line), proj.prob_under(line)
    over, under = market_view(prices, "Over", line), market_view(prices, "Under", line)
    row.over_price, row.over_book = over.best_price, over.best_book
    row.under_price, row.under_book = under.best_price, under.best_book
    row.market_over_prob = over.fair_prob
    candidates = []
    if over.best_price is not None:
        candidates.append(("Over", row.p_over, over))
    if under.best_price is not None:
        candidates.append(("Under", row.p_under, under))
    if candidates:
        side, p, view = max(candidates, key=lambda c: expected_value(c[1], c[2].best_price))
        row.pick = side
        row.ev = expected_value(p, view.best_price)
        row.edge = p - view.fair_prob if view.fair_prob is not None else None


# -- main ---------------------------------------------------------------------


def run(client: HttpClient, settings: Settings, odds_client: HttpClient | None = None) -> Report:
    report = Report(
        date=settings.date,
        season=settings.season,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    games = mlb_stats.get_schedule(client, settings.date)
    if not games:
        report.notes.append(f"No MLB games scheduled on {settings.date}.")
        return report
    mlb_stats.fill_projected_lineups(client, games, settings.date)

    hitter_ids = {pid for g in games for side in (g.away, g.home) for pid in side.lineup_ids}
    pitcher_ids = {
        side.probable_pitcher_id for g in games for side in (g.away, g.home) if side.probable_pitcher_id
    }
    players = mlb_stats.get_players(client, hitter_ids, pitcher_ids, settings.season)

    parks, team_counts = _park_factors(client, settings.season, settings.park_factor_seasons)
    batter_sc = savant.get_barrels(client, settings.season, "batter")
    pitcher_sc = savant.get_barrels(client, settings.season, "pitcher")
    lg = _league_rates(team_counts, batter_sc)
    if not batter_sc:
        report.notes.append("Statcast barrel data unavailable: HR model used results-based rates only.")

    odds: OddsBook | None = None
    if settings.use_odds and settings.odds_api_key:
        odds = get_prop_odds(
            odds_client or client,
            settings.odds_api_key,
            settings.date,
            games,
            settings.odds_regions,
            settings.odds_bookmakers,
        )
        report.odds_credits_used = odds.credits_used_this_run
        report.odds_credits_remaining = odds.credits_remaining
        if odds.error:
            report.notes.append(f"Odds API: {odds.error}")
    elif settings.use_odds:
        report.notes.append("No ODDS_API_KEY set: showing model projections without sportsbook prices.")

    for game in games:
        _project_game(game, players, parks, team_counts, batter_sc, pitcher_sc, lg, odds, client, report)

    report.home_runs.sort(key=lambda r: r.prob, reverse=True)
    report.strikeouts.sort(key=lambda r: r.expected_k, reverse=True)
    return report


def _project_game(
    game: Game,
    players: dict[int, Player],
    parks: dict[int, ParkFactor],
    team_counts: dict[int, dict[str, Counts]],
    batter_sc: dict[int, StatcastLine],
    pitcher_sc: dict[int, StatcastLine],
    lg: LeagueRates,
    odds: OddsBook | None,
    client: HttpClient,
    report: Report,
) -> None:
    park = parks.get(game.venue.id, ParkFactor())
    wx = Weather(None, None, None)
    if game.venue.latitude is not None and game.venue.longitude is not None:
        wx = weather.get_game_weather(client, game.venue.latitude, game.venue.longitude, game.start_time)
    temp_adj = temperature_factor(game.venue, wx)
    hr_env = park.hr * temp_adj

    report.games.append(
        GameContext(
            label=game.label,
            start_time=game.start_time.isoformat(),
            venue=game.venue.name,
            park_hr=round(park.hr, 3),
            park_k=round(park.k, 3),
            temp_f=wx.temp_f,
            roof_closed=roof_closed(game.venue, wx),
            hr_env=round(hr_env, 3),
            away_lineup=game.away.lineup_status,
            home_lineup=game.home.lineup_status,
        )
    )

    for batting, pitching in ((game.away, game.home), (game.home, game.away)):
        starter = players.get(pitching.probable_pitcher_id) if pitching.probable_pitcher_id else None
        _hr_rows(
            game,
            batting,
            pitching,
            starter,
            players,
            batter_sc,
            pitcher_sc,
            team_counts,
            hr_env,
            park,
            lg,
            odds,
            report,
        )
        if starter is not None:
            _k_row(game, batting, pitching, starter, players, team_counts, park, lg, odds, report)


def _hr_rows(
    game,
    batting: TeamSide,
    pitching: TeamSide,
    starter,
    players,
    batter_sc,
    pitcher_sc,
    team_counts,
    hr_env,
    park,
    lg,
    odds,
    report,
) -> None:
    opp_pitching = _team_total(team_counts, pitching.team_id, "pit")
    starter_sc = pitcher_sc.get(starter.id) if starter else None
    for slot, pid in enumerate(batting.lineup_ids[:9], start=1):
        batter = players.get(pid)
        if batter is None:
            continue
        prob, inputs = project_home_run(
            batter, batter_sc.get(pid), starter, starter_sc, opp_pitching, slot, hr_env, lg
        )
        row = HrRow(
            game=game.label,
            team=batting.short_name,
            opponent=pitching.short_name,
            player=batter.name,
            player_id=pid,
            bats=batter.bat_side or "?",
            slot=slot,
            lineup_status=batting.lineup_status,
            pitcher=starter.name if starter else "TBD",
            pitcher_hand=(starter.pitch_hand or "?") if starter else "?",
            prob=prob,
            fair_odds=prob_to_american(prob),
            expected_pa=inputs.pa_total,
            batter_hr_rate=inputs.batter_hr_rate,
            pitcher_hr_rate=inputs.pitcher_hr_rate,
            park_hr=park.hr,
            hr_env=hr_env,
        )
        _attach_hr_market(row, odds)
        report.home_runs.append(row)


def _k_row(
    game, batting: TeamSide, pitching: TeamSide, starter: Player, players, team_counts, park, lg, odds, report
) -> None:
    lineup = [players[pid] for pid in batting.lineup_ids[:9] if pid in players]
    proj = project_strikeouts(starter, lineup, _team_total(team_counts, batting.team_id, "bat"), park.k, lg)
    line = _nearest_half(proj.expected_k)
    row = KRow(
        game=game.label,
        team=pitching.short_name,
        opponent=batting.short_name,
        pitcher=starter.name,
        pitcher_id=starter.id,
        throws=starter.pitch_hand or "?",
        opp_lineup_status=batting.lineup_status if lineup else "team average",
        expected_bf=proj.expected_bf,
        expected_k=proj.expected_k,
        k_rate=proj.pitcher_k_rate,
        line=line,
        p_over=proj.prob_over(line),
        p_under=proj.prob_under(line),
        line_source="model",
    )
    _attach_k_market(row, odds, proj)
    report.strikeouts.append(row)

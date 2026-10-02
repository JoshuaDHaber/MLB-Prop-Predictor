"""Walk-forward backtest of the HR and strikeout models over a past season.

For each date: project every game using season-to-date stats from *before* that date,
then fold the day's results into the stats. Inputs that come from outside the season
(park factors, barrel rates) use prior seasons only. The projection code is the same
code the daily report runs.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from mlb_prop_predictor.backtest.data import (
    HOME_RUN_EVENTS,
    STRIKEOUT_EVENTS,
    HistGame,
    PlayByPlayStore,
    get_season_games,
)
from mlb_prop_predictor.backtest.metrics import (
    auc,
    brier,
    calibration_bins,
    calibration_slope,
    expected_calibration_error,
    log_loss,
    mae,
    rmse,
    skill,
)
from mlb_prop_predictor.backtest.state import SeasonState
from mlb_prop_predictor.domain import StatcastLine, Venue, Weather
from mlb_prop_predictor.http import HttpClient
from mlb_prop_predictor.models.common import LeagueRates, clamp
from mlb_prop_predictor.models.home_runs import project_home_run
from mlb_prop_predictor.models.matchup import LEAGUE_BF_PER_START, expected_starter_bf, slot_pa
from mlb_prop_predictor.models.park import ParkFactor, compute_park_factors, temperature_factor
from mlb_prop_predictor.models.strikeouts import project_strikeouts
from mlb_prop_predictor.sources import mlb_stats, savant

log = logging.getLogger(__name__)

K_LINES = (3.5, 4.5, 5.5, 6.5, 7.5, 8.5)
MIN_LEAGUE_PA = 2000


@dataclass
class HrPrediction:
    date: str
    game_pk: int
    batter: int
    slot: int
    p_model: float
    p_raw_rate: float
    p_league: float
    homered: int


@dataclass
class KPrediction:
    date: str
    game_pk: int
    pitcher: int
    expected_k: float
    raw_rate_k: float
    league_k: float
    actual_k: int
    p10: int
    p90: int
    p_over: dict[str, float] = field(default_factory=dict)


@dataclass
class BacktestResult:
    season: int
    games: int
    hr: list[HrPrediction]
    k: list[KPrediction]
    park_seasons: list[int]
    barrel_season: int | None


# -- helpers --------------------------------------------------------------------


def _league_rates(state: SeasonState, barrels_per_pa: float | None) -> LeagueRates:
    d = LeagueRates()
    if state.league.opp >= MIN_LEAGUE_PA:
        hr, k = state.league.hr / state.league.opp, state.league.k / state.league.opp
    else:
        hr, k = d.hr_per_pa, d.k_per_pa
    return LeagueRates(hr_per_pa=hr, k_per_pa=k, barrels_per_pa=barrels_per_pa or d.barrels_per_pa)


def _league_barrel_rate(sc: dict[int, StatcastLine]) -> float | None:
    barrels = sum(s.barrels for s in sc.values())
    pa = sum(s.barrels / s.barrels_per_pa for s in sc.values() if s.barrels_per_pa > 0)
    return barrels / pa if barrels >= 200 and pa > 0 else None


def _percentile(dist: list[float], q: float) -> int:
    acc = 0.0
    for n, p in enumerate(dist):
        acc += p
        if acc >= q:
            return n
    return len(dist) - 1


def _env(game: HistGame, parks: dict[int, ParkFactor]) -> tuple[ParkFactor, float]:
    park = parks.get(game.venue_id, ParkFactor())
    roof = "Dome" if game.roof_closed_reported else game.roof_type
    temp = temperature_factor(Venue(game.venue_id, "", roof_type=roof), Weather(game.temp_f, None, None))
    return park, park.hr * temp


def _park_factors(client: HttpClient, season: int, n: int) -> tuple[dict[int, ParkFactor], list[int]]:
    prior = list(range(season - n, season))
    data = []
    for s in prior:
        venues = mlb_stats.get_team_venues(client, s, season)
        data.append((mlb_stats.get_team_home_away(client, s, season, venues.keys()), venues))
    current_venues = mlb_stats.get_team_venues(client, season, season + 1)
    return compute_park_factors(data, current_venues), prior


# -- main loop ---------------------------------------------------------------------


def project_day(
    games: list[HistGame],
    state: SeasonState,
    parks: dict[int, ParkFactor],
    batter_sc: dict[int, StatcastLine],
    pitcher_sc: dict[int, StatcastLine],
    lg: LeagueRates,
) -> tuple[list[HrPrediction], list[KPrediction]]:
    hr_out: list[HrPrediction] = []
    k_out: list[KPrediction] = []
    for game in games:
        park, hr_env = _env(game, parks)
        team = {"away": game.away_id, "home": game.home_id}
        for batting, fielding in (("away", "home"), ("home", "away")):
            starter_id = game.starter(fielding)
            starter = state.player(starter_id) if starter_id else None
            lineup_ids = game.lineup(batting)
            homered = {pa.batter for pa in game.pas if pa.batting == batting and pa.event in HOME_RUN_EVENTS}

            for slot, pid in enumerate(lineup_ids, start=1):
                batter = state.player(pid)
                p_model, _ = project_home_run(
                    batter,
                    batter_sc.get(pid),
                    starter,
                    pitcher_sc.get(starter_id) if starter_id else None,
                    state.team_pit[team[fielding]],
                    slot,
                    hr_env,
                    lg,
                )
                pa = slot_pa(slot)
                raw = batter.hitting.hr / batter.hitting.opp if batter.hitting.opp else lg.hr_per_pa
                hr_out.append(
                    HrPrediction(
                        date=game.date,
                        game_pk=game.game_pk,
                        batter=pid,
                        slot=slot,
                        p_model=p_model,
                        p_raw_rate=1 - (1 - clamp(raw, 0.0, 0.2)) ** pa,
                        p_league=1 - (1 - lg.hr_per_pa) ** pa,
                        homered=int(pid in homered),
                    )
                )

            if starter is None or len(lineup_ids) < 9:
                continue
            proj = project_strikeouts(
                starter, [state.player(pid) for pid in lineup_ids], state.team_bat[team[batting]], park.k, lg
            )
            actual = sum(1 for pa in game.pas if pa.pitcher == starter_id and pa.event in STRIKEOUT_EVENTS)
            raw_k = starter.pitching.k / starter.pitching.opp if starter.pitching.opp else lg.k_per_pa
            k_out.append(
                KPrediction(
                    date=game.date,
                    game_pk=game.game_pk,
                    pitcher=starter_id,
                    expected_k=proj.expected_k,
                    raw_rate_k=raw_k * expected_starter_bf(starter),
                    league_k=lg.k_per_pa * LEAGUE_BF_PER_START,
                    actual_k=actual,
                    p10=_percentile(proj.distribution, 0.10),
                    p90=_percentile(proj.distribution, 0.90),
                    p_over={f"{line:g}": proj.prob_over(line) for line in K_LINES},
                )
            )
    return hr_out, k_out


def run_backtest(
    client: HttpClient,
    season: int,
    pbp_cache: Path | None,
    start: date | None = None,
    end: date | None = None,
    park_seasons: int = 3,
    use_barrels: bool = True,
    workers: int = 4,
    progress=None,
) -> BacktestResult:
    say = progress or (lambda msg: None)
    say(f"[{season}] loading schedule ...")
    games = get_season_games(client, season, start, end)
    say(f"[{season}] {len(games)} completed games; loading play-by-play (cached after first run) ...")
    store = PlayByPlayStore(client, pbp_cache)
    games = store.attach(games, workers=workers, progress=lambda i, n: say(f"[{season}]   {i}/{n} games"))

    say(f"[{season}] park factors from prior seasons, prior-season barrel rates ...")
    parks, park_years = _park_factors(client, season, park_seasons)
    batter_sc = savant.get_barrels(client, season - 1, "batter") if use_barrels else {}
    pitcher_sc = savant.get_barrels(client, season - 1, "pitcher") if use_barrels else {}
    brl = _league_barrel_rate(batter_sc)

    state = SeasonState()
    for g in games:
        state.learn_handedness(g)

    by_date: dict[str, list[HistGame]] = defaultdict(list)
    for g in games:
        by_date[g.date].append(g)

    hr_all: list[HrPrediction] = []
    k_all: list[KPrediction] = []
    say(f"[{season}] replaying {len(by_date)} game days ...")
    for day in sorted(by_date):
        lg = _league_rates(state, brl)
        hr, k = project_day(by_date[day], state, parks, batter_sc, pitcher_sc, lg)
        hr_all += hr
        k_all += k
        for g in by_date[day]:  # results become known only after the day's projections
            state.update(g)

    return BacktestResult(
        season=season,
        games=len(games),
        hr=hr_all,
        k=k_all,
        park_seasons=park_years,
        barrel_season=season - 1 if batter_sc else None,
    )


# -- scoring ----------------------------------------------------------------------


def _hr_metrics(rows: list[HrPrediction]) -> dict:
    y = [r.homered for r in rows]
    out = {"batter_games": len(rows), "observed_rate": sum(y) / len(y)}
    models = {
        "model": [r.p_model for r in rows],
        "raw_rate": [r.p_raw_rate for r in rows],
        "league": [r.p_league for r in rows],
    }
    ref = brier(models["league"], y)
    for name, preds in models.items():
        b = brier(preds, y)
        out[name] = {
            "mean_pred": sum(preds) / len(preds),
            "brier": b,
            "log_loss": log_loss(preds, y),
            "brier_skill_vs_league": skill(b, ref),
            "auc": auc(preds, y),
        }
    cal = calibration_bins(models["model"], y)
    a, b = calibration_slope(models["model"], y)
    out["calibration"] = {
        "ece": expected_calibration_error(cal),
        "intercept": a,
        "slope": b,
        "bins": [asdict(c) for c in cal],
    }
    top = sorted(rows, key=lambda r: r.p_model, reverse=True)[: max(1, len(rows) // 10)]
    out["top_decile"] = {
        "mean_pred": sum(r.p_model for r in top) / len(top),
        "observed": sum(r.homered for r in top) / len(top),
    }
    return out


def _k_metrics(rows: list[KPrediction]) -> dict:
    actual = [r.actual_k for r in rows]
    out = {"starts": len(rows), "mean_actual": sum(actual) / len(actual)}
    for name, preds in (
        ("model", [r.expected_k for r in rows]),
        ("raw_rate", [r.raw_rate_k for r in rows]),
        ("league", [r.league_k for r in rows]),
    ):
        out[name] = {
            "mean_pred": sum(preds) / len(preds),
            "mae": mae(preds, actual),
            "rmse": rmse(preds, actual),
            "bias": sum(p - a for p, a in zip(preds, actual)) / len(preds),
        }
    out["model"]["mae_skill_vs_league"] = skill(out["model"]["mae"], out["league"]["mae"])
    preds, ys = [], []
    for r in rows:
        for line, p in r.p_over.items():
            preds.append(p)
            ys.append(int(r.actual_k > float(line)))
    cal = calibration_bins(preds, ys)
    a, b = calibration_slope(preds, ys)
    out["over_under"] = {
        "lines": list(K_LINES),
        "brier": brier(preds, ys),
        "ece": expected_calibration_error(cal),
        "intercept": a,
        "slope": b,
        "bins": [asdict(c) for c in cal],
    }
    out["interval_80_coverage"] = sum(1 for r in rows if r.p10 <= r.actual_k <= r.p90) / len(rows)
    return out


def summarize(results: list[BacktestResult], warmup_until: str | None = None) -> dict:
    hr = [r for res in results for r in res.hr if not warmup_until or r.date >= warmup_until]
    k = [r for res in results for r in res.k if not warmup_until or r.date >= warmup_until]
    if not hr or not k:
        raise ValueError("backtest produced no predictions; check the season/date range")
    return {
        "seasons": [r.season for r in results],
        "games": sum(r.games for r in results) if not warmup_until else len({x.game_pk for x in hr}),
        "from_date": warmup_until,
        "park_factor_seasons": {r.season: r.park_seasons for r in results},
        "barrel_season": {r.season: r.barrel_season for r in results},
        "home_runs": _hr_metrics(hr),
        "strikeouts": _k_metrics(k),
    }


# -- report -----------------------------------------------------------------------


def _f(x: float, d: int = 3) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{d}f}"


def _p(x: float, d: int = 1) -> str:
    return f"{x * 100:.{d}f}%"


def headline(s: dict) -> str:
    hr, k = s["home_runs"], s["strikeouts"]
    seasons = ", ".join(str(x) for x in s["seasons"])
    return (
        f"Walk-forward backtest over {s['games']:,} games ({seasons}): HR probabilities were calibrated to "
        f"within {_p(hr['calibration']['ece'], 2)} on average (calibration slope {_f(hr['calibration']['slope'], 2)}), "
        f"with a Brier skill of {_p(hr['model']['brier_skill_vs_league'])} vs a league-average baseline and "
        f"AUC {_f(hr['model']['auc'], 3)}. Strikeout projections had MAE {_f(k['model']['mae'], 2)} K per start "
        f"({_p(k['model']['mae_skill_vs_league'])} better than league average), and "
        f"{_p(k['interval_80_coverage'], 0)} of starts landed inside the model's 80% range."
    )


def to_markdown(s: dict) -> str:
    hr, k = s["home_runs"], s["strikeouts"]
    lines = [
        f"## Backtest results: {', '.join(str(x) for x in s['seasons'])}"
        + (f" (games from {s['from_date']})" if s.get("from_date") else ""),
        "",
        headline(s),
        "",
        f"Walk-forward: each game was projected using only stats from before that date. Park factors used "
        f"prior seasons only, and barrel rates the previous season's. "
        f"{s['games']:,} games, {hr['batter_games']:,} batter-games, {k['starts']:,} starts.",
        "",
        "### Home runs: P(1+ HR) for every starting hitter",
        "",
        "| Model | Mean pred. | Brier | Log loss | Brier skill vs league | AUC |",
        "|---|---|---|---|---|---|",
    ]
    names = {"model": "**This model**", "raw_rate": "Batter's raw season HR rate", "league": "League average"}
    for key, label in names.items():
        m = hr[key]
        lines.append(
            f"| {label} | {_p(m['mean_pred'])} | {_f(m['brier'], 4)} | {_f(m['log_loss'], 4)} | "
            f"{_p(m['brier_skill_vs_league'])} | {_f(m['auc'], 3)} |"
        )
    cal = hr["calibration"]
    lines += [
        "",
        f"Observed HR rate: {_p(hr['observed_rate'])}. Calibration: ECE {_p(cal['ece'], 2)}, slope "
        f"{_f(cal['slope'], 2)}, intercept {_f(cal['intercept'], 2)} (perfect = 0%, 1.00, 0.00). "
        f"Top 10% of projections: predicted {_p(hr['top_decile']['mean_pred'])}, "
        f"observed {_p(hr['top_decile']['observed'])}.",
        "",
        "| Decile | n | Predicted | Observed | 95% CI |",
        "|---|---|---|---|---|",
    ]
    for i, b in enumerate(cal["bins"], start=1):
        lines.append(
            f"| {i} | {b['n']:,} | {_p(b['mean_pred'])} | {_p(b['observed'])} | "
            f"{_p(b['ci_low'])}–{_p(b['ci_high'])} |"
        )
    ou = k["over_under"]
    lines += [
        "",
        "### Pitcher strikeouts: starters",
        "",
        "| Model | Mean K | MAE | RMSE | Bias |",
        "|---|---|---|---|---|",
    ]
    for key, label in (
        ("model", "**This model**"),
        ("raw_rate", "Pitcher's raw K rate × workload"),
        ("league", "League average start"),
    ):
        m = k[key]
        lines.append(
            f"| {label} | {_f(m['mean_pred'], 2)} | {_f(m['mae'], 2)} | {_f(m['rmse'], 2)} | {_f(m['bias'], 2)} |"
        )
    lines += [
        "",
        f"Actual mean: {_f(k['mean_actual'], 2)} K. 80% prediction interval coverage: "
        f"{_p(k['interval_80_coverage'])} (target 80%). Over/under probabilities at lines "
        f"{', '.join(f'{x:g}' for x in ou['lines'])}: Brier {_f(ou['brier'], 4)}, ECE {_p(ou['ece'], 2)}, "
        f"slope {_f(ou['slope'], 2)}.",
        "",
        "| Decile | n | P(Over) predicted | Observed |",
        "|---|---|---|---|",
    ]
    for i, b in enumerate(ou["bins"], start=1):
        lines.append(f"| {i} | {b['n']:,} | {_p(b['mean_pred'])} | {_p(b['observed'])} |")
    return "\n".join(lines) + "\n"

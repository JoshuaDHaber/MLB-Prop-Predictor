import json
import random

import pytest

from mlb_prop_predictor.backtest import cli as backtest_cli
from mlb_prop_predictor.backtest.data import PA, HistGame, is_plate_appearance, parse_play_by_play
from mlb_prop_predictor.backtest.metrics import auc, brier, calibration_bins, calibration_slope, wilson
from mlb_prop_predictor.backtest.runner import run_backtest, summarize, to_markdown
from mlb_prop_predictor.backtest.state import SeasonState
from tests.sim_season import SimSeason

# -- parsing ----------------------------------------------------------------------


def test_non_plate_appearance_events_are_skipped():
    assert is_plate_appearance("home_run") and is_plate_appearance("walk")
    assert not is_plate_appearance("caught_stealing_2b")
    assert not is_plate_appearance("pickoff_1b")
    assert not is_plate_appearance("")


def test_parse_play_by_play_handles_dict_and_string_codes():
    payload = {
        "allPlays": [
            {
                "result": {"eventType": "strikeout"},
                "about": {"halfInning": "top", "isComplete": True},
                "matchup": {
                    "batter": {"id": 1},
                    "batSide": {"code": "L"},
                    "pitcher": {"id": 9},
                    "pitchHand": "R",
                },
            },
            {
                "result": {"eventType": "home_run"},
                "about": {"halfInning": "bottom", "isComplete": True},
                "matchup": {
                    "batter": {"id": 2},
                    "batSide": "R",
                    "pitcher": {"id": 8},
                    "pitchHand": {"code": "L"},
                },
            },
            {
                "result": {"eventType": "single"},
                "about": {"halfInning": "bottom", "isComplete": False},
                "matchup": {"batter": {"id": 3}, "pitcher": {"id": 8}},
            },
        ]
    }
    pas = parse_play_by_play(payload)
    assert [(p.batter, p.bat_side, p.pitch_hand, p.batting) for p in pas] == [
        (1, "L", "R", "away"),
        (2, "R", "L", "home"),
    ]


def _game(pas, lineups=None):
    return HistGame(
        1,
        "2025-05-01",
        "2025-05-01T23:05:00Z",
        10,
        20,
        "A",
        "H",
        99,
        "Open",
        70.0,
        False,
        lineups=lineups or {},
        pas=pas,
    )


def test_starters_and_lineup_fallback():
    pas = [PA(100 + i, "R", 7, "R", "away", "field_out") for i in range(10)]
    pas += [PA(200, "R", 8, "L", "home", "strikeout")]
    g = _game(pas)
    assert g.starter("home") == 7 and g.starter("away") == 8
    assert g.lineup("away") == list(range(100, 109))


# -- no lookahead ---------------------------------------------------------------------


def test_state_reflects_only_games_already_folded_in():
    state = SeasonState()
    g = _game([PA(1, "R", 7, "R", "away", "home_run"), PA(2, "L", 8, "R", "home", "strikeout")])
    state.learn_handedness(g)
    assert state.player(1).hitting.opp == 0  # before update: nothing known
    state.update(g)
    assert state.player(1).hitting.hr == 1
    assert state.player(7).pitching.opp == 1 and state.games_started[7] == 1
    assert state.player(2).hitting_vs["R"].k == 1


def test_switch_hitter_detected_from_both_sides():
    state = SeasonState()
    state.learn_handedness(
        _game([PA(1, "R", 7, "L", "away", "single"), PA(1, "L", 8, "R", "away", "single")])
    )
    assert state.bat_side(1) == "S"


# -- metrics ----------------------------------------------------------------------


def test_metric_basics():
    assert brier([0.0, 1.0], [0, 1]) == 0
    assert auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == 1.0
    assert auc([0.5, 0.5], [0, 1]) == 0.5
    lo, hi = wilson(10, 100)
    assert lo < 0.1 < hi


def test_calibration_slope_recovers_known_miscalibration():
    rng = random.Random(1)
    preds, ys = [], []
    for _ in range(20000):
        p = rng.uniform(0.02, 0.4)
        preds.append(p)
        ys.append(int(rng.random() < p))
    a, b = calibration_slope(preds, ys)
    assert abs(a) < 0.15 and abs(b - 1) < 0.1
    bins = calibration_bins(preds, ys)
    assert len(bins) == 10 and all(abs(x.mean_pred - x.observed) < 0.03 for x in bins)


# -- end to end on a simulated season --------------------------------------------------


@pytest.fixture(scope="module")
def sim_result():
    sim = SimSeason()
    return run_backtest(sim, 2025, pbp_cache=None, use_barrels=True)


def test_simulated_season_is_calibrated(sim_result):
    s = summarize([sim_result])
    hr, k = s["home_runs"], s["strikeouts"]
    assert s["games"] == 600 and hr["batter_games"] == 600 * 18 and k["starts"] == 1200
    # The model should beat both naive baselines and be close to calibrated.
    assert hr["model"]["brier"] < hr["raw_rate"]["brier"]
    assert hr["model"]["brier_skill_vs_league"] > 0
    assert hr["calibration"]["ece"] < 0.02
    assert 0.6 < hr["calibration"]["slope"] < 1.5
    assert k["model"]["mae"] < k["league"]["mae"]
    assert abs(k["model"]["bias"]) < 0.5
    assert 0.65 < k["interval_80_coverage"] < 0.95


def test_markdown_report_renders(sim_result):
    md = to_markdown(summarize([sim_result]))
    assert "Backtest results" in md and "This model" in md and "Decile" in md


def test_backtest_cli_writes_reports(tmp_path, monkeypatch):
    sim = SimSeason(days=40)
    monkeypatch.setattr(backtest_cli, "CachedHttpClient", lambda cache: sim)
    code = backtest_cli.main(
        [
            "--season",
            "2025",
            "--output-dir",
            str(tmp_path),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--warmup",
            "04-20",
        ]
    )
    assert code == 0
    data = json.loads((tmp_path / "backtest_2025.json").read_text())
    assert data["full_season"]["home_runs"]["batter_games"] > 0
    assert data["after_warmup"]["from_date"] == "2025-04-20"
    assert list((tmp_path / "cache" / "pbp" / "2025").glob("*.json"))  # compact play-by-play cache

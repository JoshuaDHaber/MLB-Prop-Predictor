import json

from mlb_prop_predictor.cli import main
from mlb_prop_predictor.config import Settings
from mlb_prop_predictor.demo import DEMO_DATE, DEMO_SEASON, DemoClient
from mlb_prop_predictor.pipeline import run


def demo_report(use_odds=True):
    settings = Settings(date=DEMO_DATE, season=DEMO_SEASON, odds_api_key="demo", use_odds=use_odds)
    return run(DemoClient(), settings)


def test_demo_slate_produces_projections():
    report = demo_report()
    assert len(report.games) == 2
    assert len(report.home_runs) == 36  # 4 lineups x 9 (one game uses projected lineups)
    assert len(report.strikeouts) == 4
    assert all(0 < r.prob < 0.6 for r in report.home_runs)
    assert report.home_runs == sorted(report.home_runs, key=lambda r: r.prob, reverse=True)
    assert {r.lineup_status for r in report.home_runs} == {"confirmed", "projected"}


def test_demo_slate_attaches_market_prices():
    report = demo_report()
    assert all(r.best_price is not None for r in report.home_runs)
    assert all(r.line_source == "market" and r.pick in ("Over", "Under") for r in report.strikeouts)


def test_hitter_park_factor_shows_up():
    report = demo_report()
    parks = {g.venue: g.park_hr for g in report.games}
    assert parks["Lighthouse Field"] > 1.05  # built with a 22% home HR boost


def test_no_odds_mode_uses_model_lines():
    report = demo_report(use_odds=False)
    assert all(r.best_price is None for r in report.home_runs)
    assert all(r.line_source == "model" for r in report.strikeouts)


def test_cli_demo_writes_reports(tmp_path, capsys):
    assert main(["--demo", "--output-dir", str(tmp_path), "--format", "html", "json", "csv"]) == 0
    out = capsys.readouterr().out
    assert "HOME RUNS" in out and "PITCHER STRIKEOUTS" in out
    data = json.loads((tmp_path / f"props_{DEMO_DATE}.json").read_text())
    assert data["home_runs"] and data["strikeouts"]
    assert (tmp_path / f"props_{DEMO_DATE}.html").read_text().startswith("<!doctype html>")
    assert (tmp_path / f"home_runs_{DEMO_DATE}.csv").exists()

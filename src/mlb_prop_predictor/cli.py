"""Command-line entry point: ``mlb-props`` or ``python -m mlb_prop_predictor``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import webbrowser
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mlb_prop_predictor import __version__
from mlb_prop_predictor.config import Settings, default_cache_dir, load_dotenv
from mlb_prop_predictor.http import CachedHttpClient, HttpError
from mlb_prop_predictor.pipeline import run
from mlb_prop_predictor.report import render_console, write_csv, write_html, write_json


def _today_eastern() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mlb-props",
        description="Project MLB home run and pitcher strikeout props from free public data.",
    )
    p.add_argument("--date", default=None, help="Slate date YYYY-MM-DD (default: today, US Eastern)")
    p.add_argument("--season", type=int, default=None, help="Season for stats (default: year of --date)")
    p.add_argument("--output-dir", type=Path, default=Path("reports"), help="Where reports are written")
    p.add_argument(
        "--format",
        nargs="+",
        choices=["html", "json", "csv"],
        default=["html", "json"],
        help="Report formats to write (default: html json)",
    )
    p.add_argument("--top", type=int, default=15, help="Rows per table in console output")
    p.add_argument("--no-odds", action="store_true", help="Skip The Odds API (spends no credits)")
    p.add_argument(
        "--bookmakers",
        default=os.environ.get("ODDS_BOOKMAKERS"),
        help="Comma-separated bookmaker keys to request instead of a whole region",
    )
    p.add_argument("--park-seasons", type=int, default=3, help="Seasons pooled for park factors (default 3)")
    p.add_argument("--cache-dir", type=Path, default=None, help="HTTP cache directory")
    p.add_argument("--no-cache", action="store_true", help="Disable the HTTP cache")
    p.add_argument("--demo", action="store_true", help="Run on a bundled fictional slate (no network needed)")
    p.add_argument("--open", action="store_true", help="Open the HTML report when finished")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.demo:
        from mlb_prop_predictor.demo import DEMO_DATE, DEMO_SEASON, DemoClient

        client = DemoClient()
        day, season, api_key = DEMO_DATE, DEMO_SEASON, "demo"
    else:
        day = args.date or _today_eastern()
        try:
            date.fromisoformat(day)
        except ValueError:
            print(f"error: --date must be YYYY-MM-DD, got {day!r}", file=sys.stderr)
            return 2
        season = args.season or int(day[:4])
        api_key = os.environ.get("ODDS_API_KEY")
        cache = None if args.no_cache else (args.cache_dir or default_cache_dir())
        client = CachedHttpClient(cache)

    settings = Settings(
        date=day,
        season=season,
        odds_api_key=api_key,
        use_odds=not args.no_odds,
        odds_bookmakers=args.bookmakers,
        park_factor_seasons=max(1, args.park_seasons),
    )

    try:
        report = run(client, settings)
    except HttpError as exc:
        print(f"error: a required data source failed: {exc}", file=sys.stderr)
        return 1

    print(render_console(report, top=args.top))

    if not report.games:
        return 0
    args.output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if "json" in args.format:
        written.append(write_json(report, args.output_dir / f"props_{day}.json"))
    if "csv" in args.format:
        written += write_csv(report, args.output_dir)
    html_path = None
    if "html" in args.format:
        html_path = write_html(report, args.output_dir / f"props_{day}.html")
        written.append(html_path)
    print("\nWrote: " + ", ".join(str(p) for p in written))
    if args.open and html_path is not None:
        webbrowser.open(html_path.resolve().as_uri())
    return 0

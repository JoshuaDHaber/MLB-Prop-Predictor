"""``mlb-backtest``: replay past seasons and score the models.

mlb-backtest --season 2025
mlb-backtest --season 2024 2025 --warmup 05-01
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

from mlb_prop_predictor.backtest.runner import headline, run_backtest, summarize, to_markdown
from mlb_prop_predictor.config import default_cache_dir
from mlb_prop_predictor.http import CachedHttpClient, HttpError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mlb-backtest", description="Walk-forward backtest of the HR and K models."
    )
    p.add_argument("--season", type=int, nargs="+", required=True, help="Season(s) to replay, e.g. 2024 2025")
    p.add_argument(
        "--start", type=date.fromisoformat, default=None, help="First date (YYYY-MM-DD), one season only"
    )
    p.add_argument(
        "--end", type=date.fromisoformat, default=None, help="Last date (YYYY-MM-DD), one season only"
    )
    p.add_argument(
        "--warmup",
        default=None,
        metavar="MM-DD",
        help="Also report results from this date onward each season (e.g. 05-01)",
    )
    p.add_argument("--park-seasons", type=int, default=3, help="Prior seasons used for park factors")
    p.add_argument("--no-barrels", action="store_true", help="Skip prior-season Statcast barrel rates")
    p.add_argument("--workers", type=int, default=4, help="Parallel downloads for play-by-play")
    p.add_argument("--output-dir", type=Path, default=Path("reports"))
    p.add_argument("--cache-dir", type=Path, default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    if len(args.season) > 1 and (args.start or args.end):
        print("error: --start/--end only work with a single --season", file=sys.stderr)
        return 2

    cache = args.cache_dir or default_cache_dir()
    client = CachedHttpClient(cache)
    t0 = time.time()

    def say(msg: str) -> None:
        print(f"{time.time() - t0:6.0f}s  {msg}", flush=True)

    results = []
    try:
        for season in args.season:
            results.append(
                run_backtest(
                    client,
                    season,
                    pbp_cache=cache / "pbp" / str(season),
                    start=args.start,
                    end=args.end,
                    park_seasons=max(1, args.park_seasons),
                    use_barrels=not args.no_barrels,
                    workers=args.workers,
                    progress=say,
                )
            )
    except HttpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    summary = summarize(results)
    tag = "-".join(str(s) for s in args.season)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    md = to_markdown(summary)
    payload = {"full_season": summary}
    if args.warmup and len(args.season) == 1:
        warm = summarize(results, f"{args.season[0]}-{args.warmup}")
        md += "\n" + to_markdown(warm)
        payload["after_warmup"] = warm
    (args.output_dir / f"backtest_{tag}.md").write_text(md, encoding="utf-8")
    (args.output_dir / f"backtest_{tag}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("\n" + headline(summary))
    print(f"\nFull results: {args.output_dir / f'backtest_{tag}.md'} (and .json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

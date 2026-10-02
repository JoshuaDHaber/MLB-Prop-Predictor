# MLB Prop Predictor

[![CI](https://github.com/JoshuaDHaber/MLB-Prop-Predictor/actions/workflows/ci.yml/badge.svg)](https://github.com/JoshuaDHaber/MLB-Prop-Predictor/actions/workflows/ci.yml)

Daily **home run** and **pitcher strikeout** projections for every MLB game. They're built from
free public data and compared against sportsbook lines to surface potential edges.

The models are written from scratch. They regress small samples toward the mean, combine
batter and pitcher with the log5 method, compute park factors from home/road splits, adjust for
game-time temperature, and model each starter's full strikeout distribution. See
[docs/METHODOLOGY.md](docs/METHODOLOGY.md) for the details.

## Try it in 10 seconds (no API keys, no network)

```bash
git clone https://github.com/JoshuaDHaber/MLB-Prop-Predictor.git
cd MLB-Prop-Predictor
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
mlb-props --demo --open
```

`--demo` runs the whole pipeline on a bundled slate of **fictional** teams and players.

## Run it for real

```bash
cp .env.example .env        # then paste your free Odds API key into .env
mlb-props --open            # today's slate (US Eastern date)
mlb-props --date 2026-07-04 --format html json csv
mlb-props --no-odds         # projections only; spends no Odds API credits
```

Reports are written to `reports/`: a sortable HTML page, plus JSON and/or CSV. A summary also
prints to the terminal.

| Option | Description |
|---|---|
| `--date YYYY-MM-DD` | Slate date (default: today, US Eastern) |
| `--format html json csv` | Report formats (default: html json) |
| `--no-odds` | Skip sportsbook prices |
| `--bookmakers draftkings,fanduel` | Only these books (also `ODDS_BOOKMAKERS`) |
| `--park-seasons N` | Seasons pooled for park factors (default 3) |
| `--no-cache` / `--cache-dir PATH` | HTTP cache control (default `~/.cache/mlb-prop-predictor`) |
| `--demo` | Offline fictional slate |
| `--open` | Open the HTML report when done |

## Data sources

| Data | Source | Cost |
|---|---|---|
| Schedule, probable starters, lineups, venues | [MLB Stats API](https://statsapi.mlb.com) | Free |
| Player season and platoon splits, team home/road splits | MLB Stats API | Free |
| Barrel rates (hitters and pitchers) | [Baseball Savant](https://baseballsavant.mlb.com) leaderboards | Free |
| Game-time weather | [Open-Meteo](https://open-meteo.com) | Free, non-commercial |
| HR and strikeout prop prices | [The Odds API](https://the-odds-api.com) | Free tier: 500 credits/month |

**Odds API budget:** only two markets are requested (`batter_home_runs`, `pitcher_strikeouts`),
so each game costs about 2 credits and a full 15-game slate about 30. That's roughly
**16 full-slate runs a month** on the free plan. Odds are cached for 30 minutes, so re-running
a report doesn't spend credits again. Use `--bookmakers` to narrow requests, or `--no-odds`
to skip them entirely.

No logins, cookies or scraping of paid sites. MLB data is © MLB Advanced Media and is used for
individual, non-commercial purposes, so this repo ships code, not data.

## Project layout

```
src/mlb_prop_predictor/
├── cli.py              # argument parsing, output files
├── pipeline.py         # fetch → project → attach market prices
├── http.py             # stdlib HTTP client with retries + on-disk TTL cache
├── config.py           # settings, .env loading, cache lifetimes
├── domain.py           # dataclasses shared across layers
├── sources/            # one module per API: mlb_stats, savant, weather, odds
├── models/             # pure functions: common math, park factors, matchup, home_runs, strikeouts
├── report/             # console, JSON, CSV and HTML writers
└── demo/               # fictional fixtures + offline client
tests/                  # unit tests for the math and parsers, plus end-to-end demo runs
scripts/                # fixture generator
docs/METHODOLOGY.md
```

Design notes:
- **Zero runtime dependencies.** Standard library only, so it installs anywhere with Python 3.10+.
- **Sources are injectable.** Every source takes an `HttpClient`, so tests and demo mode run offline.
- **Models are pure functions.** They're unit-tested for calibration: a league-average matchup
  reproduces league-average rates.
- **Secrets stay out of git.** The API key lives in `.env` and is redacted from cache keys and logs.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check src tests scripts
python scripts/build_demo_fixtures.py   # regenerate demo data
```

## Disclaimer

For research and entertainment only. Projections are estimates, not guarantees, and nothing
here is betting advice. Gamble responsibly.

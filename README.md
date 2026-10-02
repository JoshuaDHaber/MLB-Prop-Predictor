# MLB Prop Predictor

A single-file Python tool that pulls MLB matchup data from a PropFinder-compatible API, scores the day's slate, and writes a ranked report of the most promising player and game props.

## What it produces

- **Home run props:** batters ranked by a composite score built from matchup data, odds-implied probability, ballpark factor, weather, and pitcher HR risk
- **Hits / runs / RBIs:** a secondary ranking of batters
- **Pitcher strikeouts:** starters ranked by expected strikeouts against the opposing lineup
- **Team totals:** expected runs per team
- **NRFI / YRFI:** first-inning probabilities that blend recent pitcher form with season splits

Reports are written as JSON, CSV, or text, plus an HTML report that opens in your browser. Batting-order positions are added once lineups are posted.

## Requirements

- Python 3.9+ (standard library only, nothing to `pip install`)
- A PropFinder account, which the live API calls need
- macOS with Google Chrome, but only if you use `--browser-login`

## Quick start

Try it with built-in demo data, no account needed:

```bash
python3 propfinder.py --sample --limit 10
```

## Authentication

The script needs your PropFinder session, supplied in one of these ways (checked in this order):

1. **Browser login (macOS + Chrome).** Log in to propfinder.app in Chrome, then run:
   ```bash
   python3 propfinder.py --browser-login
   ```
   This reads your PropFinder cookies from Chrome and saves them to `propfinder-auth.json`. macOS will ask for Keychain access.
2. **Environment variables:**
   ```bash
   export PROPFINDER_BASE_URL="https://api.propfinder.app"
   export PROPFINDER_COOKIE="accessToken=...; refreshToken=..."
   # or PROPFINDER_TOKEN / PROPFINDER_API_KEY
   ```
3. **Config file:** copy the template and fill it in:
   ```bash
   cp propfinder-config.example.json propfinder-config.json
   ```

> ⚠️ `propfinder-config.json`, `propfinder-auth.json`, and any `.har` files contain your login session. They are listed in `.gitignore`. **Never commit them.**

## Usage

```bash
# Today's slate with your saved auth
python3 propfinder.py

# A specific date, top 20, CSV output
python3 propfinder.py --dates 2026-07-05 --limit 20 --format csv --output picks.csv

# Also write a human-readable text summary
python3 propfinder.py --text-output home_run_report.txt
```

### Useful options

| Flag | Env var | Default | Description |
|---|---|---|---|
| `--base-url` | `PROPFINDER_BASE_URL` | none | API host |
| `--dates` | `PROPFINDER_DATES` | today (UTC) | Slate date, `YYYY-MM-DD` |
| `--season` | `PROPFINDER_SEASON` | `2026` | Season used for pitcher splits |
| `--limit` | `PROPFINDER_LIMIT` | `15` | Number of ranked picks printed |
| `--format` | `PROPFINDER_FORMAT` | `json` | `json`, `csv`, or `text` |
| `--output` | `PROPFINDER_OUTPUT` | `home_run_report.json` | Report path |
| `--text-output` | `PROPFINDER_TEXT_OUTPUT` | none | Optional text summary path |
| `--config` | `PROPFINDER_CONFIG` | auto-detect | Path to a JSON config |
| `--sample` | | | Use demo data instead of the API |
| `--browser-login` | | | Capture auth from Chrome |

Run `python3 propfinder.py --help` for the full list.

## Disclaimer

For personal research and entertainment only. Nothing here is betting advice, and the model's scores are not guaranteed probabilities. Follow PropFinder's terms of service, and bet responsibly.

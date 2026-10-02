#!/usr/bin/env python3
"""Fetch MLB home-run prop data from Propfinder-compatible APIs and rank the best bets.

Usage examples:
  python3 propfinder.py --sample --limit 10
  python3 propfinder.py --base-url https://your-propfinder-host --token "$PROPFINDER_TOKEN" --league MLB --market home_run --output home_run_report.json

Environment variables:
  PROPFINDER_BASE_URL, PROPFINDER_API_BASE_URL
  PROPFINDER_TOKEN, PROPFINDER_API_TOKEN
  PROPFINDER_API_KEY
  PROPFINDER_ENDPOINT, PROPFINDER_API_PATH
  PROPFINDER_LEAGUE
  PROPFINDER_MARKET
  PROPFINDER_OUTPUT
  PROPFINDER_FORMAT
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import json
import os
import re
import shutil
import sqlite3
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib import error, request
from urllib.parse import quote, urlencode, urlparse
from zoneinfo import ZoneInfo

DEFAULT_ENDPOINTS = [
    "/api/v1/props",
    "/api/props",
    "/v1/props",
    "/props",
    "/api/v1/search",
    "/api/search",
    "/search",
]

DEFAULT_AUTH_ENDPOINT = "/identity/user"
DEFAULT_MATCHUP_ENDPOINT = "/mlb/hr-matchup"
DEFAULT_PITCHER_SPLITS_ENDPOINT = "/mlb/pitcher-splits"
DEFAULT_ZONE_MATCHUPS_ENDPOINT = "/mlb/zone-matchups/pitcher"
DEFAULT_PARK_FACTORS_ENDPOINT = "/MLB/park-factors"
DEFAULT_HR_RISK_ENDPOINT = "/mlb/hr-risk"
DEFAULT_GAME_SLATE_ENDPOINT = "/MLB/v2"
DEFAULT_BROWSER_COOKIE_PATH = "~/Library/Application Support/Google/Chrome/Default/Cookies"
RECENT_NRFI_SPLITS = [
    "Last 3 Starts / 15 Days",
    "3 Starts / 15 Days",
    "Last 3 Starts",
    "Last 15 Days",
    "3 Starts",
    "15 Days",
    "Recent",
]
EASTERN_TIMEZONE = ZoneInfo("America/New_York")

SAMPLE_PAYLOAD = {
    "data": [
        {
            "player": "Mookie Betts",
            "market": "Home Runs",
            "line": 0.5,
            "odds": -140,
            "rating": 94.2,
            "game": "Dodgers @ Padres",
            "time": "2026-07-05T20:10:00Z",
            "source": "demo",
            "notes": "Elite power matchup with strong barrel rate.",
        },
        {
            "player": "Shohei Ohtani",
            "market": "Home Runs",
            "line": 0.5,
            "odds": -130,
            "rating": 92.8,
            "game": "Dodgers @ Padres",
            "time": "2026-07-05T20:10:00Z",
            "source": "demo",
            "notes": "High slugging vs right-handed starter.",
        },
        {
            "player": "Matt Olson",
            "market": "Home Runs",
            "line": 0.5,
            "odds": +120,
            "rating": 89.9,
            "game": "Braves @ Phillies",
            "time": "2026-07-05T18:05:00Z",
            "source": "demo",
            "notes": "Strong pull-side power and a favorable ballpark.",
        },
    ]
}


def default_config_path() -> Optional[str]:
    candidates = [
        os.getenv("PROPFINDER_CONFIG"),
        "propfinder-config.json",
        "propfinder.json",
        ".propfinder.json",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rank likely MLB home run props from Propfinder")
    parser.add_argument("--config", default=default_config_path(), help="Path to a JSON config file containing base_url/token/api_key values")
    parser.add_argument("--base-url", default=os.getenv("PROPFINDER_BASE_URL") or os.getenv("PROPFINDER_API_BASE_URL"))
    parser.add_argument("--token", default=os.getenv("PROPFINDER_TOKEN") or os.getenv("PROPFINDER_API_TOKEN"))
    parser.add_argument("--cookie", default=os.getenv("PROPFINDER_COOKIE") or os.getenv("PROPFINDER_COOKIES"))
    parser.add_argument("--api-key", default=os.getenv("PROPFINDER_API_KEY"))
    parser.add_argument("--endpoint", default=os.getenv("PROPFINDER_ENDPOINT") or os.getenv("PROPFINDER_API_PATH"))
    parser.add_argument("--auth-endpoint", default=os.getenv("PROPFINDER_AUTH_ENDPOINT", DEFAULT_AUTH_ENDPOINT))
    parser.add_argument("--matchup-endpoint", default=os.getenv("PROPFINDER_MATCHUP_ENDPOINT", DEFAULT_MATCHUP_ENDPOINT))
    parser.add_argument("--pitcher-id", default=os.getenv("PROPFINDER_PITCHER_ID", "676664"))
    parser.add_argument("--pitcher-ids", default=os.getenv("PROPFINDER_PITCHER_IDS"))
    parser.add_argument("--team-id", default=os.getenv("PROPFINDER_TEAM_ID", "119"))
    parser.add_argument("--team-ids", default=os.getenv("PROPFINDER_TEAM_IDS"))
    parser.add_argument("--season", default=os.getenv("PROPFINDER_SEASON", "2026"))
    parser.add_argument("--range-value", default=os.getenv("PROPFINDER_RANGE_VALUE", "15"))
    parser.add_argument("--range-type", default=os.getenv("PROPFINDER_RANGE_TYPE", "battedBall"))
    parser.add_argument("--bat-side", default=os.getenv("PROPFINDER_BAT_SIDE"))
    parser.add_argument("--dates", default=os.getenv("PROPFINDER_DATES", datetime.now(timezone.utc).strftime("%Y-%m-%d")))
    parser.add_argument("--league", default=os.getenv("PROPFINDER_LEAGUE", "MLB"))
    parser.add_argument("--market", default=os.getenv("PROPFINDER_MARKET", "home_run"))
    parser.add_argument("--limit", type=int, default=int(os.getenv("PROPFINDER_LIMIT", "15")))
    parser.add_argument("--output", default=os.getenv("PROPFINDER_OUTPUT", "home_run_report.json"))
    parser.add_argument("--text-output", default=os.getenv("PROPFINDER_TEXT_OUTPUT"), help="Optional path for a human-readable text summary")
    parser.add_argument("--format", choices=["json", "csv", "text"], default=os.getenv("PROPFINDER_FORMAT", "json"))
    parser.add_argument("--sample", action="store_true", help="Use built-in demo data instead of calling an API")
    parser.add_argument("--browser-login", action="store_true", help="Open a browser-based login flow and save the resulting auth state")
    parser.add_argument("--auth-state-path", default=os.getenv("PROPFINDER_AUTH_STATE_PATH", "propfinder-auth.json"), help="Path for saving browser-auth cookie/token state")
    parser.add_argument("--browser-cookie-path", default=os.getenv("PROPFINDER_BROWSER_COOKIE_PATH", DEFAULT_BROWSER_COOKIE_PATH), help="Path to the Chrome cookie database used for browser login")
    return parser.parse_args()


def load_config(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Config file must contain a JSON object")
    return data


def resolve_args(args: argparse.Namespace) -> argparse.Namespace:
    config = load_config(args.config)
    defaults = {
        "base_url": None,
        "token": None,
        "cookie": None,
        "api_key": None,
        "endpoint": None,
        "auth_endpoint": DEFAULT_AUTH_ENDPOINT,
        "matchup_endpoint": DEFAULT_MATCHUP_ENDPOINT,
        "pitcher_id": "676664",
        "pitcher_ids": None,
        "team_id": "119",
        "team_ids": None,
        "season": "2026",
        "range_value": "15",
        "range_type": "battedBall",
        "bat_side": None,
        "dates": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "league": "MLB",
        "market": "home_run",
        "output": "home_run_report.json",
        "text_output": None,
        "format": "json",
    }

    for key in ["base_url", "token", "cookie", "api_key", "endpoint", "auth_endpoint", "matchup_endpoint", "pitcher_id", "pitcher_ids", "team_id", "team_ids", "season", "range_value", "range_type", "league", "market", "output", "text_output", "format"]:
        if key in config:
            current_value = getattr(args, key, None)
            if current_value in {None, ""} or current_value == defaults.get(key):
                setattr(args, key, config[key])
    if not getattr(args, "bat_side", None) and config.get("bat_side"):
        args.bat_side = config["bat_side"]
    if not getattr(args, "dates", None) and config.get("dates"):
        args.dates = config["dates"]

    auth_state = load_browser_auth_state(args.auth_state_path)
    if auth_state.get("cookie"):
        args.cookie = auth_state["cookie"]
    if auth_state.get("token"):
        args.token = auth_state["token"]

    if not getattr(args, "base_url", None) and config.get("baseUrl"):
        args.base_url = config["baseUrl"]
    if not getattr(args, "token", None) and config.get("access_token"):
        args.token = config["access_token"]
    if not getattr(args, "cookie", None) and config.get("cookie"):
        args.cookie = config["cookie"]
    if not getattr(args, "cookie", None) and config.get("cookies"):
        args.cookie = config["cookies"]
    if not getattr(args, "api_key", None) and config.get("api_key"):
        args.api_key = config["api_key"]
    return args


def save_browser_auth_state(path: Any, cookie: Optional[str] = None, token: Optional[str] = None) -> Dict[str, Optional[str]]:
    state = {"cookie": cookie, "token": token}
    auth_path = Path(path).expanduser()
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    with auth_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
    return state


def load_browser_auth_state(path: Any) -> Dict[str, Optional[str]]:
    auth_path = Path(path).expanduser()
    if not auth_path.exists():
        return {"cookie": None, "token": None}
    with auth_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        return {"cookie": None, "token": None}
    return {"cookie": data.get("cookie"), "token": data.get("token")}


def derive_browser_login_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return "https://propfinder.app/"
    host = parsed.netloc
    if host.startswith("api."):
        host = host[4:]
    return f"{parsed.scheme}://{host}/"


def strip_chrome_cookie_prefix(host_key: str, value: bytes) -> bytes:
    domain_prefix = hashlib.sha256(host_key.encode("utf-8")).digest()
    if value.startswith(domain_prefix):
        return value[len(domain_prefix):]
    return value


def get_chrome_safe_storage_password() -> str:
    candidates = [
        ["/usr/bin/security", "find-generic-password", "-w", "-a", "Chrome", "-s", "Chrome Safe Storage"],
        ["/usr/bin/security", "find-generic-password", "-w", "-a", "Google Chrome", "-s", "Chrome Safe Storage"],
    ]
    for command in candidates:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0:
            password = result.stdout.strip()
            if password:
                return password
    raise RuntimeError("Unable to read the Chrome Safe Storage password from macOS Keychain")


def decrypt_chrome_cookie_value(host_key: str, encrypted_value: Any) -> str:
    if not encrypted_value:
        return ""
    if isinstance(encrypted_value, memoryview):
        encrypted_value = encrypted_value.tobytes()
    if not isinstance(encrypted_value, (bytes, bytearray)):
        return normalize_text(encrypted_value)

    encrypted_bytes = bytes(encrypted_value)
    if not encrypted_bytes:
        return ""
    if not encrypted_bytes.startswith((b"v10", b"v11")):
        return encrypted_bytes.decode("utf-8", errors="ignore")

    password = get_chrome_safe_storage_password()
    key = hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), b"saltysalt", 1003, 16)
    payload = encrypted_bytes[3:]
    result = subprocess.run(
        ["/usr/bin/openssl", "enc", "-aes-128-cbc", "-d", "-K", key.hex(), "-iv", "20" * 16],
        input=payload,
        capture_output=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(f"Unable to decrypt Chrome cookie for {host_key}: {stderr or 'openssl failed'}")

    decrypted = result.stdout
    if decrypted:
        padding = decrypted[-1]
        if 0 < padding <= 16:
            decrypted = decrypted[:-padding]
    decrypted = strip_chrome_cookie_prefix(host_key, decrypted)
    return decrypted.decode("utf-8", errors="ignore")


def load_chrome_cookies(domain: str, cookie_path: Any) -> List[Dict[str, str]]:
    source_path = Path(cookie_path).expanduser()
    if not source_path.exists():
        raise FileNotFoundError(f"Chrome cookie database not found: {source_path}")

    rows: List[Any] = []
    with tempfile.TemporaryDirectory() as temp_dir:
        copied_path = Path(temp_dir) / "Cookies"
        shutil.copy2(source_path, copied_path)
        connection = sqlite3.connect(str(copied_path))
        try:
            rows = connection.execute(
                "SELECT host_key, name, value, encrypted_value FROM cookies WHERE host_key LIKE ? ORDER BY host_key, name",
                (f"%{domain}%",),
            ).fetchall()
        finally:
            connection.close()

    cookies: List[Dict[str, str]] = []
    for host_key, name, value, encrypted_value in rows:
        cookie_value = value or decrypt_chrome_cookie_value(host_key, encrypted_value)
        if not cookie_value:
            continue
        cookies.append({
            "host_key": normalize_text(host_key),
            "name": normalize_text(name),
            "value": normalize_text(cookie_value),
        })
    return cookies


def build_cookie_header(cookies: List[Dict[str, str]]) -> str:
    by_name: Dict[str, str] = {}
    for cookie in cookies:
        name = cookie.get("name") or ""
        value = cookie.get("value") or ""
        if name and value:
            by_name[name] = value
    return "; ".join(f"{name}={value}" for name, value in by_name.items())


def capture_browser_auth_state(base_url: str, auth_state_path: Any, cookie_path: Any) -> Dict[str, Optional[str]]:
    login_url = derive_browser_login_url(base_url)
    domain = urlparse(login_url).netloc
    cookies = load_chrome_cookies(domain, cookie_path)
    cookie_header = build_cookie_header(cookies)
    token = next((cookie["value"] for cookie in cookies if cookie.get("name") == "accessToken"), None)
    if not cookie_header and not token:
        raise RuntimeError(f"No browser auth cookies were found for {domain} in {Path(cookie_path).expanduser()}")
    return save_browser_auth_state(auth_state_path, cookie=cookie_header or None, token=token)


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def find_first(obj: Any, keys: Iterable[str]) -> Any:
    if isinstance(obj, dict):
        for key in keys:
            if key in obj:
                return obj[key]
        for value in obj.values():
            found = find_first(value, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_first(item, keys)
            if found is not None:
                return found
    return None


def recursive_collect(node: Any, predicate) -> List[Any]:
    matches: List[Any] = []
    if isinstance(node, dict):
        if predicate(node):
            matches.append(node)
        for value in node.values():
            matches.extend(recursive_collect(value, predicate))
    elif isinstance(node, list):
        for item in node:
            matches.extend(recursive_collect(item, predicate))
    return matches


def looks_like_home_run_prop(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    text = json.dumps(node, sort_keys=True).lower()
    home_run_keywords = ["home run", "home runs", "home_run", "home_runs", "hr", "homer", "homerun"]
    return any(keyword in text for keyword in home_run_keywords)


def extract_matchup_records(payload: Any, league: str, market: str) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    if "matchup" in payload:
        payload = payload["matchup"]

    pitcher = payload.get("pitcher") or {}
    batter_entries = payload.get("batters") or []
    records: List[Dict[str, Any]] = []

    for batter in batter_entries:
        if not isinstance(batter, dict):
            continue
        player_name = batter.get("name") or batter.get("playerName") or ""
        if not player_name:
            continue

        hr_count = batter.get("hr")
        near_hr = batter.get("nearHr")
        barrel_pct = batter.get("barrelPct")
        avg_distance = batter.get("avgDistance")
        pull_air_pct = batter.get("pullAirPct")
        hr_per_fb_pct = batter.get("hrPerFbPct")
        woba = batter.get("woba")
        batting_type = batter.get("battingType")
        pitcher_name = pitcher.get("name") or ""
        pitcher_type = pitcher.get("pitchingType") or ""

        score = 0.0
        if isinstance(hr_count, (int, float)):
            score += hr_count * 12.0
        if isinstance(near_hr, (int, float)):
            score += near_hr * 6.0
        if isinstance(barrel_pct, (int, float)):
            score += barrel_pct * 0.3
        if isinstance(avg_distance, (int, float)):
            score += avg_distance / 25.0
        if isinstance(pull_air_pct, (int, float)):
            score += pull_air_pct * 0.2
        if isinstance(hr_per_fb_pct, (int, float)):
            score += hr_per_fb_pct * 0.15
        if isinstance(woba, (int, float)):
            score += woba * 2.0

        notes_parts = []
        if pitcher_name:
            notes_parts.append(f"vs {pitcher_name} ({pitcher_type})")
        if batting_type:
            notes_parts.append(f"batting type: {batting_type}")
        if isinstance(hr_count, (int, float)):
            notes_parts.append(f"HRs: {hr_count}")
        if isinstance(near_hr, (int, float)):
            notes_parts.append(f"nearHR: {near_hr}")
        if isinstance(barrel_pct, (int, float)):
            notes_parts.append(f"barrel%: {barrel_pct}")
        if isinstance(avg_distance, (int, float)):
            notes_parts.append(f"avg distance: {avg_distance}")

        time_value = payload.get("game_time") or payload.get("time") or payload.get("datetime") or payload.get("start_time")
        if not time_value:
            time_value = payload.get("season")

        records.append({
            "player": player_name,
            "market": market,
            "league": league,
            "line": None,
            "odds": None,
            "score": round(score, 2),
            "game": f"{pitcher_name} vs {player_name}" if pitcher_name else player_name,
            "time": normalize_text(time_value),
            "notes": "; ".join(notes_parts),
            "raw": batter,
        })

    return sorted(records, key=lambda item: item["score"], reverse=True)


def parse_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if not cleaned:
            return None
        if cleaned.startswith(("+", "-")) and cleaned[1:].replace(".", "", 1).isdigit():
            return float(cleaned)
        if cleaned.replace(".", "", 1).isdigit():
            return float(cleaned)
    return None


def implied_probability_from_odds(value: Any) -> Optional[float]:
    odds = parse_number(value)
    if odds is None:
        return None
    if odds > 0:
        return 100 / (odds + 100)
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    return None


def score_candidate(candidate: Dict[str, Any]) -> float:
    score = 0.0

    rating_value = parse_number(find_first(candidate, ["rating", "pf_rating", "score", "edge", "model_rating"]))
    if rating_value is not None:
        score += rating_value

    projection = parse_number(find_first(candidate, ["projection", "projected_hr", "expected_hr", "home_runs", "line"]))
    if projection is not None:
        score += projection * 10.0

    if parse_number(find_first(candidate, ["probability", "model_prob", "probability_pct", "implied_probability"])) is not None:
        score += parse_number(find_first(candidate, ["probability", "model_prob", "probability_pct", "implied_probability"])) * 100.0

    implied_prob = implied_probability_from_odds(find_first(candidate, ["odds", "american_odds", "price_odds"]))
    if implied_prob is not None:
        score += implied_prob * 100.0

    # Reward props that explicitly mention a home-run market.
    if any(keyword in normalize_text(candidate).lower() for keyword in ["home run", "home runs", "home_run", "home_runs", "hr"]):
        score += 5.0

    return score


def build_record(candidate: Dict[str, Any], league: str, market: str) -> Dict[str, Any]:
    player = find_first(candidate, ["player", "player_name", "name", "athlete", "athlete_name"])
    market_name = find_first(candidate, ["market", "market_name", "prop", "prop_type", "type"])
    line_value = find_first(candidate, ["line", "over_under", "projection", "projected_hr"])
    odds_value = find_first(candidate, ["odds", "american_odds", "price_odds"])
    game_value = find_first(candidate, ["game", "matchup", "fixture", "opp", "opponent"])
    time_value = find_first(candidate, ["time", "datetime", "game_time", "start_time"])
    notes_value = find_first(candidate, ["notes", "reason", "research", "analysis"])

    return {
        "player": normalize_text(player),
        "market": normalize_text(market_name or market),
        "league": league,
        "line": parse_number(line_value),
        "odds": parse_number(odds_value),
        "score": score_candidate(candidate),
        "game": normalize_text(game_value),
        "time": normalize_text(time_value),
        "notes": normalize_text(notes_value),
        "raw": candidate,
    }


def extract_candidates(payload: Any, league: str, market: str) -> List[Dict[str, Any]]:
    if isinstance(payload, dict) and any(key in payload for key in ["matchup", "matchups", "splits", "zone", "park", "hr_risk"]):
        records: List[Dict[str, Any]] = []
        if isinstance(payload.get("matchups"), list):
            for matchup_item in payload["matchups"]:
                if isinstance(matchup_item, dict):
                    matchup_payload = matchup_item.get("payload")
                    if isinstance(matchup_payload, dict):
                        matchup_records = extract_matchup_records(matchup_payload, league, market)
                        for record in matchup_records:
                            record["game"] = normalize_text(matchup_item.get("game_label") or f"{matchup_item.get('pitcher_id')} vs {matchup_item.get('team_id')}")
                            if matchup_item.get("game_time"):
                                record["time"] = normalize_text(matchup_item.get("game_time") or "")
                            records.append(record)
        matchup_records = extract_matchup_records(payload, league, market)
        if matchup_records:
            records.extend(matchup_records)

        splits_payload = payload.get("splits") if isinstance(payload.get("splits"), dict) else None
        if isinstance(splits_payload, dict):
            for key in ["pitcher", "pitchers", "data"]:
                if isinstance(splits_payload.get(key), dict):
                    records.append({
                        "player": normalize_text(splits_payload[key].get("name") or splits_payload[key].get("pitcherName") or "pitcher"),
                        "market": market,
                        "league": league,
                        "line": None,
                        "odds": None,
                        "score": 15.0,
                        "game": normalize_text(splits_payload[key].get("name") or splits_payload[key].get("pitcherName") or "pitcher"),
                        "time": normalize_text(splits_payload.get("season") or ""),
                        "notes": "pitcher splits available",
                        "raw": splits_payload[key],
                    })

        zone_payload = payload.get("zone") if isinstance(payload.get("zone"), dict) else None
        if isinstance(zone_payload, dict):
            for key in ["batter", "batterData", "batters", "data"]:
                if isinstance(zone_payload.get(key), list):
                    for item in zone_payload[key]:
                        if isinstance(item, dict):
                            records.append({
                                "player": normalize_text(item.get("name") or item.get("batterName") or ""),
                                "market": market,
                                "league": league,
                                "line": None,
                                "odds": None,
                                "score": 10.0 + (parse_number(item.get("score")) or 0),
                                "game": normalize_text(item.get("matchup") or item.get("game") or ""),
                                "time": normalize_text(zone_payload.get("season") or ""),
                                "notes": normalize_text(item.get("notes") or item.get("reason") or "zone matchup available"),
                                "raw": item,
                            })

        park_payload = payload.get("park") if isinstance(payload.get("park"), (dict, list)) else None
        if isinstance(park_payload, (dict, list)):
            records.append({
                "player": "Park factors",
                "market": market,
                "league": league,
                "line": None,
                "odds": None,
                "score": 8.0,
                "game": "Park factors",
                "time": normalize_text(payload.get("dates") or ""),
                "notes": "park factors loaded",
                "raw": park_payload,
            })

        hr_risk_payload = payload.get("hr_risk") if isinstance(payload.get("hr_risk"), (dict, list)) else None
        if isinstance(hr_risk_payload, (dict, list)):
            records.append({
                "player": "HR risk",
                "market": market,
                "league": league,
                "line": None,
                "odds": None,
                "score": 7.0,
                "game": "HR risk",
                "time": normalize_text(payload.get("dates") or ""),
                "notes": "hr-risk loaded",
                "raw": hr_risk_payload,
            })

        if records:
            seen = set()
            deduped: List[Dict[str, Any]] = []
            for record in sorted(records, key=lambda item: item["score"], reverse=True):
                key = (record["player"], record["game"], record["market"], record["league"])
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(record)
            return deduped

    matchup_records = extract_matchup_records(payload, league, market)
    if matchup_records:
        return matchup_records

    matches = recursive_collect(payload, looks_like_home_run_prop)
    records = [build_record(candidate, league, market) for candidate in matches]
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for record in records:
        key = (record["player"], record["game"], record["market"], record["line"], record["odds"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return sorted(deduped, key=lambda item: item["score"], reverse=True)


def build_request_headers(token: Optional[str], api_key: Optional[str], cookie: Optional[str]) -> Dict[str, str]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
        "Connection": "keep-alive",
        "Origin": "https://propfinder.app",
        "Referer": "https://propfinder.app/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "macOS",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if api_key:
        headers["X-API-Key"] = api_key
    if cookie:
        headers["Cookie"] = cookie
    return headers


def browser_login(base_url: str, auth_state_path: Any) -> Dict[str, Optional[str]]:
    return load_browser_auth_state(auth_state_path)


def run_browser_login(base_url: str, auth_state_path: Any, cookie_path: Any) -> Dict[str, Optional[str]]:
    login_url = derive_browser_login_url(base_url)
    try:
        subprocess.run(["open", "-a", "Google Chrome", login_url], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        subprocess.run(["open", login_url], check=True, capture_output=True)

    print(f"Opened {login_url} in your browser.", file=sys.stderr)
    print("Log in to Propfinder, then return to the terminal and press Enter to continue.", file=sys.stderr)
    input()
    return capture_browser_auth_state(base_url, auth_state_path, cookie_path)


def request_json(url: str, token: Optional[str], api_key: Optional[str], cookie: Optional[str], method: str = "GET", payload: Optional[Dict[str, Any]] = None) -> Any:
    headers = build_request_headers(token, api_key, cookie)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, headers=headers, method=method, data=data)
    with request.urlopen(req, timeout=20) as response:
        body = response.read().decode("utf-8")
        return json.loads(body)


def build_probe_urls(base_url: str, endpoint: Optional[str], league: str, market: str) -> List[str]:
    base_url = base_url.rstrip("/")
    if endpoint:
        if endpoint.startswith("http"):
            return [endpoint]
        return [f"{base_url}{endpoint}"]

    params = urlencode({"league": league, "market": market})
    urls: List[str] = []
    for item in DEFAULT_ENDPOINTS:
        urls.append(f"{base_url}{item}?{params}")
    urls.append(f"{base_url}/api/v1/props?{params}")
    urls.append(f"{base_url}/api/props?{params}")
    return urls


def build_matchup_specs(args: argparse.Namespace) -> List[tuple[str, str]]:
    pitcher_ids = []
    if args.pitcher_ids:
        pitcher_ids = [part.strip() for part in args.pitcher_ids.split(",") if part.strip()]
    elif args.pitcher_id:
        pitcher_ids = [str(args.pitcher_id).strip()]

    team_ids = []
    if args.team_ids:
        team_ids = [part.strip() for part in args.team_ids.split(",") if part.strip()]
    elif args.team_id:
        team_ids = [str(args.team_id).strip()]

    if not pitcher_ids and not team_ids:
        return []
    if not pitcher_ids:
        pitcher_ids = [args.pitcher_id or "676664"]
    if not team_ids:
        team_ids = [args.team_id or "119"]

    if len(pitcher_ids) == len(team_ids) and len(pitcher_ids) > 1:
        return list(zip(pitcher_ids, team_ids))

    specs = []
    for pitcher_id in pitcher_ids:
        for team_id in team_ids:
            specs.append((pitcher_id, team_id))
    return specs


def discover_game_slate(base_url: str, token: Optional[str], api_key: Optional[str], cookie: Optional[str]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    url = f"{base_url}{DEFAULT_GAME_SLATE_ENDPOINT}?sportsbooks=draftkings&sportsbooks=fanduel&sportsbooks=betmgm"
    data = request_json(url, token, api_key, cookie)
    if isinstance(data, dict):
        games = data.get("games")
        players = data.get("players")
        games_list = [game for game in games if isinstance(game, dict)] if isinstance(games, list) else []
        players_list = [player for player in players if isinstance(player, dict)] if isinstance(players, list) else []
        return games_list, players_list
    if isinstance(data, list):
        return [game for game in data if isinstance(game, dict)], []
    return [], []


def build_game_matchup_specs(game_slate: List[Dict[str, Any]]) -> List[tuple[str, str, str]]:
    specs: List[tuple[str, str, str]] = []
    for game in game_slate:
        home_pitcher_id = game.get("homePitcherId")
        visitor_pitcher_id = game.get("visitorPitcherId")
        home_team = game.get("homeTeam") or {}
        visitor_team = game.get("visitorTeam") or {}
        home_team_id = home_team.get("id")
        visitor_team_id = visitor_team.get("id")
        game_label = format_game_label(game)
        game_time = normalize_text(game.get("gameDate") or "")

        if home_pitcher_id and visitor_team_id:
            specs.append((str(home_pitcher_id), str(visitor_team_id), game_label))
        if visitor_pitcher_id and home_team_id:
            specs.append((str(visitor_pitcher_id), str(home_team_id), game_label))
    return specs


def format_game_label(game: Optional[Dict[str, Any]]) -> str:
    if not isinstance(game, dict):
        return ""
    home_team = game.get("homeTeam") or {}
    away_team = game.get("visitorTeam") or {}
    home_name = home_team.get("clubName") or home_team.get("shortName") or home_team.get("fullName") or home_team.get("location")
    away_name = away_team.get("clubName") or away_team.get("shortName") or away_team.get("fullName") or away_team.get("location")
    if home_name and away_name:
        return f"{away_name} @ {home_name}"
    return ""


def fetch_payload(args: argparse.Namespace) -> Any:
    if args.sample:
        return SAMPLE_PAYLOAD

    if not args.base_url:
        raise ValueError("Set --base-url or PROPFINDER_BASE_URL to use the live API")

    auth_state = load_browser_auth_state(args.auth_state_path)
    if args.browser_login:
        auth_state = run_browser_login(args.base_url, args.auth_state_path, args.browser_cookie_path)
        if auth_state.get("cookie"):
            args.cookie = auth_state["cookie"]
        if auth_state.get("token"):
            args.token = auth_state["token"]
    elif not args.token and not args.cookie and auth_state.get("cookie"):
        args.cookie = auth_state["cookie"]
    elif not args.token and not args.cookie and auth_state.get("token"):
        args.token = auth_state["token"]

    base_url = args.base_url.rstrip("/")

    if args.endpoint:
        endpoint_value = args.endpoint if args.endpoint.startswith("http") else f"{base_url}{args.endpoint}"
        return request_json(endpoint_value, args.token, args.api_key, args.cookie)

    auth_url = f"{base_url}{args.auth_endpoint}" if args.auth_endpoint.startswith("/") else f"{base_url}/{args.auth_endpoint}"
    try:
        request_json(auth_url, args.token, args.api_key, args.cookie)
    except (error.HTTPError, error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Authentication check failed for {auth_url}: {exc}. "
            f"If your saved token is expired, re-run with --browser-login to refresh {args.auth_state_path}."
        ) from exc

    game_slate, players_directory = discover_game_slate(base_url, args.token, args.api_key, args.cookie)
    matchup_specs = []
    if args.pitcher_ids or args.team_ids:
        matchup_specs = build_matchup_specs(args)
        matchup_specs = [(pitcher_id, team_id, "") for pitcher_id, team_id in matchup_specs]

    if not matchup_specs:
        matchup_specs = build_game_matchup_specs(game_slate)
        if not matchup_specs:
            matchup_specs = [(args.pitcher_id or "676664", args.team_id or "119", "")]

    game_time_lookup = {}
    for game in game_slate:
        home_pitcher_id = game.get("homePitcherId")
        visitor_pitcher_id = game.get("visitorPitcherId")
        home_team = game.get("homeTeam") or {}
        visitor_team = game.get("visitorTeam") or {}
        home_team_id = home_team.get("id")
        visitor_team_id = visitor_team.get("id")
        game_time = normalize_text(game.get("gameDate") or "")
        if home_pitcher_id and visitor_team_id and game_time:
            game_time_lookup[(str(home_pitcher_id), str(visitor_team_id))] = game_time
        if visitor_pitcher_id and home_team_id and game_time:
            game_time_lookup[(str(visitor_pitcher_id), str(home_team_id))] = game_time

    payloads = {"matchups": [], "game_slate": game_slate, "players_directory": players_directory}
    for matchup in matchup_specs:
        if len(matchup) >= 3:
            pitcher_id, team_id, game_label = matchup[0], matchup[1], matchup[2]
            game_time = game_time_lookup.get((str(pitcher_id), str(team_id)), "")
        else:
            pitcher_id, team_id = matchup
            game_label = ""
            game_time = game_time_lookup.get((str(pitcher_id), str(team_id)), "")
        matchup_query = urlencode({
            "pitcherId": pitcher_id,
            "teamId": team_id,
            "season": args.season,
            "rangeValue": args.range_value,
            "rangeType": args.range_type,
        })
        matchup_url = f"{base_url}{args.matchup_endpoint}?{matchup_query}" if args.matchup_endpoint.startswith("/") else f"{base_url}/{args.matchup_endpoint}?{matchup_query}"
        payloads["matchups"].append({
            "pitcher_id": pitcher_id,
            "team_id": team_id,
            "game_label": game_label,
            "game_time": game_time,
            "payload": request_json(matchup_url, args.token, args.api_key, args.cookie),
        })

    split_query = urlencode({"pitcherId": args.pitcher_id, "season": args.season})
    split_url = f"{base_url}{DEFAULT_PITCHER_SPLITS_ENDPOINT}?{split_query}" if DEFAULT_PITCHER_SPLITS_ENDPOINT.startswith("/") else f"{base_url}/{DEFAULT_PITCHER_SPLITS_ENDPOINT}?{split_query}"

    zone_query = urlencode({"pitcherId": args.pitcher_id, "teamId": args.team_id, "season": args.season, "minPitchUsage": 0})
    zone_url = f"{base_url}{DEFAULT_ZONE_MATCHUPS_ENDPOINT}?{zone_query}" if DEFAULT_ZONE_MATCHUPS_ENDPOINT.startswith("/") else f"{base_url}/{DEFAULT_ZONE_MATCHUPS_ENDPOINT}?{zone_query}"

    park_query = urlencode({"batSide": args.bat_side}) if args.bat_side else None
    park_url = f"{base_url}{DEFAULT_PARK_FACTORS_ENDPOINT}" if DEFAULT_PARK_FACTORS_ENDPOINT.startswith("/") else f"{base_url}/{DEFAULT_PARK_FACTORS_ENDPOINT}"
    if park_query:
        park_url = f"{park_url}?{park_query}"

    hr_risk_url = f"{base_url}{DEFAULT_HR_RISK_ENDPOINT}?dates={args.dates}" if DEFAULT_HR_RISK_ENDPOINT.startswith("/") else f"{base_url}/{DEFAULT_HR_RISK_ENDPOINT}?dates={args.dates}"

    payloads.update({
        "splits": request_json(split_url, args.token, args.api_key, args.cookie),
        "zone": request_json(zone_url, args.token, args.api_key, args.cookie),
        "park": request_json(park_url, args.token, args.api_key, args.cookie),
        "hr_risk": request_json(hr_risk_url, args.token, args.api_key, args.cookie),
    })
    return payloads


def derive_text_output_path(output_path: str) -> str:
    output_path = os.path.abspath(output_path)
    stem, suffix = os.path.splitext(output_path)
    if suffix.lower() in {".txt"}:
        return output_path
    return f"{stem}.txt"


def build_hits_runs_rbis_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    secondary: List[Dict[str, Any]] = []
    for record in records:
        raw = record.get("raw")
        if not isinstance(raw, dict):
            continue
        hits = parse_number(raw.get("hits")) or 0.0
        avg = parse_number(raw.get("avg")) or 0.0
        obp = parse_number(raw.get("obp")) or 0.0
        slg = parse_number(raw.get("slg")) or 0.0
        woba = parse_number(raw.get("woba")) or 0.0
        score = (hits * 6.0) + (avg * 20.0) + (obp * 18.0) + (slg * 10.0) + (woba * 12.0)
        if score <= 0:
            continue

        primary_line = 0.5
        secondary_line = 1.5
        if score >= 95:
            suggested_bet = f"Play Over {secondary_line:.1f}; fallback Over {primary_line:.1f}"
        elif score >= 75:
            suggested_bet = f"Target Over {primary_line:.1f}; lean Over {secondary_line:.1f}"
        elif score >= 55:
            suggested_bet = f"Conservative Over {primary_line:.1f} only"
        else:
            suggested_bet = f"Low-confidence Over {primary_line:.1f} only"

        secondary.append({
            "player": record.get("player", ""),
            "market": "Hits + Runs + RBI",
            "league": record.get("league", "MLB"),
            "line": primary_line,
            "derived_line": secondary_line,
            "odds": record.get("odds"),
            "score": round(score, 2),
            "game": record.get("game", ""),
            "time": record.get("time", ""),
            "notes": f"Suggested bet: {suggested_bet}; hits={hits:.1f}; avg={avg:.3f}; obp={obp:.3f}; slg={slg:.3f}; woba={woba:.3f}",
            "raw": raw,
        })
    secondary.sort(key=lambda item: item["score"], reverse=True)
    return secondary


def format_report_timestamp(value: Any) -> Dict[str, str]:
    raw_value = normalize_text(value)
    if not raw_value:
        return {"display_time": "", "display_day": "", "day_key": "", "sort_time": ""}

    parsed: Optional[datetime] = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None

    if parsed is None:
        return {"display_time": raw_value, "display_day": "", "day_key": "", "sort_time": raw_value}

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    eastern = parsed.astimezone(EASTERN_TIMEZONE)
    display_day = eastern.strftime("%a, %b %d, %Y").replace(" 0", " ")
    display_clock = eastern.strftime("%I:%M %p").lstrip("0")
    return {
        "display_time": f"{display_day} {display_clock} ET",
        "display_day": display_day,
        "day_key": eastern.strftime("%Y-%m-%d"),
        "sort_time": eastern.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def get_team_ranking_value(team: Dict[str, Any], split_name: str) -> Optional[float]:
    rankings = team.get("rankings") or []
    if not isinstance(rankings, list):
        return None
    fallback = None
    for ranking in rankings:
        if not isinstance(ranking, dict):
            continue
        if normalize_text(ranking.get("category")).lower() != "strikeouts":
            continue
        split = normalize_text(ranking.get("split"))
        value = parse_number(ranking.get("value"))
        if value is None:
            continue
        if split == split_name:
            return value
        if split == "Season":
            fallback = value
    return fallback


def build_pitcher_strikeout_records(
    game_slate: List[Dict[str, Any]],
    pitcher_lookup: Dict[str, Dict[str, Any]],
    pitcher_splits: Dict[str, Dict[str, Any]],
    html_limit: int = 50,
) -> List[Dict[str, Any]]:
    strikeout_records: List[Dict[str, Any]] = []
    teams_on_slate: List[Dict[str, Any]] = []
    for game in game_slate:
        for team_key in ["homeTeam", "visitorTeam"]:
            team = game.get(team_key) or {}
            if isinstance(team, dict):
                teams_on_slate.append(team)

    def build_record(pitcher_id: Any, pitcher_team: Dict[str, Any], opponent_team: Dict[str, Any], game: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not pitcher_id:
            return None
        pitcher_key = str(pitcher_id)
        pitcher = pitcher_lookup.get(pitcher_key) or {}
        split_payload = pitcher_splits.get(pitcher_key) or {}
        rows = split_payload.get("rows") if isinstance(split_payload, dict) else None
        if not isinstance(rows, list):
            return None
        season_row = next((row for row in rows if isinstance(row, dict) and normalize_text(row.get("split")) == "Season"), None)
        if not isinstance(season_row, dict):
            return None

        pitching_type = normalize_text(pitcher.get("pitchingType"))
        split_name = "vs LHP" if pitching_type == "LHP" else "vs RHP"
        opponent_value = get_team_ranking_value(opponent_team, split_name)
        comparison_values = [
            value
            for team in teams_on_slate
            for value in [get_team_ranking_value(team, split_name)]
            if value is not None
        ]
        baseline = sum(comparison_values) / len(comparison_values) if comparison_values else None
        strikeout_factor = (opponent_value / baseline) if baseline and baseline > 0 else 1.0

        k_per_9 = parse_number(season_row.get("kPer9")) or 0.0
        k_pct = parse_number(season_row.get("kPct")) or 0.0
        expected_strikeouts = (k_per_9 * 5.5 / 9.0) * strikeout_factor
        if k_pct:
            expected_strikeouts += max(0.0, (k_pct - 22.0) * 0.05)
        expected_strikeouts = round(expected_strikeouts, 2)
        suggested_line = max(1.5, round(expected_strikeouts) - 0.5)
        line_gap = round(expected_strikeouts - suggested_line, 2)
        if line_gap >= 0.6:
            suggested_bet = f"Over {suggested_line:.1f}"
        elif line_gap <= -0.35:
            suggested_bet = f"Under {suggested_line:.1f}"
        else:
            suggested_bet = f"Lean Over {suggested_line:.1f}"

        pitcher_name = normalize_text(pitcher.get("name") or pitcher.get("fullName") or pitcher_id)
        opponent_name = normalize_text(opponent_team.get("clubName") or opponent_team.get("shortName") or opponent_team.get("fullName") or opponent_team.get("location"))
        game_label = format_game_label(game)
        game_time = normalize_text(game.get("gameDate") or "")
        team_rank = None
        rankings = opponent_team.get("rankings") or []
        for ranking in rankings:
            if not isinstance(ranking, dict):
                continue
            if normalize_text(ranking.get("category")).lower() != "strikeouts":
                continue
            if normalize_text(ranking.get("split")) == split_name:
                team_rank = ranking.get("rank")
                break

        return {
            "player": pitcher_name,
            "market": "Expected Strikeouts",
            "league": "MLB",
            "line": None,
            "derived_line": suggested_line,
            "odds": None,
            "score": expected_strikeouts,
            "game": game_label,
            "time": game_time,
            "notes": f"Suggested bet: {suggested_bet}; projected={expected_strikeouts:.2f}; edge={line_gap:+.2f}; vs {opponent_name}; hand={pitching_type or 'UNK'}; k/9={k_per_9:.2f}; k%={k_pct:.1f}; opp split={split_name}; opp rank={team_rank}; opp K value={opponent_value}",
            "raw": season_row,
        }

    for game in game_slate:
        home_team = game.get("homeTeam") or {}
        visitor_team = game.get("visitorTeam") or {}
        home_record = build_record(game.get("homePitcherId"), home_team, visitor_team, game)
        visitor_record = build_record(game.get("visitorPitcherId"), visitor_team, home_team, game)
        if home_record:
            strikeout_records.append(home_record)
        if visitor_record:
            strikeout_records.append(visitor_record)

    strikeout_records.sort(key=lambda item: item["score"], reverse=True)
    return strikeout_records[: max(1, html_limit)]


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def normalize_split_label(value: Any) -> str:
    return " ".join(normalize_text(value).lower().replace("/", " ").replace("-", " ").replace("—", " ").split())


def split_matches_preference(split_value: Any, preferred_splits: Iterable[str]) -> bool:
    normalized_split = normalize_split_label(split_value)
    if not normalized_split:
        return False
    if normalized_split == "season":
        return any(normalize_split_label(preferred) == "season" for preferred in preferred_splits)

    for preferred in preferred_splits:
        normalized_preferred = normalize_split_label(preferred)
        if not normalized_preferred:
            continue
        if normalized_split == normalized_preferred or normalized_preferred in normalized_split or normalized_split in normalized_preferred:
            return True

    if any(normalize_split_label(preferred) != "season" for preferred in preferred_splits):
        if "recent" in normalized_split or ("start" in normalized_split and "3" in normalized_split) or ("day" in normalized_split and "15" in normalized_split):
            return True
    return False


def get_team_metric_for_splits(team: Dict[str, Any], category_name: str, preferred_splits: Iterable[str]) -> Optional[float]:
    rankings = team.get("rankings") or []
    if not isinstance(rankings, list):
        return None

    season_fallback = None
    recent_fallback = None
    category_name = category_name.lower()
    for ranking in rankings:
        if not isinstance(ranking, dict):
            continue
        category = normalize_text(ranking.get("category")).lower()
        if category != category_name:
            continue
        split = ranking.get("split")
        value = parse_number(ranking.get("value"))
        if value is None:
            continue
        if split_matches_preference(split, preferred_splits):
            return value
        if normalize_split_label(split) == "season":
            season_fallback = value
        elif recent_fallback is None and split_matches_preference(split, RECENT_NRFI_SPLITS):
            recent_fallback = value
    return season_fallback if season_fallback is not None else recent_fallback


def get_pitcher_era_for_splits(pitcher: Dict[str, Any], split_payload: Dict[str, Any], preferred_splits: Iterable[str]) -> Optional[float]:
    rows = split_payload.get("rows") if isinstance(split_payload, dict) else None
    selected_row = None
    season_row = None
    recent_row = None
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            split = row.get("split")
            if split_matches_preference(split, preferred_splits):
                selected_row = row
                break
            if normalize_split_label(split) == "season" and season_row is None:
                season_row = row
            elif recent_row is None and split_matches_preference(split, RECENT_NRFI_SPLITS):
                recent_row = row

    row = selected_row or season_row or recent_row
    if isinstance(row, dict):
        for key in ["era", "xera", "fip"]:
            value = parse_number(row.get(key))
            if value is not None and value > 0:
                return value

    return get_pitcher_era(pitcher, split_payload)


def calculate_nrfi_probability(
    home_team: Dict[str, Any],
    visitor_team: Dict[str, Any],
    home_pitcher_era: Optional[float],
    visitor_pitcher_era: Optional[float],
    run_baseline: float,
    preferred_splits: Iterable[str],
) -> tuple[float, float, float, float]:
    run_values = []
    for team in [home_team, visitor_team]:
        if not isinstance(team, dict):
            continue
        run_value = get_team_metric_for_splits(team, "runs", preferred_splits)
        if run_value is not None and run_value > 0:
            run_values.append(run_value)
    average_runs = (sum(run_values) / len(run_values)) if run_values else 4.35

    pitcher_eras = [value for value in [home_pitcher_era, visitor_pitcher_era] if value is not None and value > 0]
    average_era = (sum(pitcher_eras) / len(pitcher_eras)) if pitcher_eras else 4.20
    pitcher_factor = clamp(4.20 / average_era, 0.55, 1.65)
    offense_factor = clamp(average_runs / run_baseline, 0.60, 1.55) if run_baseline > 0 else 1.0
    expected_first_inning_runs = 0.66 * (offense_factor / pitcher_factor) ** 1.2
    nrfi_probability = round(math.exp(-expected_first_inning_runs) * 100.0, 2)
    return nrfi_probability, offense_factor, pitcher_factor, average_runs


def blend_recent_with_season(season_value: Optional[float], recent_value: Optional[float], recent_weight: float = 0.75) -> Optional[float]:
    if recent_value is None and season_value is None:
        return None
    if recent_value is None:
        return season_value
    if season_value is None:
        return recent_value
    return (season_value * (1.0 - recent_weight)) + (recent_value * recent_weight)


def get_pitcher_recent_workload_factor(pitcher: Dict[str, Any], game_time_value: Any) -> float:
    last_played = parse_iso_datetime(pitcher.get("lastPlayed"))
    game_time = parse_iso_datetime(game_time_value)
    if last_played is None or game_time is None:
        return 1.0

    days_since_last_played = max(0.0, (game_time - last_played).total_seconds() / 86400.0)
    if days_since_last_played <= 3.0:
        return 0.94
    if days_since_last_played <= 5.0:
        return 0.98
    if days_since_last_played <= 8.0:
        return 1.02
    return 1.05


def get_team_metric(team: Dict[str, Any], category_name: str, split_name: str = "Season") -> Optional[float]:
    rankings = team.get("rankings") or []
    if not isinstance(rankings, list):
        return None

    fallback = None
    category_name = category_name.lower()
    for ranking in rankings:
        if not isinstance(ranking, dict):
            continue
        category = normalize_text(ranking.get("category")).lower()
        if category != category_name:
            continue
        split = normalize_text(ranking.get("split"))
        value = parse_number(ranking.get("value"))
        if value is None:
            continue
        if split == split_name:
            return value
        if split == "Season":
            fallback = value
    return fallback


def get_pitcher_era(pitcher: Dict[str, Any], split_payload: Dict[str, Any]) -> Optional[float]:
    rows = split_payload.get("rows") if isinstance(split_payload, dict) else None
    if isinstance(rows, list):
        season_row = next((row for row in rows if isinstance(row, dict) and normalize_text(row.get("split")) == "Season"), None)
        if isinstance(season_row, dict):
            for key in ["era", "xera", "fip"]:
                value = parse_number(season_row.get(key))
                if value is not None and value > 0:
                    return value

    for key in ["era", "xera", "fip"]:
        value = parse_number(pitcher.get(key))
        if value is not None and value > 0:
            return value
    return None


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def mean_or_none(values: Iterable[Optional[float]]) -> Optional[float]:
    filtered = [value for value in values if value is not None]
    if not filtered:
        return None
    return sum(filtered) / len(filtered)


def factor_from_average(value: Optional[float], anchor: float, scale: float, minimum: float, maximum: float) -> float:
    if value is None:
        return 1.0
    delta = (value - anchor) / scale
    return clamp(1.0 + delta, minimum, maximum)


def select_weather_snapshot(game: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    weather_data = game.get("weatherData")
    if not isinstance(weather_data, list) or not weather_data:
        return None

    game_time = parse_iso_datetime(game.get("gameDate"))
    if game_time is None:
        for item in weather_data:
            if isinstance(item, dict):
                return item
        return None

    game_epoch = game_time.timestamp()
    best_item = None
    best_distance = None
    for item in weather_data:
        if not isinstance(item, dict):
            continue
        epoch = parse_number(item.get("dateTimeEpoch"))
        if epoch is None:
            continue
        distance = abs(epoch - game_epoch)
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_item = item
    if best_item is not None:
        return best_item
    for item in weather_data:
        if isinstance(item, dict):
            return item
    return None


def get_ballpark_factor(game: Dict[str, Any], park_payload: Any = None) -> float:
    park = game.get("ballpark") if isinstance(game.get("ballpark"), dict) else {}
    matched_park = None
    if isinstance(park_payload, list):
        park_id = park.get("id")
        park_name = normalize_text(park.get("name"))
        home_team = game.get("homeTeam") if isinstance(game.get("homeTeam"), dict) else {}
        park_team_code = normalize_text(home_team.get("code"))
        for item in park_payload:
            if not isinstance(item, dict):
                continue
            if park_id is not None and parse_number(item.get("ballparkId")) == parse_number(park_id):
                matched_park = item
                break
            if park_name and normalize_text(item.get("ballparkName")) == park_name:
                matched_park = item
                break
            if park_team_code and normalize_text(item.get("teamCode")).upper() == park_team_code.upper():
                matched_park = item
                break

    values: List[float] = []
    for source in [park, matched_park or {}]:
        if not isinstance(source, dict):
            continue
        for key in ["parkFactor", "hrFactor", "barrelFactor", "distanceFactor", "hitFactor", "hardHitFactor", "bbFactor", "kFactor"]:
            value = parse_number(source.get(key))
            if value is not None and value > 0:
                values.append(value)

    return clamp((sum(values) / len(values)) if values else 1.0, 0.78, 1.28)


def get_weather_factor(game: Dict[str, Any]) -> float:
    snapshot = select_weather_snapshot(game)
    if not isinstance(snapshot, dict):
        return 1.0

    ballpark = game.get("ballpark") if isinstance(game.get("ballpark"), dict) else {}
    roof_type = normalize_text(ballpark.get("roofType")).lower()
    if any(keyword in roof_type for keyword in ["dome", "closed", "retractable"]):
        return 1.0

    temp = parse_number(snapshot.get("feelsLike")) or parse_number(snapshot.get("temp"))
    humidity = parse_number(snapshot.get("humidity"))
    wind_speed = parse_number(snapshot.get("windSpeed"))
    precip_prob = parse_number(snapshot.get("precipProb"))
    severe_risk = parse_number(snapshot.get("severeRisk"))

    factor = 1.0
    if temp is not None:
        factor += max(0.0, temp - 78.0) / 180.0
    if humidity is not None:
        factor += max(0.0, humidity - 60.0) / 500.0
    if wind_speed is not None:
        factor += max(0.0, wind_speed - 4.0) / 250.0
    if precip_prob is not None:
        factor += max(0.0, precip_prob - 5.0) / 900.0
    if severe_risk is not None:
        factor += max(0.0, severe_risk - 40.0) / 1200.0
    return clamp(factor, 0.84, 1.22)


def get_game_total_factor(game: Dict[str, Any]) -> float:
    game_total = parse_number(game.get("gameRunLine"))
    if game_total is None or game_total <= 0:
        return 1.0
    return clamp(1.0 + ((8.0 - game_total) * 0.05), 0.80, 1.22)


def get_matchup_pressure_factor(payload_bundle: Dict[str, Any], game_label: str) -> float:
    matchup_scores: List[float] = []
    matchups = payload_bundle.get("matchups") if isinstance(payload_bundle, dict) else None
    if isinstance(matchups, list):
        for matchup_item in matchups:
            if not isinstance(matchup_item, dict):
                continue
            if game_label and normalize_text(matchup_item.get("game_label")) != game_label:
                continue
            matchup_payload = matchup_item.get("payload")
            if not isinstance(matchup_payload, dict):
                continue
            records = extract_matchup_records(matchup_payload, "MLB", "nrfi_environment")
            top_scores = [parse_number(record.get("score")) for record in records[:5]]
            top_average = mean_or_none(top_scores)
            if top_average is not None:
                matchup_scores.append(top_average)
    average_score = mean_or_none(matchup_scores)
    if average_score is None:
        return 1.0
    return factor_from_average(average_score, 95.0, 460.0, 0.82, 1.24)


def get_zone_pressure_factor(payload_bundle: Dict[str, Any]) -> float:
    zone_payload = payload_bundle.get("zone") if isinstance(payload_bundle, dict) else None
    if not isinstance(zone_payload, dict):
        return 1.0

    rows = zone_payload.get("rows")
    if not isinstance(rows, list):
        return 1.0

    values: List[float] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_values = [
            parse_number(row.get("contactScore")),
            parse_number(row.get("barrelScore")),
            parse_number(row.get("hrScore")),
            parse_number(row.get("hardHitScore")),
            parse_number(row.get("zonePowerScore")),
        ]
        row_average = mean_or_none(row_values)
        if row_average is not None:
            values.append(row_average)

    average_score = mean_or_none(values)
    if average_score is None:
        return 1.0
    return factor_from_average(average_score, 35.0, 230.0, 0.84, 1.20)


def get_hr_risk_factor(payload_bundle: Dict[str, Any], pitcher_ids: Iterable[Any]) -> float:
    risk_payload = payload_bundle.get("hr_risk") if isinstance(payload_bundle, dict) else None
    if not isinstance(risk_payload, dict):
        return 1.0

    scores: List[float] = []
    for pitcher_id in pitcher_ids:
        pitcher_record = risk_payload.get(str(pitcher_id))
        if not isinstance(pitcher_record, dict):
            continue
        pitcher_scores = [
            parse_number(pitcher_record.get("scoreOverall")),
            parse_number(pitcher_record.get("scoreVsLhb")),
            parse_number(pitcher_record.get("scoreVsRhb")),
        ]
        pitcher_average = mean_or_none(pitcher_scores)
        if pitcher_average is not None:
            scores.append(pitcher_average)

    average_score = mean_or_none(scores)
    if average_score is None:
        return 1.0
    return clamp(1.0 - (average_score * 0.16), 0.80, 1.20)


def get_nrfi_environment_factor(game: Dict[str, Any], payload_bundle: Dict[str, Any], pitcher_ids: Iterable[Any]) -> float:
    ballpark_factor = get_ballpark_factor(game, payload_bundle.get("park") if isinstance(payload_bundle, dict) else None)
    weather_factor = get_weather_factor(game)
    total_factor = get_game_total_factor(game)
    matchup_factor = get_matchup_pressure_factor(payload_bundle, format_game_label(game))
    zone_factor = get_zone_pressure_factor(payload_bundle)
    hr_risk_factor = get_hr_risk_factor(payload_bundle, pitcher_ids)

    weighted_total = (
        (ballpark_factor * 2.4) +
        (weather_factor * 1.4) +
        (total_factor * 1.8) +
        (matchup_factor * 2.2) +
        (zone_factor * 1.2) +
        (hr_risk_factor * 2.0)
    )
    return clamp(weighted_total / 11.0, 0.72, 1.32)


def get_risk_profile_score(payload_bundle: Dict[str, Any], pitcher_ids: Iterable[Any]) -> Optional[float]:
    risk_payload = payload_bundle.get("hr_risk") if isinstance(payload_bundle, dict) else None
    if not isinstance(risk_payload, dict):
        return None
    scores: List[float] = []
    for pitcher_id in pitcher_ids:
        pitcher_record = risk_payload.get(str(pitcher_id))
        if isinstance(pitcher_record, dict):
            pitcher_score = mean_or_none([
                parse_number(pitcher_record.get("scoreOverall")),
                parse_number(pitcher_record.get("scoreVsLhb")),
                parse_number(pitcher_record.get("scoreVsRhb")),
            ])
            if pitcher_score is not None:
                scores.append(pitcher_score)
    return mean_or_none(scores)


def get_pitcher_display_name(
    pitcher_id: Any,
    pitcher_lookup: Dict[str, Dict[str, Any]],
    game: Dict[str, Any],
    game_key_candidates: List[str],
) -> str:
    pitcher = pitcher_lookup.get(str(pitcher_id)) or {}
    if isinstance(pitcher, dict):
        name = normalize_text(
            pitcher.get("name")
            or pitcher.get("fullName")
            or pitcher.get("pitcherName")
            or pitcher.get("displayName")
        )
        if name:
            return name

    for key in game_key_candidates:
        nested = game.get(key)
        if not isinstance(nested, dict):
            continue
        name = normalize_text(
            nested.get("name")
            or nested.get("fullName")
            or nested.get("pitcherName")
            or nested.get("displayName")
        )
        if name:
            return name

    if pitcher_id is None:
        return ""
    return normalize_text(pitcher_id)


def build_team_total_expected_runs_records(
    game_slate: List[Dict[str, Any]],
    pitcher_lookup: Dict[str, Dict[str, Any]],
    pitcher_splits: Dict[str, Dict[str, Any]],
    html_limit: int = 50,
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if not game_slate:
        return records

    # Build slate-level baselines from team run values when available.
    run_values = []
    for game in game_slate:
        for team_key in ["homeTeam", "visitorTeam"]:
            team = game.get(team_key) or {}
            if not isinstance(team, dict):
                continue
            run_value = get_team_metric(team, "runs")
            if run_value is not None and run_value > 0:
                run_values.append(run_value)
    run_baseline = (sum(run_values) / len(run_values)) if run_values else 4.35

    def build_record(offense_team: Dict[str, Any], pitcher_id: Any, game: Dict[str, Any], side_label: str) -> Optional[Dict[str, Any]]:
        if not isinstance(offense_team, dict):
            return None

        offense_name = normalize_text(
            offense_team.get("clubName")
            or offense_team.get("shortName")
            or offense_team.get("fullName")
            or offense_team.get("location")
            or side_label
        )
        if not offense_name:
            return None

        pitcher = pitcher_lookup.get(str(pitcher_id)) or {}
        split_payload = pitcher_splits.get(str(pitcher_id)) or {}
        pitcher_name = normalize_text(pitcher.get("name") or pitcher.get("fullName") or pitcher_id)

        offense_runs_value = get_team_metric(offense_team, "runs")
        offense_factor = 1.0
        if offense_runs_value is not None and run_baseline > 0:
            offense_factor = clamp(offense_runs_value / run_baseline, 0.75, 1.3)

        pitcher_era = get_pitcher_era(pitcher, split_payload)
        pitcher_factor = 1.0
        if pitcher_era is not None:
            pitcher_factor = clamp(pitcher_era / 4.20, 0.70, 1.45)

        baseline_runs = 4.35
        expected_runs = round(baseline_runs * offense_factor * pitcher_factor, 2)
        suggested_line = max(2.5, min(6.5, round(expected_runs) - 0.5))
        edge = round(expected_runs - suggested_line, 2)

        if edge >= 0.5:
            suggested_bet = f"Over {suggested_line:.1f}"
        elif edge <= -0.4:
            suggested_bet = f"Under {suggested_line:.1f}"
        else:
            suggested_bet = f"Lean Over {suggested_line:.1f}"

        game_label = format_game_label(game)
        game_time = normalize_text(game.get("gameDate") or "")

        return {
            "player": offense_name,
            "market": "Team Total Expected Runs",
            "league": "MLB",
            "line": None,
            "derived_line": suggested_line,
            "odds": None,
            "score": expected_runs,
            "game": game_label,
            "time": game_time,
            "notes": (
                f"Suggested bet: {suggested_bet}; projected={expected_runs:.2f}; edge={edge:+.2f}; "
                f"offense runs value={offense_runs_value}; opp pitcher={pitcher_name}; opp ERA proxy={pitcher_era}"
            ),
            "raw": {
                "offense_team": offense_team,
                "opposing_pitcher": pitcher,
                "pitcher_split": split_payload,
            },
        }

    for game in game_slate:
        home_team = game.get("homeTeam") or {}
        visitor_team = game.get("visitorTeam") or {}
        home_pitcher_id = game.get("homePitcherId")
        visitor_pitcher_id = game.get("visitorPitcherId")

        away_record = build_record(visitor_team, home_pitcher_id, game, "Away")
        home_record = build_record(home_team, visitor_pitcher_id, game, "Home")
        if away_record:
            records.append(away_record)
        if home_record:
            records.append(home_record)

    records.sort(key=lambda item: item["score"], reverse=True)
    return records[: max(1, html_limit)]


def build_nrfi_probability_records(
    game_slate: List[Dict[str, Any]],
    pitcher_lookup: Dict[str, Dict[str, Any]],
    pitcher_splits: Dict[str, Dict[str, Any]],
    payload_bundle: Optional[Dict[str, Any]] = None,
    html_limit: int = 50,
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if not game_slate:
        return records
    payload_bundle = payload_bundle or {}

    run_values = []
    for game in game_slate:
        for team_key in ["homeTeam", "visitorTeam"]:
            team = game.get(team_key) or {}
            if not isinstance(team, dict):
                continue
            run_value = get_team_metric(team, "runs")
            if run_value is not None and run_value > 0:
                run_values.append(run_value)
    run_baseline = (sum(run_values) / len(run_values)) if run_values else 4.35

    for game in game_slate:
        home_team = game.get("homeTeam") or {}
        visitor_team = game.get("visitorTeam") or {}
        if not isinstance(home_team, dict) or not isinstance(visitor_team, dict):
            continue

        game_label = format_game_label(game)
        if not game_label:
            continue

        home_pitcher = pitcher_lookup.get(str(game.get("homePitcherId"))) or {}
        visitor_pitcher = pitcher_lookup.get(str(game.get("visitorPitcherId"))) or {}
        home_split_payload = pitcher_splits.get(str(game.get("homePitcherId"))) or {}
        visitor_split_payload = pitcher_splits.get(str(game.get("visitorPitcherId"))) or {}
        pitcher_ids = [game.get("homePitcherId"), game.get("visitorPitcherId")]
        environment_factor = get_nrfi_environment_factor(game, payload_bundle, pitcher_ids)

        home_season_era = get_pitcher_era_for_splits(home_pitcher, home_split_payload, ["Season"])
        visitor_season_era = get_pitcher_era_for_splits(visitor_pitcher, visitor_split_payload, ["Season"])
        nrfi_probability, offense_factor, pitcher_factor, average_runs = calculate_nrfi_probability(
            home_team,
            visitor_team,
            home_season_era,
            visitor_season_era,
            run_baseline,
            ["Season"],
        )
        nrfi_probability = round(clamp(nrfi_probability * environment_factor, 0.0, 99.5), 2)

        home_recent_era = get_pitcher_era_for_splits(home_pitcher, home_split_payload, RECENT_NRFI_SPLITS)
        visitor_recent_era = get_pitcher_era_for_splits(visitor_pitcher, visitor_split_payload, RECENT_NRFI_SPLITS)
        home_recent_blend = blend_recent_with_season(home_season_era, home_recent_era, 0.80)
        visitor_recent_blend = blend_recent_with_season(visitor_season_era, visitor_recent_era, 0.80)
        recent_probability, recent_offense_factor, recent_pitcher_factor, recent_average_runs = calculate_nrfi_probability(
            home_team,
            visitor_team,
            home_recent_blend,
            visitor_recent_blend,
            run_baseline,
            RECENT_NRFI_SPLITS,
        )
        home_recent_workload_factor = get_pitcher_recent_workload_factor(home_pitcher, game.get("gameDate"))
        visitor_recent_workload_factor = get_pitcher_recent_workload_factor(visitor_pitcher, game.get("gameDate"))
        recent_workload_factor = clamp((home_recent_workload_factor + visitor_recent_workload_factor) / 2.0, 0.90, 1.08)
        recent_probability = round(clamp(recent_probability * environment_factor * recent_workload_factor, 0.0, 99.5), 2)

        hr_risk_score = get_risk_profile_score(payload_bundle, pitcher_ids)
        hr_risk_text = f"{hr_risk_score:.3f}" if hr_risk_score is not None else "n/a"

        home_name = normalize_text(home_team.get("clubName") or home_team.get("shortName") or home_team.get("fullName") or home_team.get("location"))
        visitor_name = normalize_text(visitor_team.get("clubName") or visitor_team.get("shortName") or visitor_team.get("fullName") or visitor_team.get("location"))
        home_pitcher_name = get_pitcher_display_name(
            game.get("homePitcherId"),
            pitcher_lookup,
            game,
            ["homePitcher", "homeStartingPitcher", "homeProbablePitcher"],
        )
        visitor_pitcher_name = get_pitcher_display_name(
            game.get("visitorPitcherId"),
            pitcher_lookup,
            game,
            ["visitorPitcher", "visitorStartingPitcher", "visitorProbablePitcher"],
        )
        records.append({
            "player": game_label,
            "market": "NRFI Probability",
            "league": "MLB",
            "line": None,
            "derived_line": None,
            "odds": None,
            "score": nrfi_probability,
            "recent_probability": recent_probability,
            "game": game_label,
            "time": normalize_text(game.get("gameDate") or ""),
            "starting_pitchers": f"{visitor_pitcher_name} @ {home_pitcher_name}" if visitor_pitcher_name or home_pitcher_name else "",
            "notes": (
                f"Projected NRFI probability: {nrfi_probability:.2f}%; base lambda=0.66; "
                f"recent probability={recent_probability:.2f}%; recent offense factor={recent_offense_factor:.2f}; "
                f"recent pitcher factor={recent_pitcher_factor:.2f}; offense factor={offense_factor:.2f}; pitcher factor={pitcher_factor:.2f}; "
                f"avg runs={average_runs:.2f}; recent avg runs={recent_average_runs:.2f}; "
                f"environment factor={environment_factor:.3f}; recent workload factor={recent_workload_factor:.3f}; hr-risk score={hr_risk_text}; home={home_name}; away={visitor_name}"
            ),
            "raw": {
                "home_team": home_team,
                "visitor_team": visitor_team,
                "home_pitcher": home_pitcher,
                "visitor_pitcher": visitor_pitcher,
                "home_pitcher_split": home_split_payload,
                "visitor_pitcher_split": visitor_split_payload,
                "environment_factor": environment_factor,
                "recent_workload_factor": recent_workload_factor,
                "hr_risk_score": hr_risk_score,
            },
        })

    records.sort(key=lambda item: item["score"], reverse=True)
    return records[: max(1, html_limit)]


def build_yrfi_probability_records(
    game_slate: List[Dict[str, Any]],
    pitcher_lookup: Dict[str, Dict[str, Any]],
    pitcher_splits: Dict[str, Dict[str, Any]],
    payload_bundle: Optional[Dict[str, Any]] = None,
    html_limit: int = 50,
) -> List[Dict[str, Any]]:
    # YRFI is the complement of NRFI: P(run scored) = 100 - P(no run scored).
    nrfi_records = build_nrfi_probability_records(game_slate, pitcher_lookup, pitcher_splits, payload_bundle, html_limit=len(game_slate) or 1)
    records: List[Dict[str, Any]] = []
    for nrfi_record in nrfi_records:
        yrfi_probability = round(100.0 - nrfi_record["score"], 2)
        recent_nrfi_probability = nrfi_record.get("recent_probability")
        recent_yrfi_probability = round(100.0 - recent_nrfi_probability, 2) if recent_nrfi_probability is not None else None

        record = dict(nrfi_record)
        record["market"] = "YRFI Probability"
        record["score"] = yrfi_probability
        record["recent_probability"] = recent_yrfi_probability
        record["notes"] = re.sub(
            r"^Projected NRFI probability: [\d.]+%; base lambda=0\.66; recent probability=[\d.]+%;",
            f"Projected YRFI probability: {yrfi_probability:.2f}%; base lambda=0.66; "
            f"recent YRFI probability={(recent_yrfi_probability if recent_yrfi_probability is not None else 0.0):.2f}%;",
            nrfi_record["notes"],
        )
        records.append(record)

    records.sort(key=lambda item: item["score"], reverse=True)
    return records[: max(1, html_limit)]


def ordinal_label(value: int) -> str:
    if 10 <= (value % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def build_batting_order_lookup(
    game_slate: List[Dict[str, Any]],
    players_directory: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    players_by_id: Dict[Any, Dict[str, Any]] = {}
    for player in players_directory or []:
        if isinstance(player, dict) and player.get("id") is not None:
            players_by_id[player["id"]] = player

    lookup: Dict[str, Dict[str, Any]] = {}
    for game in game_slate or []:
        if not isinstance(game, dict):
            continue
        for order_key, side in [("homeBattingOrder", "home"), ("visitorBattingOrder", "away")]:
            order_value = game.get(order_key)
            if not order_value:
                continue
            for slot, id_text in enumerate(str(order_value).split(","), start=1):
                id_text = id_text.strip()
                if not id_text:
                    continue
                try:
                    player_id = int(id_text)
                except ValueError:
                    continue
                player = players_by_id.get(player_id)
                if not isinstance(player, dict):
                    continue
                name_key = normalize_text(player.get("fullName") or player.get("name")).lower()
                if not name_key:
                    continue
                lookup[name_key] = {"battingOrder": slot, "side": side}
    return lookup


def annotate_batting_order_records(
    records: List[Dict[str, Any]],
    batting_order_lookup: Dict[str, Dict[str, Any]],
    lineups_available: bool,
) -> List[Dict[str, Any]]:
    annotated: List[Dict[str, Any]] = []
    for record in records:
        item = dict(record)
        name_key = normalize_text(item.get("player")).lower()
        info = batting_order_lookup.get(name_key)
        if info is not None:
            item["batting_order"] = info["battingOrder"]
            item["batting_order_label"] = ordinal_label(info["battingOrder"])
        elif lineups_available:
            item["batting_order"] = None
            item["batting_order_label"] = "Not in lineup"
        else:
            item["batting_order"] = None
            item["batting_order_label"] = "Lineup TBD"
        annotated.append(item)
    return annotated


def build_html_report_record_sets(records: List[Dict[str, Any]], html_limit: int = 50) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    html_limit = max(1, html_limit)
    home_run_records = records[:html_limit]
    secondary_records = build_hits_runs_rbis_records(records)[:html_limit]
    return home_run_records, secondary_records


def write_text_report(records: List[Dict[str, Any]], output_path: str, title: str = "Most likely MLB home run props") -> None:
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(title + "\n")
        handle.write("=" * 40 + "\n")
        handle.write(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
        handle.write(f"Count: {len(records)}\n\n")
        for index, record in enumerate(records, start=1):
            handle.write(f"{index}. {record['player']}\n")
            handle.write(f"   Market: {record['market']}\n")
            handle.write(f"   Score: {record['score']:.2f}\n")
            handle.write(f"   Line: {record['line']}\n")
            handle.write(f"   Odds: {record['odds']}\n")
            if record["game"]:
                handle.write(f"   Game: {record['game']}\n")
            if record["time"]:
                handle.write(f"   Time: {record['time']}\n")
            if record["notes"]:
                handle.write(f"   Notes: {record['notes']}\n")
            handle.write("\n")


def write_html_report(
    home_run_records: List[Dict[str, Any]],
    secondary_records: List[Dict[str, Any]],
    output_path: str,
    pitcher_strikeout_records: Optional[List[Dict[str, Any]]] = None,
    team_total_records: Optional[List[Dict[str, Any]]] = None,
    nrfi_probability_records: Optional[List[Dict[str, Any]]] = None,
    yrfi_probability_records: Optional[List[Dict[str, Any]]] = None,
) -> None:
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    generated_at = format_report_timestamp(datetime.now(timezone.utc))["display_time"]
    pitcher_strikeout_records = pitcher_strikeout_records or []
    team_total_records = team_total_records or []
    nrfi_probability_records = nrfi_probability_records or []
    yrfi_probability_records = yrfi_probability_records or []
    def normalize_notes(value: Any) -> str:
        notes_text = str(value or "")
        notes_text = notes_text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
        notes_text = notes_text.replace("\r\n", "\n").replace("\r", "\n")
        return notes_text

    def serialize_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        serialized = []
        for record in records:
            time_parts = format_report_timestamp(record.get("time"))
            serialized.append({
                "player": normalize_text(record.get("player")),
                "market": normalize_text(record.get("market")),
                "score": round(parse_number(record.get("score")) or 0.0, 2),
                "line": record.get("line"),
                "derivedLine": record.get("derived_line"),
                "odds": record.get("odds"),
                "game": normalize_text(record.get("game")),
                "time": time_parts["display_time"],
                "day": time_parts["display_day"],
                "dayKey": time_parts["day_key"],
                "timeSort": time_parts["sort_time"],
                "startingPitchers": normalize_text(record.get("starting_pitchers")),
                "recentProbability": round(parse_number(record.get("recent_probability")) or 0.0, 2) if record.get("recent_probability") is not None else None,
                "battingOrder": record.get("batting_order"),
                "battingOrderLabel": normalize_text(record.get("batting_order_label")),
                "notes": normalize_notes(record.get("notes")),
            })
        return serialized

    def score_band(score: float, ranges: List[Dict[str, Any]]) -> str:
        for index, item in enumerate(ranges):
            bounds = normalize_text(item.get("range"))
            if bounds.endswith("+"):
                threshold = parse_number(bounds[:-1]) or 0.0
                if score >= threshold:
                    return f"band-{index + 1}"
            elif "-" in bounds:
                left, right = bounds.split("-", 1)
                minimum = parse_number(left)
                maximum = parse_number(right)
                if minimum is not None and maximum is not None and minimum <= score <= maximum:
                    return f"band-{index + 1}"
            elif bounds.lower().startswith("below"):
                threshold = parse_number(bounds.split()[-1])
                if threshold is not None and score < threshold:
                    return f"band-{index + 1}"
        return "band-default"

    def with_score_bands(records: List[Dict[str, Any]], ranges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        enhanced = []
        for record in records:
            item = dict(record)
            score_value = parse_number(item.get("score")) or 0.0
            item["scoreBand"] = score_band(score_value, ranges)
            enhanced.append(item)
        return enhanced

    report_data = {
        "generatedAt": generated_at,
        "datasets": {
            "homeRuns": {
                "label": "Home Run Props",
                "showDerivedLine": False,
                "showBattingOrder": True,
                "lineLabel": "Book Line",
                "scoreGuide": {
                    "title": "Score guide",
                    "description": "Home run scores stack recent barrels, near-homers, distance and power contact. Higher is better.",
                    "ranges": [
                        {"range": "140+", "label": "Elite target", "detail": "Build around these first."},
                        {"range": "100-139", "label": "Strong target", "detail": "Quality primary looks."},
                        {"range": "70-99", "label": "Secondary target", "detail": "Use selectively by matchup."},
                        {"range": "Below 70", "label": "Fringe", "detail": "Usually pass unless the price is exceptional."},
                    ],
                },
                "records": with_score_bands(serialize_records(home_run_records), [
                    {"range": "140+"},
                    {"range": "100-139"},
                    {"range": "70-99"},
                    {"range": "Below 70"},
                ]),
            },
            "hitsRunsRbis": {
                "label": "Hits + Runs + RBI Props",
                "showDerivedLine": True,
                "showBattingOrder": True,
                "lineLabel": "0.5 Prop",
                "derivedLineLabel": "1.5 Prop",
                "scoreGuide": {
                    "title": "Score guide",
                    "description": "This score weights recent hits plus AVG, OBP, SLG and wOBA to flag which combined-production ladder is safest.",
                    "ranges": [
                        {"range": "95+", "label": "Attack 1.5", "detail": "These are the best candidates to play the 1.5 ladder, with 0.5 as the safer fallback."},
                        {"range": "75-94", "label": "0.5 target", "detail": "Prioritize 0.5 and only lean 1.5 when the matchup is clean."},
                        {"range": "55-74", "label": "Conservative 0.5", "detail": "Playable only at the lower 0.5 threshold."},
                        {"range": "Below 55", "label": "Pass range", "detail": "Usually too weak to force unless you have an external angle."},
                    ],
                },
                "records": with_score_bands(serialize_records(secondary_records), [
                    {"range": "95+"},
                    {"range": "75-94"},
                    {"range": "55-74"},
                    {"range": "Below 55"},
                ]),
            },
            "pitcherStrikeouts": {
                "label": "Pitcher Expected Strikeouts",
                "showDerivedLine": True,
                "lineLabel": "Book Line",
                "derivedLineLabel": "Derived Line",
                "scoreGuide": {
                    "title": "Score guide",
                    "description": "Pitcher strikeout scores approximate expected Ks from season K/9, strikeout rate and opponent split tendencies.",
                    "ranges": [
                        {"range": "7.5+", "label": "Strong over target", "detail": "Best ceiling and cleanest over cases."},
                        {"range": "6.0-7.49", "label": "Playable over range", "detail": "Viable when the derived line still leaves edge."},
                        {"range": "5.0-5.99", "label": "Thin edge", "detail": "More matchup-dependent; shop carefully."},
                        {"range": "Below 5.0", "label": "Caution", "detail": "Usually not worth forcing."},
                    ],
                },
                "records": with_score_bands(serialize_records(pitcher_strikeout_records), [
                    {"range": "7.5+"},
                    {"range": "6.0-7.49"},
                    {"range": "5.0-5.99"},
                    {"range": "Below 5.0"},
                ]),
            },
            "teamTotals": {
                "label": "Team Total Expected Runs",
                "showDerivedLine": True,
                "lineLabel": "Book Line",
                "derivedLineLabel": "Derived Line",
                "scoreGuide": {
                    "title": "Score guide",
                    "description": "Team total scores estimate expected runs using offense run profile vs opposing starter run prevention.",
                    "ranges": [
                        {"range": "5.0+", "label": "Strong over target", "detail": "Top environments to back team total overs."},
                        {"range": "4.3-4.99", "label": "Playable over range", "detail": "Good setups if market line leaves room."},
                        {"range": "3.7-4.29", "label": "Thin edge", "detail": "Smaller margin; shop prices and weather context."},
                        {"range": "Below 3.7", "label": "Caution", "detail": "Usually pass or consider unders only with confirmation."},
                    ],
                },
                "records": with_score_bands(serialize_records(team_total_records), [
                    {"range": "5.0+"},
                    {"range": "4.3-4.99"},
                    {"range": "3.7-4.29"},
                    {"range": "Below 3.7"},
                ]),
            },
            "nrfiProbability": {
                "label": "NRFI Probability",
                "showDerivedLine": False,
                "lineLabel": "Book Line",
                "scoreGuide": {
                    "title": "Probability guide",
                    "description": "This projection blends both starters and both offenses to estimate the chance of a scoreless first inning.",
                    "ranges": [
                        {"range": "65+", "label": "Top NRFI target", "detail": "Best environments for a scoreless first inning."},
                        {"range": "58-64.99", "label": "Playable NRFI range", "detail": "Reasonable NRFI looks with a stable edge."},
                        {"range": "50-57.99", "label": "Toss-up range", "detail": "Use only when the matchup and price line up."},
                        {"range": "Below 50", "label": "Avoid", "detail": "Usually too risky for a NRFI play."},
                    ],
                },
                "records": with_score_bands(serialize_records(nrfi_probability_records), [
                    {"range": "65+"},
                    {"range": "58-64.99"},
                    {"range": "50-57.99"},
                    {"range": "Below 50"},
                ]),
            },
            "yrfiProbability": {
                "label": "YRFI Probability",
                "showDerivedLine": False,
                "lineLabel": "Book Line",
                "scoreGuide": {
                    "title": "Probability guide",
                    "description": "This projection blends both starters and both offenses to estimate the chance of at least one run scoring in the first inning.",
                    "ranges": [
                        {"range": "50+", "label": "Top YRFI target", "detail": "Best environments for a first-inning run."},
                        {"range": "42-49.99", "label": "Playable YRFI range", "detail": "Reasonable YRFI looks with a stable edge."},
                        {"range": "35-41.99", "label": "Toss-up range", "detail": "Use only when the matchup and price line up."},
                        {"range": "Below 35", "label": "Avoid", "detail": "Usually too risky for a YRFI play."},
                    ],
                },
                "records": with_score_bands(serialize_records(yrfi_probability_records), [
                    {"range": "50+"},
                    {"range": "42-49.99"},
                    {"range": "35-41.99"},
                    {"range": "Below 35"},
                ]),
            },
        },
    }

    app_script_path = os.path.splitext(output_path)[0] + ".app.js"

    app_script = """
const reportData = JSON.parse(document.getElementById('report-data').textContent);
const datasetOrder = ['homeRuns', 'hitsRunsRbis', 'teamTotals', 'nrfiProbability', 'yrfiProbability', 'pitcherStrikeouts'];
const h = React.createElement;

function formatValue(value) {
    if (value === null || value === undefined || value === '') {
        return '';
    }
    return String(value);
}

function compareValues(left, right, direction) {
    if (left === right) {
        return 0;
    }
    if (left === null || left === undefined || left === '') {
        return 1;
    }
    if (right === null || right === undefined || right === '') {
        return -1;
    }
    if (typeof left === 'number' && typeof right === 'number') {
        return direction === 'asc' ? left - right : right - left;
    }
    return direction === 'asc' ? String(left).localeCompare(String(right)) : String(right).localeCompare(String(left));
}

function metaCard(value, label) {
    return h('div', { className: 'meta-card', key: label }, [
        h('strong', { key: 'value' }, value),
        h('span', { key: 'label' }, label),
    ]);
}

function scoreRangeCard(item, index) {
    return h('div', { className: `score-range-card band-${index + 1}`, key: `${item.range}-${index}` }, [
        h('strong', { key: 'range' }, item.range),
        h('span', { key: 'label', className: 'score-range-label' }, item.label),
        h('p', { key: 'detail' }, item.detail),
    ]);
}

function sortButton(sortConfig, key, label, toggleSort) {
    const rendered = sortConfig.key === key ? `${label} ${sortConfig.direction === 'asc' ? '↑' : '↓'}` : label;
    return h('button', { type: 'button', className: 'sort-button', onClick: () => toggleSort(key) }, rendered);
}

function App() {
    const [activeTab, setActiveTab] = React.useState(datasetOrder[0]);
    const [gameFilter, setGameFilter] = React.useState('');
    const [dayFilter, setDayFilter] = React.useState('');
    const [searchTerm, setSearchTerm] = React.useState('');
    const [sortConfig, setSortConfig] = React.useState({ key: 'score', direction: 'desc' });

    const dataset = reportData.datasets[activeTab];
    const scoreGuide = dataset.scoreGuide || { title: 'Score guide', description: '', ranges: [] };
    const gameOptions = React.useMemo(() => Array.from(new Set(dataset.records.map((record) => record.game).filter(Boolean))), [dataset]);
    const dayOptions = React.useMemo(() => {
        const values = [];
        const seen = new Set();
        dataset.records.forEach((record) => {
            if (!record.dayKey || seen.has(record.dayKey)) {
                return;
            }
            seen.add(record.dayKey);
            values.push({ key: record.dayKey, label: record.day || record.dayKey });
        });
        values.sort((left, right) => left.key.localeCompare(right.key));
        return values;
    }, [dataset]);
    const filteredRecords = React.useMemo(() => {
        const lowered = searchTerm.trim().toLowerCase();
        return dataset.records.filter((record) => {
            const matchesGame = !gameFilter || record.game === gameFilter;
            const matchesDay = !dayFilter || record.dayKey === dayFilter;
            if (!matchesGame || !matchesDay) {
                return false;
            }
            if (!lowered) {
                return true;
            }
            const haystack = [record.player, record.market, record.game, record.day, record.time, record.notes].map((value) => String(value || '').toLowerCase()).join(' ');
            return haystack.includes(lowered);
        });
    }, [dataset, dayFilter, gameFilter, searchTerm]);

    const sortedRecords = React.useMemo(() => {
        const items = filteredRecords.slice();
        items.sort((left, right) => compareValues(sortConfig.key === 'time' ? left.timeSort : left[sortConfig.key], sortConfig.key === 'time' ? right.timeSort : right[sortConfig.key], sortConfig.direction));
        return items;
    }, [filteredRecords, sortConfig]);

    React.useEffect(() => {
        setGameFilter('');
        setDayFilter('');
        setSearchTerm('');
        setSortConfig({ key: 'score', direction: 'desc' });
    }, [activeTab]);

    function toggleSort(key) {
        setSortConfig((current) => {
            if (current.key === key) {
                return { key, direction: current.direction === 'asc' ? 'desc' : 'asc' };
            }
            return { key, direction: key === 'player' || key === 'game' || key === 'time' || key === 'market' ? 'asc' : 'desc' };
        });
    }

    const tabButtons = datasetOrder.map((key) => h('button', {
        key,
        type: 'button',
        className: ['tab', activeTab === key ? 'active' : ''].join(' ').trim(),
        onClick: () => setActiveTab(key),
    }, reportData.datasets[key].label));
    const isProbabilityTab = activeTab === 'nrfiProbability' || activeTab === 'yrfiProbability';

    const headerCells = [
        h('th', { key: 'player' }, sortButton(sortConfig, 'player', 'Player', toggleSort)),
        h('th', { key: 'market' }, sortButton(sortConfig, 'market', 'Market', toggleSort)),
        h('th', { key: 'score' }, sortButton(sortConfig, 'score', isProbabilityTab ? 'Full Season Probability' : 'Score', toggleSort)),
    ];
    if (!isProbabilityTab) {
        headerCells.push(h('th', { key: 'line' }, sortButton(sortConfig, 'line', dataset.lineLabel || 'Book Line', toggleSort)));
    }
    if (dataset.showDerivedLine) {
        headerCells.push(h('th', { key: 'derivedLine' }, sortButton(sortConfig, 'derivedLine', dataset.derivedLineLabel || 'Derived Line', toggleSort)));
    }
    if (isProbabilityTab) {
        headerCells.push(
            h('th', { key: 'recentProbability' }, sortButton(sortConfig, 'recentProbability', 'Recent Probability (3 Starts / 15 Days)', toggleSort)),
            h('th', { key: 'startingPitchers' }, 'Starting Pitchers')
        );
    }
    if (dataset.showBattingOrder) {
        headerCells.push(h('th', { key: 'battingOrder' }, sortButton(sortConfig, 'battingOrder', 'Batting Order', toggleSort)));
    }
    headerCells.push(
        h('th', { key: 'odds' }, sortButton(sortConfig, 'odds', 'Odds', toggleSort)),
        h('th', { key: 'game' }, sortButton(sortConfig, 'game', 'Game', toggleSort)),
        h('th', { key: 'time' }, sortButton(sortConfig, 'time', 'Time', toggleSort)),
        h('th', { key: 'notes' }, 'Notes')
    );

    const bodyRows = sortedRecords.length ? sortedRecords.map((record, index) => {
        const cells = [
            h('td', { key: 'player' }, record.player),
            h('td', { key: 'market' }, record.market),
            h('td', { key: 'score' }, isProbabilityTab ? `${record.score.toFixed(2)}%` : record.score.toFixed(2)),
        ];
        if (!isProbabilityTab) {
            cells.push(h('td', { key: 'line' }, formatValue(record.line)));
        }
        if (dataset.showDerivedLine) {
            cells.push(h('td', { key: 'derivedLine' }, formatValue(record.derivedLine)));
        }
        if (isProbabilityTab) {
            cells.push(
                h('td', { key: 'recentProbability' }, formatValue(record.recentProbability)),
                h('td', { key: 'startingPitchers' }, formatValue(record.startingPitchers || record.starting_pitchers))
            );
        }
        if (dataset.showBattingOrder) {
            cells.push(h('td', { key: 'battingOrder' }, formatValue(record.battingOrderLabel)));
        }
        cells.push(
            h('td', { key: 'odds' }, formatValue(record.odds)),
            h('td', { key: 'game' }, record.game),
            h('td', { key: 'time' }, record.time),
            h('td', { key: 'notes', className: 'notes-cell' }, record.notes)
        );
        return h('tr', { key: `${record.player}-${record.game}-${index}`, className: `score-row ${record.scoreBand || 'band-default'}` }, cells);
    }) : [h('tr', { key: 'empty' }, h('td', { colSpan: headerCells.length, className: 'empty-state' }, 'No rows match the current filters.'))];

    return h('div', { className: 'page' }, [
        h('section', { key: 'hero', className: 'hero' }, [
            h('h1', { key: 'title' }, 'MLB Prop Report'),
            h('p', { key: 'subtitle' }, 'React-powered report view with one master table, searchable rows, and sortable columns across four datasets.'),
            h('div', { key: 'meta-grid', className: 'meta-grid' }, [
                metaCard(reportData.datasets.homeRuns.records.length, 'Home Run Props'),
                metaCard(reportData.datasets.hitsRunsRbis.records.length, 'Hits + Runs + RBI'),
                metaCard(reportData.datasets.teamTotals.records.length, 'Team Total Expected Runs'),
                metaCard(reportData.datasets.nrfiProbability.records.length, 'NRFI Probability'),
                metaCard(reportData.datasets.yrfiProbability.records.length, 'YRFI Probability'),
                metaCard(reportData.datasets.pitcherStrikeouts.records.length, 'Pitcher Expected Strikeouts'),
                metaCard(reportData.generatedAt, 'Generated')
            ])
        ]),
        h('section', { key: 'workspace', className: 'workspace' }, [
            h('div', { key: 'score-guide', className: 'score-guide' }, [
                h('div', { key: 'score-guide-copy', className: 'score-guide-copy' }, [
                    h('h2', { key: 'title' }, `${dataset.label} ${scoreGuide.title || 'Score guide'}`),
                    h('p', { key: 'description' }, scoreGuide.description || ''),
                ]),
                h('div', { key: 'score-guide-grid', className: 'score-guide-grid' }, (scoreGuide.ranges || []).map(scoreRangeCard)),
            ]),
            h('div', { key: 'toolbar', className: 'toolbar' }, [
                h('div', { key: 'tabs', className: 'tabs' }, tabButtons),
                h('div', { key: 'filters', className: 'filters' }, [
                    h('label', { key: 'search-label', htmlFor: 'table-search' }, 'Search'),
                    h('input', { key: 'search-input', id: 'table-search', className: 'search-input', type: 'search', placeholder: 'Player, game, notes...', value: searchTerm, onChange: (event) => setSearchTerm(event.target.value) }),
                    h('label', { key: 'day-label', htmlFor: 'day-filter' }, 'Filter by day'),
                    h('select', { key: 'day-select', id: 'day-filter', className: 'game-filter', value: dayFilter, onChange: (event) => setDayFilter(event.target.value) }, [
                        h('option', { key: 'all-days', value: '' }, 'All days'),
                        ...dayOptions.map((day) => h('option', { key: day.key, value: day.key }, day.label))
                    ]),
                    h('label', { key: 'game-label', htmlFor: 'game-filter' }, 'Filter by game'),
                    h('select', { key: 'game-select', id: 'game-filter', className: 'game-filter', value: gameFilter, onChange: (event) => setGameFilter(event.target.value) }, [
                        h('option', { key: 'all', value: '' }, 'All games'),
                        ...gameOptions.map((game) => h('option', { key: game, value: game }, game))
                    ])
                ])
            ]),
            h('div', { key: 'table-shell', className: 'table-shell' },
                h('table', null, [
                    h('thead', { key: 'thead' }, h('tr', { key: 'head-row' }, headerCells)),
                    h('tbody', { key: 'tbody' }, bodyRows)
                ])
            )
        ])
    ]);
}

ReactDOM.createRoot(document.getElementById('root')).render(h(App));
"""

    html = """<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>MLB Prop Report</title>
  <style>
      body { font-family: Arial, sans-serif; margin: 0; background: linear-gradient(180deg, #eef4ff 0%, #f9fbff 100%); color: #1f2937; }
      #root { min-height: 100vh; }
      .page { max-width: 1480px; margin: 0 auto; padding: 24px; }
      .hero { background: #0f172a; color: #f8fafc; border-radius: 18px; padding: 24px; box-shadow: 0 20px 45px rgba(15, 23, 42, 0.22); }
      .hero h1 { margin: 0 0 8px; font-size: 32px; }
      .hero p { margin: 0; color: #cbd5e1; }
      .meta-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-top: 18px; }
      .meta-card { background: rgba(148, 163, 184, 0.16); border: 1px solid rgba(148, 163, 184, 0.24); border-radius: 14px; padding: 12px 14px; }
      .meta-card strong { display: block; font-size: 24px; margin-bottom: 4px; }
    .workspace { margin-top: 20px; background: white; border-radius: 18px; padding: 20px; box-shadow: 0 18px 40px rgba(15, 23, 42, 0.08); }
    .score-guide { display: grid; grid-template-columns: minmax(260px, 1.1fr) minmax(0, 2fr); gap: 16px; margin-bottom: 18px; padding: 16px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 16px; }
    .score-guide-copy h2 { margin: 0 0 8px; font-size: 20px; }
    .score-guide-copy p { margin: 0; color: #475569; }
    .score-guide-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
    .score-range-card { background: white; border: 1px solid #e2e8f0; border-radius: 14px; padding: 12px; }
    .score-range-card strong { display: block; font-size: 18px; margin-bottom: 4px; }
    .score-range-label { display: block; font-weight: 700; margin-bottom: 6px; color: #0f172a; }
    .score-range-card p { margin: 0; color: #475569; font-size: 13px; line-height: 1.4; }
      .toolbar { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; margin-bottom: 18px; }
      .tabs { display: flex; flex-wrap: wrap; gap: 10px; }
      .tab { border: 1px solid #cbd5e1; background: #f8fafc; color: #0f172a; border-radius: 999px; padding: 10px 14px; cursor: pointer; font-weight: 600; }
      .tab.active { background: #0f172a; color: white; border-color: #0f172a; }
      .filters { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
      .filters label { font-size: 14px; color: #475569; }
      .search-input { padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 10px; min-width: 220px; background: white; }
      .game-filter { padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 10px; min-width: 220px; background: white; }
      .table-shell { overflow-x: auto; border: 1px solid #e2e8f0; border-radius: 14px; }
      table { width: 100%; border-collapse: collapse; font-size: 14px; background: white; }
      th, td { border-bottom: 1px solid #e5e7eb; padding: 12px 10px; text-align: left; vertical-align: top; }
      th { background: #f8fafc; white-space: nowrap; }
      .sort-button { appearance: none; border: none; background: transparent; color: inherit; font: inherit; font-weight: 700; padding: 0; cursor: pointer; }
      tr:hover td { background: #f8fafc; }
    .score-row.band-1 td, .score-range-card.band-1 { background: #ecfdf5; }
    .score-row.band-2 td, .score-range-card.band-2 { background: #f0f9ff; }
    .score-row.band-3 td, .score-range-card.band-3 { background: #fffbeb; }
    .score-row.band-4 td, .score-range-card.band-4 { background: #f8fafc; }
    .score-row.band-1 td:first-child, .score-row.band-2 td:first-child, .score-row.band-3 td:first-child, .score-row.band-4 td:first-child { box-shadow: inset 4px 0 0 rgba(15, 23, 42, 0.12); }
      .notes-cell { white-space: pre-wrap; min-width: 320px; }
      .empty-state { padding: 36px 18px; text-align: center; color: #64748b; }
  </style>
</head>
<body>
    <div id=\"root\"></div>
    <script id=\"report-data\" type=\"application/json\">__REPORT_DATA_JSON__</script>
    <script crossorigin src=\"https://unpkg.com/react@18/umd/react.production.min.js\"></script>
    <script crossorigin src=\"https://unpkg.com/react-dom@18/umd/react-dom.production.min.js\"></script>
    <script src=\"{app_script_name}\"></script>
</body>
</html>
"""
    html = html.replace("__REPORT_DATA_JSON__", escape(json.dumps(report_data), quote=False))
    html = html.replace("{app_script_name}", escape(os.path.basename(app_script_path), quote=False))
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(html)
    with open(app_script_path, "w", encoding="utf-8") as handle:
        handle.write(app_script)


def open_html_report(output_path: str) -> None:
    output_path = os.path.abspath(output_path)
    output_dir = os.path.dirname(output_path) or "."
    output_name = os.path.basename(output_path)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "http.server",
                str(port),
                "--bind",
                "127.0.0.1",
                "--directory",
                output_dir,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.2)
        report_url = f"http://127.0.0.1:{port}/{quote(output_name)}"
        subprocess.run(["open", report_url], check=False, capture_output=True)
        return
    except OSError as exc:
        print(f"Warning: unable to start local report server automatically: {exc}", file=sys.stderr)

    try:
        subprocess.run(["open", output_path], check=False, capture_output=True)
    except OSError as exc:
        print(f"Warning: unable to open HTML report automatically: {exc}", file=sys.stderr)


def write_report(
    records: List[Dict[str, Any]],
    output_path: str,
    output_format: str,
    text_output_path: Optional[str] = None,
    html_home_run_records: Optional[List[Dict[str, Any]]] = None,
    html_secondary_records: Optional[List[Dict[str, Any]]] = None,
    html_pitcher_strikeout_records: Optional[List[Dict[str, Any]]] = None,
    html_team_total_records: Optional[List[Dict[str, Any]]] = None,
    html_nrfi_probability_records: Optional[List[Dict[str, Any]]] = None,
    html_yrfi_probability_records: Optional[List[Dict[str, Any]]] = None,
) -> None:
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    if output_format == "json":
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(records),
            "records": records,
        }
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    elif output_format == "csv":
        with open(output_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["player", "market", "league", "line", "odds", "score", "game", "time", "notes"])
            writer.writeheader()
            for record in records:
                writer.writerow(record)
    else:
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("Most likely MLB home run props\n")
            handle.write("=" * 32 + "\n")
            for record in records:
                handle.write(f"- {record['player']} | {record['market']} | score={record['score']:.2f} | line={record['line']} | odds={record['odds']}\n")
                if record["game"]:
                    handle.write(f"  game: {record['game']}\n")
                if record["notes"]:
                    handle.write(f"  notes: {record['notes']}\n")

    target_text_path = text_output_path or derive_text_output_path(output_path)
    write_text_report(records, target_text_path)
    secondary_path = os.path.splitext(target_text_path)[0] + "_hits_runs_rbis.txt"
    secondary_records = build_hits_runs_rbis_records(records)
    write_text_report(secondary_records, secondary_path, title="Most likely MLB hits + runs + RBI props")
    html_output_path = os.path.splitext(output_path)[0] + ".html"
    if html_home_run_records is None or html_secondary_records is None:
        html_home_run_records, html_secondary_records = build_html_report_record_sets(records)
    write_html_report(
        html_home_run_records,
        html_secondary_records,
        html_output_path,
        html_pitcher_strikeout_records,
        html_team_total_records,
        html_nrfi_probability_records,
        html_yrfi_probability_records,
    )
    print(f"Wrote {len(records)} records to {output_path}")
    print(f"Wrote text summary to {target_text_path}")
    print(f"Wrote secondary text summary to {secondary_path}")
    print(f"Wrote HTML report to {html_output_path}")
    open_html_report(html_output_path)


def main() -> int:
    args = parse_args()
    args = resolve_args(args)
    try:
        payload = fetch_payload(args)
        records = extract_candidates(payload, args.league, args.market)
        ranked = records[: max(1, args.limit)]
        html_home_run_records, html_secondary_records = build_html_report_record_sets(records)
        game_slate = payload.get("game_slate") if isinstance(payload, dict) else []
        players_directory = payload.get("players_directory") if isinstance(payload, dict) else []
        batting_order_lookup = build_batting_order_lookup(
            game_slate if isinstance(game_slate, list) else [],
            players_directory if isinstance(players_directory, list) else [],
        )
        lineups_available = bool(batting_order_lookup)
        html_home_run_records = annotate_batting_order_records(html_home_run_records, batting_order_lookup, lineups_available)
        html_secondary_records = annotate_batting_order_records(html_secondary_records, batting_order_lookup, lineups_available)
        pitcher_lookup = {}
        pitcher_splits = {}
        if isinstance(game_slate, list) and game_slate:
            unique_team_ids = sorted({
                str(team_id)
                for game in game_slate
                for team_id in [
                    (game.get("homeTeam") or {}).get("id"),
                    (game.get("visitorTeam") or {}).get("id"),
                ]
                if team_id is not None
            })
            for team_id in unique_team_ids:
                pitcher_data = request_json(f"{args.base_url.rstrip('/')}/mlb/pitchers?teamIds={team_id}", args.token, args.api_key, args.cookie)
                if isinstance(pitcher_data, list):
                    for pitcher in pitcher_data:
                        if isinstance(pitcher, dict) and pitcher.get("id") is not None:
                            pitcher_lookup[str(pitcher.get("id"))] = pitcher
            starter_ids = sorted({
                str(pitcher_id)
                for game in game_slate
                for pitcher_id in [game.get("homePitcherId"), game.get("visitorPitcherId")]
                if pitcher_id is not None
            })
            for pitcher_id in starter_ids:
                pitcher_splits[pitcher_id] = request_json(
                    f"{args.base_url.rstrip('/')}{DEFAULT_PITCHER_SPLITS_ENDPOINT}?pitcherId={pitcher_id}&season={args.season}",
                    args.token,
                    args.api_key,
                    args.cookie,
                )
        html_pitcher_strikeout_records = build_pitcher_strikeout_records(game_slate if isinstance(game_slate, list) else [], pitcher_lookup, pitcher_splits)
        html_team_total_records = build_team_total_expected_runs_records(
            game_slate if isinstance(game_slate, list) else [],
            pitcher_lookup,
            pitcher_splits,
        )
        html_nrfi_probability_records = build_nrfi_probability_records(
            game_slate if isinstance(game_slate, list) else [],
            pitcher_lookup,
            pitcher_splits,
            payload if isinstance(payload, dict) else {},
        )
        html_yrfi_probability_records = build_yrfi_probability_records(
            game_slate if isinstance(game_slate, list) else [],
            pitcher_lookup,
            pitcher_splits,
            payload if isinstance(payload, dict) else {},
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not ranked:
        print("No home-run prop candidates were found.", file=sys.stderr)
        return 2

    for record in ranked:
        print(f"{record['player']} | score={record['score']:.2f} | line={record['line']} | odds={record['odds']} | game={record['game']}")

    write_report(
        ranked,
        args.output,
        args.format,
        args.text_output,
        html_home_run_records,
        html_secondary_records,
        html_pitcher_strikeout_records,
        html_team_total_records,
        html_nrfi_probability_records,
        html_yrfi_probability_records,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

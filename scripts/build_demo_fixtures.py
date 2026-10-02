"""Generate the fictional demo slate in src/mlb_prop_predictor/demo/.

The demo uses invented teams and players so the project can be run (and tested) without
network access or API keys. Responses mimic the shape of the real APIs.

    python scripts/build_demo_fixtures.py
"""

from __future__ import annotations

import csv
import json
import random
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src" / "mlb_prop_predictor" / "demo"
DATE = "2026-07-04"
SEASON = 2026
rng = random.Random(20260704)

TEAMS = [
    # id, full name, nickname, venue id, venue name, roof, lat, lon, home HR boost, home K boost
    (9001, "Harbor City Herons", "Herons", 8001, "Lighthouse Field", "Open", 41.0, -73.5, 1.22, 0.98),
    (9002, "Ridgeview Rockets", "Rockets", 8002, "Ridgeview Dome", "Dome", 39.0, -94.5, 0.95, 1.03),
    (9003, "Portside Pilots", "Pilots", 8003, "Pier 9 Park", "Retractable", 33.4, -112.0, 1.02, 1.00),
    (9004, "Summit Peaks", "Peaks", 8004, "Altitude Yards", "Open", 39.7, -104.9, 1.30, 0.93),
]
GAMES = [(9002, 9001, "2026-07-04T23:05:00Z", 700001), (9004, 9003, "2026-07-05T01:40:00Z", 700002)]

FIRST = [
    "Ace",
    "Bo",
    "Cal",
    "Dex",
    "Eli",
    "Finn",
    "Gus",
    "Hank",
    "Ike",
    "Jett",
    "Kai",
    "Lou",
    "Max",
    "Ned",
    "Otis",
    "Pax",
    "Quin",
    "Rex",
    "Sal",
    "Ty",
    "Ugo",
    "Vic",
    "Wes",
    "Xan",
    "Yuri",
    "Zed",
    "Abe",
    "Ben",
    "Cy",
    "Dom",
    "Ezra",
    "Flynn",
    "Gil",
    "Hal",
    "Ira",
    "Jax",
    "Kip",
    "Leo",
    "Mack",
    "Nico",
]
LAST = [
    "Demo",
    "Sample",
    "Fixture",
    "Mockley",
    "Testa",
    "Placeholder",
    "Synth",
    "Example",
    "Dummy",
    "Proto",
    "Stubbs",
    "Fakeson",
    "Specimen",
    "Trialer",
    "Prototyp",
    "Imitato",
    "Modelo",
    "Draftsman",
    "Pilotti",
    "Sandbox",
]


def name(i: int) -> str:
    return f"{FIRST[i % len(FIRST)]} {LAST[(i * 7) % len(LAST)]}"


def hitter(pid: int, idx: int) -> dict:
    pa = rng.randint(250, 420)
    power = rng.uniform(0.012, 0.070)
    k_rate = rng.uniform(0.14, 0.32)
    side = rng.choice(["R", "R", "L", "L", "S"])
    hr, k = round(pa * power), round(pa * k_rate)
    pa_l = round(pa * 0.3)
    pa_r = pa - pa_l
    platoon = rng.uniform(0.8, 1.2)
    hr_l = min(hr, round(hr * 0.3 * platoon))
    k_l = round(k * 0.3 * rng.uniform(0.85, 1.15))

    def stat(p, h, s):
        return {"plateAppearances": p, "homeRuns": h, "strikeOuts": s, "gamesPlayed": p // 4}

    return {
        "id": pid,
        "fullName": name(idx),
        "batSide": {"code": side},
        "pitchHand": {"code": "R"},
        "stats": [
            {
                "type": {"displayName": "season"},
                "group": {"displayName": "hitting"},
                "splits": [{"stat": stat(pa, hr, k)}],
            },
            {
                "type": {"displayName": "statSplits"},
                "group": {"displayName": "hitting"},
                "splits": [
                    {"split": {"code": "vl", "description": "vs Left"}, "stat": stat(pa_l, hr_l, k_l)},
                    {
                        "split": {"code": "vr", "description": "vs Right"},
                        "stat": stat(pa_r, hr - hr_l, k - k_l),
                    },
                ],
            },
        ],
        "_barrel_pa": max(0.01, power * 2.1 * rng.uniform(0.8, 1.2)),
        "_pa": pa,
    }


def pitcher(pid: int, idx: int) -> dict:
    gs = rng.randint(14, 19)
    bf = round(gs * rng.uniform(20, 26))
    k_rate = rng.uniform(0.18, 0.33)
    hr_rate = rng.uniform(0.020, 0.040)
    k, hr = round(bf * k_rate), round(bf * hr_rate)
    bf_l = round(bf * 0.45)

    def stat(b, h, s, g=None):
        out = {"battersFaced": b, "homeRuns": h, "strikeOuts": s}
        if g is not None:
            out.update({"gamesPlayed": g, "gamesStarted": g})
        return out

    return {
        "id": pid,
        "fullName": name(idx),
        "batSide": {"code": "R"},
        "pitchHand": {"code": rng.choice(["R", "R", "L"])},
        "stats": [
            {
                "type": {"displayName": "season"},
                "group": {"displayName": "pitching"},
                "splits": [{"stat": stat(bf, hr, k, gs)}],
            },
            {
                "type": {"displayName": "statSplits"},
                "group": {"displayName": "pitching"},
                "splits": [
                    {"split": {"code": "vl"}, "stat": stat(bf_l, round(hr * 0.45), round(k * 0.43))},
                    {
                        "split": {"code": "vr"},
                        "stat": stat(bf - bf_l, hr - round(hr * 0.45), k - round(k * 0.43)),
                    },
                ],
            },
        ],
        "_barrel_pa": hr_rate * 2.0 * rng.uniform(0.85, 1.15),
        "_pa": bf,
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    hitters, pitchers, lineup_by_team, sp_by_team = [], [], {}, {}
    idx = 0
    for t in TEAMS:
        lineup = []
        for _ in range(9):
            pid = 100000 + idx
            hitters.append(hitter(pid, idx))
            lineup.append(pid)
            idx += 1
        lineup_by_team[t[0]] = lineup
        pid = 200000 + idx
        pitchers.append(pitcher(pid, idx))
        sp_by_team[t[0]] = pid
        idx += 1

    team = {t[0]: t for t in TEAMS}
    people = {p["id"]: p for p in hitters + pitchers}

    def side(team_id: int, with_lineup: bool) -> dict:
        t = team[team_id]
        sp = people[sp_by_team[team_id]]
        return {
            "team": {"id": t[0], "name": t[1], "teamName": t[2]},
            "probablePitcher": {"id": sp["id"], "fullName": sp["fullName"]},
        }

    games = []
    for away, home, when, pk in GAMES:
        t = team[home]
        g = {
            "gamePk": pk,
            "gameDate": when,
            "status": {"detailedState": "Scheduled"},
            "teams": {"away": side(away, True), "home": side(home, True)},
            "venue": {
                "id": t[3],
                "name": t[4],
                "location": {"defaultCoordinates": {"latitude": t[6], "longitude": t[7]}, "elevation": 500},
                "fieldInfo": {"roofType": t[5]},
            },
        }
        # First game has confirmed lineups; the second relies on the previous day's lineups.
        if pk == 700001:
            g["lineups"] = {
                "awayPlayers": [{"id": i, "fullName": people[i]["fullName"]} for i in lineup_by_team[away]],
                "homePlayers": [{"id": i, "fullName": people[i]["fullName"]} for i in lineup_by_team[home]],
            }
        games.append(g)
    dump("schedule.json", {"dates": [{"date": DATE, "games": games}]})

    prev = {
        "gamePk": 699999,
        "gameDate": "2026-07-03T23:05:00Z",
        "teams": {"away": {"team": {"id": 9004}}, "home": {"team": {"id": 9003}}},
        "lineups": {
            "awayPlayers": [{"id": i} for i in lineup_by_team[9004]],
            "homePlayers": [{"id": i} for i in lineup_by_team[9003]],
        },
    }
    dump("schedule_recent.json", {"dates": [{"date": "2026-07-03", "games": [prev]}]})

    clean = lambda p: {k: v for k, v in p.items() if not k.startswith("_")}  # noqa: E731
    dump("people_hitting.json", {"people": [clean(p) for p in hitters]})
    dump("people_pitching.json", {"people": [clean(p) for p in pitchers]})

    # Team home/away counts with built-in park effects; league ~3.0% HR/PA and ~22% K/PA.
    for group, opp_key in (("hitting", "plateAppearances"), ("pitching", "battersFaced")):
        splits = []
        for t in TEAMS:
            for code, hr_mult, k_mult in (("h", t[8], t[9]), ("a", 1.0, 1.0)):
                pa = 3000 + rng.randint(-60, 60)
                splits.append(
                    {
                        "team": {"id": t[0], "name": t[1]},
                        "split": {"code": code},
                        "stat": {
                            opp_key: pa,
                            "homeRuns": round(pa * 0.030 * hr_mult),
                            "strikeOuts": round(pa * 0.222 * k_mult),
                        },
                    }
                )
        dump(f"team_stats_{group}.json", {"stats": [{"splits": splits}]})
    dump("teams.json", {"teams": [{"id": t[0], "name": t[1], "venue": {"id": t[3]}} for t in TEAMS]})

    for kind, group in (("batter", hitters), ("pitcher", pitchers)):
        with (OUT / f"savant_{kind}.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(
                [
                    "last_name, first_name",
                    "player_id",
                    "attempts",
                    "avg_hit_speed",
                    "barrels",
                    "brl_percent",
                    "brl_pa",
                ]
            )
            for p in group:
                bbe = round(p["_pa"] * 0.68)
                barrels = round(p["_pa"] * p["_barrel_pa"])
                w.writerow(
                    [
                        p["fullName"],
                        p["id"],
                        bbe,
                        round(rng.uniform(86, 94), 1),
                        barrels,
                        round(barrels / bbe * 100, 1),
                        round(p["_barrel_pa"] * 100, 1),
                    ]
                )

    hours = [f"{DATE}T{h:02d}:00" for h in range(24)] + [f"2026-07-05T{h:02d}:00" for h in range(24)]
    dump(
        "weather.json",
        {
            "hourly": {
                "time": hours,
                "temperature_2m": [round(70 + 15 * ((h % 24) / 24), 1) for h in range(48)],
                "wind_speed_10m": [8.0] * 48,
                "precipitation_probability": [10] * 48,
            }
        },
    )

    events = []
    for away, home, when, pk in GAMES:
        ev_id = f"demo{pk}"
        events.append(
            {
                "id": ev_id,
                "sport_key": "baseball_mlb",
                "commence_time": when,
                "home_team": team[home][1],
                "away_team": team[away][1],
            }
        )
        books = []
        for book, vig in (("DemoBook", 0.045), ("SampleSports", 0.06)):
            hr_out = []
            for pid in lineup_by_team[home] + lineup_by_team[away]:
                p = 0.08 + people[pid]["_barrel_pa"] * 1.6 * rng.uniform(0.7, 1.3)
                hr_out.append(
                    {
                        "name": "Over",
                        "description": people[pid]["fullName"],
                        "point": 0.5,
                        "price": to_american(min(0.6, p * (1 + vig))),
                    }
                )
                if book == "DemoBook":
                    hr_out.append(
                        {
                            "name": "Under",
                            "description": people[pid]["fullName"],
                            "point": 0.5,
                            "price": to_american(min(0.97, (1 - p) * (1 + vig))),
                        }
                    )
            k_out = []
            for team_id in (home, away):
                sp = people[sp_by_team[team_id]]
                line = rng.choice([4.5, 5.5, 6.5])
                p_over = rng.uniform(0.4, 0.6)
                k_out += [
                    {
                        "name": "Over",
                        "description": sp["fullName"],
                        "point": line,
                        "price": to_american(p_over * (1 + vig)),
                    },
                    {
                        "name": "Under",
                        "description": sp["fullName"],
                        "point": line,
                        "price": to_american((1 - p_over) * (1 + vig)),
                    },
                ]
            books.append(
                {
                    "key": book.lower(),
                    "title": book,
                    "markets": [
                        {"key": "batter_home_runs", "outcomes": hr_out},
                        {"key": "pitcher_strikeouts", "outcomes": k_out},
                    ],
                }
            )
        dump(f"odds_{ev_id}.json", {"id": ev_id, "bookmakers": books})
    dump("odds_events.json", events)
    print(f"wrote demo fixtures to {OUT}")


def to_american(p: float) -> int:
    p = min(max(p, 0.01), 0.99)
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def dump(name: str, data) -> None:
    (OUT / name).write_text(json.dumps(data, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()

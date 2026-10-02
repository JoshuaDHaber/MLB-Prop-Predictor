"""A simulated MLB-like season served through a fake HTTP client.

Every hitter and pitcher has a known true HR and K rate; plate appearances are drawn with
log5, so a correct, leak-free backtest should come out close to calibrated.
"""

from __future__ import annotations

import json
import math
import random
from datetime import date, timedelta

from mlb_prop_predictor.http import HttpError, Response

LG_HR, LG_K = 0.030, 0.222


def _odds(p):
    return p / (1 - p)


def _log5(b, p, lg):
    o = _odds(b) * _odds(p) / _odds(lg)
    return o / (1 + o)


class SimSeason:
    def __init__(self, season=2025, teams=10, days=120, seed=7):
        rng = random.Random(seed)
        self.season = season
        self.games: dict[int, dict] = {}
        self.pbp: dict[int, dict] = {}
        self.team_ids = list(range(1, teams + 1))
        hitters, starters, pens = {}, {}, {}
        pid = 1000
        for t in self.team_ids:
            hitters[t] = []
            for _ in range(9):
                hr = min(0.09, max(0.005, rng.lognormvariate(math.log(LG_HR) - 0.08, 0.4)))
                k = min(0.4, max(0.08, rng.gauss(LG_K, 0.05)))
                hitters[t].append((pid, rng.choice("RRL"), hr, k))
                pid += 1
            starters[t] = []
            for _ in range(5):
                starters[t].append(
                    (
                        pid,
                        rng.choice("RRL"),
                        rng.uniform(0.022, 0.040),
                        rng.gauss(LG_K, 0.045),
                        rng.gauss(23, 2.5),
                    )
                )
                pid += 1
            pens[t] = (pid, "R", LG_HR, LG_K)
            pid += 1

        start = date(season, 4, 1)
        pk = 1
        rotation = {t: 0 for t in self.team_ids}
        for d in range(days):
            day = start + timedelta(days=d)
            order = self.team_ids[:]
            rng.shuffle(order)
            for i in range(0, len(order), 2):
                away, home = order[i], order[i + 1]
                plays = []
                lineups = {}
                for side, bat_team, fld_team in (("away", away, home), ("home", home, away)):
                    sp = starters[fld_team][rotation[fld_team] % 5]
                    bf_limit = max(12, round(rng.gauss(sp[4], 3)))
                    lineup = hitters[bat_team]
                    lineups[side] = [h[0] for h in lineup]
                    n_pa = max(30, round(rng.gauss(38, 3)))
                    for j in range(n_pa):
                        b = lineup[j % 9]
                        pitcher = sp if j < bf_limit else pens[fld_team]
                        u = rng.random()
                        p_hr = _log5(b[2], pitcher[2], LG_HR)
                        p_k = _log5(b[3], pitcher[3], LG_K)
                        event = "home_run" if u < p_hr else ("strikeout" if u < p_hr + p_k else "field_out")
                        plays.append(
                            {
                                "result": {"type": "atBat", "eventType": event},
                                "about": {
                                    "halfInning": "top" if side == "away" else "bottom",
                                    "isComplete": True,
                                },
                                "matchup": {
                                    "batter": {"id": b[0]},
                                    "batSide": {"code": b[1]},
                                    "pitcher": {"id": pitcher[0]},
                                    "pitchHand": {"code": pitcher[1]},
                                },
                            }
                        )
                    if side == "away":
                        # One administrative play that must not count as a plate appearance.
                        plays.append(
                            {
                                "result": {"eventType": "caught_stealing_2b"},
                                "about": {"halfInning": "top", "isComplete": True},
                                "matchup": {"batter": {"id": lineup[0][0]}, "pitcher": {"id": sp[0]}},
                            }
                        )
                rotation[away] += 1
                rotation[home] += 1
                self.games[pk] = {
                    "gamePk": pk,
                    "gameType": "R",
                    "gameDate": f"{day.isoformat()}T23:05:00Z",
                    "officialDate": day.isoformat(),
                    "status": {"detailedState": "Final"},
                    "teams": {
                        "away": {"team": {"id": away, "name": f"T{away}"}},
                        "home": {"team": {"id": home, "name": f"T{home}"}},
                    },
                    "venue": {"id": 500 + home, "fieldInfo": {"roofType": "Open"}},
                    "weather": {"temp": "70", "condition": "Clear"},
                    "lineups": {
                        "awayPlayers": [{"id": x} for x in lineups["away"]],
                        "homePlayers": [{"id": x} for x in lineups["home"]],
                    },
                }
                self.pbp[pk] = {"allPlays": plays}
                pk += 1

    # -- fake HttpClient -----------------------------------------------------------
    def get(self, url, params=None, ttl=0):
        params = params or {}
        if url.endswith("/schedule"):
            lo, hi = params["startDate"], params["endDate"]
            by_date: dict[str, list] = {}
            for g in self.games.values():
                if lo <= g["officialDate"] <= hi:
                    by_date.setdefault(g["officialDate"], []).append(g)
            return Response(
                json.dumps({"dates": [{"date": d, "games": gs} for d, gs in sorted(by_date.items())]})
            )
        if "/playByPlay" in url:
            return Response(json.dumps(self.pbp[int(url.split("/")[-2])]))
        if url.endswith("/teams"):
            return Response(
                json.dumps({"teams": [{"id": t, "venue": {"id": 500 + t}} for t in self.team_ids]})
            )
        if url.endswith("/teams/stats") or "/teams/" in url:
            group = params.get("group", "hitting")
            key = "plateAppearances" if group == "hitting" else "battersFaced"
            splits = [
                {
                    "team": {"id": t},
                    "split": {"code": c},
                    "stat": {key: 3000, "homeRuns": 90, "strikeOuts": 666},
                }
                for t in self.team_ids
                for c in ("h", "a")
            ]
            return Response(json.dumps({"stats": [{"splits": splits}]}))
        if "baseballsavant" in url:
            raise HttpError("savant not simulated")
        raise KeyError(url)

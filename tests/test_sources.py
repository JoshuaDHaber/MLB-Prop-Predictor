import json
from datetime import datetime, timezone

from mlb_prop_predictor.domain import Counts, Game, TeamSide, Venue
from mlb_prop_predictor.http import Response, redact
from mlb_prop_predictor.sources import mlb_stats, savant
from mlb_prop_predictor.sources.odds import match_event, normalize_name


class FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, ttl=0):
        self.calls.append((url, params))
        for key, body in self.routes.items():
            if url.endswith(key):
                return Response(body if isinstance(body, str) else json.dumps(body))
        raise KeyError(url)


def test_redact_hides_api_key():
    assert "secret" not in redact("https://x/odds", {"apiKey": "secret", "markets": "a"})


def test_normalize_name():
    assert normalize_name("José Ramírez") == "jose ramirez"
    assert normalize_name("Vladimir Guerrero Jr.") == "vladimir guerrero"
    assert normalize_name("Ke'Bryan Hayes") == normalize_name("Ke Bryan Hayes")


def test_traded_player_uses_combined_split_row():
    person = {
        "id": 7,
        "fullName": "Traded Guy",
        "pitchHand": {"code": "L"},
        "stats": [
            {
                "type": {"displayName": "statSplits"},
                "group": {"displayName": "pitching"},
                "splits": [
                    {
                        "split": {"code": "vl"},
                        "team": {"id": 1},
                        "stat": {"battersFaced": 50, "homeRuns": 1, "strikeOuts": 15},
                    },
                    {
                        "split": {"code": "vl"},
                        "team": {"id": 2},
                        "stat": {"battersFaced": 30, "homeRuns": 2, "strikeOuts": 5},
                    },
                    {
                        "split": {"code": "vr"},
                        "team": {"id": 1},
                        "stat": {"battersFaced": 100, "homeRuns": 3, "strikeOuts": 20},
                    },
                ],
            }
        ],
    }
    client = FakeClient({"/people": {"people": [person]}})
    players = mlb_stats.get_players(client, [], [7], 2026)
    p = players[7]
    assert p.pitching_vs["L"] == Counts(80, 3, 20)  # summed across teams (no combined row present)
    assert p.pitching_vs["R"] == Counts(100, 3, 20)


def test_hitting_pa_fallback_when_plate_appearances_missing():
    counts = mlb_stats._hitting_counts(
        {"atBats": 90, "baseOnBalls": 8, "hitByPitch": 1, "sacFlies": 1, "homeRuns": 4}
    )
    assert counts == Counts(100, 4, 0)


def test_schedule_skips_postponed_games():
    game = {
        "gamePk": 1,
        "gameDate": "2026-07-04T23:05:00Z",
        "status": {"detailedState": "Postponed"},
        "teams": {"away": {"team": {"id": 1}}, "home": {"team": {"id": 2}}},
        "venue": {"id": 3},
    }
    client = FakeClient({"/schedule": {"dates": [{"games": [game]}]}})
    assert mlb_stats.get_schedule(client, "2026-07-04") == []


def test_savant_parser_handles_quoted_headers_and_bad_rows():
    text = (
        '"last_name, first_name","player_id","attempts","barrels","brl_percent","brl_pa"\n'
        '"Doe, John",123,250,30,12.0,8.5\n"Bad, Row",,,,,\n'
    )
    rows = savant.parse_leaderboard(text)
    assert list(rows) == [123]
    assert rows[123].barrels_per_pa == 0.085


def test_match_event_by_nickname_and_time():
    game = Game(
        1,
        datetime(2026, 7, 4, 23, 5, tzinfo=timezone.utc),
        "Scheduled",
        TeamSide(1, "Boston Red Sox", "Red Sox"),
        TeamSide(2, "New York Yankees", "Yankees"),
        Venue(3, "V"),
    )
    events = [
        {
            "id": "wrong",
            "home_team": "New York Yankees",
            "away_team": "Chicago White Sox",
            "commence_time": "2026-07-04T23:05:00Z",
        },
        {
            "id": "right",
            "home_team": "New York Yankees",
            "away_team": "Boston Red Sox",
            "commence_time": "2026-07-04T23:05:00Z",
        },
    ]
    assert match_event(game, events)["id"] == "right"


def test_team_home_away_fills_missing_teams_individually():
    bulk = {
        "stats": [
            {
                "splits": [
                    {
                        "team": {"id": 1},
                        "split": {"code": "h"},
                        "stat": {"plateAppearances": 3000, "homeRuns": 90},
                    },
                    {
                        "team": {"id": 1},
                        "split": {"code": "a"},
                        "stat": {"plateAppearances": 3000, "homeRuns": 80},
                    },
                ]
            }
        ]
    }
    per_team = {
        "stats": [
            {
                "group": {"displayName": "hitting"},
                "splits": [
                    {"split": {"code": "h"}, "stat": {"plateAppearances": 2900, "homeRuns": 100}},
                    {"split": {"code": "a"}, "stat": {"plateAppearances": 3100, "homeRuns": 85}},
                ],
            },
            {
                "group": {"displayName": "pitching"},
                "splits": [
                    {"split": {"code": "h"}, "stat": {"battersFaced": 3000, "homeRuns": 95}},
                    {"split": {"code": "a"}, "stat": {"battersFaced": 3050, "homeRuns": 88}},
                ],
            },
        ]
    }
    client = FakeClient({"/teams/stats": bulk, "/teams/2/stats": per_team})
    out = mlb_stats.get_team_home_away(client, 2025, 2026, [2])
    assert out[2]["bat_h"] == Counts(2900, 100, 0)
    assert out[2]["pit_a"] == Counts(3050, 88, 0)
    assert out[1]["bat_h"].hr == 90

"""Season-to-date statistics, accumulated one game at a time.

The backtest asks this object for a player's numbers *before* updating it with the game
being projected, so no projection can see its own outcome or anything later.
"""

from __future__ import annotations

from collections import defaultdict

from mlb_prop_predictor.backtest.data import HOME_RUN_EVENTS, STRIKEOUT_EVENTS, HistGame
from mlb_prop_predictor.domain import Counts, Player


def _outcome(event: str) -> Counts:
    return Counts(1, int(event in HOME_RUN_EVENTS), int(event in STRIKEOUT_EVENTS))


class SeasonState:
    def __init__(self) -> None:
        self.hit: dict[int, Counts] = defaultdict(Counts)
        self.hit_vs: dict[int, dict[str, Counts]] = defaultdict(lambda: defaultdict(Counts))
        self.pit: dict[int, Counts] = defaultdict(Counts)
        self.pit_vs: dict[int, dict[str, Counts]] = defaultdict(lambda: defaultdict(Counts))
        self.games_pitched: dict[int, int] = defaultdict(int)
        self.games_started: dict[int, int] = defaultdict(int)
        self.team_bat: dict[int, Counts] = defaultdict(Counts)
        self.team_pit: dict[int, Counts] = defaultdict(Counts)
        self.league = Counts()
        # Handedness is a fixed attribute, not an outcome, so it may be learned from any game.
        self.sides_seen: dict[int, set[str]] = defaultdict(set)
        self.pitch_hand: dict[int, str] = {}

    def learn_handedness(self, game: HistGame) -> None:
        for pa in game.pas:
            self.sides_seen[pa.batter].add(pa.bat_side)
            self.pitch_hand[pa.pitcher] = pa.pitch_hand

    def bat_side(self, pid: int) -> str | None:
        sides = self.sides_seen.get(pid)
        if not sides:
            return None
        return "S" if len(sides) > 1 else next(iter(sides))

    def player(self, pid: int) -> Player:
        return Player(
            id=pid,
            name=str(pid),
            bat_side=self.bat_side(pid),
            pitch_hand=self.pitch_hand.get(pid),
            hitting=self.hit[pid],
            hitting_vs=dict(self.hit_vs[pid]),
            pitching=self.pit[pid],
            pitching_vs=dict(self.pit_vs[pid]),
            games_pitched=self.games_pitched[pid],
            games_started=self.games_started[pid],
        )

    def update(self, game: HistGame) -> None:
        team = {"away": game.away_id, "home": game.home_id}
        fielding = {"away": game.home_id, "home": game.away_id}
        pitchers: set[int] = set()
        for pa in game.pas:
            c = _outcome(pa.event)
            self.hit[pa.batter] += c
            self.hit_vs[pa.batter][pa.pitch_hand] += c
            self.pit[pa.pitcher] += c
            self.pit_vs[pa.pitcher][pa.bat_side] += c
            self.team_bat[team[pa.batting]] += c
            self.team_pit[fielding[pa.batting]] += c
            self.league += c
            pitchers.add(pa.pitcher)
        for pid in pitchers:
            self.games_pitched[pid] += 1
        for side in ("away", "home"):
            starter = game.starter(side)
            if starter is not None:
                self.games_started[starter] += 1

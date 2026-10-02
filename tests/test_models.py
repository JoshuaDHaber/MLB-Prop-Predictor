import math

import pytest

from mlb_prop_predictor.domain import Counts, Player, PropPrice, StatcastLine, Venue, Weather
from mlb_prop_predictor.models.common import (
    LeagueRates,
    american_to_prob,
    expected_value,
    log5,
    market_view,
    poisson_binomial,
    prob_to_american,
    regress,
    scale_probability,
)
from mlb_prop_predictor.models.home_runs import project_home_run
from mlb_prop_predictor.models.matchup import batter_hand_vs, expected_starter_bf, times_facing_starter
from mlb_prop_predictor.models.park import compute_park_factors, roof_closed, temperature_factor
from mlb_prop_predictor.models.strikeouts import project_strikeouts

LG = LeagueRates(hr_per_pa=0.030, k_per_pa=0.222, barrels_per_pa=0.060)


def avg_batter(pid=1, side="R", pa=500, hr_rate=0.030, k_rate=0.222):
    return Player(
        id=pid, name=f"B{pid}", bat_side=side, hitting=Counts(pa, round(pa * hr_rate), round(pa * k_rate))
    )


def avg_pitcher(hand="R", starts=25, bf_per=22, hr_rate=0.030, k_rate=0.222):
    bf = starts * bf_per
    return Player(
        id=99,
        name="P",
        pitch_hand=hand,
        pitching=Counts(bf, round(bf * hr_rate), round(bf * k_rate)),
        games_pitched=starts,
        games_started=starts,
    )


# -- common math --------------------------------------------------------------


def test_regress_with_no_data_returns_prior():
    assert regress(0, 0, 0.03, 200) == pytest.approx(0.03)


def test_regress_pulls_small_samples_toward_prior():
    # 3 HR in 20 PA (15%) is mostly noise.
    assert regress(3, 20, 0.03, 200) == pytest.approx((3 + 6) / 220)


def test_log5_is_neutral_for_league_average_inputs():
    assert log5(0.03, 0.03, 0.03) == pytest.approx(0.03)


def test_log5_moves_with_both_sides():
    assert log5(0.06, 0.03, 0.03) == pytest.approx(0.06, rel=1e-6)
    assert log5(0.06, 0.045, 0.03) > 0.06


def test_scale_probability_stays_bounded():
    assert 0 < scale_probability(0.9, 5.0) < 1
    assert scale_probability(0.2, 1.0) == pytest.approx(0.2)


@pytest.mark.parametrize("price,prob", [(-110, 110 / 210), (100, 0.5), (300, 0.25)])
def test_american_to_prob(price, prob):
    assert american_to_prob(price) == pytest.approx(prob)


@pytest.mark.parametrize("p", [0.1, 0.25, 0.5, 0.6, 0.8])
def test_prob_american_round_trip(p):
    assert american_to_prob(prob_to_american(p)) == pytest.approx(p, abs=0.003)


def test_expected_value_zero_at_fair_price():
    assert expected_value(0.25, 300) == pytest.approx(0.0)


def test_poisson_binomial_matches_binomial():
    dist = poisson_binomial([0.3] * 5)
    assert sum(dist) == pytest.approx(1.0)
    assert dist[2] == pytest.approx(math.comb(5, 2) * 0.3**2 * 0.7**3)


def test_market_view_removes_vig_when_both_sides_quoted():
    prices = [
        PropPrice("A", "Over", 5.5, -120),
        PropPrice("A", "Under", 5.5, 100),
        PropPrice("B", "Over", 5.5, -110),
        PropPrice("B", "Under", 5.5, -110),
        PropPrice("B", "Over", 6.5, 150),
    ]
    view = market_view(prices, "Over", 5.5)
    assert view.devigged and view.books == 2
    assert view.best_price == -110 and view.best_book == "B"
    a = american_to_prob(-120) / (american_to_prob(-120) + american_to_prob(100))
    assert view.fair_prob == pytest.approx((a + 0.5) / 2)


def test_market_view_one_sided():
    view = market_view([PropPrice("A", "Over", 0.5, 400)], "Over", 0.5)
    assert not view.devigged and view.fair_prob == pytest.approx(0.2)


# -- matchup helpers ------------------------------------------------------------


def test_switch_hitter_bats_opposite_side():
    s = Player(1, "S", bat_side="S")
    assert batter_hand_vs(s, "R") == "L" and batter_hand_vs(s, "L") == "R"


def test_starter_workload_regresses_to_league():
    assert expected_starter_bf(None) == 22.0
    one_great_start = Player(1, "P", pitching=Counts(30, 0, 12), games_pitched=1, games_started=1)
    assert 22 < expected_starter_bf(one_great_start) <= 24  # (30 + 3*22) / 4


def test_times_facing_starter():
    # 22 BF: slots 1-4 see the starter 3 times (1, 10, 19 ... 4, 13, 22), slot 5 twice.
    assert times_facing_starter(1, 22, 4.65) == pytest.approx(3)
    assert times_facing_starter(5, 22, 4.25) == pytest.approx(2)
    assert times_facing_starter(5, 22.5, 4.25) == pytest.approx(2.5)  # half of a third trip
    assert times_facing_starter(4, 21.5, 4.35) == pytest.approx(2.5)


# -- home runs ----------------------------------------------------------------


def test_league_average_hr_probability_is_realistic():
    prob, inputs = project_home_run(
        avg_batter(), None, avg_pitcher(), None, Counts(6000, 180, 1332), 5, 1.0, LG
    )
    expected = 1 - (1 - 0.03) ** 4.25
    assert prob == pytest.approx(expected, abs=0.004)
    assert inputs.pa_total == pytest.approx(4.25)


def test_power_hitter_and_hitter_park_raise_hr_probability():
    base, _ = project_home_run(avg_batter(), None, avg_pitcher(), None, None, 4, 1.0, LG)
    slugger, _ = project_home_run(avg_batter(hr_rate=0.07), None, avg_pitcher(), None, None, 4, 1.0, LG)
    coors, _ = project_home_run(avg_batter(), None, avg_pitcher(), None, None, 4, 1.3, LG)
    assert slugger > base * 1.6
    assert coors > base


def test_barrels_influence_hr_projection():
    b = avg_batter(pa=300)
    low = StatcastLine(bbe=200, barrels=9, barrels_per_pa=0.03)
    high = StatcastLine(bbe=200, barrels=36, barrels_per_pa=0.12)
    p_low, _ = project_home_run(b, low, avg_pitcher(), None, None, 3, 1.0, LG)
    p_high, _ = project_home_run(b, high, avg_pitcher(), None, None, 3, 1.0, LG)
    assert p_high > p_low


# -- strikeouts -----------------------------------------------------------------


def test_league_average_strikeouts():
    proj = project_strikeouts(avg_pitcher(), [], Counts(6000, 180, 1332), 1.0, LG)
    assert proj.expected_k == pytest.approx(22 * 0.222, abs=0.15)
    assert sum(proj.distribution) == pytest.approx(1.0)
    assert proj.prob_over(4.5) + proj.prob_under(4.5) == pytest.approx(1.0)


def test_strikeout_pitcher_vs_high_k_lineup():
    ace = avg_pitcher(k_rate=0.32)
    whiffers = [avg_batter(pid=i, k_rate=0.30) for i in range(9)]
    contact = [avg_batter(pid=i, k_rate=0.15) for i in range(9)]
    high = project_strikeouts(ace, whiffers, None, 1.0, LG)
    low = project_strikeouts(ace, contact, None, 1.0, LG)
    assert high.expected_k > low.expected_k + 1.5


# -- park & weather ---------------------------------------------------------------


def test_park_factor_detects_hitter_park_and_respects_moves():
    hitter_park = {
        "bat_h": Counts(3000, 120, 650),
        "pit_h": Counts(3000, 120, 650),
        "bat_a": Counts(3000, 90, 650),
        "pit_a": Counts(3000, 90, 650),
    }
    seasons = [({1: hitter_park}, {1: 500}), ({1: hitter_park}, {1: 500}), ({1: hitter_park}, {1: 777})]
    factors = compute_park_factors(seasons, {1: 500})
    pf = factors[500]
    assert pf.seasons == 2  # the season played elsewhere is excluded
    assert 1.1 < pf.hr < 4 / 3  # shrunk toward 1 from the raw 1.333
    assert pf.k == pytest.approx(1.0)


def test_temperature_factor_and_roofs():
    open_air = Venue(1, "Open", roof_type="Open")
    dome = Venue(2, "Dome", roof_type="Dome")
    retract = Venue(3, "Retract", roof_type="Retractable")
    hot = Weather(90, 5, 0)
    assert temperature_factor(open_air, hot) > 1.1
    assert temperature_factor(dome, hot) == 1.0
    assert roof_closed(retract, hot) and not roof_closed(retract, Weather(75, 5, 10))
    assert temperature_factor(open_air, Weather(None, None, None)) == 1.0

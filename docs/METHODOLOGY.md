# Methodology

This project builds its own projections from public data instead of relying on a vendor's
pre-computed scores. The models are deliberately simple, interpretable and testable: each
step is a well-known sabermetric technique and every constant is named in the code.

## Building blocks

### Regression to the mean
Small samples lie. A hitter with 3 HR in 20 PA is not a 15% HR hitter. Every rate is shrunk
toward a prior by adding a fixed number of "league-average" trials:

```
regressed = (successes + prior_rate * prior_n) / (trials + prior_n)
```

`prior_n` is set near the sample size at which the stat becomes about half signal and half
noise (its stabilization point). Strikeout rate stabilizes fast, home run rate slowly:

| Rate | prior_n |
|---|---|
| Batter HR/PA | 200 PA |
| Batter barrels/PA | 120 PA |
| Pitcher HR/BF | 1,300 BF |
| Pitcher K/BF | 70 BF |
| Batter K/PA | 60 PA |
| HR platoon splits | 1,500, toward the player's *own* overall rate |
| K platoon splits | 600, toward the player's own overall rate |

Platoon priors are deliberately heavy. *The Book* (Tango, Lichtman & Dolphin) shows most of an
individual's observed platoon split is noise. The simulated-season check below found the model
overconfident when they were lighter (300–600 PA).

### log5 (odds-ratio) matchups
Bill James' log5 combines a batter's rate and a pitcher's rate relative to the league:

```
odds(matchup) = odds(batter) * odds(pitcher) / odds(league),   odds(p) = p / (1 - p)
```

A league-average batter against a league-average pitcher gets the league rate. A 6% HR hitter
against a pitcher who allows 1.5x the league HR rate gets well above 6%.

### Environment adjustments on the odds scale
Park and weather multipliers are applied to the *odds* of an event, not the probability,
so results always stay between 0 and 1.

## Park factors (computed, not downloaded)
For each team's current ballpark:

```
home rate = (team's HR hit at home + HR allowed at home) / (PA + batters faced at home)
road rate = the same on the road
raw factor = home rate / road rate
```

- Seasons are pooled (3 by default), but only seasons in which the team actually played in
  that park count. The Athletics and Rays have both moved recently.
- The log of the raw factor is shrunk toward neutral by its sampling noise:
  `weight = tau^2 / (tau^2 + 1/HR_home + 1/HR_road)`, with tau = 0.12 for home runs and
  0.04 for strikeouts (the real-world spread of park effects).
- Games at neutral sites get a factor of 1.0.

## Weather
Open-Meteo hourly forecasts for the park's coordinates at first pitch. HR odds move by about
0.8% per °F away from 70°F, capped between 0.85x and 1.20x. Domes are neutral. Retractable
roofs are assumed closed below 60°F, above 88°F, or with a 50%+ chance of rain.
Wind is **not** modelled yet (see Limitations).

## Home run model: P(1+ HR)
1. **Batter power.** 55% barrel-based estimate (regressed barrels/PA × league HR per barrel)
   and 45% regressed HR/PA. Barrel rate predicts future HR better than past HR do. Then a
   regressed platoon ratio vs the starter's hand is applied. Switch hitters are evaluated from the
   side they actually bat on.
2. **Starter HR allowed.** The same blend for HR per batter faced, plus a platoon ratio vs the
   batter's side.
3. **Bullpen.** The opposing team's regressed season HR/BF.
4. **Exposure.** Expected PAs come from the lineup slot (4.65 for leadoff down to 3.77 for 9th).
   The starter's expected workload (batters faced per start, regressed toward 22) decides how
   many of those PAs come against him. For example, a #5 hitter facing a 22-BF starter sees him
   twice. Partial trips are prorated.
5. `P(1+ HR) = 1 − (1 − p_starter)^PA_starter × (1 − p_bullpen)^PA_bullpen`

## Strikeout model: full distribution
1. Pitcher K/BF regressed toward the league, with a platoon ratio per opposing hitter.
2. Each hitter's K/PA vs the pitcher's hand, regressed. If no lineup is posted, the last posted
   lineup is used ("projected"), and if none exists the team's K rate stands in.
3. log5 per hitter, scaled by the park K factor.
4. Batters faced is uncertain: a normal spread (sd 3.5) around the expected workload. For each
   possible BF the K count is a Poisson-binomial over the batting order (1–9, 1–9, …). The
   mixture gives `P(K = n)` and therefore P(Over/Under) at any line.

## Comparing to the market
- **HR (`batter_home_runs`, Over 0.5)** and **K (`pitcher_strikeouts`)** lines come from The Odds API.
- Where a book quotes both sides, the vig is removed (`p_over / (p_over + p_under)`) and the
  results are averaged across books. One-sided HR markets are flagged, because their implied
  probability still includes the vig.
- **Edge** = model probability − market probability.
- **EV** = expected profit per unit at the best available price.
- For strikeouts the "main" line is the one quoted by the most books, and the pick is
  whichever side has the higher EV.

## Backtest

`mlb-backtest --season 2025` replays a past season with **the same projection code** the daily
report uses, and scores it against what actually happened.

**No lookahead.** Games are processed date by date. Each day's games are projected from
season-to-date stats accumulated from play-by-play *before* that date, and only then are that
day's results added. Inputs from outside the season use earlier data only: park factors come
from the three prior seasons, and barrel rates from the previous season's Statcast leaderboard.
Handedness is treated as a known, fixed attribute.

**Two simplifications, both standard for this kind of test:**
- The actual starting pitcher and the posted lineup are used. In live use these are usually
  known before odds are bet.
- Recorded game-time temperature is used instead of a forecast.

**What's scored**
- *Home runs*: P(1+ HR) for every starting hitter vs whether he homered. Reported: Brier score,
  log loss, Brier skill vs a league-average baseline, AUC, calibration by decile (with 95% CIs),
  expected calibration error (ECE), and calibration slope/intercept from a logistic recalibration
  fit (perfect = slope 1, intercept 0).
- *Strikeouts*: expected Ks vs actual (MAE, RMSE, bias); calibration of P(Over) at lines
  3.5–8.5; and how often the actual total lands inside the model's 80% interval.
- Every metric is compared with two baselines: **league average**, and the player's **raw
  season-to-date rate** (no regression, no matchup). Beating the raw rate shows what the
  modelling adds.

**Reading the numbers.** Home runs are mostly luck at the single-game level, even for the best
hitters, so Brier skill scores in the low single digits are normal. The meaningful claims are
calibration (when the model says 20%, it happens about 20% of the time) and ranking (AUC above
0.5, with top-decile projections homering far more often than the bottom decile).

**Validated on a simulated season first.** `tests/sim_season.py` generates a season in which every
player's true HR and K rates are known, then runs the full backtest on it. A leak-free, correctly
wired backtest should come out close to calibrated, and the test suite asserts that it does.

## Limitations and roadmap
- No wind direction yet. That needs each park's home-plate-to-center-field bearing.
- Platoon splits use results (HR, K), not Statcast quality-of-contact by handedness.
- Starter workload ignores pitch-count trends, injuries and bullpen days announced late.
- No pitch-type matchup (e.g. a hitter's damage vs sliders × a pitcher's slider usage).
- The backtest measures accuracy against outcomes, not against betting markets.
  Historical prop odds cost 10x credits on The Odds API, so closing-line value isn't measured yet.

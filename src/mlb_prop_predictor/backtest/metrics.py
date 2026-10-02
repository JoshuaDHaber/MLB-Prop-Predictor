"""Scoring rules and calibration diagnostics (standard library only)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


def brier(preds: Sequence[float], outcomes: Sequence[int]) -> float:
    return sum((p - y) ** 2 for p, y in zip(preds, outcomes)) / len(preds)


def log_loss(preds: Sequence[float], outcomes: Sequence[int]) -> float:
    eps = 1e-12
    return -sum(
        y * math.log(max(p, eps)) + (1 - y) * math.log(max(1 - p, eps)) for p, y in zip(preds, outcomes)
    ) / len(preds)


def skill(score: float, reference: float) -> float:
    """1 - score/reference: share of the reference model's error removed (higher is better)."""
    return 1 - score / reference if reference > 0 else float("nan")


def auc(preds: Sequence[float], outcomes: Sequence[int]) -> float:
    """Probability a random positive outranks a random negative (Mann-Whitney U, ties averaged)."""
    order = sorted(range(len(preds)), key=lambda i: preds[i])
    ranks = [0.0] * len(preds)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and preds[order[j + 1]] == preds[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    pos = sum(outcomes)
    neg = len(outcomes) - pos
    if pos == 0 or neg == 0:
        return float("nan")
    rank_sum = sum(r for r, y in zip(ranks, outcomes) if y)
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return center - half, center + half


@dataclass(frozen=True)
class CalibrationBin:
    n: int
    mean_pred: float
    observed: float
    ci_low: float
    ci_high: float


def calibration_bins(preds: Sequence[float], outcomes: Sequence[int], bins: int = 10) -> list[CalibrationBin]:
    """Equal-count bins sorted by prediction."""
    order = sorted(range(len(preds)), key=lambda i: preds[i])
    out = []
    for b in range(bins):
        idx = order[b * len(order) // bins : (b + 1) * len(order) // bins]
        if not idx:
            continue
        hits = sum(outcomes[i] for i in idx)
        lo, hi = wilson(hits, len(idx))
        out.append(CalibrationBin(len(idx), sum(preds[i] for i in idx) / len(idx), hits / len(idx), lo, hi))
    return out


def expected_calibration_error(cal: list[CalibrationBin]) -> float:
    total = sum(b.n for b in cal)
    return sum(b.n * abs(b.mean_pred - b.observed) for b in cal) / total


def calibration_slope(
    preds: Sequence[float], outcomes: Sequence[int], iters: int = 25
) -> tuple[float, float]:
    """Fit logit(P(y)) = a + b * logit(pred) by Newton's method. Perfect calibration is a=0, b=1.

    b < 1 means predictions are too extreme (overconfident); b > 1 means too timid.
    """
    xs = [math.log(max(p, 1e-9) / max(1 - p, 1e-9)) for p in preds]
    a, b = 0.0, 1.0
    for _ in range(iters):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for x, y in zip(xs, outcomes):
            z = max(-30.0, min(30.0, a + b * x))
            p = 1 / (1 + math.exp(-z))
            w = p * (1 - p)
            g0 += y - p
            g1 += (y - p) * x
            h00 += w
            h01 += w * x
            h11 += w * x * x
        det = h00 * h11 - h01 * h01
        if det <= 1e-12:
            break
        da = (h11 * g0 - h01 * g1) / det
        db = (h00 * g1 - h01 * g0) / det
        a, b = a + da, b + db
        if abs(da) < 1e-9 and abs(db) < 1e-9:
            break
    return a, b


def mae(preds: Sequence[float], actual: Sequence[float]) -> float:
    return sum(abs(p - a) for p, a in zip(preds, actual)) / len(preds)


def rmse(preds: Sequence[float], actual: Sequence[float]) -> float:
    return math.sqrt(sum((p - a) ** 2 for p, a in zip(preds, actual)) / len(preds))

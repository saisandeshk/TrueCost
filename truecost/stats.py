"""Small, dependency-free statistics for paired crash-test comparisons."""

from __future__ import annotations

import math
import random
from typing import Callable, Sequence


def mcnemar_exact(lost: int, gained: int) -> float:
    """Two-sided exact McNemar p-value from the discordant pairs (baseline-only vs treatment-only successes)."""
    n = lost + gained
    if n == 0:
        return 1.0
    k = min(lost, gained)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def percentile(xs: Sequence[float], q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def bootstrap_ci(n: int, stat: Callable[[list[int]], float], reps: int = 2000, alpha: float = 0.05,
                 seed: int = 0) -> tuple[float, float]:
    """Percentile bootstrap CI for a statistic of resampled item indices (paired: same indices for both arms)."""
    rng = random.Random(seed)
    vals = []
    for _ in range(reps):
        idx = [rng.randrange(n) for _ in range(n)]
        v = stat(idx)
        if v == v and not math.isinf(v):
            vals.append(v)
    if not vals:
        return float("nan"), float("nan")
    return percentile(vals, alpha / 2), percentile(vals, 1 - alpha / 2)


def sign_flip_p(diffs: list[float], reps: int = 20000, seed: int = 0) -> float:
    """Two-sided paired permutation (sign-flip) test of mean difference = 0 over independent clusters
    (one value per task). Exact enumeration up to 16 non-zero clusters, Monte Carlo above."""
    d = [x for x in diffs if x != 0]
    if not d:
        return 1.0
    obs = abs(sum(d))
    if len(d) <= 16:
        hits = total = 0
        for mask in range(1 << len(d)):
            s = sum(x if (mask >> i) & 1 else -x for i, x in enumerate(d))
            hits += abs(s) >= obs - 1e-12
            total += 1
        return hits / total
    rng = random.Random(seed)
    hits = sum(abs(sum(x if rng.random() < 0.5 else -x for x in d)) >= obs - 1e-12 for _ in range(reps))
    return (hits + 1) / (reps + 1)


def cluster_bootstrap_ci(clusters: list[list[int]], stat: Callable[[list[int]], float], reps: int = 2000,
                         alpha: float = 0.05, seed: int = 0) -> tuple[float, float]:
    """Percentile bootstrap that resamples whole clusters (e.g. all runs of one task), so run-to-run noise
    and task-to-task variation both enter the interval."""
    rng = random.Random(seed)
    vals = []
    k = len(clusters)
    for _ in range(reps):
        idx = [i for _ in range(k) for i in clusters[rng.randrange(k)]]
        v = stat(idx)
        if v == v:  # keep +inf draws (e.g. spend with no verified outcome); drop only undefined ones
            vals.append(v)
    if not vals:
        return float("nan"), float("nan")
    return percentile(vals, alpha / 2), percentile(vals, 1 - alpha / 2)

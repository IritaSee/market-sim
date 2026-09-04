"""
Headless parameter grid search — jalankan ini SEBELUM server interaktif.

Target kalibrasi (sesuai spec):
  - max_price / fundamental  : kisaran 1.50 – 2.00  (target ~1.82)
  - min_price / fundamental  : kisaran 0.55 – 0.75  (target ~0.67)
  - Minimal 3 siklus boom-bust per 600 tick
  - Tidak meledak (price ≤ 10x fundamental) dan tidak kolaps (price ≥ 1.0)

Jalankan:
    cd market-sim
    python -m tuning.grid_search
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import itertools
import numpy as np
from dataclasses import dataclass
from typing import Any

from sim.market import Market, DEFAULT_PARAMS


# ------------------------------------------------------------------ #
#  Grid yang akan dicari                                              #
# ------------------------------------------------------------------ #

PARAM_GRID = {
    "lambda_price":             [0.08, 0.12, 0.16, 0.20],
    "fundamentalist_alpha":     [1.0, 1.5, 2.0, 2.5],
    "chartist_factor":          [1.5, 2.0, 2.8, 3.5],
    "chartist_momentum_window": [3, 5, 8],
    "noise_sigma":              [0.30, 0.42, 0.55],
}

N_SEEDS   = 8     # seed per kombinasi parameter
N_TICKS   = 600   # tick per simulasi
N_AGENTS  = 100

# Threshold scoring
TARGET_MAX_RATIO_LO = 1.40
TARGET_MAX_RATIO_HI = 2.20
TARGET_MIN_RATIO_LO = 0.50
TARGET_MIN_RATIO_HI = 0.80
MIN_CYCLES = 3


# ------------------------------------------------------------------ #
#  Scoring                                                            #
# ------------------------------------------------------------------ #

@dataclass
class SimResult:
    params: dict
    score:  float
    avg_max_ratio: float
    avg_min_ratio: float
    avg_cycles: float
    stable_runs: int


def run_single(params: dict, seed: int) -> dict:
    m = Market(n_agents=N_AGENTS, params=params, seed=seed)
    prices = []
    for _ in range(N_TICKS):
        state = m.step()
        prices.append(state["price"])
    return {"prices": prices, "fundamental": m.fundamental}


def count_cycles(prices: list[float], fundamental: float, window: int = 15) -> int:
    """Hitung pergantian regime Bubble ↔ Normal ↔ Crash."""
    f = fundamental
    BUBBLE_T = 1.20
    CRASH_T  = 0.85

    regimes = []
    for p in prices:
        r = p / f
        if r > BUBBLE_T:
            regime = "bubble"
        elif r < CRASH_T:
            regime = "crash"
        else:
            regime = "normal"
        regimes.append(regime)

    # Hitung transisi unik
    transitions = 0
    smooth = [regimes[0]]
    for i in range(1, len(regimes)):
        if regimes[i] != smooth[-1]:
            smooth.append(regimes[i])
            transitions += 1
    return transitions


def score_run(prices: list[float], fundamental: float) -> tuple[float, float, float, int]:
    max_p = max(prices)
    min_p = min(prices)
    max_r = max_p / fundamental
    min_r = min_p / fundamental

    # Stabilitas: tidak meledak atau kolaps ekstrem
    stable = all(0.05 < p / fundamental < 8.0 for p in prices)
    if not stable:
        return 0.0, max_r, min_r, 0

    score = 0.0

    # Poin untuk range atas
    if TARGET_MAX_RATIO_LO <= max_r <= TARGET_MAX_RATIO_HI:
        score += 3.0
    elif max_r > TARGET_MAX_RATIO_HI:
        score += max(0.0, 3.0 - (max_r - TARGET_MAX_RATIO_HI) * 2)
    else:
        score += max(0.0, (max_r - 1.1) / (TARGET_MAX_RATIO_LO - 1.1) * 2)

    # Poin untuk range bawah
    if TARGET_MIN_RATIO_LO <= min_r <= TARGET_MIN_RATIO_HI:
        score += 3.0
    elif min_r < TARGET_MIN_RATIO_LO:
        score += max(0.0, 3.0 - (TARGET_MIN_RATIO_LO - min_r) * 3)
    else:
        score += max(0.0, (0.95 - min_r) / (0.95 - TARGET_MIN_RATIO_HI) * 2)

    # Poin untuk siklus
    n_cycles = count_cycles(prices, fundamental)
    if n_cycles >= MIN_CYCLES * 2:
        score += 4.0
    elif n_cycles >= MIN_CYCLES:
        score += 2.0 + (n_cycles - MIN_CYCLES) * 0.4
    else:
        score += n_cycles * 0.5

    return score, max_r, min_r, n_cycles


# ------------------------------------------------------------------ #
#  Main grid search                                                   #
# ------------------------------------------------------------------ #

def grid_search(top_n: int = 5, verbose: bool = True) -> list[SimResult]:
    keys   = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    combos = list(itertools.product(*values))

    total = len(combos)
    if verbose:
        print(f"\n=== Grid Search: {total} kombinasi × {N_SEEDS} seeds × {N_TICKS} ticks ===\n")

    results: list[SimResult] = []

    for ci, combo in enumerate(combos):
        params = {**DEFAULT_PARAMS, **dict(zip(keys, combo))}

        scores_per_seed  = []
        max_ratios       = []
        min_ratios       = []
        cycle_counts     = []
        stable_count     = 0

        for seed in range(N_SEEDS):
            out    = run_single(params, seed)
            s, mx, mn, cyc = score_run(out["prices"], out["fundamental"])
            scores_per_seed.append(s)
            max_ratios.append(mx)
            min_ratios.append(mn)
            cycle_counts.append(cyc)
            if s > 0:
                stable_count += 1

        mean_score = float(np.mean(scores_per_seed))
        results.append(SimResult(
            params         = dict(zip(keys, combo)),
            score          = mean_score,
            avg_max_ratio  = float(np.mean(max_ratios)),
            avg_min_ratio  = float(np.mean(min_ratios)),
            avg_cycles     = float(np.mean(cycle_counts)),
            stable_runs    = stable_count,
        ))

        if verbose and (ci + 1) % 50 == 0:
            print(f"  [{ci+1}/{total}] best score so far: "
                  f"{max(r.score for r in results):.2f}")

    results.sort(key=lambda r: r.score, reverse=True)

    if verbose:
        print(f"\n=== TOP {top_n} HASIL ===\n")
        for rank, r in enumerate(results[:top_n], 1):
            print(f"[#{rank}] Score={r.score:.2f} | "
                  f"max_ratio={r.avg_max_ratio:.2f} | "
                  f"min_ratio={r.avg_min_ratio:.2f} | "
                  f"cycles={r.avg_cycles:.1f} | "
                  f"stable={r.stable_runs}/{N_SEEDS}")
            for k, v in r.params.items():
                print(f"       {k:35s} = {v}")
            print()

        best = results[0]
        print("=== PARAMETER TERBAIK (copy ke DEFAULT_PARAMS di sim/market.py) ===\n")
        for k, v in best.params.items():
            print(f'    "{k}": {v},')

    return results


if __name__ == "__main__":
    grid_search(top_n=5, verbose=True)

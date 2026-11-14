#!/usr/bin/env python3
"""
Fit Peloton's power -> speed curve from your P2G JSON ride history and emit a
C header for the Peloton BLE bridge firmware.

Peloton does not measure speed; it derives it from power. Empirically the
relationship is almost deterministic and is captured very well by

    speed_mph = a * P + b * sqrt(P) + c          (P = power in watts, P > 0)

The fit is ordinary least squares on every 1-second sample, validated with
k-fold cross-validation grouped by ride.

Usage:
    python3 fit_speed_model.py output/json                       # report only
    python3 fit_speed_model.py output/json --header ../firmware/peloton_ble_bridge/speed_model.h

Standard library only (Python 3.9+).
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
import statistics
import sys
from dataclasses import dataclass

MPH_PER_KPH = 1 / 1.609344


@dataclass
class Ride:
    ride_id: str
    power_w: list[float]
    speed_mph: list[float]


@dataclass
class Model:
    a: float
    b: float
    c: float
    p_max: float = math.inf  # beyond this, extrapolate linearly along the tangent

    def _curve(self, p: float) -> float:
        return self.a * p + self.b * math.sqrt(p) + self.c

    def predict(self, power_w: float) -> float:
        if power_w <= 0:
            return 0.0
        if power_w > self.p_max:
            slope = max(0.0, self.a + self.b / (2 * math.sqrt(self.p_max)))
            return max(0.0, self._curve(self.p_max) + slope * (power_w - self.p_max))
        return max(0.0, self._curve(power_w))


@dataclass
class Metrics:
    mae_mph: float
    rmse_mph: float
    r2: float
    ride_distance_mape_pct: float
    ride_distance_max_pct: float


# --------------------------------------------------------------------------- #
def _metric(metrics: list[dict], slug: str) -> dict | None:
    return next((m for m in metrics or [] if (m.get("Slug") or "").lower() == slug), None)


def load_ride(path: str) -> Ride | None:
    with open(path, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    workout = data.get("Workout") or {}
    if (workout.get("Fitness_Discipline") or "").lower() != "cycling":
        return None
    metrics = (data.get("WorkoutSamples") or {}).get("Metrics") or []
    pwr, spd = _metric(metrics, "output"), _metric(metrics, "speed")
    if not pwr or not spd:
        return None
    unit = (spd.get("Display_Unit") or "").lower()
    if unit in ("mph", "mi/h"):
        factor = 1.0
    elif unit in ("kph", "km/h", "kmh"):
        factor = MPH_PER_KPH
    else:
        return None
    ride = Ride(workout.get("Id") or os.path.basename(path), [], [])
    for p, s in zip(pwr.get("Values") or [], spd.get("Values") or []):
        if p is None or s is None or p <= 0 or s <= 0:
            continue
        ride.power_w.append(float(p))
        ride.speed_mph.append(float(s) * factor)
    return ride if ride.power_w else None


def load_rides(paths: list[str], min_samples: int = 30) -> list[Ride]:
    files: list[str] = []
    for p in paths:
        files += sorted(glob.glob(os.path.join(p, "**", "*.json"), recursive=True)) if os.path.isdir(p) else sorted(glob.glob(p))
    rides, seen = [], set()
    for f in files:
        try:
            r = load_ride(f)
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            continue
        if r and len(r.power_w) >= min_samples and r.ride_id not in seen:
            seen.add(r.ride_id)
            rides.append(r)
    return rides


# --------------------------------------------------------------------------- #
def _solve3(m: list[list[float]], v: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting for a 3x3 system."""
    a = [row[:] + [v[i]] for i, row in enumerate(m)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            raise ValueError("Singular system: need samples over a wider range of power")
        a[col], a[piv] = a[piv], a[col]
        for r in range(3):
            if r != col:
                f = a[r][col] / a[col][col]
                a[r] = [x - f * y for x, y in zip(a[r], a[col])]
    return [a[i][3] / a[i][i] for i in range(3)]


def fit(rides: list[Ride]) -> Model:
    # Normal equations for features [P, sqrt(P), 1]
    xtx = [[0.0] * 3 for _ in range(3)]
    xty = [0.0] * 3
    for r in rides:
        for p, s in zip(r.power_w, r.speed_mph):
            x = (p, math.sqrt(p), 1.0)
            for i in range(3):
                xty[i] += x[i] * s
                for j in range(3):
                    xtx[i][j] += x[i] * x[j]
    p_max = max(p for r in rides for p in r.power_w)
    return Model(*_solve3(xtx, xty), p_max=p_max)


def evaluate(pairs: list[tuple[Ride, Model]]) -> Metrics:
    errs, ys, ride_errs = [], [], []
    for r, m in pairs:
        d_true = d_pred = 0.0
        for p, s in zip(r.power_w, r.speed_mph):
            pred = m.predict(p)
            errs.append(pred - s)
            ys.append(s)
            d_true += s
            d_pred += pred
        ride_errs.append(abs(d_pred - d_true) / d_true * 100)
    mean_y = statistics.fmean(ys)
    ss_res = sum(e * e for e in errs)
    ss_tot = sum((y - mean_y) ** 2 for y in ys) or 1e-12
    return Metrics(statistics.fmean(abs(e) for e in errs), math.sqrt(ss_res / len(errs)),
                   1 - ss_res / ss_tot, statistics.fmean(ride_errs), max(ride_errs))


def cross_validate(rides: list[Ride], folds: int = 5, seed: int = 42) -> Metrics:
    k = max(2, min(folds, len(rides)))
    idx = list(range(len(rides)))
    random.Random(seed).shuffle(idx)
    pairs = []
    for g in (idx[i::k] for i in range(k)):
        gs = set(g)
        m = fit([rides[i] for i in idx if i not in gs])
        pairs += [(rides[i], m) for i in g]
    return evaluate(pairs)


def _c_float(x: float) -> str:
    """C float literal; always has a '.' or exponent so the 'f' suffix is valid."""
    t = f"{x:.9g}"
    if not any(ch in t for ch in ".eEn"):
        t += ".0"
    return t + "f"


def render_header(m: Model, rides: int, samples: int, p_min: float, p_max: float, cv: Metrics | None) -> str:
    cv_line = (f" * Cross-validated: MAE {cv.mae_mph:.3f} mph, ride distance error "
               f"{cv.ride_distance_mape_pct:.2f}% avg / {cv.ride_distance_max_pct:.2f}% max\n") if cv else ""
    return (
        "// Generated by tools/peloton-ble-bridge/model/fit_speed_model.py - do not edit by hand.\n"
        "#pragma once\n\n"
        "/*\n"
        " * Peloton speed model:  speed_mph = A*P + B*sqrt(P) + C   (P in watts, 0 < P <= P_MAX)\n"
        " * Above P_MAX the firmware extrapolates along the tangent at P_MAX.\n"
        f" * Trained on {rides} rides / {samples} samples, power range {p_min:.0f}-{p_max:.0f} W.\n"
        f"{cv_line}"
        " */\n"
        f"#define SPEED_MODEL_A ({_c_float(m.a)})\n"
        f"#define SPEED_MODEL_B ({_c_float(m.b)})\n"
        f"#define SPEED_MODEL_C ({_c_float(m.c)})\n"
        f"#define SPEED_MODEL_P_MAX ({_c_float(m.p_max)})\n"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fit Peloton power->speed model from P2G JSON files.")
    ap.add_argument("paths", nargs="+", help="P2G JSON files, globs or directories (e.g. output/json)")
    ap.add_argument("--header", help="write firmware header (speed_model.h) to this path")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--min-samples", type=int, default=30)
    args = ap.parse_args(argv)

    rides = load_rides(args.paths, args.min_samples)
    if not rides:
        print("No usable cycling rides found. Enable Format.Json in P2G and point this at output/json.", file=sys.stderr)
        return 1
    model = fit(rides)
    samples = sum(len(r.power_w) for r in rides)
    all_p = [p for r in rides for p in r.power_w]
    train = evaluate([(r, model) for r in rides])
    cv = cross_validate(rides, args.folds) if len(rides) >= 2 else None

    print(f"Trained on {len(rides)} rides / {samples:,} samples, power {min(all_p):.0f}-{max(all_p):.0f} W")
    print(f"speed_mph = {model.a:.6f}*P + {model.b:.6f}*sqrt(P) + {model.c:.6f}  (P <= {model.p_max:.0f} W)")
    print(f"train: MAE {train.mae_mph:.3f} mph, R² {train.r2:.5f}")
    if cv:
        print(f"CV   : MAE {cv.mae_mph:.3f} mph, R² {cv.r2:.5f}, ride distance error "
              f"{cv.ride_distance_mape_pct:.2f}% avg / {cv.ride_distance_max_pct:.2f}% max")
    if args.header:
        with open(args.header, "w", encoding="utf-8") as fh:
            fh.write(render_header(model, len(rides), samples, min(all_p), max(all_p), cv))
        print(f"wrote {args.header}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

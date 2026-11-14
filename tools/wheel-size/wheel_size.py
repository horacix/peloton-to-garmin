#!/usr/bin/env python3
"""
Estimate the "wheel size" (circumference) to configure on a Garmin device for a
Bluetooth/ANT+ speed sensor mounted on a Peloton Bike, using your Peloton ride
history as training data.

How it works
------------
A speed sensor counts revolutions of whatever it is strapped to. Garmin turns
those revolutions into speed with a single number, the wheel circumference C:

    garmin_speed = sensor_rpm / 60 * C

On a Peloton the sensor normally goes on the crank arm (the flywheel is
enclosed), so sensor_rpm == cadence. Peloton, however, computes its displayed
speed from *power*, not cadence. A single C therefore can only approximate
Peloton's speed; the job of the model is to pick the C that makes the error as
small as possible across all your rides.

We fit a no-intercept linear model  speed = k * cadence  on every 1-second
sample of every ride, and report three estimators of k (converted to mm):

* ride      - minimises the mean absolute *per-ride* distance error (%).
              This is the error you actually see on your watch after each
              ride. RECOMMENDED (default).
* distance  - ratio of sums, sum(speed) / sum(cadence). Makes total Garmin
              distance equal total Peloton distance over your history.
              Unbiased in aggregate (e.g. yearly totals).
* ols       - least squares through the origin, sum(speed*cadence)/sum(cadence^2).
              Minimises squared speed error, sample by sample.
* median    - median of the per-sample ratios; robust to junk samples.

Each estimator is validated with leave-rides-out cross-validation so the error
numbers reflect rides the model has not seen.

Input
-----
The JSON files P2G writes when `Format.Json` is enabled (by default in
`output/json/`). Non-cycling workouts are skipped automatically.

Only dependency: Python 3.9+ (standard library only).
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
from dataclasses import dataclass, field

MM_PER_MILE = 1_609_344.0
MM_PER_KM = 1_000_000.0
SECONDS_PER_HOUR = 3600.0

ESTIMATORS = ("ride", "distance", "ols", "median")


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #
@dataclass
class Ride:
    ride_id: str
    title: str
    cadence_rpm: list[float] = field(default_factory=list)
    speed_mm_s: list[float] = field(default_factory=list)
    resistance: list[float | None] = field(default_factory=list)
    dt_s: list[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.cadence_rpm)


def _speed_factor_to_mm_s(unit: str | None) -> float:
    unit = (unit or "").strip().lower()
    if unit in ("mph", "mi/h"):
        return MM_PER_MILE / SECONDS_PER_HOUR
    if unit in ("kph", "km/h", "kmh"):
        return MM_PER_KM / SECONDS_PER_HOUR
    raise ValueError(f"Unsupported speed unit: {unit!r}")


def _find_metric(metrics: list[dict], slug: str) -> dict | None:
    for m in metrics or []:
        if (m.get("Slug") or "").lower() == slug:
            return m
    return None


def load_ride(path: str, min_cadence: float = 20.0, sensor_ratio: float = 1.0) -> Ride | None:
    """Parse a P2G JSON workout. Returns None if it is not a usable cycling ride.

    sensor_ratio = sensor revolutions per crank revolution (1.0 for a crank mount).
    """
    with open(path, encoding="utf-8-sig") as fh:
        data = json.load(fh)

    workout = data.get("Workout") or {}
    if (workout.get("Fitness_Discipline") or "").lower() != "cycling":
        return None

    samples = data.get("WorkoutSamples") or {}
    metrics = samples.get("Metrics") or []
    cad_m = _find_metric(metrics, "cadence")
    spd_m = _find_metric(metrics, "speed")
    if not cad_m or not spd_m:
        return None

    cad = cad_m.get("Values") or []
    spd = spd_m.get("Values") or []
    res_m = _find_metric(metrics, "resistance")
    res = (res_m.get("Values") if res_m else None) or []
    try:
        factor = _speed_factor_to_mm_s(spd_m.get("Display_Unit"))
    except ValueError:
        return None

    secs = samples.get("Seconds_Since_Pedaling_Start") or []
    n = min(len(cad), len(spd))
    ride = Ride(ride_id=workout.get("Id") or os.path.basename(path),
                title=(workout.get("Ride") or {}).get("Title") or workout.get("Name") or "")

    for i in range(n):
        c, s = cad[i], spd[i]
        if c is None or s is None:
            continue
        if c < min_cadence or s <= 0:
            continue  # coasting / stopped / sensor dropout
        if i + 1 < len(secs) and secs[i + 1] is not None and secs[i] is not None:
            dt = float(secs[i + 1] - secs[i])
        else:
            dt = 1.0
        if dt <= 0 or dt > 10:
            dt = 1.0
        ride.cadence_rpm.append(float(c) * sensor_ratio)
        ride.speed_mm_s.append(float(s) * factor)
        ride.resistance.append(float(res[i]) if i < len(res) and res[i] is not None else None)
        ride.dt_s.append(dt)

    return ride if len(ride) else None


def load_rides(paths: list[str], min_cadence: float, sensor_ratio: float, min_samples: int) -> list[Ride]:
    files: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "**", "*.json"), recursive=True)))
        else:
            files.extend(sorted(glob.glob(p)))
    rides = []
    seen: set[str] = set()
    for f in files:
        try:
            r = load_ride(f, min_cadence=min_cadence, sensor_ratio=sensor_ratio)
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            continue
        if r and len(r) >= min_samples and r.ride_id not in seen:
            seen.add(r.ride_id)
            rides.append(r)
    return rides


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
def _rev_per_s(c_rpm: float) -> float:
    return c_rpm / 60.0


def fit(rides: list[Ride], estimator: str = "distance") -> float:
    """Return circumference in mm."""
    if estimator == "ride":
        # minimise sum_r |C*R_r - D_r| / D_r  ==  weighted median of x_r = D_r/R_r, weights 1/x_r
        pts = []
        for r in rides:
            d = sum(s * dt for s, dt in zip(r.speed_mm_s, r.dt_s))
            revs = sum(_rev_per_s(c) * dt for c, dt in zip(r.cadence_rpm, r.dt_s))
            if d > 0 and revs > 0:
                pts.append((d / revs, revs / d))
        if not pts:
            raise ValueError("No usable samples to fit")
        pts.sort()
        half, acc = sum(w for _, w in pts) / 2, 0.0
        for x, w in pts:
            acc += w
            if acc >= half:
                return x
    if estimator == "distance":
        # Time-weighted: sum(speed*dt) / sum(rev_rate*dt)  == total dist / total revs
        num = sum(s * dt for r in rides for s, dt in zip(r.speed_mm_s, r.dt_s))
        den = sum(_rev_per_s(c) * dt for r in rides for c, dt in zip(r.cadence_rpm, r.dt_s))
    elif estimator == "ols":
        num = sum(s * _rev_per_s(c) for r in rides for s, c in zip(r.speed_mm_s, r.cadence_rpm))
        den = sum(_rev_per_s(c) ** 2 for r in rides for c in r.cadence_rpm)
    elif estimator == "median":
        return statistics.median(s / _rev_per_s(c) for r in rides for s, c in zip(r.speed_mm_s, r.cadence_rpm))
    else:
        raise ValueError(f"Unknown estimator {estimator!r}")
    if den <= 0:
        raise ValueError("No usable samples to fit")
    return num / den


@dataclass
class Metrics:
    speed_mae_kph: float
    speed_rmse_kph: float
    r2: float
    total_distance_err_pct: float
    ride_distance_mape_pct: float
    ride_distance_p90_pct: float


def evaluate(rides: list[Ride], circumference_mm: float) -> Metrics:
    to_kph = SECONDS_PER_HOUR / MM_PER_KM
    errs, ys = [], []
    ride_errs = []
    tot_true = tot_pred = 0.0
    for r in rides:
        d_true = d_pred = 0.0
        for c, s, dt in zip(r.cadence_rpm, r.speed_mm_s, r.dt_s):
            pred = _rev_per_s(c) * circumference_mm
            errs.append((pred - s) * to_kph)
            ys.append(s * to_kph)
            d_true += s * dt
            d_pred += pred * dt
        tot_true += d_true
        tot_pred += d_pred
        if d_true > 0:
            ride_errs.append(abs(d_pred - d_true) / d_true * 100)
    mean_y = statistics.fmean(ys)
    ss_res = sum(e * e for e in errs)
    ss_tot = sum((y - mean_y) ** 2 for y in ys) or 1e-12
    ride_errs.sort()
    p90 = ride_errs[min(len(ride_errs) - 1, int(math.ceil(0.9 * len(ride_errs))) - 1)] if ride_errs else float("nan")
    return Metrics(
        speed_mae_kph=statistics.fmean(abs(e) for e in errs),
        speed_rmse_kph=math.sqrt(ss_res / len(errs)),
        r2=1 - ss_res / ss_tot,
        total_distance_err_pct=(tot_pred - tot_true) / tot_true * 100 if tot_true else float("nan"),
        ride_distance_mape_pct=statistics.fmean(ride_errs) if ride_errs else float("nan"),
        ride_distance_p90_pct=p90,
    )


def cross_validate(rides: list[Ride], estimator: str, folds: int = 5, seed: int = 42) -> tuple[Metrics, list[float]]:
    """K-fold CV grouped by ride (no leakage of a ride's samples into its own fit)."""
    k = max(2, min(folds, len(rides)))
    idx = list(range(len(rides)))
    random.Random(seed).shuffle(idx)
    groups = [idx[i::k] for i in range(k)]
    fitted = []
    held_out_preds: list[tuple[Ride, float]] = []
    for g in groups:
        g_set = set(g)
        train = [rides[i] for i in idx if i not in g_set]
        c = fit(train, estimator)
        fitted.append(c)
        held_out_preds.extend((rides[i], c) for i in g)
    # Pool held-out predictions into one metric set.
    pooled = _evaluate_pairs(held_out_preds)
    return pooled, fitted


def _evaluate_pairs(pairs: list[tuple[Ride, float]]) -> Metrics:
    # Evaluate each ride with its own (out-of-fold) circumference by scaling cadence.
    scaled = []
    for r, c in pairs:
        scaled.append(Ride(r.ride_id, r.title, [x * c for x in r.cadence_rpm], r.speed_mm_s, r.resistance, r.dt_s))
    return evaluate(scaled, 1.0)


def bootstrap_ci(rides: list[Ride], estimator: str, n: int = 200, seed: int = 7) -> tuple[float, float]:
    """95% CI of the circumference, resampling whole rides."""
    rng = random.Random(seed)
    vals = sorted(fit([rides[rng.randrange(len(rides))] for _ in rides], estimator) for _ in range(n))
    return vals[int(0.025 * n)], vals[int(0.975 * n) - 1]


def resistance_breakdown(rides: list[Ride], circumference_mm: float, bins=(0, 30, 40, 50, 60, 101)) -> list[tuple[str, int, float, float]]:
    """Per-resistance-band implied circumference and speed bias of the chosen C."""
    out = []
    to_kph = SECONDS_PER_HOUR / MM_PER_KM
    for lo, hi in zip(bins, bins[1:]):
        num = den = bias = 0.0
        n = 0
        for r in rides:
            for c, s, dt, res in zip(r.cadence_rpm, r.speed_mm_s, r.dt_s, r.resistance):
                if res is None or not (lo <= res < hi):
                    continue
                num += s * dt
                den += _rev_per_s(c) * dt
                bias += (_rev_per_s(c) * circumference_mm - s) * to_kph
                n += 1
        if n:
            out.append((f"{lo}-{min(hi, 100)}%", n, num / den, bias / n))
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="P2G JSON files, globs or directories (e.g. output/json)")
    ap.add_argument("--min-cadence", type=float, default=20.0, help="drop samples below this rpm (default 20)")
    ap.add_argument("--min-samples", type=int, default=60, help="drop rides with fewer usable samples (default 60)")
    ap.add_argument("--sensor-ratio", type=float, default=1.0,
                    help="sensor revolutions per crank revolution; 1.0 for a crank/pedal mount (default)")
    ap.add_argument("--folds", type=int, default=5, help="cross-validation folds (default 5)")
    ap.add_argument("--estimator", choices=ESTIMATORS, default="ride", help="estimator to recommend (default ride)")
    ap.add_argument("--json", action="store_true", help="print machine-readable results")
    args = ap.parse_args(argv)

    rides = load_rides(args.paths, args.min_cadence, args.sensor_ratio, args.min_samples)
    if not rides:
        print("No usable cycling rides found. Enable Format.Json in P2G and point this at output/json.", file=sys.stderr)
        return 1

    n_samples = sum(len(r) for r in rides)
    results = {}
    for est in ESTIMATORS:
        c = fit(rides, est)
        cv, fold_vals = cross_validate(rides, est, args.folds) if len(rides) >= 2 else (evaluate(rides, c), [c])
        results[est] = {"circumference_mm": c, "train": evaluate(rides, c), "cv": cv, "fold_values": fold_vals}

    best = results[args.estimator]
    c_best = best["circumference_mm"]
    ci = bootstrap_ci(rides, args.estimator) if len(rides) >= 3 else (float("nan"), float("nan"))
    bands = resistance_breakdown(rides, c_best)

    if args.json:
        print(json.dumps({
            "rides": len(rides), "samples": n_samples, "recommended_estimator": args.estimator,
            "recommended_circumference_mm": round(c_best), "diameter_mm": round(c_best / math.pi),
            "ci95_mm": [round(ci[0]), round(ci[1])] if not math.isnan(ci[0]) else None,
            "estimators": {k: {"circumference_mm": round(v["circumference_mm"], 1),
                               "train": vars(v["train"]), "cv": vars(v["cv"])} for k, v in results.items()},
            "resistance_bands": [{"band": b, "samples": n, "implied_circumference_mm": round(c), "speed_bias_kph": round(bias, 2)}
                                 for b, n, c, bias in bands],
        }, indent=2))
        return 0

    print(f"Trained on {len(rides)} rides / {n_samples:,} one-second samples\n")
    print(f"{'estimator':<10}{'C (mm)':>9}{'CV speed MAE':>14}{'CV R²':>8}{'CV ride dist err':>18}{'CV total dist':>15}")
    for est, v in results.items():
        cv = v["cv"]
        mark = " *" if est == args.estimator else ""
        print(f"{est + mark:<10}{v['circumference_mm']:>9.0f}{cv.speed_mae_kph:>10.2f} kph{cv.r2:>8.2f}"
              f"{cv.ride_distance_mape_pct:>13.1f}% avg{cv.total_distance_err_pct:>+14.1f}%")

    print("\nImplied circumference by resistance band (why one number can't be perfect):")
    print(f"{'band':<10}{'samples':>10}{'implied C':>12}{'speed bias w/ rec. C':>23}")
    for b, n, c, bias in bands:
        print(f"{b:<10}{n:>10,}{c:>9.0f} mm{bias:>+19.2f} kph")

    print("\n" + "=" * 60)
    print(f"Recommended Garmin wheel size: {round(c_best)} mm (circumference)")
    print(f"  equivalent wheel diameter : {c_best / math.pi:.0f} mm")
    if not math.isnan(ci[0]):
        print(f"  95% CI (bootstrap by ride): {ci[0]:.0f} - {ci[1]:.0f} mm")
    print(f"  expected per-ride distance error: ~{best['cv'].ride_distance_mape_pct:.1f}% avg, "
          f"{best['cv'].ride_distance_p90_pct:.1f}% at p90")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

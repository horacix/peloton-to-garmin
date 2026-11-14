# Peloton speed-sensor wheel size estimator

This tool works out the **wheel size (circumference, in mm)** to enter on a Garmin watch or bike computer when you put a Bluetooth/ANT+ **speed sensor on a Peloton Bike's crank**. It fits that number to your own Peloton ride history, so the speed and distance on your Garmin come as close as they can to what Peloton reports.

## Why a model is needed

A speed sensor only counts revolutions. Garmin converts them to speed with one constant:

```
garmin_speed = sensor_rpm / 60 × wheel_circumference
```

Peloton calculates its speed from **power** (cadence × resistance), not from cadence alone. At the same 80 rpm, you go faster on Peloton at 45% resistance than at 25%. No single circumference can match every ride. The tool looks for the circumference that does best across your history, then tells you how far off you should expect to be.

## 1. Export your rides

Enable JSON output in P2G (`configuration.local.json`):

```json
"Format": { "Json": true }
```

Then sync as many rides as you like (for example `"Peloton": { "NumWorkoutsToDownload": 200 }`). The JSON files land in `output/json/`. They hold 1-second cadence, speed and resistance samples for every ride.

## 2. Run the estimator

Requires Python 3.9 or later. It uses the standard library only.

```bash
python3 tools/wheel-size/wheel_size.py output/json
```

Example output:

```
estimator    C (mm)  CV speed MAE   CV R²  CV ride dist err  CV total dist
ride *         3971      3.36 kph   -0.85          7.0% avg          -9.4%
distance       4355      4.49 kph   -1.33         16.2% avg          -0.4%
...
Recommended Garmin wheel size: 3971 mm (circumference)
```

Enter the recommended number as the sensor's **wheel size** (manual, in mm) on your Garmin.

### Estimators

| Estimator | What it optimizes | When to use it |
|---|---|---|
| `ride` (default) | Mean per-ride distance error | You care about each ride's distance looking right |
| `distance` | Total distance over all rides (no bias) | You care about weekly or yearly totals |
| `ols` | Squared speed error, sample by sample | You care about live speed |
| `median` | Median speed/cadence ratio | Robust baseline |

All error figures are **cross-validated by ride**: each ride is scored with a circumference fitted without it.

The **resistance-band table** shows the circumference each resistance range would want. If you mostly ride one style of class, filter your exports to those rides to get a closer fit.

### Options

| Flag | Default | Purpose |
|---|---|---|
| `--estimator` | `ride` | Which estimator to recommend |
| `--min-cadence` | `20` | Drop samples below this rpm (coasting) |
| `--min-samples` | `60` | Drop very short rides |
| `--sensor-ratio` | `1.0` | Sensor revolutions per crank revolution (keep 1.0 for a crank or pedal mount) |
| `--folds` | `5` | Cross-validation folds |
| `--json` | off | Machine-readable output |

## Tests

```bash
cd tools/wheel-size && python3 -m unittest -v
```

## Want exact speed instead?

A cadence-only sensor can't see resistance, so a single wheel size is always a compromise. [`tools/peloton-ble-bridge`](../peloton-ble-bridge/README.md) is a small device that reads power from the bike's sensor cable and broadcasts speed calculated from it.

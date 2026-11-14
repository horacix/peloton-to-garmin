import json
import math
import os
import tempfile
import unittest

import wheel_size as ws

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SAMPLE_DIR = os.path.join(REPO_ROOT, "src", "UnitTests", "Data", "p2g_workouts")


def make_workout(cadence, speed, unit="mph", discipline="Cycling", resistance=None, workout_id="w1"):
    metrics = [
        {"Slug": "cadence", "Display_Unit": "rpm", "Values": cadence},
        {"Slug": "speed", "Display_Unit": unit, "Values": speed},
    ]
    if resistance is not None:
        metrics.append({"Slug": "resistance", "Display_Unit": "%", "Values": resistance})
    return {
        "Workout": {"Id": workout_id, "Fitness_Discipline": discipline, "Name": "test"},
        "WorkoutSamples": {"Seconds_Since_Pedaling_Start": list(range(len(cadence))), "Metrics": metrics},
    }


def speed_for(cadence_rpm, circumference_mm, unit="mph"):
    mm_s = cadence_rpm / 60 * circumference_mm
    return mm_s / ws._speed_factor_to_mm_s(unit)


class WheelSizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    def synthetic_rides(self, circumference, unit="mph", n=4):
        for i in range(n):
            cad = [60 + (j % 40) for j in range(120)]
            self.write(f"r{i}.json", make_workout(cad, [speed_for(c, circumference, unit) for c in cad],
                                                  unit=unit, workout_id=f"w{i}"))
        return ws.load_rides([self.tmp.name], min_cadence=20, sensor_ratio=1.0, min_samples=10)

    def test_every_estimator_recovers_exact_circumference(self):
        rides = self.synthetic_rides(3000)
        for est in ws.ESTIMATORS:
            with self.subTest(est=est):
                self.assertAlmostEqual(ws.fit(rides, est), 3000, delta=0.5)

    def test_kph_units_are_supported(self):
        rides = self.synthetic_rides(2500, unit="kph")
        self.assertAlmostEqual(ws.fit(rides, "distance"), 2500, delta=0.5)

    def test_perfect_fit_has_zero_error(self):
        rides = self.synthetic_rides(3000)
        m = ws.evaluate(rides, 3000)
        self.assertAlmostEqual(m.speed_mae_kph, 0, places=6)
        self.assertAlmostEqual(m.total_distance_err_pct, 0, places=6)

    def test_cross_validation_on_perfect_data_has_zero_error(self):
        rides = self.synthetic_rides(3000)
        cv, folds = ws.cross_validate(rides, "distance", folds=2)
        self.assertAlmostEqual(cv.ride_distance_mape_pct, 0, places=6)
        self.assertEqual(len(folds), 2)

    def test_distance_estimator_matches_total_distance(self):
        # Two rides with different speed/cadence ratios -> total distance must still match.
        cad = [80] * 100
        self.write("a.json", make_workout(cad, [speed_for(c, 2000) for c in cad], workout_id="a"))
        self.write("b.json", make_workout(cad, [speed_for(c, 4000) for c in cad], workout_id="b"))
        rides = ws.load_rides([self.tmp.name], 20, 1.0, 10)
        c = ws.fit(rides, "distance")
        self.assertAlmostEqual(ws.evaluate(rides, c).total_distance_err_pct, 0, places=6)

    def test_ride_estimator_picks_majority_ride_ratio(self):
        cad = [80] * 100
        for i, c_mm in enumerate([3000, 3000, 3000, 6000]):
            self.write(f"{i}.json", make_workout(cad, [speed_for(c, c_mm) for c in cad], workout_id=str(i)))
        rides = ws.load_rides([self.tmp.name], 20, 1.0, 10)
        self.assertAlmostEqual(ws.fit(rides, "ride"), 3000, delta=0.5)

    def test_low_cadence_and_zero_speed_samples_are_dropped(self):
        self.write("r.json", make_workout([0, 10, 80, 80, None], [0, 1.0, 0, 20.0, 20.0]))
        ride = ws.load_ride(os.path.join(self.tmp.name, "r.json"), min_cadence=20)
        self.assertEqual(len(ride), 1)

    def test_non_cycling_workouts_are_skipped(self):
        path = self.write("run.json", make_workout([80], [6.0], discipline="Running"))
        self.assertIsNone(ws.load_ride(path))

    def test_sensor_ratio_scales_circumference(self):
        rides_crank = self.synthetic_rides(3000)
        rides_fly = ws.load_rides([self.tmp.name], 20, sensor_ratio=10.0, min_samples=10)
        self.assertAlmostEqual(ws.fit(rides_fly, "distance") * 10, ws.fit(rides_crank, "distance"), delta=0.5)

    def test_duplicate_workout_ids_are_counted_once(self):
        cad = [80] * 100
        w = make_workout(cad, [speed_for(c, 3000) for c in cad], workout_id="same")
        self.write("a.json", w)
        self.write("b.json", w)
        self.assertEqual(len(ws.load_rides([self.tmp.name], 20, 1.0, 10)), 1)

    @unittest.skipUnless(os.path.isdir(SAMPLE_DIR), "repo sample data not present")
    def test_real_p2g_sample_files_load_and_produce_plausible_size(self):
        rides = ws.load_rides([SAMPLE_DIR], 20, 1.0, 60)
        self.assertGreater(len(rides), 0)
        c = ws.fit(rides, "ride")
        self.assertTrue(1000 < c < 10000, c)

    def test_cli_json_output(self):
        self.synthetic_rides(3000)
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ws.main([self.tmp.name, "--json", "--min-samples", "10"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["recommended_circumference_mm"], 3000)
        self.assertEqual(out["diameter_mm"], round(3000 / math.pi))

    def test_cli_returns_error_when_no_rides(self):
        self.assertEqual(ws.main([self.tmp.name]), 1)


if __name__ == "__main__":
    unittest.main()

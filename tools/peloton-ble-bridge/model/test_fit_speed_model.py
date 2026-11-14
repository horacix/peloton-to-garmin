import json
import math
import os
import re
import tempfile
import unittest

import fit_speed_model as fsm

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SAMPLE_DIR = os.path.join(REPO_ROOT, "src", "UnitTests", "Data", "p2g_workouts")


def workout(power, speed, unit="mph", discipline="Cycling", wid="w"):
    return {
        "Workout": {"Id": wid, "Fitness_Discipline": discipline},
        "WorkoutSamples": {"Metrics": [
            {"Slug": "output", "Values": power},
            {"Slug": "speed", "Display_Unit": unit, "Values": speed},
        ]},
    }


def truth(p):
    return -0.05 * p + 2.3 * math.sqrt(p) - 1.8


class FitSpeedModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        with open(os.path.join(self.tmp.name, name), "w") as fh:
            json.dump(data, fh)

    def synthetic(self, n=4, unit="mph"):
        for i in range(n):
            p = [20 + ((j * 7 + i * 13) % 250) for j in range(100)]
            s = [truth(x) * (1.609344 if unit == "kph" else 1) for x in p]
            self.write(f"{i}.json", workout(p, s, unit=unit, wid=str(i)))
        return fsm.load_rides([self.tmp.name])

    def test_recovers_exact_coefficients(self):
        m = fsm.fit(self.synthetic())
        self.assertAlmostEqual(m.a, -0.05, places=6)
        self.assertAlmostEqual(m.b, 2.3, places=5)
        self.assertAlmostEqual(m.c, -1.8, places=4)

    def test_kph_input_is_converted_to_mph(self):
        m = fsm.fit(self.synthetic(unit="kph"))
        self.assertAlmostEqual(m.b, 2.3, places=5)

    def test_predict_is_zero_for_no_power_and_never_negative(self):
        m = fsm.Model(-0.05, 2.3, -1.8)
        self.assertEqual(m.predict(0), 0.0)
        self.assertEqual(m.predict(-5), 0.0)
        self.assertEqual(m.predict(0.1), 0.0)  # curve is negative here -> clamped

    def test_extrapolation_above_p_max_is_monotonic(self):
        m = fsm.Model(-0.05, 2.3, -1.8, p_max=300)
        speeds = [m.predict(p) for p in range(300, 1500, 50)]
        self.assertEqual(speeds, sorted(speeds))
        self.assertAlmostEqual(m.predict(300), m._curve(300))

    def test_p_max_is_set_from_training_data(self):
        m = fsm.fit(self.synthetic())
        self.assertEqual(m.p_max, 269)

    def test_cross_validation_on_noise_free_data(self):
        cv = fsm.cross_validate(self.synthetic(), folds=2)
        self.assertLess(cv.mae_mph, 1e-6)

    def test_non_cycling_and_missing_metrics_skipped(self):
        self.write("run.json", workout([100] * 50, [6] * 50, discipline="Running"))
        self.write("nospeed.json", {"Workout": {"Fitness_Discipline": "Cycling"},
                                    "WorkoutSamples": {"Metrics": [{"Slug": "output", "Values": [1]}]}})
        self.assertEqual(fsm.load_rides([self.tmp.name]), [])

    def test_header_contains_all_defines(self):
        m = fsm.Model(-0.05, 2.3, -1.8, p_max=400)
        h = fsm.render_header(m, 3, 300, 10, 400, None)
        for name in ("SPEED_MODEL_A", "SPEED_MODEL_B", "SPEED_MODEL_C", "SPEED_MODEL_P_MAX"):
            self.assertRegex(h, rf"#define {name} \(-?[0-9]+\.[0-9]*(e[+-]?[0-9]+)?f\)")
        self.assertIn("#pragma once", h)

    def test_header_float_literals_are_valid_c(self):
        h = fsm.render_header(fsm.Model(1, -2, 0, p_max=165), 1, 1, 1, 165, None)
        self.assertIn("SPEED_MODEL_P_MAX (165.0f)", h)
        self.assertIn("SPEED_MODEL_B (-2.0f)", h)
        self.assertEqual(fsm._c_float(1.5e-12), "1.5e-12f")

    def test_cli_writes_header(self):
        self.synthetic()
        out = os.path.join(self.tmp.name, "speed_model.h")
        self.assertEqual(fsm.main([self.tmp.name, "--header", out]), 0)
        b = float(re.search(r"SPEED_MODEL_B \(([-0-9.e+]+)f\)", open(out).read()).group(1))
        self.assertAlmostEqual(b, 2.3, places=4)

    def test_cli_fails_without_rides(self):
        self.assertEqual(fsm.main([self.tmp.name]), 1)

    @unittest.skipUnless(os.path.isdir(SAMPLE_DIR), "repo sample data not present")
    def test_real_samples_fit_well(self):
        rides = fsm.load_rides([SAMPLE_DIR])
        cv = fsm.cross_validate(rides)
        self.assertGreater(cv.r2, 0.99)
        self.assertLess(cv.ride_distance_mape_pct, 1.0)


if __name__ == "__main__":
    unittest.main()

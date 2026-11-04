"""T11: deterministic metric formulas and unknown handling."""

import unittest

from demo.app.metrics import (
    adaptive_threshold,
    long_frame_runs,
    summarize_intervals,
)


class MetricTests(unittest.TestCase):
    def test_rate_uses_total_duration_and_tail_stays_unknown(self):
        result = summarize_intervals([10.0, 20.0, 30.0, 40.0])
        self.assertEqual(result["average_present_rate"], 40.0)
        self.assertEqual(result["interval_p95_ms"], 40.0)
        self.assertIsNone(result["slowest_1pct_fps"])

    def test_fifty_ms_is_not_strictly_longer_than_fifty_ms(self):
        result = long_frame_runs([[60.0, 70.0, 16.0, 50.0, 80.0]], 50.0)
        self.assertEqual(result["cluster_count"], 2)
        self.assertEqual(result["interval_count"], 3)
        self.assertEqual(result["total_ms"], 210.0)

    def test_empty_samples_return_unknown_not_zero(self):
        result = summarize_intervals([])
        self.assertEqual(result["interval_count"], 0)
        for key in (
            "average_present_rate",
            "interval_p95_ms",
            "interval_p99_ms",
            "slowest_1pct_fps",
        ):
            self.assertIsNone(result[key])
        self.assertEqual(result["quality"], "unknown")

    def test_p95_and_p99_use_nearest_rank(self):
        values = [float(index) for index in range(1, 101)]
        result = summarize_intervals(values)
        self.assertEqual(result["interval_p95_ms"], 95.0)
        self.assertEqual(result["interval_p99_ms"], 99.0)
        # One percent of 100 samples is a single tail sample: below the
        # twenty-sample threshold, so the value stays unknown.
        self.assertIsNone(result["slowest_1pct_fps"])

    def test_slowest_tail_uses_the_tail_sample_count_as_its_gate(self):
        result = summarize_intervals([float(index) for index in range(1, 2001)])
        self.assertIsNotNone(result["slowest_1pct_fps"])
        tail_mean = sum(float(index) for index in range(1981, 2001)) / 20
        self.assertEqual(result["slowest_1pct_fps"], round(1000.0 / tail_mean, 3))

    def test_adaptive_threshold_never_drops_below_fifty(self):
        self.assertEqual(adaptive_threshold([16.0] * 100), 50.0)
        self.assertEqual(adaptive_threshold([40.0] * 100), 80.0)
        self.assertIsNone(adaptive_threshold([]))

    def test_long_frame_metrics_use_valid_duration(self):
        result = long_frame_runs([[100.0, 100.0, 100.0, 100.0]], 50.0)
        self.assertEqual(result["cluster_count"], 1)
        self.assertEqual(result["interval_count"], 4)
        self.assertEqual(result["valid_duration_s"], 0.4)
        self.assertEqual(result["long_frame_time_ratio"], 1.0)
        self.assertEqual(result["clusters_per_minute"], 150.0)

    def test_invalid_threshold_is_refused(self):
        for value in (0, -5, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    long_frame_runs([[20.0]], value)

    def test_non_finite_and_non_positive_values_are_ignored(self):
        result = summarize_intervals([10.0, float("nan"), float("inf"), -3, 0, 30.0])
        self.assertEqual(result["interval_count"], 2)
        self.assertEqual(result["average_present_rate"], 50.0)

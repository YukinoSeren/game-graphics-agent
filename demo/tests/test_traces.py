"""T11: deterministic demo frame traces and CSV edge handling."""

import unittest
from pathlib import Path

from demo.app.metrics import long_frames, summarize
from demo.app.scenarios import ScenarioCatalog
from demo.app.traces import clean_trace, generate_trace, parse_trace

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def catalog():
    return ScenarioCatalog.load(FIXTURES)


class TraceTests(unittest.TestCase):
    def test_generation_is_byte_for_byte_deterministic(self):
        recipe = catalog().recipe("mw2-rtx4060", "recommended")
        first = generate_trace("mw2-rtx4060", "recommended", recipe)
        second = generate_trace("mw2-rtx4060", "recommended", recipe)
        self.assertEqual(first, second)

    def test_header_and_accumulated_timestamps(self):
        recipe = catalog().recipe("mw2-rtx4060", "short")
        parsed = parse_trace(generate_trace("mw2-rtx4060", "short", recipe), scenario_id="mw2-rtx4060")
        self.assertEqual(parsed.issues, [])
        self.assertEqual(parsed.frames[0]["interval_ms"], None)
        previous = 0.0
        for frame in parsed.frames[1:]:
            self.assertAlmostEqual(
                frame["timestamp_ms"], previous + frame["interval_ms"], places=3
            )
            previous = frame["timestamp_ms"]

    def test_mainline_sample_has_enough_intervals_for_the_tail(self):
        recipe = catalog().recipe("mw2-rtx4060", "recommended")
        parsed = parse_trace(
            generate_trace("mw2-rtx4060", "recommended", recipe), scenario_id="mw2-rtx4060"
        )
        clean = clean_trace(parsed)
        summary = summarize(clean)
        self.assertGreaterEqual(summary["interval_count"], 2000)
        self.assertGreaterEqual(summary["valid_duration_s"], 55)
        self.assertIsNotNone(summary["slowest_1pct_fps"])
        self.assertEqual(summary["quality"], "usable")

    def test_short_sample_is_limited_and_has_no_tail_metric(self):
        recipe = catalog().recipe("mw2-rtx4060", "short")
        parsed = parse_trace(
            generate_trace("mw2-rtx4060", "short", recipe), scenario_id="mw2-rtx4060"
        )
        summary = summarize(clean_trace(parsed))
        self.assertLess(summary["interval_count"], 20)
        self.assertIsNone(summary["slowest_1pct_fps"])
        self.assertEqual(summary["quality"], "limited")

    def test_adjusted_sample_has_fewer_long_frames_than_recommended(self):
        catalog_instance = catalog()
        traces = {}
        for label in ("recommended", "adjusted"):
            parsed = parse_trace(
                generate_trace("mw2-rtx4060", label, catalog_instance.recipe("mw2-rtx4060", label)),
                scenario_id="mw2-rtx4060",
            )
            traces[label] = long_frames(clean_trace(parsed), 50.0)
        self.assertLess(
            traces["adjusted"]["interval_count"], traces["recommended"]["interval_count"]
        )

    def test_no_gain_sample_stays_close_to_recommended(self):
        catalog_instance = catalog()
        counts = {}
        for label in ("recommended", "no_gain"):
            parsed = parse_trace(
                generate_trace("mw2-rtx4060", label, catalog_instance.recipe("mw2-rtx4060", label)),
                scenario_id="mw2-rtx4060",
            )
            counts[label] = long_frames(clean_trace(parsed), 50.0)["interval_count"]
        self.assertLessEqual(abs(counts["recommended"] - counts["no_gain"]), 2)

    def test_bad_row_breaks_adjacency_without_dropping_the_spike(self):
        recipe = catalog().recipe("mw2-rtx4060", "short")
        text = generate_trace("mw2-rtx4060", "short", recipe).decode("utf-8")
        lines = text.splitlines()
        parts = lines[5].split(",")
        parts[5] = "broken"
        lines[5] = ",".join(parts)
        parsed = parse_trace(("\n".join(lines) + "\n").encode("utf-8"), scenario_id="mw2-rtx4060")
        self.assertTrue(parsed.issues)
        self.assertGreaterEqual(len(parsed.runs), 2)
        self.assertEqual(parsed.issues[0]["line"], 6)

    def test_cross_case_trace_is_rejected(self):
        recipe = catalog().recipe("fh6-rtx5090", "short")
        data = generate_trace("fh6-rtx5090", "short", recipe)
        with self.assertRaises(Exception) as caught:
            parse_trace(data, scenario_id="mw2-rtx4060")
        self.assertEqual(getattr(caught.exception, "code", None), "cross_case_trace")

    def test_unknown_columns_are_rejected_instead_of_guessed(self):
        data = b"demo_only,scenario_id,frame_index\n"
        with self.assertRaises(Exception) as caught:
            parse_trace(data, scenario_id="mw2-rtx4060")
        self.assertEqual(getattr(caught.exception, "code", None), "unsupported_format")

    def test_explicit_exclusion_splits_runs(self):
        recipe = catalog().recipe("mw2-rtx4060", "short")
        parsed = parse_trace(
            generate_trace("mw2-rtx4060", "short", recipe), scenario_id="mw2-rtx4060"
        )
        clean = clean_trace(
            parsed,
            exclusions=[{"start_frame": 5, "end_frame": 6, "reason": "演示排除加载帧"}],
        )
        self.assertEqual(len(clean.exclusions), 1)
        self.assertGreaterEqual(len(clean.runs), 2)
        self.assertEqual(len(clean.intervals), len(parsed.frames) - 1 - 2)


if __name__ == "__main__":
    unittest.main()

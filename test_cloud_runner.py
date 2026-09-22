import copy
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import runner


CST = timezone(timedelta(hours=8))


def sample_snapshot():
    groups = {}
    for key, counts in {"chem_large": (3, 3), "chem_small": (5, 5), "oil": (5, 5),
                        "bj": (3, 3), "hk": (3, 2)}.items():
        sides = {}
        for side, count in zip(("gainers", "losers"), counts):
            rows = []
            for index in range(count):
                code = f"{index:05d}.HK" if key == "hk" else (
                    f"{index:06d}.BJ" if key == "bj" else f"{index:06d}.SH")
                rows.append({"code": code, "name": "样本", "return_pct": float(index),
                             "market": "HK" if key == "hk" else "CN",
                             "currency": "HKD" if key == "hk" else "CNY",
                             "as_of": "2026-09-18", "window_start": "2026-09-14",
                             "quote_at": "2026-09-18 15:01:00",
                             "adjustment": "qfq_sina" if key == "bj" else (
                                 "qfq_eastmoney" if key == "hk" else "qfq"),
                             "daily": [{"date": "2026-09-18"}], "weekly": [{"date": "2026-09-18"}]})
            sides[side] = rows
        groups[key] = sides
    return {"as_of": "2026-09-18", "window_start": "2026-09-14",
            "market_windows": {"CN": {"window_start": "2026-09-14", "as_of": "2026-09-18"},
                               "HK": {"window_start": "2026-09-14", "as_of": "2026-09-18"}},
            "generated_at": "2026-09-18 16:00:00", "stats": {"universe": 580, "eligible": 580},
            "groups": groups}


class CloudPublicationTests(unittest.TestCase):
    def test_low_coverage_uses_a_typed_retryable_error(self):
        self.assertTrue(hasattr(runner, "SnapshotCoverageError"))
        self.assertTrue(issubclass(runner.SnapshotCoverageError, ValueError))

    def test_low_coverage_error_carries_stock_level_diagnostics(self):
        snapshot = sample_snapshot()
        snapshot["stats"]["eligible"] = 500
        snapshot["excluded_codes"] = {"stale_history": ["600001.SH"]}
        error = runner.SnapshotCoverageError(snapshot, {"600002.SH": "timeout"})
        self.assertEqual(getattr(error, "diagnostics", None), {
            "as_of": "2026-09-18",
            "stats": snapshot["stats"],
            "excluded_codes": {"stale_history": ["600001.SH"]},
            "fetch_error_codes": ["600002.SH"],
        })

    def test_low_coverage_retries_after_five_minutes_then_returns_success(self):
        self.assertTrue(hasattr(runner, "build_bundle_with_retries"))
        current = datetime(2026, 9, 21, 16, 12, tzinfo=CST)
        sleeps = []
        attempts = []
        logs = []
        ready = ({"snapshot": sample_snapshot(), "products": {}}, {})

        def clock():
            return current

        def sleep(seconds):
            nonlocal current
            sleeps.append(seconds)
            current += timedelta(seconds=seconds)

        def build(_root):
            attempts.append(current)
            if len(attempts) == 1:
                sparse = sample_snapshot()
                sparse["stats"]["eligible"] = 500
                raise runner.SnapshotCoverageError(sparse, {"600002.SH": "timeout"})
            return ready

        result = runner.build_bundle_with_retries(
            Path("."), now_fn=clock, sleep_fn=sleep, builder=build, log_fn=logs.append)
        self.assertIs(result, ready)
        self.assertEqual(sleeps, [300])
        self.assertEqual(len(attempts), 2)
        self.assertIn('"event": "coverage_retry"', logs[0])
        self.assertIn('"fetch_error_codes": ["600002.SH"]', logs[0])

    def test_low_coverage_retries_at_deadline_then_raises(self):
        self.assertTrue(hasattr(runner, "build_bundle_with_retries"))
        current = datetime(2026, 9, 21, 16, 39, tzinfo=CST)
        sleeps = []
        attempts = []

        def clock():
            return current

        def sleep(seconds):
            nonlocal current
            sleeps.append(seconds)
            current += timedelta(seconds=seconds)

        def build(_root):
            attempts.append(current)
            sparse = sample_snapshot()
            sparse["stats"]["eligible"] = 500
            raise runner.SnapshotCoverageError(sparse, {})

        with self.assertRaises(runner.SnapshotCoverageError):
            runner.build_bundle_with_retries(
                Path("."), now_fn=clock, sleep_fn=sleep, builder=build, log_fn=lambda _message: None)
        self.assertEqual(sleeps, [60])
        self.assertEqual(len(attempts), 2)

    def test_main_waits_until_1610_before_collecting_market_data(self):
        with tempfile.NamedTemporaryFile(dir=Path(__file__).parent, suffix=".json", delete=False) as handle:
            target = Path(handle.name)
        current = datetime(2026, 9, 21, 15, 42, tzinfo=CST)
        events = []

        def clock():
            return current

        def sleep(seconds):
            nonlocal current
            events.append(("sleep", seconds))
            current += timedelta(seconds=seconds)

        def build(_root):
            events.append(("build", current))
            return {"snapshot": sample_snapshot(), "products": {}}, {}

        try:
            target.write_text('{"snapshot":{"as_of":"2026-09-17"}}', encoding="utf-8")
            with patch.object(sys, "argv", ["runner.py", "--output", str(target)]), \
                    patch.object(runner, "now_china", side_effect=clock), \
                    patch.object(runner.time, "sleep", side_effect=sleep), \
                    patch.object(runner, "build_bundle", side_effect=build), \
                    patch.object(runner, "write_bundle"):
                runner.main()
            self.assertEqual(events[0], ("sleep", 28 * 60))
            self.assertEqual(events[1][0], "build")
        finally:
            target.unlink(missing_ok=True)

    def test_sina_fallback_retries_only_transient_failures(self):
        first = ({"920175.BJ": {"bars": []}}, {"920175.BJ": {"name": "A"}}, {"920642.BJ": "temporary"})
        second = ({"920642.BJ": {"bars": []}}, {"920642.BJ": {"name": "B"}}, {})
        with patch.object(runner.daily_site, "fetch_sina_adjusted_fallback", side_effect=[first, second]) as fetch:
            histories, quotes, errors = runner.fetch_sina_with_retries(["920175.BJ", "920642.BJ"])
        self.assertEqual(set(histories), {"920175.BJ", "920642.BJ"})
        self.assertEqual(set(quotes), {"920175.BJ", "920642.BJ"})
        self.assertEqual(errors, {})
        self.assertEqual(fetch.call_args_list[1].args[0], ["920642.BJ"])

    def test_bundle_validation_requires_all_rankings_and_adjusted_bj(self):
        bundle = {"snapshot": sample_snapshot(), "products": {}}
        runner.validate_bundle(bundle)
        broken = copy.deepcopy(bundle)
        broken["snapshot"]["groups"]["bj"]["gainers"][0]["adjustment"] = "raw_bj"
        with self.assertRaisesRegex(ValueError, "adjusted|复权"):
            runner.validate_bundle(broken)
        broken = copy.deepcopy(bundle)
        broken["snapshot"]["groups"]["oil"]["losers"] = []
        with self.assertRaisesRegex(ValueError, "rankings|榜单"):
            runner.validate_bundle(broken)

    def test_bundle_validation_uses_each_market_window(self):
        bundle = {"snapshot": sample_snapshot(), "products": {}}
        snapshot = bundle["snapshot"]
        snapshot["as_of"] = "2026-09-21"
        snapshot["market_windows"]["HK"] = {"window_start": "2026-09-15", "as_of": "2026-09-21"}
        for side in ("gainers", "losers"):
            for row in snapshot["groups"]["hk"][side]:
                row["as_of"] = "2026-09-21"
                row["window_start"] = "2026-09-15"
                row["quote_at"] = "2026-09-21 16:11:00"
                row["daily"][-1]["date"] = "2026-09-21"
        runner.validate_bundle(bundle)

    def test_bundle_validation_rejects_stale_quote_and_low_coverage(self):
        bundle = {"snapshot": sample_snapshot(), "products": {}}
        broken = copy.deepcopy(bundle)
        broken["snapshot"]["groups"]["chem_large"]["gainers"][0]["quote_at"] = "2026-09-17 15:00:00"
        with self.assertRaisesRegex(ValueError, "quote|报价"):
            runner.validate_bundle(broken)
        broken = copy.deepcopy(bundle)
        broken["snapshot"]["stats"]["eligible"] = 100
        with self.assertRaisesRegex(ValueError, "coverage|覆盖"):
            runner.validate_bundle(broken)

    def test_publish_only_when_session_is_new_and_not_in_future(self):
        now = datetime(2026, 9, 21, 16, 0, tzinfo=CST)
        self.assertFalse(runner.should_publish("2026-09-18", "2026-09-18", now))
        self.assertTrue(runner.should_publish("2026-09-21", "2026-09-18", now))
        self.assertFalse(runner.should_publish("2026-09-22", "2026-09-18", now))

    def test_wait_until_1610_china_time(self):
        self.assertEqual(runner.seconds_until_release(datetime(2026, 9, 21, 15, 42, tzinfo=CST)), 28 * 60)
        self.assertEqual(runner.seconds_until_release(datetime(2026, 9, 21, 16, 5, tzinfo=CST)), 5 * 60)
        self.assertEqual(runner.seconds_until_release(datetime(2026, 9, 21, 16, 10, tzinfo=CST)), 0)

    def test_atomic_write_does_not_replace_previous_bundle_on_validation_failure(self):
        with tempfile.NamedTemporaryFile(dir=Path(__file__).parent, suffix=".json", delete=False) as handle:
            target = Path(handle.name)
        try:
            target.write_text('{"sentinel":true}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "rankings|榜单"):
                runner.write_bundle({"snapshot": {"groups": {}}, "products": {}}, target)
            self.assertEqual(target.read_text(encoding="utf-8"), '{"sentinel":true}')
        finally:
            target.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

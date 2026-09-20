import copy
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import runner


CST = timezone(timedelta(hours=8))


def sample_snapshot():
    groups = {}
    for key, count in {"chem_large": 3, "chem_small": 5, "oil": 5, "bj": 3}.items():
        rows = []
        for index in range(count):
            code = f"{index:06d}.BJ" if key == "bj" else f"{index:06d}.SH"
            rows.append({"code": code, "name": "样本", "return_pct": float(index),
                         "quote_at": "2026-09-18 15:01:00", "adjustment": "qfq_sina" if key == "bj" else "qfq",
                         "daily": [{"date": "2026-09-18"}], "weekly": [{"date": "2026-09-18"}]})
        groups[key] = {"gainers": rows, "losers": list(reversed(rows))}
    return {"as_of": "2026-09-18", "window_start": "2026-09-14",
            "generated_at": "2026-09-18 16:00:00", "stats": {"universe": 580, "eligible": 580},
            "groups": groups}


class CloudPublicationTests(unittest.TestCase):
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

    def test_wait_until_16_china_time(self):
        self.assertEqual(runner.seconds_until_release(datetime(2026, 9, 21, 15, 42, tzinfo=CST)), 18 * 60)
        self.assertEqual(runner.seconds_until_release(datetime(2026, 9, 21, 16, 5, tzinfo=CST)), 0)

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

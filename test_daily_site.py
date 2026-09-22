import unittest
from datetime import date
import json
from pathlib import Path
import tempfile

import daily_site


def bar(day: str, open_price: float, close: float, high: float | None = None, low: float | None = None) -> dict:
    return {
        "date": day,
        "open": open_price,
        "close": close,
        "high": high if high is not None else max(open_price, close),
        "low": low if low is not None else min(open_price, close),
        "volume": 1000,
    }


class FiveDayWindowTests(unittest.TestCase):
    def test_return_uses_first_open_and_last_close_of_five_sessions(self) -> None:
        bars = [
            bar("2026-09-11", 100, 120),
            bar("2026-09-14", 120, 121),
            bar("2026-09-15", 121, 122),
            bar("2026-09-16", 122, 123),
            bar("2026-09-17", 123, 124),
            bar("2026-09-18", 125, 130),
        ]
        selected = daily_site.last_five_sessions(bars)
        self.assertEqual([item["date"] for item in selected], [
            "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18",
        ])
        self.assertAlmostEqual(daily_site.five_day_return(selected), (130 / 120 - 1) * 100)

    def test_incomplete_window_is_rejected(self) -> None:
        bars = [bar(f"2026-09-{day:02d}", 10, 11) for day in range(14, 18)]
        self.assertEqual(daily_site.last_five_sessions(bars), [])

    def test_ex_rights_history_requires_adjusted_prices(self) -> None:
        bars = [bar(f"2026-09-{day:02d}", 10, 10.2) for day in range(14, 19)]
        self.assertAlmostEqual(daily_site.five_day_return(bars), 2.0)


class MovingAverageTests(unittest.TestCase):
    def test_daily_ma_is_simple_average_of_closes(self) -> None:
        bars = [bar(f"2026-09-{day:02d}", day, day) for day in range(1, 7)]
        enriched = daily_site.add_moving_averages(bars, (5,))
        self.assertIsNone(enriched[3]["ma5"])
        self.assertEqual(enriched[4]["ma5"], 3.0)
        self.assertEqual(enriched[5]["ma5"], 4.0)

    def test_weekly_bars_use_first_open_last_close_highest_high_lowest_low(self) -> None:
        bars = [
            bar("2026-09-07", 10, 11, 12, 9),
            bar("2026-09-08", 11, 12, 13, 10),
            bar("2026-09-11", 12, 13, 14, 11),
            bar("2026-09-14", 13, 15, 16, 12),
        ]
        weekly = daily_site.to_weekly_bars(bars)
        self.assertEqual(len(weekly), 2)
        self.assertEqual((weekly[0]["open"], weekly[0]["close"], weekly[0]["high"], weekly[0]["low"]), (10, 13, 14, 9))
        self.assertTrue(weekly[-1]["partial"])


class RankingTests(unittest.TestCase):
    def test_hk_uses_three_gainers_and_two_losers_without_market_cap(self) -> None:
        rows = [
            {"code": f"{index:05d}.HK", "group": "港股", "market_cap": None,
             "return_pct": value}
            for index, value in enumerate([8, 6, 4, 2, -1, -3], start=1)
        ]
        ranked = daily_site.select_rankings(rows)["hk"]
        self.assertEqual([row["return_pct"] for row in ranked["gainers"]], [8, 6, 4])
        self.assertEqual([row["return_pct"] for row in ranked["losers"]], [-3, -1])

    def test_small_pool_keeps_independent_top_and_bottom(self) -> None:
        rows = [{"code": f"O{i}", "group": "石油石化", "market_cap_yuan": 1,
                 "return_pct": float(i)} for i in range(3)]
        ranked = daily_site.select_rankings(rows)["oil"]
        self.assertEqual([row["code"] for row in ranked["gainers"]], ["O2", "O1", "O0"])
        self.assertEqual([row["code"] for row in ranked["losers"]], ["O0", "O1", "O2"])

    def test_strict_cap_boundary_and_separate_gain_loss_lists(self) -> None:
        rows = []
        for i, value in enumerate([9, 8, 7, 6, -1, -2, -3, -4], start=1):
            rows.append({"code": f"L{i}", "group": "基础化工", "market_cap_yuan": 20_000_000_001, "return_pct": value})
        for i, value in enumerate([12, 11, 10, 9, 8, 7, -1, -2, -3, -4, -5, -6], start=1):
            rows.append({"code": f"S{i}", "group": "基础化工", "market_cap_yuan": 19_999_999_999, "return_pct": value})
        rows.append({"code": "EQ", "group": "基础化工", "market_cap_yuan": 20_000_000_000, "return_pct": 100})
        rows.append({"code": "NA", "group": "基础化工", "market_cap_yuan": None, "return_pct": 100})
        for i in range(12):
            rows.append({"code": f"O{i}", "group": "石油石化", "market_cap_yuan": None, "return_pct": i - 6})
        for i in range(8):
            rows.append({"code": f"B{i}", "group": "北交所", "market_cap_yuan": None, "return_pct": i - 4})
        ranked = daily_site.select_rankings(rows)
        self.assertEqual([row["code"] for row in ranked["chem_large"]["gainers"]], ["L1", "L2", "L3"])
        self.assertEqual([row["code"] for row in ranked["chem_large"]["losers"]], ["L8", "L7", "L6"])
        self.assertEqual(len(ranked["chem_small"]["gainers"]), 5)
        self.assertEqual(len(ranked["chem_small"]["losers"]), 5)
        self.assertEqual(len(ranked["oil"]["gainers"]), 5)
        self.assertEqual(len(ranked["oil"]["losers"]), 5)
        self.assertEqual(len(ranked["bj"]["gainers"]), 3)
        self.assertEqual(len(ranked["bj"]["losers"]), 3)
        all_selected = [row["code"] for bucket in ranked.values() for side in bucket.values() for row in side]
        self.assertNotIn("EQ", all_selected)
        self.assertNotIn("NA", all_selected)


class MarketSourceTests(unittest.TestCase):
    def test_hk_symbol_is_normalized_to_five_digits(self) -> None:
        self.assertEqual(daily_site.normalize_code("0166.HK"), "00166.HK")
        self.assertEqual(daily_site.tencent_symbol("00166.HK"), "hk00166")

    def test_tencent_hk_quote_uses_hkd_cap_when_available(self) -> None:
        fields = [""] * 88
        fields[1] = "东方化工"
        fields[2] = "00166"
        fields[3] = "25.00"
        fields[30] = "2026/09/21 16:15:00"
        fields[45] = "250.00"
        quote = daily_site.parse_tencent_quote('v_hk00166="' + '~'.join(fields) + '";', "00166.HK")
        self.assertEqual(quote["last_price"], 25.0)
        self.assertEqual(quote["market_cap"], 25_000_000_000)
        self.assertEqual(quote["market_cap_currency"], "HKD")
        self.assertIsNone(quote["market_cap_yuan"])

    def test_eastmoney_hk_history_is_marked_forward_adjusted(self) -> None:
        payload = {"data": {"klines": [
            "2026-09-18,23.860,23.600,23.900,23.500,49515676,1170480672,1.68,-1.17,-0.28,0.11",
            "2026-09-21,23.340,23.940,23.960,23.280,46643730,1104511088,2.88,1.44,0.34,0.10",
        ]}}
        result = daily_site.parse_eastmoney_hk_history(payload, "00883.HK")
        self.assertEqual(result["adjustment"], "qfq_eastmoney")
        self.assertEqual(result["bars"][-1]["close"], 23.94)

    def test_tencent_hk_quote_allows_missing_cap(self) -> None:
        fields = [""] * 88
        fields[1] = "东方化工"
        fields[2] = "00166"
        fields[3] = "25.00"
        fields[30] = "20260921161500"
        quote = daily_site.parse_tencent_quote('v_hk00166="' + '~'.join(fields) + '";', "00166.HK")
        self.assertIsNone(quote["market_cap"])
        self.assertEqual(quote["currency"], "HKD")

    def test_sina_adjusted_frame_is_marked_qfq(self) -> None:
        import pandas as pd
        frame = pd.DataFrame([{"date": "2026-09-18", "open": 10, "close": 11, "high": 12,
                               "low": 9, "volume": 100}])
        result = daily_site.parse_sina_adjusted_frame(frame, "301292.SZ")
        self.assertEqual(result["adjustment"], "qfq_sina")
        self.assertEqual(result["bars"][0]["close"], 11)

    def test_futures_trend_uses_five_complete_daily_bars(self) -> None:
        rows = [{"d": f"2026-09-{n:02d}", "o": "100", "c": str(100+n), "h": "120", "l": "90"} for n in range(13, 19)]
        result = daily_site.summarize_futures_trend(rows, "SC0", "上海原油期货")
        self.assertIn("上涨", result["trend"])
        self.assertEqual(result["as_of"], "2026-09-18")

    def test_product_trend_allows_latest_prior_day_but_not_future_or_old_data(self) -> None:
        self.assertTrue(daily_site.product_trend_is_recent("2026-09-17", "2026-09-18"))
        self.assertFalse(daily_site.product_trend_is_recent("2026-09-19", "2026-09-18"))
        self.assertFalse(daily_site.product_trend_is_recent("2026-09-01", "2026-09-18"))

    def test_sina_bj_fallback_parses_full_unadjusted_history(self) -> None:
        payload = [{"day": "2026-09-17", "open": "10", "close": "11", "high": "12", "low": "9", "volume": "100"},
                   {"day": "2026-09-18", "open": "11", "close": "12", "high": "13", "low": "10", "volume": "200"}]
        result = daily_site.parse_sina_bj_history(payload)
        self.assertEqual(result["adjustment"], "raw_bj")
        self.assertEqual(result["bars"][-1]["close"], 12)

    def test_tencent_quote_uses_total_shares_and_last_price_for_cap(self) -> None:
        fields = [""] * 88
        fields[1] = "测试化工"
        fields[2] = "600001"
        fields[3] = "25.00"
        fields[30] = "20260918161500"
        fields[45] = "250.00"
        fields[73] = "1000000000"
        quote = daily_site.parse_tencent_quote('v_sh600001="' + '~'.join(fields) + '";', "600001.SH")
        self.assertEqual(quote["name"], "测试化工")
        self.assertEqual(quote["market_cap_yuan"], 25_000_000_000)
        self.assertEqual(quote["quote_at"], "2026-09-18 16:15:00")

    def test_tencent_history_requires_qfq_for_shenzhen_and_shanghai(self) -> None:
        payload = {"data": {"sz300243": {"qfqday": [
            ["2026-09-17", "10", "11", "11", "9", "1000"],
            ["2026-09-18", "11", "12", "13", "10", "1100"],
        ]}}}
        result = daily_site.parse_tencent_history(payload, "300243.SZ")
        self.assertEqual(result["adjustment"], "qfq")
        self.assertEqual(result["bars"][-1]["close"], 12.0)

    def test_tencent_history_labels_bj_unadjusted(self) -> None:
        payload = {"data": {"bj920957": {"day": [
            ["2026-09-18", "10", "11", "12", "9", "1000"],
        ]}}}
        result = daily_site.parse_tencent_history(payload, "920957.BJ")
        self.assertEqual(result["adjustment"], "raw_bj")

    def test_tencent_history_rejects_unadjusted_shanghai_fallback(self) -> None:
        payload = {"data": {"sh600001": {"day": [
            ["2026-09-18", "10", "11", "12", "9", "1000"],
        ]}}}
        with self.assertRaisesRegex(ValueError, "qfq"):
            daily_site.parse_tencent_history(payload, "600001.SH")


class SnapshotTests(unittest.TestCase):
    def test_snapshot_uses_independent_cn_and_hk_five_session_windows(self) -> None:
        cn_days = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]
        hk_days = ["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21"]
        histories = {
            "600001.SH": {"bars": [bar(day, 10, 11) for day in cn_days], "adjustment": "qfq"},
            "00166.HK": {"bars": [bar(day, 20, 22) for day in hk_days], "adjustment": "qfq"},
        }
        quotes = {
            "600001.SH": {"name": "沪股", "last_price": 11, "market_cap_yuan": 30_000_000_000,
                          "market_cap": 30_000_000_000, "market_cap_currency": "CNY",
                          "currency": "CNY", "quote_at": "2026-09-18 16:00:00"},
            "00166.HK": {"name": "港股", "last_price": 22, "market_cap_yuan": None,
                         "market_cap": None, "market_cap_currency": "HKD",
                         "currency": "HKD", "quote_at": "2026-09-21 16:15:00"},
        }
        snapshot = daily_site.make_snapshot(
            {"基础化工": ["600001.SH"], "港股": ["00166.HK"]}, histories, quotes,
            "2026-09-21 16:20:00")
        self.assertEqual(snapshot["market_windows"], {
            "CN": {"window_start": "2026-09-14", "as_of": "2026-09-18"},
            "HK": {"window_start": "2026-09-15", "as_of": "2026-09-21"},
        })
        hk = snapshot["groups"]["hk"]["gainers"][0]
        self.assertEqual((hk["market"], hk["currency"]), ("HK", "HKD"))
        self.assertEqual((hk["window_start"], hk["as_of"]), ("2026-09-15", "2026-09-21"))
        self.assertEqual(snapshot["as_of"], "2026-09-21")

    def test_load_universe_normalizes_hk_codes(self) -> None:
        codes = daily_site.load_universe(Path(__file__).parent)["港股"]
        self.assertEqual(codes[0], "00166.HK")
        self.assertIn("80883.HK", codes)
        self.assertEqual(len(codes), 68)

    def test_snapshot_records_every_excluded_code_by_reason(self) -> None:
        days = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]
        full = {"bars": [bar(day, 10, 11) for day in days], "adjustment": "qfq"}
        stale = {"bars": [bar(day, 10, 11) for day in days[:-1]], "adjustment": "qfq"}
        histories = {
            "600002.SH": stale,
            "600003.SH": full,
            "600004.SH": full,
            "600005.SH": full,
            "920001.BJ": {"bars": full["bars"], "adjustment": "raw_bj"},
        }
        quotes = {
            "600002.SH": {"name": "stale history", "last_price": 11, "market_cap_yuan": 1,
                           "quote_at": "2026-09-18 16:00:00"},
            "600004.SH": {"name": "stale quote", "last_price": 11, "market_cap_yuan": 1,
                           "quote_at": "2026-09-17 16:00:00"},
            "600005.SH": {"name": "missing cap", "last_price": 11, "market_cap_yuan": None,
                           "quote_at": "2026-09-18 16:00:00"},
            "920001.BJ": {"name": "raw", "last_price": 11, "market_cap_yuan": 1,
                           "quote_at": "2026-09-18 16:00:00"},
        }
        snapshot = daily_site.make_snapshot(
            {"基础化工": ["600001.SH", "600002.SH", "600003.SH", "600004.SH", "600005.SH"],
             "北交所": ["920001.BJ"]}, histories, quotes, "2026-09-18 16:00:00")
        self.assertEqual(snapshot.get("excluded_codes"), {
            "missing_history": ["600001.SH"],
            "stale_history": ["600002.SH"],
            "missing_quote": ["600003.SH"],
            "stale_quote": ["600004.SH"],
            "missing_cap": ["600005.SH"],
            "unadjusted_history": ["920001.BJ"],
        })

    def test_stale_quote_is_not_ranked_or_used_for_cap_bucket(self) -> None:
        days = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]
        history = {"bars": [bar(day, 10, 11) for day in days], "adjustment": "qfq"}
        snapshot = daily_site.make_snapshot(
            {"基础化工": ["600001.SH"]}, {"600001.SH": history},
            {"600001.SH": {"name": "stale", "last_price": 11, "market_cap_yuan": 30_000_000_000,
                           "quote_at": "2026-09-17 15:00:00"}}, "2026-09-18 16:00:00")
        self.assertEqual(snapshot["stats"]["stale_quote"], 1)
        self.assertEqual(snapshot["groups"]["chem_large"]["gainers"], [])

    def test_snapshot_uses_common_five_dates_and_excludes_suspended_name(self) -> None:
        days = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]
        full = [bar(day, 10, 10 + index) for index, day in enumerate(days)]
        stale = [bar(day, 10, 10) for day in days[:-1]]
        universe = {"基础化工": ["600001.SH", "600002.SH"], "石油石化": ["600003.SH"], "北交所": ["920001.BJ"]}
        histories = {
            "600001.SH": {"bars": full, "adjustment": "qfq"},
            "600002.SH": {"bars": stale, "adjustment": "qfq"},
            "600003.SH": {"bars": full, "adjustment": "qfq"},
            "920001.BJ": {"bars": full, "adjustment": "qfq_sina"},
        }
        quotes = {code: {"name": code, "last_price": 14, "market_cap_yuan": cap,
                          "quote_at": "2026-09-18 16:00:00", "quote_source": "test", "quote_url": "https://example.com"}
                  for code, cap in [("600001.SH", 25_000_000_000), ("600002.SH", 10_000_000_000),
                                    ("600003.SH", 5_000_000_000), ("920001.BJ", 3_000_000_000)]}
        snapshot = daily_site.make_snapshot(universe, histories, quotes, "2026-09-18 16:00:00")
        self.assertEqual(snapshot["window_start"], days[0])
        self.assertEqual(snapshot["as_of"], days[-1])
        self.assertEqual(snapshot["stats"]["stale_history"], 1)
        self.assertEqual(snapshot["groups"]["chem_large"]["gainers"][0]["code"], "600001.SH")
        self.assertEqual(snapshot["groups"]["chem_small"]["gainers"], [])
        self.assertEqual(snapshot["groups"]["bj"]["gainers"][0]["adjustment"], "qfq_sina")

    def test_unadjusted_bj_is_not_ranked(self) -> None:
        days = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]
        snapshot = daily_site.make_snapshot(
            {"北交所": ["920001.BJ"]},
            {"920001.BJ": {"bars": [bar(day, 10, 11) for day in days], "adjustment": "raw_bj"}},
            {"920001.BJ": {"name": "raw", "last_price": 11, "market_cap_yuan": 1_000_000_000,
                           "quote_at": "2026-09-18 16:00:00"}}, "2026-09-18 16:00:00")
        self.assertEqual(snapshot["stats"]["unadjusted_history"], 1)
        self.assertEqual(snapshot["groups"]["bj"]["gainers"], [])

    def test_invalid_snapshot_does_not_replace_previous_success(self) -> None:
        with tempfile.NamedTemporaryFile(dir=Path(__file__).parent, suffix=".json", delete=False) as handle:
            target = Path(handle.name)
        try:
            target.write_text('{"as_of":"2026-09-18"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "rankings"):
                daily_site.write_snapshot({"groups": {}}, target)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["as_of"], "2026-09-18")
        finally:
            target.unlink(missing_ok=True)

    def test_sparse_snapshot_does_not_replace_previous_success(self) -> None:
        with tempfile.NamedTemporaryFile(dir=Path(__file__).parent, suffix=".json", delete=False) as handle:
            target = Path(handle.name)
        try:
            target.write_text('{"as_of":"2026-09-17"}', encoding="utf-8")
            sparse = {"as_of": "2026-09-18", "window_start": "2026-09-14",
                      "stats": {"universe": 580, "eligible": 1},
                      "groups": {"bj": {"gainers": [{"code": "920001.BJ"}], "losers": []}}}
            with self.assertRaisesRegex(ValueError, "coverage"):
                daily_site.write_snapshot(sparse, target)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["as_of"], "2026-09-17")
        finally:
            target.unlink(missing_ok=True)

    def test_product_output_is_atomically_valid_json(self) -> None:
        with tempfile.NamedTemporaryFile(dir=Path(__file__).parent, suffix=".json", delete=False) as handle:
            target = Path(handle.name)
        try:
            daily_site.write_products({"600001.SH": {"product": "品种", "trend": "未取得"}}, target)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["600001.SH"]["product"], "品种")
        finally:
            target.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

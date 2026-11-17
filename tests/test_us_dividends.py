"""us_dividends.py（美股配息）的解析、ttm、單檔失敗沿用與全部失敗測試，全部用假的 Yahoo 回應、不連網。

執行：python -m unittest discover -s tests（在 repo 根目錄）
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import us_dividends  # noqa: E402
from common import NY, UTC  # noqa: E402

TODAY = dt.datetime(2026, 11, 18, 3, 0, tzinfo=UTC)  # 台北 11:00、美東 11/17 22:00 → 美東日期 2026-11-17


def ts(y, m, d, hh=9, mm=30, tz=NY):
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=tz).timestamp())


def chart(*divs):
    """divs: (時間戳, 金額)；沒有任何配息就不放 events（Yahoo 對不配息的股票就是這樣）。"""
    r = {"meta": {"exchangeTimezoneName": "America/New_York"}, "timestamp": [], "indicators": {"quote": [{}]}}
    if divs:
        r["events"] = {"dividends": {str(t): {"amount": a, "date": t} for t, a in divs}}
    return {"chart": {"result": [r], "error": None}}


class ParseTest(unittest.TestCase):
    def test_parse_sorted_by_ex_date(self):
        j = chart((ts(2026, 9, 29), 1.77), (ts(2026, 3, 24), 1.85), (ts(2026, 6, 30), 1.80))
        self.assertEqual(us_dividends.parse_events(j, "VOO"),
                         [{"ex_date": "2026-03-24", "cash": 1.85}, {"ex_date": "2026-06-30", "cash": 1.8},
                          {"ex_date": "2026-09-29", "cash": 1.77}])

    def test_date_is_new_york_not_utc_or_taipei(self):
        # 美東 6/30 23:30 → UTC 7/01 03:30、台北 7/01 11:30；除息日要是 6/30
        got = us_dividends.parse_events(chart((ts(2026, 6, 30, 23, 30), 1.0)), "X")
        self.assertEqual(got[0]["ex_date"], "2026-06-30")
        # 冬令時間：美東 12/22 09:30 = UTC 14:30，同一天
        got = us_dividends.parse_events(chart((ts(2026, 12, 22), 0.21)), "GOOGL")
        self.assertEqual(got[0]["ex_date"], "2026-12-22")

    def test_no_dividends_is_empty_list(self):
        self.assertEqual(us_dividends.parse_events(chart(), "TSLA"), [])

    def test_skips_non_positive_and_missing_amount(self):
        j = chart((ts(2026, 6, 30), 0), (ts(2026, 9, 29), None), (ts(2026, 3, 24), 1.5))
        self.assertEqual([e["ex_date"] for e in us_dividends.parse_events(j, "X")], ["2026-03-24"])

    def test_no_result_raises(self):
        with self.assertRaises(RuntimeError):
            us_dividends.parse_events({"chart": {"result": None, "error": {"code": "Not Found"}}}, "ZZZ")


class SummarizeTest(unittest.TestCase):
    def test_ttm_window_and_last(self):
        evs = [{"ex_date": "2025-11-17", "cash": 1.0},   # 剛好 365 天前：不算
               {"ex_date": "2025-11-18", "cash": 2.0},
               {"ex_date": "2026-06-30", "cash": 3.0},
               {"ex_date": "2026-11-17", "cash": 4.0},   # 今天：算
               {"ex_date": "2026-12-22", "cash": 5.0}]   # 未來：不算
        out = us_dividends.summarize(evs, "2026-11-17")
        self.assertEqual((out["ttm_cash"], out["ttm_count"]), (9.0, 3))
        self.assertEqual(out["last"], {"ex_date": "2026-11-17", "cash": 4.0})
        self.assertEqual(len(out["events"]), 5)

    def test_empty(self):
        out = us_dividends.summarize([], "2026-11-17")
        self.assertEqual((out["events"], out["ttm_cash"], out["ttm_count"], out["last"]), ([], 0, 0, None))


class FetchTest(unittest.TestCase):
    CFG = {"us": {"VOO": {"role": "buy"}, "TSLA": {"role": "buy"}, "ARKK": {"role": "watch"}, "IAUM": {"role": "gauge"}}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "dividends", "us.json")
        patch = mock.patch.object(us_dividends, "NOW", TODAY)
        patch.start()
        self.addCleanup(patch.stop)
        self.meta = {"datasets": {}, "errors": []}

    def run_fetch(self, fetcher):
        us_dividends.fetch_us_dividends(self.CFG, self.meta, fetcher=fetcher, out=self.out, pause=0)

    def read(self):
        with open(self.out, encoding="utf-8") as fh:
            return json.load(fh)

    def test_only_buy_role_and_meta(self):
        asked = []

        def fetcher(sym):
            asked.append(sym)
            return chart((ts(2026, 9, 29), 1.77), (ts(2026, 6, 30), 1.80)) if sym == "VOO" else chart()

        self.run_fetch(fetcher)
        self.assertEqual(asked, ["VOO", "TSLA"])
        doc = self.read()
        self.assertEqual(list(doc["symbols"]), ["VOO", "TSLA"])
        self.assertEqual(doc["symbols"]["TSLA"]["events"], [])
        self.assertEqual(doc["symbols"]["VOO"]["ttm_count"], 2)
        m = self.meta["datasets"]["dividends/us"]
        self.assertEqual((m["status"], m["codes"], m["events"], m["last"]), ("ok", 2, 2, "2026-09-29"))
        self.assertNotIn("failed_codes", m)

    def test_single_failure_keeps_previous_events(self):
        self.run_fetch(lambda s: chart((ts(2026, 6, 30), 1.80)) if s == "VOO" else chart((ts(2026, 5, 1), 0.5)))

        def flaky(sym):
            if sym == "VOO":
                raise RuntimeError("HTTP 429")
            return chart((ts(2026, 5, 1), 0.5), (ts(2026, 8, 1), 0.5))

        self.meta = {"datasets": {}, "errors": []}
        self.run_fetch(flaky)
        doc = self.read()
        self.assertEqual(doc["symbols"]["VOO"]["events"], [{"ex_date": "2026-06-30", "cash": 1.8}])  # 沒消失
        self.assertEqual(len(doc["symbols"]["TSLA"]["events"]), 2)
        m = self.meta["datasets"]["dividends/us"]
        self.assertEqual((m["status"], m["failed_codes"]), ("stale", ["VOO"]))

    def test_empty_response_does_not_erase_previous(self):
        self.run_fetch(lambda s: chart((ts(2026, 6, 30), 1.80)))
        self.run_fetch(lambda s: chart())  # 兩檔都回「沒有配息」，但上一次有 → 不採信
        self.assertEqual(self.meta["datasets"]["dividends/us"]["status"], "stale")  # 全部失敗，標 stale 並保留舊檔
        self.assertEqual(self.read()["symbols"]["VOO"]["events"], [{"ex_date": "2026-06-30", "cash": 1.8}])

    def test_all_fail_marks_failed_and_keeps_file(self):
        self.run_fetch(lambda s: chart((ts(2026, 6, 30), 1.80)))
        before = self.read()

        def down(sym):
            raise RuntimeError("down")

        self.meta = {"datasets": {}, "errors": []}
        self.run_fetch(down)
        m = self.meta["datasets"]["dividends/us"]
        self.assertEqual(m["status"], "stale")  # 有舊檔
        self.assertIn("down", m["error"])
        self.assertEqual(self.read(), before)  # 檔案沒被動
        self.assertEqual(len(self.meta["errors"]), 1)

    def test_all_fail_without_previous_file_is_fail(self):
        def down(sym):
            raise RuntimeError("down")

        self.run_fetch(down)
        self.assertEqual(self.meta["datasets"]["dividends/us"]["status"], "fail")
        self.assertFalse(os.path.exists(self.out))


if __name__ == "__main__":
    unittest.main()

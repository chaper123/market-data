"""24 小時交易品項（期貨、美元指數）的收盤截止與週末列測試，全部用假資料、不連網。

執行：python -m unittest discover -s tests（在 repo 根目錄）
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common  # noqa: E402
import fetch  # noqa: E402
import macro  # noqa: E402

NY = ZoneInfo("America/New_York")


def ny(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=NY)


def bar(day, close, vol=100000):
    return (day, vol, close, close, close, close)


def yahoo_rows(extra):
    """2026-05 起的平日假 K 棒（夠 100 列），再加上 extra。"""
    rows, d = {}, dt.date(2026, 5, 1)
    while d <= dt.date(2026, 10, 16):
        if d.weekday() < 5:
            rows[d.isoformat()] = bar(d.isoformat(), 100.0)
        d += dt.timedelta(days=1)
    rows["2026-10-16"] = bar("2026-10-16", 101.41)
    for r in extra:
        rows[r[0]] = r
    return rows


class FuturesCutoffTest(unittest.TestCase):
    def test_clean_window_takes_today(self):
        self.assertEqual(common.futures_cutoff(ny(2026, 10, 19, 17, 13)), "2026-10-19")  # 台北 05:13（夏令）

    def test_evening_session_takes_previous_day(self):
        self.assertEqual(common.futures_cutoff(ny(2026, 10, 19, 18, 37)), "2026-10-18")  # 台北 06:37
        self.assertEqual(common.futures_cutoff(ny(2026, 10, 19, 22, 13)), "2026-10-18")  # 台北 10:13

    def test_after_midnight_takes_previous_day(self):
        self.assertEqual(common.futures_cutoff(ny(2026, 10, 20, 3, 41)), "2026-10-19")

    def test_before_close_takes_previous_day(self):
        self.assertEqual(common.futures_cutoff(ny(2026, 11, 2, 16, 13)), "2026-11-01")  # 冬令 05:13 還沒收
        self.assertEqual(common.futures_cutoff(ny(2026, 11, 2, 17, 5)), "2026-11-01")

    def test_weekdays_only(self):
        rows = [bar("2026-10-16", 1), bar("2026-10-17", 2), bar("2026-10-18", 3), bar("2026-10-19", 4)]
        self.assertEqual([r[0] for r in common.weekdays_only(rows)], ["2026-10-16", "2026-10-19"])


class FetchMacro24hTest(unittest.TestCase):
    CFG = {"macro": {"WTI": {"yahoo": "CL=F", "name": "WTI", "session": "24h"},
                     "SPX": {"yahoo": "^GSPC", "name": "S&P 500"}}}

    def run_macro(self, now, yahoo, prev=None, old_csv=None):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "macro"))
            if old_csv:
                common.write_rows(os.path.join(tmp, "macro", "WTI.csv"), old_csv)
            meta = {"datasets": {}, "errors": []}
            with mock.patch.object(common, "NOW", now), mock.patch.object(macro, "DATA", tmp), \
                    mock.patch.object(common, "_PREV_META", prev or {}), \
                    mock.patch.object(macro, "yahoo_daily", lambda sym: dict(yahoo)), \
                    mock.patch.object(macro.time, "sleep", lambda s: None):
                macro.fetch_macro(self.CFG, meta)
            wti = common.read_rows(os.path.join(tmp, "macro", "WTI.csv"))
            spx = common.read_rows(os.path.join(tmp, "macro", "SPX.csv"))
        return meta, wti, spx

    def test_sunday_and_evening_bars_dropped(self):
        # 10/19（一）美東 20:20 抓：Yahoo 有週日晚盤列，10/19 那根已被晚盤未完成資料蓋掉
        y = yahoo_rows([bar("2026-10-18", 98.18, 4621), bar("2026-10-19", 98.10, 2618)])
        meta, wti, spx = self.run_macro(ny(2026, 10, 19, 20, 20), y)
        self.assertEqual(wti[-1][0], "2026-10-16")
        self.assertNotIn("2026-10-18", [r[0] for r in wti])
        self.assertEqual(meta["datasets"]["macro/WTI"]["cutoff_rule"], macro.CUTOFF_24H)
        self.assertEqual(spx[-1][0], "2026-10-19")  # 指數照美股收盤截止，不受影響

    def test_clean_window_captures_today(self):
        y = yahoo_rows([bar("2026-10-19", 103.66, 108489)])
        meta, wti, _ = self.run_macro(ny(2026, 10, 19, 17, 13), y)
        self.assertEqual(wti[-1][0], "2026-10-19")
        self.assertAlmostEqual(float(wti[-1][5]), 103.66)
        self.assertGreater(float(wti[-1][5]), float(wti[-2][5]))  # 10/19 是上漲

    def test_evening_rerun_keeps_clean_bar(self):
        # 17:13 收到的乾淨 10/19；同一晚 18:37 再抓（Yahoo 給未完成資料、截止只到 10/18）→ 沿用 CSV 那一列
        clean = [list(r) for r in sorted(yahoo_rows([bar("2026-10-19", 103.66, 108489)]).values())]
        prev = {"macro/WTI": {"status": "ok", "last": "2026-10-19", "cutoff_rule": macro.CUTOFF_24H}}
        y = yahoo_rows([bar("2026-10-19", 98.24, 3000)])
        meta, wti, _ = self.run_macro(ny(2026, 10, 19, 18, 37), y, prev=prev, old_csv=clean)
        self.assertEqual(wti[-1][0], "2026-10-19")
        self.assertAlmostEqual(float(wti[-1][5]), 103.66)
        self.assertEqual(meta["datasets"]["macro/WTI"]["last_kept_from_previous"], "2026-10-19")

    def test_old_rule_bar_is_not_kept(self):
        # 舊程式在晚盤時段收進來的 10/19（沒有 cutoff_rule）不能沿用
        old = [list(r) for r in sorted(yahoo_rows([bar("2026-10-19", 98.60, 9846)]).values())]
        prev = {"macro/WTI": {"status": "ok", "last": "2026-10-19"}}
        meta, wti, _ = self.run_macro(ny(2026, 10, 19, 22, 13), yahoo_rows([bar("2026-10-19", 98.6, 9846)]),
                                      prev=prev, old_csv=old)
        self.assertEqual(wti[-1][0], "2026-10-16")
        self.assertNotIn("last_kept_from_previous", meta["datasets"]["macro/WTI"])

    def test_next_morning_uses_settled_bar(self):
        # 美東午夜後 Yahoo 恢復 10/19 的正常收盤
        y = yahoo_rows([bar("2026-10-19", 103.66, 108489), bar("2026-10-20", 99.54, 32427)])
        meta, wti, _ = self.run_macro(ny(2026, 10, 20, 3, 41), y)
        self.assertEqual(wti[-1][0], "2026-10-19")
        self.assertAlmostEqual(float(wti[-1][5]), 103.66)


class FreshToday24hTest(unittest.TestCase):
    def run_fresh(self, datasets):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "meta.json"), "w") as f:
                json.dump({"datasets": datasets}, f)
            with mock.patch.object(fetch, "DATA", tmp), \
                    mock.patch.object(fetch, "expected_last", return_value="2026-11-02"):
                return fetch.fresh_today()

    def test_futures_not_yet_settled_is_not_fresh(self):
        ds = {"us/VOO": {"status": "ok", "last": "2026-11-02"},
              "macro/WTI": {"status": "ok", "last": "2026-10-30", "cutoff_rule": macro.CUTOFF_24H}}
        self.assertFalse(self.run_fresh(ds))
        ds["macro/WTI"]["last"] = "2026-11-02"
        self.assertTrue(self.run_fresh(ds))


if __name__ == "__main__":
    unittest.main()

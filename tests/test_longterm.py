"""longterm.py（景氣對策信號、巴菲特指標）的解析、初值紀錄與排程略過測試，全部用假資料、不連網。

執行：python -m unittest discover -s tests（在 repo 根目錄）
"""
import datetime as dt
import json
import math
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import longterm  # noqa: E402
from common import read_rows, write_rows  # noqa: E402

NOW1, NOW2 = "2026-10-29 17:45", "2026-10-30 17:45"


def history(last="2026-08", score=41):
    """2026-01 起到 last 的 {月份: (分數, 燈號)}，每個月都是 score。"""
    out, y, m = {}, 2026, 1
    while f"{y}-{m:02d}" <= last:
        out[f"{y}-{m:02d}"] = (score, longterm.light_of(score))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


class CycleTest(unittest.TestCase):
    def test_light_boundaries(self):
        cases = {45: "紅", 38: "紅", 37: "黃紅", 32: "黃紅", 31: "綠", 23: "綠", 22: "黃藍", 17: "黃藍", 16: "藍", 9: "藍"}
        for score, light in cases.items():
            self.assertEqual(longterm.light_of(score), light, score)

    def test_release_est(self):
        self.assertEqual(longterm.release_est("2026-08"), "2026-09-30")  # 週三
        self.assertEqual(longterm.release_est("2026-01"), "2026-03-02")  # 2/28 是週六 → 順延到週一
        self.assertEqual(longterm.release_est("2026-12"), "2027-02-01")  # 跨年；1/30 是週六
        self.assertEqual(longterm.release_est("1984-01"), "1984-02-29")  # 閏年 2 月用月底

    def test_parse_csv(self):
        text = ('﻿"Date","領先指標綜合指數","景氣對策信號綜合分數","景氣對策信號"\n'
                "198312,10.1,-,-\n198401,12.3,42,紅\n202608,140.5,41,紅\n202607,140.1,20,黃藍 \n")
        rows = longterm.parse_ndc_csv(text)
        self.assertEqual(rows, {"1984-01": (42, "紅"), "2026-08": (41, "紅"), "2026-07": (20, "黃藍")})

    def test_first_build_only_latest_gets_initial(self):
        zip_rows = {"2026-06": (41, "紅"), "2026-07": (41, "紅")}
        api_rows = {"2026-06": (40, "紅"), "2026-07": (41, "紅")}
        rows, revisions, notes = longterm.build_cycle(zip_rows, api_rows, "2026-08-27", [], NOW1)
        self.assertEqual(notes["diff"], ["2026-06"])
        self.assertEqual(rows[0], ["2026-06", 41, "紅", "2026-07-30", "", "", "", ""])  # 同一期以 zip 為主
        self.assertEqual(rows[1], ["2026-07", 41, "紅", "2026-08-31", "2026-08-27", 41, "紅", NOW1])
        self.assertEqual(revisions, [])

    def test_new_month_initial_written(self):
        previous, _, _ = longterm.build_cycle(history("2026-08"), None, "2026-09-29", [], NOW1)
        new = dict(history("2026-08"), **{"2026-09": (37, "黃紅")})
        rows, revisions, notes = longterm.build_cycle(new, None, "2026-10-29", previous, NOW2)
        self.assertEqual(rows[-1], ["2026-09", 37, "黃紅", "2026-10-30", "2026-10-29", 37, "黃紅", NOW2])
        self.assertEqual(revisions, [])
        self.assertEqual(notes["missed_initial"], [])

    def test_revision_keeps_initial_and_is_logged(self):
        previous, _, _ = longterm.build_cycle(history("2026-08"), None, "2026-09-29", [], NOW1)
        new = dict(history("2026-08"), **{"2026-08": (37, "黃紅"), "2026-09": (36, "黃紅")})
        rows, revisions, _ = longterm.build_cycle(new, None, "2026-10-29", previous, NOW2)
        aug = next(r for r in rows if r[0] == "2026-08")
        self.assertEqual(aug, ["2026-08", 37, "黃紅", "2026-09-30", "2026-09-29", 41, "紅", NOW1])  # 主欄位改、初值不動
        self.assertEqual(revisions, [[NOW2, "2026-08", 41, 37, "紅", "黃紅", "2026-10-29"]])

    def test_rerun_is_idempotent(self):
        first, _, _ = longterm.build_cycle(history("2026-08"), None, "2026-09-29", [], NOW1)
        new = dict(history("2026-08"), **{"2026-08": (40, "紅"), "2026-09": (41, "紅")})
        once, rev1, _ = longterm.build_cycle(new, None, "2026-10-29", first, NOW2)
        with tempfile.TemporaryDirectory() as d:  # 經過 CSV 讀寫（數字變字串）再重跑
            p = os.path.join(d, "c.csv")
            write_rows(p, once)
            twice, rev2, _ = longterm.build_cycle(new, None, "2026-10-29", read_rows(p), "2026-10-31 17:45")
        self.assertEqual(len(rev1), 1)
        self.assertEqual(rev2, [])
        self.assertEqual([[str(x) for x in r] for r in once], [[str(x) for x in r] for r in twice])

    def test_missed_month_has_no_initial(self):
        previous, _, _ = longterm.build_cycle(history("2026-07"), None, "2026-08-27", [], NOW1)
        rows, _, notes = longterm.build_cycle(history("2026-09"), None, "2026-10-29", previous, NOW2)
        self.assertEqual(rows[-2][4:], ["", "", "", NOW2])  # 8 月已被修正過一次，不當初值
        self.assertEqual(rows[-1][4:], ["2026-10-29", 41, "紅", NOW2])
        self.assertEqual(notes["missed_initial"], ["2026-08"])

    def test_api_newer_release_wins(self):
        previous, _, _ = longterm.build_cycle(history("2026-08"), None, "2026-09-29", [], NOW1)
        zip_rows = history("2026-08")  # zip 還是上一期
        api_rows = dict(history("2026-08"), **{"2026-08": (40, "紅"), "2026-09": (41, "紅")})
        rows, revisions, notes = longterm.build_cycle(zip_rows, api_rows, "2026-10-29", previous, NOW2)
        self.assertTrue(notes["api_newer"])
        self.assertEqual(rows[-2][1], 40)
        self.assertEqual(rows[-1][5:7], [41, "紅"])
        self.assertEqual(len(revisions), 1)

    def test_upgrade_legacy_rows(self):
        legacy = [["2026-07", "41", "紅", "2026-08-31", "", ""], ["2026-08", "41", "紅", "2026-09-30", "2026-09-29", "41"]]
        rows, revisions, _ = longterm.build_cycle(history("2026-08"), None, "2026-09-29", legacy, NOW2,
                                                  legacy_seen="2026-10-28 19:06")
        self.assertEqual(rows[-2][4:], ["", "", "", ""])
        self.assertEqual(rows[-1][4:], ["2026-09-29", "41", "紅", "2026-10-28 19:06"])
        self.assertEqual(revisions, [])

    def test_build_flags_light_mismatch_and_range(self):
        _, _, notes = longterm.build_cycle({"2026-08": (41, "綠")}, None, None, [], NOW1)
        self.assertEqual(notes["bad_light"], ["2026-08"])
        with self.assertRaises(RuntimeError):
            longterm.build_cycle({"2026-08": (50, "紅")}, None, None, [], NOW1)

    def test_expected_month(self):
        self.assertEqual(longterm.expected_month(dt.date(2026, 10, 26)), "2026-08")
        self.assertEqual(longterm.expected_month(dt.date(2026, 10, 27)), "2026-09")
        self.assertEqual(longterm.expected_month(dt.date(2026, 11, 3)), "2026-09")
        self.assertEqual(longterm.expected_month(dt.date(2027, 1, 2)), "2026-11")
        self.assertEqual(longterm.expected_month(dt.date(2027, 1, 28)), "2026-12")


Z1_HTML = """
<div class="row"><div class="col-xs-2">*September</div><div class="col-xs-2">2026:Q2</div>
<div class="col-xs-5">
    <a href="/releases/z1/current/html/default.htm">HTML</a></div></div>
<div class="row"><div class="col-xs-2">June</div><div class="col-xs-2">2026:Q1</div>
<div class="col-xs-5">

  <a href="/releases/z1/20260611/html/default.htm">HTML</a></div></div>
<div class="row"><div class="col-xs-2">June</div><div class="col-xs-2">2026:Q1</div>
<div class="col-xs-5"> <a href="/releases/z1/20260620/html/default.htm">HTML</a></div></div>
<div class="lastUpdate" id="lastUpdate">Last Update: September 11, 2026  </div>
"""


def series(start_year, n, f):
    """從 start_year Q1 起 n 季的 {季: 值}。"""
    return {f"{start_year + i // 4}Q{i % 4 + 1}": f(i) for i in range(n)}


def trend_data(n=80):
    # 比率在 2% 年成長的趨勢上，偶數季 +10%、奇數季 −10% 交錯（殘差標準差固定）
    eq = series(1952, n, lambda i: 1000 * 100 * math.exp(0.02 * i / 4) * (1.1 if i % 2 == 0 else 1 / 1.1))
    return eq, series(1952, n, lambda i: 100.0)


class BuffettTest(unittest.TestCase):
    def test_quarter_helpers(self):
        self.assertEqual(longterm.quarter_of("2026-04-01"), "2026Q2")
        self.assertEqual(longterm.quarter_of("1951-10-01"), "1951Q4")
        self.assertEqual(longterm.quarter_end("2026Q1").isoformat(), "2026-03-31")
        self.assertEqual(longterm.quarter_end("2025Q4").isoformat(), "2025-12-31")

    def test_expected_quarter(self):
        self.assertEqual(longterm.expected_quarter(dt.date(2026, 10, 29)), "2026Q2")
        self.assertEqual(longterm.expected_quarter(dt.date(2026, 12, 2)), "2026Q2")  # 9/30＋63 天
        self.assertEqual(longterm.expected_quarter(dt.date(2026, 12, 3)), "2026Q3")  # 9/30＋64 天
        self.assertEqual(longterm.expected_quarter(dt.date(2027, 2, 1)), "2026Q3")

    def test_parse_z1_dates(self):
        self.assertEqual(longterm.parse_z1_dates(Z1_HTML), {"2026Q2": "2026-09-11", "2026Q1": "2026-06-11"})

    def test_build_ratio_trend_and_release(self):
        eq, gdp = trend_data()
        gdp["1951Q4"] = 100.0  # 只有 GDP 的季不算
        rows, tr, notes = longterm.build_buffett(eq, gdp, {"1952Q2": "1952-09-10"})
        self.assertEqual(len(rows), 80)
        self.assertEqual(rows[0][0], "1952Q1")
        self.assertAlmostEqual(rows[0][3], 110.0, places=2)  # 110,000 百萬＝110 十億 ÷ GDP 100 十億 → 110%
        self.assertAlmostEqual(math.expm1(tr["b"]), math.exp(0.02) - 1, places=3)
        self.assertAlmostEqual(abs(rows[10][5]), abs(rows[11][5]), delta=0.1)  # 大致正負對稱（交錯讓直線略偏）
        self.assertGreater(rows[10][5], 0)
        self.assertLess(rows[11][5], 0)
        self.assertEqual(rows[0][6], "")  # 擴張視窗前 40 季留空
        self.assertNotEqual(rows[39][6], "")
        self.assertEqual(rows[1][7:9], ["1952-09-10", "z1"])
        self.assertEqual(rows[0][7:9], ["1952-06-29", "est"])  # 季末 3/31 + 90 天
        self.assertEqual(rows[-1][9:], [""] * 5)  # 第一次建檔沒有初值
        self.assertEqual(notes["new"], [])

    def test_new_quarter_initial_kept_after_revision(self):
        eq, gdp = trend_data(80)
        legacy = [r[:9] for r in longterm.build_buffett(dict(list(eq.items())[:79]), gdp, {})[0]]  # 舊 9 欄、少最後一季
        rows, _, notes = longterm.build_buffett(eq, gdp, {}, legacy, NOW1)
        self.assertEqual(notes["new"], ["1971Q4"])
        self.assertEqual(rows[-2][9:], [""] * 5)  # 舊格式留下的季：沒有初值
        init = rows[-1][9:]
        self.assertEqual(init[:3], rows[-1][1:4])
        self.assertEqual(init[3:], [rows[-1][6], NOW1])
        # 下一次：GDP 修正，主欄位改、初值不動；重跑不重複新增
        gdp2 = dict(gdp, **{"1971Q4": 105.0})
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "b.csv")
            write_rows(p, rows)
            rows2, _, notes2 = longterm.build_buffett(eq, gdp2, {}, read_rows(p), NOW2)
        self.assertEqual(notes2["new"], [])
        self.assertEqual(rows2[-1][2], 105.0)
        self.assertEqual(rows2[-1][9:], [str(x) for x in init])


class ScheduleSkipTest(unittest.TestCase):
    def test_skip_when_current_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            cyc, buf, meta = (os.path.join(d, f) for f in ("c.csv", "b.csv", "meta.json"))
            write_rows(cyc, [["2026-09", "41", "紅"]])
            write_rows(buf, [["2026Q2", "1"]])
            now = dt.datetime(2026, 10, 30, 10, 0, tzinfo=dt.timezone.utc)
            fc, fb, saved = mock.Mock(), mock.Mock(), dict(longterm.STEPS)
            longterm.STEPS.update(cycle=("longterm/tw_business_cycle", fc), buffett=("longterm/buffett_indicator", fb))
            try:
                with mock.patch.multiple(longterm, CYCLE_CSV=cyc, BUFFETT_CSV=buf, META=meta, NOW=now):
                    self.assertEqual(longterm.main(["--skip-if-current"]), 0)
            finally:
                longterm.STEPS.clear()
                longterm.STEPS.update(saved)
            fc.assert_not_called()
            fb.assert_not_called()
            self.assertFalse(os.path.exists(meta))

    def test_runs_when_new_month_due(self):
        with tempfile.TemporaryDirectory() as d:
            cyc, meta = os.path.join(d, "c.csv"), os.path.join(d, "meta.json")
            write_rows(cyc, [["2026-08", "41", "紅"]])
            now = dt.datetime(2026, 10, 29, 10, 0, tzinfo=dt.timezone.utc)

            def fake(m):
                m["datasets"]["longterm/tw_business_cycle"] = {"status": "ok", "last": "2026-08"}

            saved = dict(longterm.STEPS)
            longterm.STEPS["cycle"] = ("longterm/tw_business_cycle", fake)
            try:
                with mock.patch.multiple(longterm, CYCLE_CSV=cyc, META=meta, NOW=now):
                    self.assertEqual(longterm.main(["--only", "cycle", "--skip-if-current"]), 0)
            finally:
                longterm.STEPS.clear()
                longterm.STEPS.update(saved)
            with open(meta, encoding="utf-8") as fh:
                got = json.load(fh)["datasets"]["longterm/tw_business_cycle"]
            self.assertEqual(got["checked_at_taipei"], "2026-10-29 18:00")


if __name__ == "__main__":
    unittest.main()

"""longterm.py（景氣對策信號、巴菲特指標）的解析與計算測試，全部用假資料、不連網。

執行：python -m unittest discover -s tests（在 repo 根目錄）
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import longterm  # noqa: E402


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

    def test_build_merges_and_records_first(self):
        zip_rows = {"2026-06": (41, "紅"), "2026-07": (41, "紅")}
        api_rows = {"2026-06": (40, "紅"), "2026-07": (41, "紅"), "2026-08": (36, "黃紅")}
        rows, notes = longterm.build_cycle(zip_rows, api_rows, "2026-09-29", [])
        self.assertEqual(notes["added_from_api"], ["2026-08"])
        self.assertEqual(notes["diff"], ["2026-06"])
        self.assertEqual(rows[0], ["2026-06", 41, "紅", "2026-07-30", "", ""])  # zip 為主
        self.assertEqual(rows[-1], ["2026-08", 36, "黃紅", "2026-09-30", "2026-09-29", 36])  # 最新一期記初值

    def test_build_keeps_previous_first_after_revision(self):
        previous = [["2026-08", "41", "紅", "2026-09-30", "2026-09-29", "41"]]
        zip_rows = {"2026-08": (40, "紅"), "2026-09": (37, "黃紅")}
        rows, _ = longterm.build_cycle(zip_rows, None, "2026-10-30", previous)
        self.assertEqual(rows[0], ["2026-08", 40, "紅", "2026-09-30", "2026-09-29", "41"])  # 修正後仍留初值
        self.assertEqual(rows[1][4:], ["2026-10-30", 37])

    def test_build_flags_light_mismatch_and_range(self):
        _, notes = longterm.build_cycle({"2026-08": (41, "綠")}, None, None, [])
        self.assertEqual(notes["bad_light"], ["2026-08"])
        with self.assertRaises(RuntimeError):
            longterm.build_cycle({"2026-08": (50, "紅")}, None, None, [])


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


class BuffettTest(unittest.TestCase):
    def test_quarter_helpers(self):
        self.assertEqual(longterm.quarter_of("2026-04-01"), "2026Q2")
        self.assertEqual(longterm.quarter_of("1951-10-01"), "1951Q4")
        self.assertEqual(longterm.quarter_end("2026Q1").isoformat(), "2026-03-31")
        self.assertEqual(longterm.quarter_end("2025Q4").isoformat(), "2025-12-31")

    def test_parse_z1_dates(self):
        self.assertEqual(longterm.parse_z1_dates(Z1_HTML), {"2026Q2": "2026-09-11", "2026Q1": "2026-06-11"})

    def test_build_ratio_trend_and_release(self):
        # 比率在 2% 年成長的趨勢上，偶數季 +10%、奇數季 −10% 交錯（殘差標準差固定）
        eq = series(1952, 80, lambda i: 1000 * 100 * math.exp(0.02 * i / 4) * (1.1 if i % 2 == 0 else 1 / 1.1))
        gdp = series(1952, 80, lambda i: 100.0)
        gdp["1951Q4"] = 100.0  # 只有 GDP 的季不算
        rows, tr = longterm.build_buffett(eq, gdp, {"1952Q2": "1952-09-10"})
        self.assertEqual(len(rows), 80)
        self.assertEqual(rows[0][0], "1952Q1")
        self.assertAlmostEqual(rows[0][3], 110.0, places=2)  # 110,000 百萬＝110 十億 ÷ GDP 100 十億 → 110%
        self.assertAlmostEqual(math.expm1(tr["b"]), math.exp(0.02) - 1, places=3)
        self.assertAlmostEqual(abs(rows[10][5]), abs(rows[11][5]), delta=0.1)  # 大致正負對稱（交錯讓直線略偏）
        self.assertGreater(rows[10][5], 0)
        self.assertLess(rows[11][5], 0)
        self.assertEqual(rows[0][6], "")  # 擴張視窗前 40 季留空
        self.assertNotEqual(rows[39][6], "")
        self.assertEqual(rows[1][7:], ["1952-09-10", "z1"])
        self.assertEqual(rows[0][7:], ["1952-06-29", "est"])  # 季末 3/31 + 90 天


if __name__ == "__main__":
    unittest.main()

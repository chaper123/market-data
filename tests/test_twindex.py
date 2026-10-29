"""twindex.py（加權指數、報酬指數）的合併、核對與來源失敗處理測試，全部用假資料、不連網。

執行：python -m unittest discover -s tests（在 repo 根目錄）
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common  # noqa: E402
import twindex  # noqa: E402

OA_OHLC = [{"Date": "1151028", "OpeningIndex": "46402.79", "HighestIndex": "47060.46", "LowestIndex": "46402.79",
            "ClosingIndex": "47035.58"},
           {"Date": "1151030", "OpeningIndex": "46400.00", "HighestIndex": "46500.00", "LowestIndex": "46300.00",
            "ClosingIndex": "46450.00"}]
OA_VOL = [{"Date": "1151028", "TradeVolume": "9000687119", "TradeValue": "750340536882"},
          {"Date": "1151030", "TradeVolume": "100", "TradeValue": "200"}]
OA_TR = [{"Date": "1151028", "TAIEXTotalReturnIndex": "108763.96"}, {"Date": "1151030", "TAIEXTotalReturnIndex": "108000.00"}]
FM_PRICE = [{"date": "2026-10-27", "open": 46211.2, "max": 46585.45, "min": 45676.67, "close": 46096.23,
             "Trading_Volume": 11786032535, "Trading_money": 1031683538938},
            {"date": "2026-10-28", "open": 46402.79, "max": 47060.46, "min": 46402.79, "close": 47035.58,
             "Trading_Volume": 9000687119, "Trading_money": 750340536882}]
FM_TR = [{"date": "2026-10-27", "price": 106591.78}, {"date": "2026-10-28", "price": 108763.96}]


def fake_openapi(path):
    return {"exchangeReport/FMTQIK": OA_VOL, "indicesReport/MI_5MINS_HIST": OA_OHLC, "indicesReport/MFI94U": OA_TR}[path]


def fake_finmind(dataset, data_id, start):
    return {"TaiwanStockPrice": FM_PRICE, "TaiwanStockTotalReturnIndex": FM_TR}[dataset]


class IndexTest(unittest.TestCase):
    def run_fetch(self, openapi=fake_openapi, fm=fake_finmind, cut="2026-10-30", previous=None):
        with tempfile.TemporaryDirectory() as d:
            ohlc, tr = os.path.join(d, "index", "TAIEX.csv"), os.path.join(d, "index", "TAIEX_TR.csv")
            if previous:
                common.write_rows(ohlc, previous[0])
                common.write_rows(tr, previous[1])
            meta = {"datasets": {}, "errors": []}
            with mock.patch.multiple(twindex, TAIEX_CSV=ohlc, TR_CSV=tr, MIN_ROWS=1, twse_openapi=openapi,
                                     finmind=fm, tw_cutoff=lambda: cut), \
                    mock.patch.object(common, "_PREV_META", {}):
                twindex.fetch_index({}, meta)
            rows = (common.read_rows(ohlc), common.read_rows(tr))
        return rows, meta["datasets"]

    def test_roc_date(self):
        self.assertEqual(common.roc_ymd("1151001"), "2026-10-01")
        self.assertEqual(common.roc_ymd("990105"), "2010-01-05")

    def test_merge_official_overrides_and_checks(self):
        (ohlc, tr), ds = self.run_fetch()
        self.assertEqual([r[0] for r in ohlc], ["2026-10-27", "2026-10-28", "2026-10-30"])
        self.assertEqual(ohlc[1], ["2026-10-28", "46402.79", "47060.46", "46402.79", "47035.58", "9000687119",
                                   "750340536882"])
        self.assertEqual(ohlc[2][4:], ["46450.0", "100", "200"])  # 只有 OpenAPI 有的日子
        self.assertEqual(tr[-1], ["2026-10-30", "108000.0"])
        self.assertTrue(ds["index/TAIEX"]["cross_check_ok"])
        self.assertEqual(ds["index/TAIEX_TR"]["cross_check"], "OpenAPI 與 FinMind 重疊 1 天，收盤不一致 0 天")
        self.assertEqual(ds["index/TAIEX"]["last"], "2026-10-30")

    def test_cutoff_drops_today_before_close(self):
        (ohlc, tr), ds = self.run_fetch(cut="2026-10-29")
        self.assertEqual(ohlc[-1][0], "2026-10-28")
        self.assertEqual(ds["index/TAIEX_TR"]["last"], "2026-10-28")

    def test_mismatch_flagged(self):
        bad = [dict(FM_TR[0]), dict(FM_TR[1], price=108700.0)]
        _, ds = self.run_fetch(fm=lambda dataset, i, s: bad if dataset == "TaiwanStockTotalReturnIndex"
                               else FM_PRICE)
        self.assertFalse(ds["index/TAIEX_TR"]["cross_check_ok"])
        self.assertEqual(ds["index/TAIEX_TR"]["diff_days"], ["2026-10-28"])
        self.assertTrue(ds["index/TAIEX"]["cross_check_ok"])

    def test_finmind_down_keeps_previous_history(self):
        previous = ([["2026-10-27", 1, 2, 0.5, 1.5, 10, 20]], [["2026-10-27", 99.0]])

        def down(*a):
            raise RuntimeError("HTTP 402")

        (ohlc, tr), ds = self.run_fetch(fm=down, previous=previous)
        self.assertEqual([r[0] for r in ohlc], ["2026-10-27", "2026-10-28", "2026-10-30"])
        self.assertEqual(tr[0], ["2026-10-27", "99.0"])
        self.assertEqual(ds["index/TAIEX"]["status"], "ok")
        self.assertIn("FinMind", ds["index/TAIEX"]["warnings"][0])
        self.assertIsNone(ds["index/TAIEX"]["cross_check_ok"])

    def test_both_down_is_failure(self):
        def down(*a):
            raise RuntimeError("down")

        _, ds = self.run_fetch(openapi=down, fm=down)
        self.assertEqual(ds["index/TAIEX"]["status"], "fail")
        self.assertEqual(ds["index/TAIEX_TR"]["status"], "fail")


if __name__ == "__main__":
    unittest.main()

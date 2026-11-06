"""stocks.py（個股融資、集保股權分散、月營收）的換算、合併與核對測試，全部用假資料、不連網。

執行：python -m unittest discover -s tests（在 repo 根目錄）
"""
import datetime as dt
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common  # noqa: E402
import stocks  # noqa: E402

NOW = dt.datetime(2026, 11, 7, 0, 0, tzinfo=dt.timezone.utc)  # 台北 11/07 08:00

MARGIN = [{"date": "2026-11-05", "stock_id": "2330", "MarginPurchaseTodayBalance": 34596, "ShortSaleTodayBalance": 47},
          {"date": "2026-11-06", "stock_id": "2330", "MarginPurchaseTodayBalance": 35072, "ShortSaleTodayBalance": 75}]
REVENUE = [{"date": "2026-10-01", "stock_id": "2330", "revenue": 511857399000, "revenue_month": 9, "revenue_year": 2026}]
# 集保 CSV：表頭（中文）＋ 2330 的 1、15、16、17 級（加總對得上）＋別的代號
TDCC = ("﻿資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%\n"
        "20261030,2330  ,1,2610784,306416985,1.18\n"
        "20261030,2330  ,15,1478,25625952082,98.82\n"
        "20261030,2330  ,16,1,1000,0.00\n"
        "20261030,2330  ,17,2612263,25932370067,100.00\n"
        "20261030,0050  ,1,100,1000,100.00\n")


def fake_finmind(dataset, data_id, start):
    return {"TaiwanStockMarginPurchaseShortSale": MARGIN, "TaiwanStockMonthRevenue": REVENUE}[dataset]


class StocksTest(unittest.TestCase):
    def run_fetch(self, tdcc=TDCC, old=None):
        with tempfile.TemporaryDirectory() as tmp:
            paths = {k: os.path.join(tmp, *k.split("/")) + ".csv" for k in stocks.PATHS}
            for k, rows in (old or {}).items():
                common.write_rows(paths[k], rows)
            meta = {"datasets": {}, "errors": []}
            with mock.patch.multiple(stocks, NOW=NOW, PATHS=paths, finmind=fake_finmind,
                                     http=lambda url, **kw: (200, tdcc.encode("utf-8"))), \
                    mock.patch.object(common, "_PREV_META", {}):
                stocks.fetch_stocks({"stocks": ["2330"]}, meta)
            rows = {k: common.read_rows(p) for k, p in paths.items()}
        return rows, meta

    def test_conversion(self):
        rows, meta = self.run_fetch()
        self.assertEqual(rows["chips/tw_stock_margin"], [["2026-11-05", "2330", "34596", "47"], ["2026-11-06", "2330", "35072", "75"]])
        self.assertEqual(rows["fundamentals/tw_revenue"], [["2026-09", "2330", "511857399000"]])  # 營收所屬月份
        self.assertEqual(len(rows["chips/tw_holders"]), 4)  # 只留 2330
        self.assertEqual(rows["chips/tw_holders"][0][:3], ["2026-10-30", "2330", "1"])
        self.assertTrue(meta["datasets"]["chips/tw_holders"]["cross_check_ok"])
        self.assertEqual(meta["errors"], [])

    def test_merge_keeps_history_and_updates(self):
        old = {"chips/tw_stock_margin": [["2026-10-30", "2330", "30000", "1"], ["2026-11-05", "2330", "1", "1"]]}
        rows, _ = self.run_fetch(old=old)
        m = rows["chips/tw_stock_margin"]
        self.assertEqual(m[0], ["2026-10-30", "2330", "30000", "1"])  # 舊的保留
        self.assertIn(["2026-11-05", "2330", "34596", "47"], m)  # 同一天以新抓的為準
        self.assertEqual(len(m), 3)

    def test_holders_check_accepts_minus_adjustment(self):
        # 實際資料（2026-10-30）：1～15 加總扣掉 16 差異數調整才等於合計
        minus = TDCC.replace("2330  ,17,2612263,25932370067", "2330  ,17,2612263,25932368067")
        _, meta = self.run_fetch(tdcc=minus)
        self.assertTrue(meta["datasets"]["chips/tw_holders"]["cross_check_ok"])

    def test_holders_check_fails_when_sum_differs(self):
        bad = TDCC.replace("2330  ,16,1,1000", "2330  ,16,1,999")
        _, meta = self.run_fetch(tdcc=bad)
        self.assertFalse(meta["datasets"]["chips/tw_holders"]["cross_check_ok"])

    def test_tdcc_failure_keeps_other_datasets(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = {k: os.path.join(tmp, *k.split("/")) + ".csv" for k in stocks.PATHS}
            meta = {"datasets": {}, "errors": []}

            def boom(url, **kw):
                raise OSError("SSL")
            with mock.patch.multiple(stocks, NOW=NOW, PATHS=paths, finmind=fake_finmind, http=boom), \
                    mock.patch.object(common, "_PREV_META", {}):
                stocks.fetch_stocks({"stocks": ["2330"]}, meta)
        self.assertEqual(meta["datasets"]["chips/tw_holders"]["status"], "fail")
        self.assertEqual(meta["datasets"]["chips/tw_stock_margin"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()

"""chips.py（法人、融資，FinMind）的換算與補 0 測試，全部用假資料、不連網。

執行：python -m unittest discover -s tests（在 repo 根目錄）
"""
import datetime as dt
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chips  # noqa: E402
import common  # noqa: E402

NOW = dt.datetime(2026, 11, 3, 3, 0, tzinfo=dt.timezone.utc)  # 台北 11:00，只收到 11/02


def insti(d, code, **net):
    return [{"date": d, "stock_id": code, "name": k, "buy": max(v, 0), "sell": max(-v, 0)} for k, v in net.items()]


MARKET = insti("2026-11-02", "", Dealer_self=-10, Dealer_Hedging=-20, Investment_Trust=30, Foreign_Investor=-40,
               Foreign_Dealer_Self=0, total=-40)
MARGIN = [{"date": "2026-11-02", "name": "MarginPurchase", "TodayBalance": 9571091},
          {"date": "2026-11-02", "name": "ShortSale", "TodayBalance": 199000},
          {"date": "2026-11-02", "name": "MarginPurchaseMoney", "TodayBalance": 625000123456}]


def fake_finmind(dataset, data_id, start):
    if dataset == "TaiwanStockTotalInstitutionalInvestors":
        return MARKET
    if dataset == "TaiwanStockTotalMarginPurchaseShortSale":
        return MARGIN
    return insti("2026-11-02", data_id, Foreign_Investor=-13507165, Dealer_self=1000,
                 Dealer_Hedging=-171552760) if data_id == "009816" else []  # 00981A 這天沒有法人交易


class ChipsTest(unittest.TestCase):
    def run_fetch(self, fm=fake_finmind):
        with tempfile.TemporaryDirectory() as tmp:
            paths = {k: os.path.join(tmp, "chips", f"{k}.csv") for k in chips.PATHS}
            common.write_rows(paths["tw_insti"], [["2026-10-30", "009816", 0, 0, 0, 0, 0]])
            common.write_rows(paths["tw_market_insti"], [["2026-10-30", 1, 1, 1, 1, 0, 4]])
            common.write_rows(paths["tw_margin"], [["2026-10-30", 1, 1, 1]])
            meta = {"datasets": {}, "errors": []}
            with mock.patch.multiple(chips, DATA=tmp, NOW=NOW, PATHS=paths, finmind=fm, LOOKBACK_DAYS=2), \
                    mock.patch.object(common, "_PREV_META", {}):
                chips.fetch_chips({"tw": {"009816": {"market": "TWSE"}, "00981A": {"market": "TWSE"}}}, meta)
            rows = {k: common.read_rows(p) for k, p in paths.items()}
        return rows, meta["datasets"]

    def test_conversion_and_zero_fill(self):
        rows, ds = self.run_fetch()
        self.assertIn(["2026-11-02", "009816", "-13507165", "0", "0", "-171551760", "-185058925"], rows["tw_insti"])
        self.assertIn(["2026-11-02", "00981A", "0", "0", "0", "0", "0"], rows["tw_insti"])  # 沒有法人交易記 0
        self.assertEqual(rows["tw_market_insti"][-1], ["2026-11-02", "-10", "-20", "30", "-40", "0", "-40"])
        self.assertEqual(rows["tw_margin"][-1], ["2026-11-02", "9571091", "199000", "625000123"])  # 元→仟元
        self.assertTrue(ds["chips/tw_market_insti"]["cross_check_ok"])
        self.assertEqual(ds["chips/tw_insti"]["source"], chips.SOURCE)

    def test_total_mismatch_flagged(self):
        bad = [dict(x) for x in MARKET]
        bad[-1]["sell"] = 99  # total 改成 -99
        _, ds = self.run_fetch(lambda d, i, s: bad if d == "TaiwanStockTotalInstitutionalInvestors"
                               else fake_finmind(d, i, s))
        self.assertFalse(ds["chips/tw_market_insti"]["cross_check_ok"])

    def test_nothing_missing_makes_no_request(self):
        calls = []

        def counting(*a):
            calls.append(a[0])
            return fake_finmind(*a)

        with tempfile.TemporaryDirectory() as tmp:
            paths = {k: os.path.join(tmp, f"{k}.csv") for k in chips.PATHS}
            common.write_rows(paths["tw_insti"], [["2026-11-02", "009816", 1, 0, 0, 0, 1]])
            common.write_rows(paths["tw_market_insti"], [["2026-11-02", 1, 1, 1, 1, 0, 4]])
            common.write_rows(paths["tw_margin"], [["2026-11-02", 1, 1, 1]])
            with mock.patch.multiple(chips, DATA=tmp, NOW=NOW, PATHS=paths, finmind=counting, LOOKBACK_DAYS=1), \
                    mock.patch.object(common, "_PREV_META", {}):
                chips.fetch_chips({"tw": {"009816": {"market": "TWSE"}}}, {"datasets": {}, "errors": []})
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

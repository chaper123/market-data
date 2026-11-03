"""dividends.py（ETF 配息）的解析、合併與核對測試，全部用假資料、不連網。

執行：python -m unittest discover -s tests（在 repo 根目錄）
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dividends  # noqa: E402


def fm(ex, cash, pay="", year="115"):
    return {"CashExDividendTradingDate": ex, "CashDividendPaymentDate": pay, "CashEarningsDistribution": cash,
            "CashStatutorySurplus": 0.0, "year": year}


class DividendTest(unittest.TestCase):
    def test_from_finmind_skips_empty(self):
        got = dividends.from_finmind([fm("2026-10-22", 1.72, "2026-11-11"), fm("", 1.0), fm("2026-07-21", 0)])
        self.assertEqual(list(got), ["2026-10-22"])
        self.assertEqual((got["2026-10-22"]["cash"], got["2026-10-22"]["pay_date"]), (1.72, "2026-11-11"))

    def test_from_twse_only_cash_events_of_listed_codes(self):
        rows = [{"Date": "1151106", "Code": "00919", "Exdividend": "息", "CashDividend": "1.200000"},
                {"Date": "1151106", "Code": "2330", "Exdividend": "息", "CashDividend": "5.0"},
                {"Date": "1151110", "Code": "0056", "Exdividend": "權", "CashDividend": ""}]
        self.assertEqual(dividends.from_twse(rows, {"00919", "0056"}), {"00919": {"2026-11-06": 1.2}})

    def test_build_ttm_next_and_official(self):
        events = dividends.from_finmind([fm("2025-10-23", 0.866), fm("2026-01-22", 0.866), fm("2026-10-22", 1.72)])
        official = {"2026-10-22": 1.72, "2026-11-20": None}  # 第二筆是 FinMind 還沒有、金額未公布的預告
        out, bad = dividends.build("0056", events, official, [], "2022-11-01", "2026-11-03")
        self.assertEqual(bad, [])
        self.assertEqual((out["ttm_cash"], out["ttm_count"]), (2.586, 2))  # 2025-10-23 已超過 365 天
        self.assertEqual(out["last"]["ex_date"], "2026-10-22")
        self.assertTrue(out["last"]["official_checked"])
        self.assertEqual(out["next"], {"ex_date": "2026-11-20", "pay_date": None, "cash": None, "year": None,
                                       "source": "證交所除權除息預告表", "official_checked": True})

    def test_build_flags_mismatch_and_keeps_old_events(self):
        old = [{"ex_date": "2021-07-19", "pay_date": "2021-08-11", "cash": 1.8, "year": "110", "source": "FinMind",
                "official_checked": False}]
        events = dividends.from_finmind([fm("2026-10-22", 1.72)])
        out, bad = dividends.build("0056", events, {"2026-10-22": 1.7}, old, "2022-11-01", "2026-11-03")
        self.assertEqual(bad, ["0056 2026-10-22 預告表 1.7 vs FinMind 1.72"])
        self.assertEqual([e["ex_date"] for e in out["events"]], ["2021-07-19", "2026-10-22"])  # 視窗以前的沿用

    def test_no_dividends(self):
        out, bad = dividends.build("009816", {}, None, [], "2022-11-01", "2026-11-03")
        self.assertEqual((out["events"], out["ttm_cash"], out["last"], out["next"]), ([], 0, None, None))


if __name__ == "__main__":
    unittest.main()

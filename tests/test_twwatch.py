"""twwatch.py（0056、00878、00919 日線）的合併與核對測試，全部用假資料、不連網。

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
import twwatch  # noqa: E402

NOW = dt.datetime(2026, 11, 3, 9, 0, tzinfo=dt.timezone.utc)  # 台北 17:00


def fm_rows(n=70, last_close=55.0):
    d, out = dt.date(2026, 7, 1), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append({"date": d.isoformat(), "Trading_Volume": 1000, "open": 50, "max": 51, "min": 49, "close": 50.0})
        d += dt.timedelta(days=1)
    out[-1]["close"] = last_close
    return out


class WatchTest(unittest.TestCase):
    def run_fetch(self, fm, oa):
        with tempfile.TemporaryDirectory() as tmp:
            meta = {"datasets": {}, "errors": []}
            with mock.patch.multiple(twwatch, DATA=tmp, NOW=NOW, finmind=fm, twse_openapi=oa,
                                     tw_cutoff=lambda: "2026-11-03"), \
                    mock.patch.object(common, "_PREV_META", {}):
                twwatch.fetch_watch({"tw_watch": {"0056": {"market": "TWSE"}}, "tw_months": 13}, meta)
            rows = common.read_rows(os.path.join(tmp, "tw", "0056_norm.csv"))
        return rows, meta["datasets"]["tw_watch/0056"]

    def test_openapi_checks_and_fills_last_day(self):
        rows_fm = fm_rows()
        last = rows_fm[-1]["date"]
        roc = f"{int(last[:4]) - 1911}{last[5:7]}{last[8:]}"
        oa = [{"Date": roc, "Code": "0056", "TradeVolume": "2000", "OpeningPrice": "54", "HighestPrice": "56",
               "LowestPrice": "53", "ClosingPrice": "55.00"}]
        rows, m = self.run_fetch(lambda *a: rows_fm, lambda p: oa)
        self.assertTrue(m["cross_check_ok"])
        self.assertEqual(rows[-1], [last, "2000", "54.0", "56.0", "53.0", "55.0"])  # 同一天以 OpenAPI 為準
        # FinMind 還沒有的那一天由 OpenAPI 補上
        oa2 = [dict(oa[0], Date="1151103", ClosingPrice="56.00")]
        rows, m = self.run_fetch(lambda *a: rows_fm, lambda p: oa2)
        self.assertEqual(rows[-1][0], "2026-11-03")
        self.assertIn("用 OpenAPI 補上", m["cross_check"])

    def test_mismatch_and_openapi_down(self):
        rows_fm = fm_rows(last_close=55.0)
        last = rows_fm[-1]["date"]
        oa = [{"Date": f"{int(last[:4]) - 1911}{last[5:7]}{last[8:]}", "Code": "0056", "TradeVolume": "1",
               "OpeningPrice": "1", "HighestPrice": "1", "LowestPrice": "1", "ClosingPrice": "54.00"}]
        _, m = self.run_fetch(lambda *a: rows_fm, lambda p: oa)
        self.assertFalse(m["cross_check_ok"])

        def down(p):
            raise RuntimeError("HTTP 503")

        rows, m = self.run_fetch(lambda *a: rows_fm, down)
        self.assertEqual((m["status"], len(rows)), ("ok", 70))
        self.assertIn("OpenAPI", m["warnings"][0])

    def test_tpex_volume_in_lots(self):
        fm = [{"date": "2026-10-30", "Trading_Volume": 8822678, "open": 9.7, "max": 9.8, "min": 9.6, "close": 9.73}]
        tp = [{"Date": "1151030", "SecuritiesCompanyCode": "00411A", "TradingShares": "8822678", "Open": "9.70",
               "High": "9.80", "Low": "9.60", "Close": "9.73"}]
        with mock.patch.multiple(twwatch, finmind=lambda *a: fm, tpex_openapi=lambda p: tp):
            latest, err = twwatch.openapi_latest({"TPEX": {"00411A"}})
            rows, check, ok, warn = twwatch.official_daily("00411A", "TPEX", "2026-10-01", latest)
        self.assertEqual(err, [])
        self.assertEqual(rows["2026-10-30"], ("2026-10-30", 8823, 9.7, 9.8, 9.6, 9.73))  # 股換成張，和舊檔一致
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()

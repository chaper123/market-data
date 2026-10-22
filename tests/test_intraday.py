"""intraday.py 的測試：假的 MIS getStockInfo 回應，不連網。

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

import intraday  # noqa: E402

TPE = ZoneInfo("Asia/Taipei")
CFG = {"tw": {"009816": {"market": "TWSE"}, "00981A": {"market": "TWSE"}, "00411A": {"market": "TPEX"}}}


def tpe(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=TPE)


def item(code, ex, d, t, z, y, v="1000", pz=None, n="名稱"):
    ms = int(dt.datetime.strptime(f"{d} {t}", "%Y%m%d %H:%M:%S").replace(tzinfo=TPE).timestamp() * 1000)
    m = {"c": code, "n": n, "ex": ex, "d": d, "t": t, "tlong": str(ms), "z": z, "y": y, "o": y, "h": y, "l": y,
         "u": "99", "w": "1"}
    if v is not None:
        m["v"] = v
    if pz is not None:
        m["pz"] = pz
    return m


def mis(items, rtcode="0000"):
    return json.dumps({"rtcode": rtcode, "rtmessage": "OK", "msgArray": items,
                       "queryTime": {"sysDate": "20261023", "sysTime": "10:05:01"}}).encode()


def all_items(d, t, z00981="28.50"):
    return [item("009816", "tse", d, t, "16.10", "16.00"), item("00981A", "tse", d, t, z00981, "28.19", pz="28.45"),
            item("00411A", "otc", d, t, "9.95", "9.93"), item("t00", "tse", d, t, "46100.00", "46041.43", v=None)]


class IntradayTest(unittest.TestCase):
    def run_at(self, now, payload, closed=()):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "calendar"))
            json.dump({"tw_market": {"closed": list(closed)}},
                      open(os.path.join(tmp, "calendar", "calendar.json"), "w", encoding="utf-8"))
            json.dump(CFG, open(os.path.join(tmp, "config.json"), "w", encoding="utf-8"))
            out = os.path.join(tmp, "tw", "intraday.json")
            with mock.patch.object(intraday, "DATA", tmp), mock.patch.object(intraday, "ROOT", tmp), \
                    mock.patch.object(intraday, "OUT", out), mock.patch.object(intraday, "NOW", now), \
                    mock.patch.object(intraday, "http", lambda url, **kw: (200, payload)), \
                    mock.patch.object(intraday.time, "sleep", lambda s: None):
                intraday.main()
            return json.load(open(out, encoding="utf-8"))

    def test_during_session(self):
        doc = self.run_at(tpe(2026, 10, 23, 10, 5), mis(all_items("20261023", "10:04:55", z00981="-")))
        self.assertEqual(doc["session"], "盤中")
        self.assertTrue(doc["is_trading"])
        q = doc["quotes"]["00981A"]
        self.assertEqual((q["price"], q["price_field"]), (28.45, "pz"))  # 這一刻沒成交（z 是 "-"）改用 pz
        self.assertEqual(q["time"], "2026-10-23 10:04:55")
        self.assertTrue(q["is_today"])
        self.assertAlmostEqual(doc["quotes"]["009816"]["change_pct"], 0.63)
        self.assertEqual(doc["quotes"]["00411A"]["market"], "otc")
        self.assertIsNone(doc["quotes"]["TAIEX"]["volume_lots"])
        self.assertEqual(doc["status"], "ok")

    def test_after_close_is_labelled(self):
        doc = self.run_at(tpe(2026, 10, 22, 17, 3), mis(all_items("20261022", "13:30:00")))
        self.assertEqual(doc["session"], "收盤後")
        self.assertFalse(doc["is_trading"])
        self.assertIn("非交易時段（收盤後）", doc["note"])

    def test_before_open_shows_previous_day(self):
        doc = self.run_at(tpe(2026, 10, 23, 8, 30), mis(all_items("20261022", "13:30:00")))
        self.assertEqual(doc["session"], "盤前")
        self.assertFalse(doc["quotes"]["009816"]["is_today"])
        self.assertIn("前一交易日", doc["note"])

    def test_holiday_and_weekend(self):
        doc = self.run_at(tpe(2026, 10, 26, 10, 5), mis(all_items("20261023", "13:30:00")), closed=["2026-10-26"])
        self.assertEqual(doc["session"], "休市日")
        self.assertFalse(doc["trading_day"])
        doc = self.run_at(tpe(2026, 10, 24, 10, 5), mis(all_items("20261023", "13:30:00")))
        self.assertEqual(doc["session"], "休市日")

    def test_missing_code_is_partial(self):
        items = all_items("20261023", "10:04:55")[:2] + all_items("20261023", "10:04:55")[3:]
        doc = self.run_at(tpe(2026, 10, 23, 10, 5), mis(items))
        self.assertEqual(doc["status"], "partial")
        self.assertIn("otc_00411A", doc["errors"][0])

    def test_bad_rtcode_keeps_old_file(self):
        with self.assertRaises(SystemExit):
            self.run_at(tpe(2026, 10, 23, 10, 5), mis([], rtcode="9999"))


if __name__ == "__main__":
    unittest.main()

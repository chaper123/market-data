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


class IntradayBase(unittest.TestCase):
    def run_at(self, now, payload, closed=()):
        """payload 可以是一份回應，或依序回傳的多份回應（重抓時用；用完就一直回最後一份）。"""
        seq = list(payload) if isinstance(payload, list) else [payload]
        calls = []

        def fake_http(url, **kw):
            calls.append(url)
            return 200, seq[min(len(calls), len(seq)) - 1]

        self.calls = calls
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "calendar"))
            json.dump({"tw_market": {"closed": list(closed)}},
                      open(os.path.join(tmp, "calendar", "calendar.json"), "w", encoding="utf-8"))
            json.dump(CFG, open(os.path.join(tmp, "config.json"), "w", encoding="utf-8"))
            out = os.path.join(tmp, "tw", "intraday.json")
            with mock.patch.object(intraday, "DATA", tmp), mock.patch.object(intraday, "ROOT", tmp), \
                    mock.patch.object(intraday, "OUT", out), mock.patch.object(intraday, "NOW", now), \
                    mock.patch.object(intraday, "http", fake_http), \
                    mock.patch.object(intraday.time, "sleep", lambda s: None):
                intraday.main()
            meta = os.path.join(tmp, "meta.json")
            self.meta = json.load(open(meta, encoding="utf-8")) if os.path.exists(meta) else None
            return json.load(open(out, encoding="utf-8"))


class IntradayTest(IntradayBase):
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

    def test_slot_and_meta(self):
        doc = self.run_at(tpe(2026, 10, 23, 10, 5), mis(all_items("20261023", "10:04:55")))
        self.assertEqual(doc["slot"], "早盤")
        m = self.meta["datasets"]["intraday/tw"]
        self.assertEqual((m["status"], m["slot"], m["last"], m["quotes"]), ("ok", "早盤", "2026-10-23 10:05:00", 4))
        doc = self.run_at(tpe(2026, 10, 23, 12, 0), mis(all_items("20261023", "11:59:58")))
        self.assertEqual(doc["slot"], "午間")
        self.assertEqual(self.meta["datasets"]["intraday/tw"]["slot"], "午間")
        doc = self.run_at(tpe(2026, 10, 23, 17, 3), mis(all_items("20261023", "13:30:00")))
        self.assertEqual(doc["slot"], "盤外")

    def test_failure_marks_meta_and_keeps_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "tw"))
            with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as fh:
                json.dump(CFG, fh)
            with open(os.path.join(tmp, "tw", "intraday.json"), "w", encoding="utf-8") as fh:
                fh.write("{}")
            with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as fh:
                json.dump({"datasets": {"us/VOO": {"status": "ok"},
                                        "intraday/tw": {"status": "ok", "last": "2026-10-23 10:05:00", "slot": "早盤"}}}, fh)
            with mock.patch.object(intraday, "DATA", tmp), mock.patch.object(intraday, "ROOT", tmp),                     mock.patch.object(intraday, "OUT", os.path.join(tmp, "tw", "intraday.json")),                     mock.patch.object(intraday, "NOW", tpe(2026, 10, 23, 12, 0)),                     mock.patch.object(intraday, "http", lambda url, **kw: (200, mis([]))),                     mock.patch.object(intraday.time, "sleep", lambda s: None):
                with self.assertRaises(SystemExit):
                    intraday.main()
            with open(os.path.join(tmp, "meta.json"), encoding="utf-8") as fh:
                ds = json.load(fh)["datasets"]
        self.assertEqual(ds["us/VOO"], {"status": "ok"})
        m = ds["intraday/tw"]
        self.assertEqual((m["status"], m["last"], m["slot"]), ("stale", "2026-10-23 10:05:00", "早盤"))

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


def quoted(code, ex, z, y, b="", a="", u=None, w=None, t="10:05:09"):
    """盤中的一筆，帶五檔買賣價（b、a）與漲跌停（u、w）。"""
    m = item(code, ex, "20261027", t, z, y)
    m.update(b=b, a=a)
    m["u"], m["w"] = u, w
    if u is None:
        m.pop("u")
    if w is None:
        m.pop("w")
    return m


def session_items(q00981, q00411, q009816=None):
    return [q009816 or quoted("009816", "tse", "15.95", "15.93", "15.9400_15.9300_", "15.9500_15.9600_", "17.52", "14.34"),
            q00981, q00411, item("t00", "tse", "20261027", "10:04:55", "46219.29", "46041.43", v=None)]


class PriceFallbackTest(IntradayBase):
    NOW = tpe(2026, 10, 27, 10, 5)

    def test_z_normal(self):
        doc = self.run_at(self.NOW, mis(session_items(
            quoted("00981A", "tse", "28.10", "28.19", "28.0900_", "28.1000_", "31.00", "25.38"),
            quoted("00411A", "otc", "9.98", "9.93", "9.9700_", "9.9800_", "9999.95"))))
        q = doc["quotes"]["00981A"]
        self.assertEqual((q["price"], q["price_field"], q["price_retries"]), (28.1, "z", 0))
        self.assertEqual((q["bid1"], q["ask1"]), (28.09, 28.1))
        self.assertEqual(len(self.calls), 1)  # 都有成交價就不重抓
        self.assertNotIn("備援", doc["note"])

    def test_retry_gets_trade(self):
        first = mis(session_items(quoted("00981A", "tse", "-", "28.19", "28.0900_", "28.1000_", "31.00", "25.38"),
                                  quoted("00411A", "otc", "9.98", "9.93", "9.9700_", "9.9800_")))
        second = mis(session_items(quoted("00981A", "tse", "28.12", "28.19", "28.1100_", "28.1200_", "31.00", "25.38",
                                          t="10:05:17"),
                                   quoted("00411A", "otc", "9.98", "9.93", "9.9700_", "9.9800_")))
        doc = self.run_at(self.NOW, [first, second])
        q = doc["quotes"]["00981A"]
        self.assertEqual((q["price"], q["price_field"], q["price_retries"], q["time"]),
                         (28.12, "z_retry", 1, "2026-10-27 10:05:17"))
        self.assertAlmostEqual(q["change_pct"], -0.25)
        self.assertEqual(doc["quotes"]["00411A"]["price_field"], "z")
        self.assertEqual(len(self.calls), 2)

    def test_no_trade_uses_bid_ask_mid(self):
        payload = mis(session_items(quoted("00981A", "tse", "-", "28.19", "27.4800_27.4700_", "27.4900_27.5000_",
                                           "31.00", "25.38"),
                                    quoted("00411A", "otc", "-", "9.93", "9.9100_", "9.9200_", "9999.95")))
        doc = self.run_at(self.NOW, payload)
        q = doc["quotes"]["00981A"]
        self.assertEqual((q["price"], q["price_field"], q["bid1"], q["ask1"]), (27.485, "bid_ask_mid", 27.48, 27.49))
        self.assertAlmostEqual(q["change"], -0.705)
        self.assertAlmostEqual(q["change_pct"], -2.5)
        self.assertIn("中間價", q["price_note"])
        self.assertEqual(len(self.calls), 1 + intraday.RETRIES)  # 重抓 3 次
        self.assertIn("00981A、00411A 那一刻沒有成交價", doc["note"])
        # 上櫃無漲跌幅限制：MIS 給 9999.95 → null 並說明
        q = doc["quotes"]["00411A"]
        self.assertEqual((q["price"], q["limit_up"], q["limit_down"]), (9.915, None, None))
        self.assertIn("9999.95", q["limit_note"])
        self.assertEqual((doc["quotes"]["00981A"]["limit_up"], doc["quotes"]["00981A"]["limit_down"]), (31.0, 25.38))

    def test_one_side_and_no_quote(self):
        payload = mis(session_items(quoted("00981A", "tse", "-", "28.19", "28.0000_", ""),
                                    quoted("00411A", "otc", "-", "9.93", "-", "0.0000_"),
                                    quoted("009816", "tse", "-", "15.93", "", "15.9600_")))
        doc = self.run_at(self.NOW, payload)
        self.assertEqual((doc["quotes"]["00981A"]["price"], doc["quotes"]["00981A"]["price_field"]), (28.0, "bid1"))
        self.assertEqual((doc["quotes"]["009816"]["price"], doc["quotes"]["009816"]["price_field"]), (15.96, "ask1"))
        q = doc["quotes"]["00411A"]
        self.assertEqual((q["price"], q["price_field"], q["change"], q["change_pct"]), (None, None, None, None))
        self.assertIn("沒有買賣報價", q["price_note"])
        self.assertEqual(doc["status"], "ok")

    def test_after_close_labels_unchanged(self):
        payload = mis([quoted("009816", "tse", "15.93", "16.08", "15.9200_", "15.9300_", "17.68", "14.48", t="13:30:00"),
                       quoted("00981A", "tse", "28.19", "28.88", "28.1900_", "28.2000_", "31.76", "26.00", t="13:30:00"),
                       quoted("00411A", "otc", "9.93", "10.04", "9.9300_", "9.9600_", "9999.95", t="13:30:00"),
                       item("t00", "tse", "20261027", "13:33:00", "46041.43", "46887.77", v=None)])
        doc = self.run_at(tpe(2026, 10, 27, 17, 3), payload)
        self.assertEqual((doc["session"], doc["is_trading"]), ("收盤後", False))
        self.assertIn("非交易時段（收盤後）", doc["note"])
        self.assertTrue(all(q["price_field"] == "z" for q in doc["quotes"].values()))


if __name__ == "__main__":
    unittest.main()

"""candidates.py（機會頁的候選 ETF：名單、成交金額、每檔日線、收盤合併檔）的測試，全部用假資料、不連網。

執行：python -m unittest discover -s tests（在 repo 根目錄）
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import candidates  # noqa: E402
import common  # noqa: E402
import fetch  # noqa: E402
import twwatch  # noqa: E402

UTC = dt.timezone.utc
T_TW = "國內成分證券指數股票型基金"
T_OS = "國外成分證券指數股票型基金"
T_ATW = "國內成分證券主動式交易所交易基金(股票)"
T_AOS = "國外成分證券主動式交易所交易基金(股票)"
T_LEV = "槓桿/反向指數股票型基金"
T_BOND = "國外成分證券主動式交易所交易基金(債券)"


def rec(code, name, typ, listed="0920630"):
    return {"基金代號": code, "基金簡稱": name, "基金類型": typ, "上市日期": listed, "是否包含國外成分股": "否"}


def weekdays(start, end):
    d, out = dt.date.fromisoformat(start), []
    while d <= dt.date.fromisoformat(end):
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


class ClassifyTest(unittest.TestCase):
    def test_categories(self):
        for typ, want in ((T_TW, "tw"), (T_OS, "overseas"), (T_ATW, "tw"), (T_AOS, "overseas")):
            self.assertEqual(candidates.classify(rec("00900", "某ETF", typ)), (want, []))
        # 主動式的尾碼 A、一般的數字結尾都不排除
        self.assertEqual(candidates.classify(rec("00981A", "主動統一台股增長", T_ATW)), ("tw", []))

    def test_excluded_by_suffix(self):
        for ch in "BLRUKCTD":
            cat, reasons = candidates.classify(rec(f"0099{ch}", "某ETF", T_TW))
            self.assertIsNone(cat)
            self.assertEqual(reasons, [f"尾碼 {ch}"])

    def test_excluded_by_type(self):
        cases = {
            T_LEV: ["基金類型含 槓桿", "基金類型含 反向"],
            "指數股票型期貨信託基金": ["基金類型含 期貨"],
            "國外成份/加掛外幣證券指數股票型基金": ["基金類型含 外幣"],  # 成「份」不是成「分」，照樣被外幣擋掉
            "連結式證券指數股票型基金": ["基金類型含 連結"],
            "境外指數股票型基金": ["基金類型含 境外"],
            "國外成分證券平衡型指數股票型基金": ["基金類型含 平衡"],
            T_BOND: ["基金類型含 債券"],
        }
        for typ, want in cases.items():
            cat, reasons = candidates.classify(rec("00900", "某ETF", typ))
            self.assertIsNone(cat, typ)
            self.assertEqual(reasons, want, typ)

    def test_excluded_by_name(self):
        for word in ("黃金", "白銀", "原油", "石油", "不動產", "REIT", "債"):
            cat, reasons = candidates.classify(rec("00900", f"某{word}ETF", T_OS))
            self.assertIsNone(cat)
            self.assertEqual(reasons, [f"名稱含 {word}"])
        # 英文不分大小寫
        self.assertEqual(candidates.classify(rec("00908", "富邦入息REITs+", T_OS))[1], ["名稱含 REIT"])
        self.assertEqual(candidates.classify(rec("00908", "入息reits", T_OS))[1], ["名稱含 REIT"])

    def test_several_reasons_are_all_listed(self):
        cat, reasons = candidates.classify(rec("00679B", "元大美債20年", T_OS))
        self.assertEqual((cat, reasons), (None, ["尾碼 B", "名稱含 債"]))

    def test_unknown_type_is_excluded_with_reason(self):
        cat, reasons = candidates.classify(rec("00900", "某ETF", "其他基金"))
        self.assertIsNone(cat)
        self.assertIn("其他基金", reasons[0])

    def test_parse_listing_splits_and_sorts(self):
        ok, excluded = candidates.parse_listing([rec("00900", "A", T_TW, "1110101"), rec("00631L", "B", T_LEV),
                                                 rec("00679B", "C", T_BOND), rec("", "空代號", T_TW)])
        self.assertEqual(ok, {"00900": {"name": "A", "category": "tw", "listed": "2022-01-01"}})
        self.assertEqual([e["code"] for e in excluded], ["00631L", "00679B"])
        self.assertIn("尾碼 L", excluded[0]["reason"])

    def test_name_is_html_unescaped(self):
        ok, _ = candidates.parse_listing([rec("00646", "元大S&amp;P500", T_OS, "1041214")])
        self.assertEqual(ok["00646"]["name"], "元大S&P500")  # 和 STOCK_DAY_ALL 的名稱一致

    def test_unparsable_listing_date_is_excluded(self):
        ok, excluded = candidates.parse_listing([rec("00900", "A", T_TW, "abc")])
        self.assertEqual(ok, {})
        self.assertIn("上市日期無法解析", excluded[0]["reason"])


class DateTest(unittest.TestCase):
    def test_roc_listing_date(self):
        self.assertEqual(common.roc_ymd("1150409"), "2026-04-09")
        self.assertEqual(common.roc_ymd("0920630"), "2003-06-30")  # 0050，民國 92 年

    def test_listed_for_a_year(self):
        # 排名日 2026-11-12：上市日在 2025-11-12（含）以前才算滿一年
        self.assertTrue(candidates.listed_enough({"listed": "2025-11-12"}, "2026-11-12"))
        self.assertTrue(candidates.listed_enough({"listed": "2003-06-30"}, "2026-11-12"))
        self.assertFalse(candidates.listed_enough({"listed": "2025-11-13"}, "2026-11-12"))


def turnover_for(codes_values, days):
    return {(d, c): v for c, v in codes_values.items() for d in days}


class RankTest(unittest.TestCase):
    def ok_for(self, n, listed="2020-01-01"):
        return {f"00{900 + i}": {"name": f"ETF{i}", "category": "tw" if i % 2 else "overseas", "listed": listed}
                for i in range(n)}

    def test_top_30_sorted_by_average_value(self):
        ok = self.ok_for(35)
        days = weekdays("2026-08-03", "2026-11-11")  # 追溯 70 個以上平日
        self.assertGreater(len(days), 70)
        turnover = turnover_for({c: (i + 1) * 1_000_000 for i, c in enumerate(sorted(ok))}, days)
        u = candidates.rank_universe(ok, [], turnover, "2026-11-11", "2026-11-12 06:15")
        self.assertEqual(len(u["items"]), 30)
        self.assertEqual(u["window_days"], 60)
        self.assertEqual([it["rank"] for it in u["items"]], list(range(1, 31)))
        self.assertEqual(u["items"][0], {"code": "00934", "name": "ETF34", "category": "overseas",
                                         "listed": "2020-01-01", "avg_value": 35_000_000, "rank": 1})
        values = [it["avg_value"] for it in u["items"]]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertEqual(u["items"][-1]["code"], "00905")  # 第 31 名以後落選，而且不算被排除
        self.assertEqual(u["excluded"], [])
        self.assertEqual((u["ranked_on"], u["generated_at_taipei"]), ("2026-11-11", "2026-11-12 06:15"))
        self.assertEqual(u["rule"], "上市 ETF、台股與海外股票型、上市滿一年、日均成交金額前 30")

    def test_window_uses_actual_days_when_short(self):
        ok = self.ok_for(3)
        turnover = turnover_for({"00900": 3, "00901": 2, "00902": 1}, weekdays("2026-11-02", "2026-11-11"))  # 8 天
        u = candidates.rank_universe(ok, [], turnover, "2026-11-11", "t")
        self.assertEqual(u["window_days"], 8)
        self.assertEqual([it["code"] for it in u["items"]], ["00900", "00901", "00902"])

    def test_average_is_over_the_days_each_etf_has(self):
        ok = self.ok_for(2)
        days = weekdays("2026-11-02", "2026-11-11")
        turnover = turnover_for({"00900": 100}, days)
        turnover.update({(d, "00901"): 90 for d in days[-5:]})  # 只有最近 5 天有資料（剛好夠）
        u = candidates.rank_universe(ok, [], turnover, "2026-11-11", "t")
        self.assertEqual([(it["code"], it["avg_value"]) for it in u["items"]], [("00900", 100), ("00901", 90)])

    def test_too_young_and_too_little_data_are_excluded_with_reason(self):
        ok = self.ok_for(3)
        ok["00901"]["listed"] = "2026-06-01"  # 未滿一年
        days = weekdays("2026-11-02", "2026-11-11")
        turnover = turnover_for({"00900": 5, "00901": 9, "00902": 7}, days)
        turnover = {k: v for k, v in turnover.items() if not (k[1] == "00902" and k[0] < "2026-11-10")}  # 00902 只剩 2 天
        u = candidates.rank_universe(ok, [{"code": "00631L", "name": "x", "reason": "尾碼 L"}], turnover,
                                     "2026-11-11", "t")
        self.assertEqual([it["code"] for it in u["items"]], ["00900"])
        reasons = {e["code"]: e["reason"] for e in u["excluded"]}
        self.assertEqual(sorted(reasons), ["00631L", "00901", "00902"])
        self.assertIn("上市未滿一年", reasons["00901"])
        self.assertIn("只有 2 天", reasons["00902"])

    def test_ties_break_by_code(self):
        ok = self.ok_for(3)
        turnover = turnover_for({"00900": 5, "00901": 5, "00902": 5}, weekdays("2026-11-02", "2026-11-11"))
        u = candidates.rank_universe(ok, [], turnover, "2026-11-11", "t")
        self.assertEqual([it["code"] for it in u["items"]], ["00900", "00901", "00902"])

    def test_need_rank_once_a_month(self):
        self.assertTrue(candidates.need_rank(None, "2026-11-11"))
        self.assertTrue(candidates.need_rank({"ranked_on": "2026-10-30"}, "2026-11-02"))
        self.assertFalse(candidates.need_rank({"ranked_on": "2026-11-02"}, "2026-11-30"))
        self.assertTrue(candidates.need_rank({"ranked_on": "2026-11-30"}, "2027-01-04"))  # 跨年
        self.assertTrue(candidates.need_rank({}, "2026-11-11"))


class TurnoverTest(unittest.TestCase):
    def test_keeps_only_last_90_trading_days(self):
        days = weekdays("2026-05-01", "2026-11-11")
        self.assertGreater(len(days), 100)
        turnover = turnover_for({"00900": 1, "00901": 2}, days)
        kept = candidates.trim_turnover(turnover)
        self.assertEqual(sorted({d for d, _ in kept}), days[-90:])
        self.assertEqual(len(kept), 180)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "candidates", "turnover.csv")
            candidates.save_turnover(path, turnover)
            rows = common.read_rows(path)
            self.assertEqual(len(rows), 180)
            self.assertEqual(rows[0], [days[-90], "00900", "1"])  # 無表頭：date,code,value
            self.assertEqual(candidates.load_turnover(path), kept)

    def test_add_day_all_keeps_only_wanted_codes(self):
        rows = [{"Date": "1151111", "Code": "0050", "TradeValue": "12595528482"},
                {"Date": "1151111", "Code": "00631L", "TradeValue": "5"},      # 不在 codes
                {"Date": "1151111", "Code": "00900", "TradeValue": ""},        # 沒成交
                {"Date": "1151112", "Code": "0056", "TradeValue": "7"}]       # 晚於 cut
        turnover = {("2026-11-11", "0050"): 1}
        n = candidates.add_day_all(turnover, rows, {"0050", "00900", "0056"}, "2026-11-11")
        self.assertEqual(n, 1)
        self.assertEqual(turnover, {("2026-11-11", "0050"): 12595528482})  # 同一天同一檔以新的為準

    def test_thin(self):
        days = weekdays("2026-08-03", "2026-11-11")
        full = turnover_for({"a": 1}, days)
        self.assertFalse(candidates.thin(full, "a", days))
        self.assertTrue(candidates.thin(full, "b", days))
        few = {(d, "b"): 1 for d in days[-20:]}  # 60 天窗口只有 20 天（不到一半）
        self.assertTrue(candidates.thin(few, "b", days))
        ok_half = {(d, "b"): 1 for d in days[-35:]}
        self.assertFalse(candidates.thin(ok_half, "b", days))
        self.assertTrue(candidates.thin({("2026-11-11", "a"): 1}, "a", ["2026-11-11"]))  # 第一天：不到 5 天


class ClosesTest(unittest.TestCase):
    def test_header_blank_cells_and_row_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            days = weekdays("2025-06-02", "2026-11-11")
            self.assertGreater(len(days), 300)
            a = [(d, 1, 10, 11, 9, 10.0 + i / 100) for i, d in enumerate(days)]
            b = [r for r in a if r[0] != "2026-11-10"]  # b 少一天
            b = b[-5:]
            common.write_csv(os.path.join(tmp, "A_norm.csv"), a)
            common.write_csv(os.path.join(tmp, "B_norm.csv"), b)
            header, rows = candidates.build_closes(["B", "A", "C"], lambda c: os.path.join(tmp, f"{c}_norm.csv"))
        self.assertEqual(header, ["date", "B", "A", "C"])  # 照傳入順序（名次）
        self.assertEqual(len(rows), 300)
        self.assertEqual(rows[-1][0], "2026-11-11")
        self.assertEqual(rows[0][1], "")  # B 那天沒有資料留空
        self.assertEqual(rows[0][3], "")  # C 沒有檔案，整欄留空
        day10 = next(r for r in rows if r[0] == "2026-11-10")
        self.assertEqual(day10[1], "")
        self.assertNotEqual(day10[2], "")


class GateTest(unittest.TestCase):
    def test_gap_between_requests_and_per_run_limit(self):
        t, waits = [0.0], []

        def sleep(s):
            waits.append(s)
            t[0] += s

        gate = candidates.FinMindGate(gap=1.5, limit=3, sleep=sleep, clock=lambda: t[0])
        calls = []
        with mock.patch.object(candidates, "finmind", lambda *a: calls.append(a) or []):
            for _ in range(3):
                gate("TaiwanStockPrice", "0050", "2026-01-01")
            with self.assertRaises(RuntimeError):
                gate("TaiwanStockPrice", "0050", "2026-01-01")
        self.assertEqual(waits, [1.5, 1.5])  # 第一次不用等，之後每次至少隔 1.5 秒
        self.assertEqual(len(calls), 3)
        self.assertIn("上限 3", gate.stopped)

    def test_quota_error_stops_everything_without_retry(self):
        calls = []

        def quota(*a):
            calls.append(a)
            raise RuntimeError("FinMind TaiwanStockPrice 0050 status 402: Requests reach the upper limit")

        gate = candidates.FinMindGate(gap=0, limit=250)
        with mock.patch.object(candidates, "finmind", quota):
            with self.assertRaises(RuntimeError):
                gate("TaiwanStockPrice", "0050", "2026-01-01")
            with self.assertRaises(RuntimeError):
                gate("TaiwanStockPrice", "0056", "2026-01-01")
        self.assertEqual(len(calls), 1)
        self.assertIn("額度用完", gate.stopped)

    def test_three_failures_in_a_row_stop_but_success_resets(self):
        results = iter(["boom", "boom", "ok", "boom", "boom", "boom", "never"])

        def flaky(*a):
            r = next(results)
            if r == "boom":
                raise RuntimeError("URLError: timed out")
            return []

        gate = candidates.FinMindGate(gap=0, limit=250)
        outcome = []
        with mock.patch.object(candidates, "finmind", flaky):
            for _ in range(7):
                try:
                    gate("TaiwanStockPrice", "0050", "2026-01-01")
                    outcome.append("ok")
                except RuntimeError:
                    outcome.append("err")
        self.assertEqual(outcome, ["err", "err", "ok", "err", "err", "err", "err"])
        self.assertIn("連續 3 次失敗", gate.stopped)
        self.assertEqual(next(results), "never")  # 第 7 次沒有再打 FinMind


# ---------------------------------------------------------------- 整合：假的證交所 OpenAPI 與 FinMind
LISTING = [
    rec("0050", "元大台灣50", T_TW, "0920630"),
    rec("0056", "元大高股息", T_TW, "0960126"),
    rec("00900", "富邦特選高股息30", T_TW, "1110101"),
    rec("00901", "永豐台灣加權", T_TW, "1110102"),
    rec("00902", "國泰全球AI", T_OS, "1110103"),
    rec("00981A", "主動統一台股增長", T_ATW, "1140501"),   # 2025-05-01：滿一年
    rec("00999", "新上市台股", T_TW, "1150601"),             # 2026-06-01：未滿一年
    rec("00903", "已終止交易的舊ETF", T_TW, "1000101"),      # 基金清單還留著、但 STOCK_DAY_ALL 已經沒有
    rec("00631L", "元大台灣50正2", T_LEV, "1080101"),
    rec("00679B", "元大美債20年", T_OS, "1060101"),
    rec("009832", "兆豐美國黃金", T_OS, "1140101"),
]
E8 = 100_000_000
VALUES = {"0050": 100 * E8, "0056": 50 * E8, "00900": 40 * E8, "00901": 30 * E8, "00902": 20 * E8,
          "00981A": 10 * E8, "00999": 999 * E8, "00631L": 5 * E8, "00679B": 5 * E8, "009832": 5 * E8}
CFG = {"tw": {"0050": {"market": "TWSE"}}, "tw_watch": {"0056": {"market": "TWSE"}}}


def day_all(roc, values, close="10.50"):
    return [{"Date": roc, "Code": c, "Name": c, "TradeVolume": "1000", "TradeValue": str(v), "OpeningPrice": "10.00",
             "HighestPrice": "11.00", "LowestPrice": "9.00", "ClosingPrice": close} for c, v in values.items()]


class FakeFinMind:
    def __init__(self, values, end="2026-12-04"):
        self.values, self.end, self.calls = values, end, []
        self.fail_by_code, self.fail_nth, self.fail_all = {}, {}, None

    def __call__(self, dataset, code, start):
        self.calls.append(code)
        msg = self.fail_all or self.fail_by_code.get(code) or self.fail_nth.get(len(self.calls))
        if msg:
            raise RuntimeError(msg)
        return [{"date": d, "stock_id": code, "Trading_Volume": 1000, "Trading_money": self.values.get(code, 1),
                 "open": 10.0, "max": 11.0, "min": 9.0, "close": 10.0} for d in weekdays(start, self.end)]


QUOTA_MSG = "FinMind TaiwanStockPrice 00900 status 402: Requests reach the upper limit"


class CandidatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = self.tmp.name
        for code, close in (("0050", 100.0), ("0056", 50.0)):  # fetch_tw、fetch_watch 已經寫好的共用日線
            common.write_csv(os.path.join(self.tmp_path, "tw", f"{code}_norm.csv"),
                             [(d, 10, close, close, close, close) for d in ("2026-11-09", "2026-11-10", "2026-11-11")])
        self.fm = FakeFinMind(VALUES)

    def p(self, *parts):
        return os.path.join(self.tmp_path, "candidates", *parts)

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def run_step(self, taipei, roc, values=None, cfg=None, prev=None, listing=None, list_error=False, day_error=False,
                 last_day_all=None, close="10.50"):
        """taipei：台北時間 'YYYY-MM-DD HH:MM'；cut＝台北 14:30 前算前一天。回傳 (meta, 這次打了哪些 OpenAPI)。"""
        now = dt.datetime.fromisoformat(taipei).replace(tzinfo=common.TPE).astimezone(UTC)
        cut = (now.astimezone(common.TPE) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        meta, oa_calls = {"datasets": {}, "errors": []}, []

        def oa(path):
            oa_calls.append(path)
            if path == candidates.LIST_PATH:
                if list_error:
                    raise RuntimeError("HTTP 503")
                return listing or LISTING
            if day_error:
                raise RuntimeError("HTTP 503")
            return day_all(roc, values or VALUES, close)

        with mock.patch.multiple(candidates, DATA=self.tmp_path, NOW=now, finmind=self.fm, twse_openapi=oa,
                                 tw_cutoff=lambda: cut, FINMIND_GAP=0, TOP_N=3), \
                mock.patch.object(common, "_PREV_META", prev if prev is not None else {}), \
                mock.patch.object(twwatch, "LAST_DAY_ALL", last_day_all):
            candidates.fetch_candidates(CFG if cfg is None else cfg, meta)
        return meta, oa_calls

    # ---- 第一次：沒有 turnover.csv，每檔向 FinMind 補一次
    def test_first_run_builds_everything(self):
        meta, oa_calls = self.run_step("2026-11-12 08:30", "1151111")
        # 上市滿一年的 6 檔各補一次成交金額；排名前三裡 00900 是唯一不在 tw／tw_watch 的，日線沿用同一次回應
        self.assertEqual(self.fm.calls, ["0050", "0056", "00900", "00901", "00902", "00981A"])
        self.assertEqual(oa_calls, [candidates.LIST_PATH, candidates.DAY_ALL_PATH])

        u = json.loads(self.read(self.p("universe.json")))
        self.assertEqual([(it["rank"], it["code"], it["category"]) for it in u["items"]],
                         [(1, "0050", "tw"), (2, "0056", "tw"), (3, "00900", "tw")])
        self.assertEqual(u["items"][0], {"code": "0050", "name": "元大台灣50", "category": "tw", "listed": "2003-06-30",
                                         "avg_value": 100 * E8, "rank": 1})
        self.assertEqual((u["ranked_on"], u["window_days"], u["generated_at_taipei"]), ("2026-11-11", 60, "2026-11-12 08:30"))
        reasons = {e["code"]: e["reason"] for e in u["excluded"]}
        self.assertEqual(reasons["00631L"], "尾碼 L、基金類型含 槓桿、基金類型含 反向")
        self.assertEqual(reasons["00679B"], "尾碼 B、名稱含 債")
        self.assertEqual(reasons["009832"], "名稱含 黃金")
        self.assertIn("上市未滿一年", reasons["00999"])  # 新上市的成交金額再大也不排名
        self.assertNotIn("00901", reasons)               # 第 4 名以後只是落選，不算被排除
        self.assertIn("只有 0 天", reasons["00903"])      # 不在 STOCK_DAY_ALL：不向 FinMind 要（上面的 calls 沒有它），也不排名

        turnover = candidates.load_turnover(self.p("turnover.csv"))
        self.assertEqual({c for _, c in turnover}, {"0050", "0056", "00900", "00901", "00902", "00981A", "00999"})
        self.assertEqual(len({d for d, _ in turnover}), 60)

        # 共用的代號不重抓，沿用 data/tw/；只有 00900 有自己的檔
        self.assertFalse(os.path.exists(self.p("0050_norm.csv")))
        self.assertFalse(os.path.exists(self.p("0056_norm.csv")))
        rows = common.read_rows(self.p("00900_norm.csv"))
        self.assertGreaterEqual(len(rows), 300)
        self.assertEqual(rows[-1], ["2026-11-11", "1000", "10.0", "11.0", "9.0", "10.5"])  # 最新一天以 OpenAPI 為準

        closes = common.read_rows(self.p("closes.csv"))
        self.assertEqual(closes[0], ["date", "0050", "0056", "00900"])
        self.assertEqual(len(closes) - 1, 300)
        self.assertEqual(closes[-1], ["2026-11-11", "100.0", "50.0", "10.5"])
        self.assertEqual(closes[1][1:3], ["", ""])  # 0050、0056 在前面的日子沒有資料

        ds = meta["datasets"]
        self.assertEqual(ds["candidates/universe"], {"status": "ok", "source": candidates.SOURCE, "last": "2026-11-11",
                                                     "items": 3, "window_days": 60})
        c = ds["candidates/closes"]
        self.assertEqual((c["status"], c["rows"], c["last"]), ("ok", 300, "2026-11-11"))
        self.assertEqual(c["history_fetched"], {"00900": "2026-11-12"})
        self.assertEqual(meta["errors"], [])

    def test_reuses_the_day_all_response_that_twwatch_fetched(self):
        shared = day_all("1151111", VALUES)
        _, oa_calls = self.run_step("2026-11-12 08:30", "1151111", last_day_all=shared)
        self.assertEqual(oa_calls, [candidates.LIST_PATH])  # STOCK_DAY_ALL 沒有再打
        self.assertTrue(os.path.exists(self.p("universe.json")))

    def test_day_all_failed_earlier_is_not_fetched_again(self):
        meta, oa_calls = self.run_step("2026-11-12 08:30", "1151111", last_day_all=[])
        self.assertEqual(oa_calls, [candidates.LIST_PATH])
        # 沒有今天的成交金額，但 FinMind 補到的還是夠排名
        self.assertEqual(meta["datasets"]["candidates/universe"]["status"], "ok")
        self.assertTrue(any("已經失敗" in e for e in meta["errors"]))

    # ---- 中途失敗：已補到的先寫檔，下次只補缺的
    def test_quota_stops_seeding_keeps_progress_and_next_run_only_fills_gaps(self):
        self.fm.fail_nth = {3: QUOTA_MSG}
        meta, _ = self.run_step("2026-11-12 08:30", "1151111")
        self.assertEqual(self.fm.calls, ["0050", "0056", "00900"])  # 額度用完就停，不再試後面的
        turnover = candidates.load_turnover(self.p("turnover.csv"))
        have = lambda code: sum(1 for _, c in turnover if c == code)  # noqa: E731
        self.assertEqual((have("0050"), have("0056"), have("00900"), have("00901")), (60, 60, 1, 1))
        # 還沒補完就不排名，也不寫名單
        self.assertFalse(os.path.exists(self.p("universe.json")))
        self.assertEqual(meta["datasets"]["candidates/universe"]["status"], "fail")
        self.assertEqual(meta["datasets"]["candidates/closes"]["status"], "fail")
        self.assertTrue(any("額度用完" in e for e in meta["errors"]))

        self.fm.fail_nth, self.fm.calls = {}, []
        meta, _ = self.run_step("2026-11-12 09:30", "1151111", prev=meta["datasets"])
        self.assertEqual(self.fm.calls, ["00900", "00901", "00902", "00981A"])  # 0050、0056 已經有了
        self.assertEqual(meta["datasets"]["candidates/universe"]["status"], "ok")
        u = json.loads(self.read(self.p("universe.json")))
        self.assertEqual([it["code"] for it in u["items"]], ["0050", "0056", "00900"])
        self.assertEqual(meta["datasets"]["candidates/closes"]["status"], "ok")

    def test_not_enough_trading_days_yet(self):
        # FinMind 全掛：只有今天一天的成交金額，不到 5 天，不排名
        self.fm.fail_all = "FinMind 掛了"
        meta, _ = self.run_step("2026-11-12 08:30", "1151111")
        self.assertEqual(meta["datasets"]["candidates/universe"]["status"], "fail")
        self.assertFalse(os.path.exists(self.p("universe.json")))

    # ---- 名單變動：新進補歷史、退出的檔案保留
    def test_monthly_rerank_adds_newcomers_and_keeps_retired_files(self):
        meta1, _ = self.run_step("2026-11-12 08:30", "1151111")
        old_00900 = self.read(self.p("00900_norm.csv"))
        old_universe = self.read(self.p("universe.json"))

        # 十二月：00900 掉出前三名、00901 與 00902 衝上來
        turnover = candidates.load_turnover(self.p("turnover.csv"))
        for (d, c) in list(turnover):
            turnover[(d, c)] = {"00900": 1, "00901": 70 * E8, "00902": 60 * E8}.get(c, turnover[(d, c)])
        candidates.save_turnover(self.p("turnover.csv"), turnover)
        values = {**VALUES, "00900": 1, "00901": 70 * E8, "00902": 60 * E8}
        self.fm.calls = []
        meta2, _ = self.run_step("2026-12-02 08:30", "1151201", values=values, prev=meta1["datasets"])

        self.assertEqual(self.fm.calls, ["00901", "00902"])  # 只有新進的補日線歷史；沒有任何一檔需要再補成交金額
        u = json.loads(self.read(self.p("universe.json")))
        self.assertEqual([it["code"] for it in u["items"]], ["0050", "00901", "00902"])
        self.assertEqual(u["ranked_on"], "2026-12-01")
        self.assertNotEqual(self.read(self.p("universe.json")), old_universe)
        self.assertGreaterEqual(len(common.read_rows(self.p("00901_norm.csv"))), 300)
        self.assertEqual(self.read(self.p("00900_norm.csv")), old_00900)  # 退出的不刪、不再更新
        self.assertEqual(common.read_rows(self.p("closes.csv"))[0], ["date", "0050", "00901", "00902"])
        self.assertEqual(meta2["datasets"]["candidates/universe"]["last"], "2026-12-01")

        # 同一個月其他日子沿用名單（檔案一個位元組都不動），歷史每天只向 FinMind 抓一次
        kept = self.read(self.p("universe.json"))
        self.fm.calls = []
        meta3, _ = self.run_step("2026-12-03 08:30", "1151202", values=values, prev=meta2["datasets"])
        self.assertEqual(self.read(self.p("universe.json")), kept)
        self.assertEqual(self.fm.calls, ["00901", "00902"])  # 新的一天：各抓一次
        self.assertEqual(meta3["datasets"]["candidates/universe"]["last"], "2026-12-01")

        self.fm.calls = []
        meta4, _ = self.run_step("2026-12-03 13:00", "1151202", values=values, prev=meta3["datasets"], close="11.11")
        self.assertEqual(self.fm.calls, [])  # 同一天後面的執行：不碰 FinMind
        self.assertEqual(common.read_rows(self.p("00901_norm.csv"))[-1][0], "2026-12-02")
        self.assertEqual(common.read_rows(self.p("00901_norm.csv"))[-1][5], "11.11")  # 只用 OpenAPI 補最新一天
        self.assertEqual(common.read_rows(self.p("closes.csv"))[-1][2], "11.11")
        self.assertEqual(meta4["datasets"]["candidates/closes"]["history_fetched"],
                         {"00901": "2026-12-03", "00902": "2026-12-03"})

    # ---- 抓失敗：保留舊檔、標狀態、不影響其他
    def test_listing_fails_on_rerank_day_keeps_old_universe(self):
        meta1, _ = self.run_step("2026-11-12 08:30", "1151111")
        old = self.read(self.p("universe.json"))
        meta2, _ = self.run_step("2026-12-02 08:30", "1151201", prev=meta1["datasets"], list_error=True)
        u = meta2["datasets"]["candidates/universe"]
        self.assertEqual((u["status"], u["last"]), ("stale", "2026-11-11"))
        self.assertEqual(self.read(self.p("universe.json")), old)
        self.assertEqual(meta2["datasets"]["candidates/closes"]["status"], "ok")  # 舊名單照樣更新日線與合併檔
        self.assertEqual(common.read_rows(self.p("closes.csv"))[-1][0], "2026-12-01")
        self.assertTrue(meta2["errors"])

    def test_no_universe_and_listing_down_marks_fail(self):
        meta, _ = self.run_step("2026-11-12 08:30", "1151111", list_error=True)
        self.assertEqual(meta["datasets"]["candidates/universe"]["status"], "fail")
        self.assertEqual(meta["datasets"]["candidates/closes"]["status"], "fail")
        self.assertFalse(os.path.exists(self.p("universe.json")))
        self.assertEqual(self.fm.calls, [])

    def test_finmind_failure_for_one_etf_keeps_its_file_and_still_adds_latest_day(self):
        meta1, _ = self.run_step("2026-11-12 08:30", "1151111")
        before = common.read_rows(self.p("00900_norm.csv"))
        self.fm.fail_by_code = {"00900": "FinMind 掛了"}
        meta2, _ = self.run_step("2026-11-13 08:30", "1151112", prev=meta1["datasets"])
        rows = common.read_rows(self.p("00900_norm.csv"))
        self.assertEqual(rows[:len(before) - 1], before[1:])  # 舊的歷史都還在（日線起點隨日期往後移一天，最舊那列被截掉）
        self.assertEqual(rows[-1][0], "2026-11-12")   # 最新一天由 OpenAPI 補上
        c = meta2["datasets"]["candidates/closes"]
        self.assertEqual(c["status"], "ok")
        self.assertNotIn("00900", c["history_fetched"])  # 沒抓成功就不記，下次再抓
        self.assertTrue(any("00900" in w and "FinMind" in w for w in c["warnings"]))

    def test_closes_failure_keeps_old_file_and_marks_stale(self):
        cfg = {}  # 三檔都是自己的日線，這樣把檔案拿掉就沒有任何收盤資料
        meta1, _ = self.run_step("2026-11-12 08:30", "1151111", cfg=cfg)
        old = self.read(self.p("closes.csv"))
        for code in ("0050", "0056", "00900"):
            os.remove(self.p(f"{code}_norm.csv"))
        self.fm.fail_all = "FinMind 掛了"
        meta2, _ = self.run_step("2026-11-13 08:30", "1151112", cfg=cfg, prev=meta1["datasets"])
        c = meta2["datasets"]["candidates/closes"]
        self.assertEqual((c["status"], c["last"]), ("stale", "2026-11-11"))
        self.assertEqual(self.read(self.p("closes.csv")), old)
        self.assertEqual(meta2["datasets"]["candidates/universe"]["status"], "ok")  # 名單不受影響


class FreshTodayTest(unittest.TestCase):
    """新資料集不能改變 fetch.fresh_today 的判斷（只看 us/、tw/0 開頭、macro/ 24 小時品項與單一來源標記）。"""

    def run_fresh(self, datasets):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "meta.json"), "w") as f:
                json.dump({"datasets": datasets}, f)
            with mock.patch.object(fetch, "DATA", tmp), mock.patch.object(fetch, "expected_last",
                                                                         return_value="2026-11-11"):
                return fetch.fresh_today()

    def test_candidates_datasets_do_not_change_the_answer(self):
        base = {"us/VOO": {"status": "ok", "last": "2026-11-11"}, "tw/00981A": {"status": "ok", "last": "2026-11-11"}}
        stale = {"candidates/universe": {"status": "stale", "last": "2026-10-01"},
                 "candidates/closes": {"status": "fail"}}
        ok = {"candidates/universe": {"status": "ok", "last": "2026-11-02"},
              "candidates/closes": {"status": "ok", "last": "2026-11-11", "history_fetched": {"00900": "2026-11-12"}}}
        self.assertTrue(self.run_fresh(base))
        self.assertTrue(self.run_fresh({**base, **stale}))
        self.assertTrue(self.run_fresh({**base, **ok}))
        self.assertFalse(self.run_fresh({**base, "tw/00981A": {"status": "ok", "last": "2026-11-10"}, **ok}))

    def test_candidates_are_a_phase1_step(self):
        self.assertIn(candidates.fetch_candidates, fetch.PHASE1_STEPS)
        steps = list(fetch.PHASE1_STEPS)
        self.assertGreater(steps.index(candidates.fetch_candidates), steps.index(twwatch.fetch_watch))


if __name__ == "__main__":
    unittest.main()

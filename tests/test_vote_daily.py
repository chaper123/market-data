"""最後一根 K 棒多數決（common.vote_daily）與 fresh_today 的測試，全部用假資料、不連網。

執行：python -m unittest discover -s tests（在 repo 根目錄）
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common  # noqa: E402
import fetch  # noqa: E402


def bars(closes, start=1, month=10):
    """連續日期的假 K 棒：2026-10-01 起，每天一根（測試不在意週末）。"""
    return [(f"2026-{month:02d}-{i:02d}", 100, c, c, c, c) for i, c in enumerate(closes, start)]


HIST = [10.0] * 20  # 10/01～10/20


def no_referee(sym, start, end):
    return {}  # Nasdaq 還沒有這一天


def referee_with(day, close):
    return lambda sym, start, end: {day: (day, 999, close, close, close, close)}


def broken_referee(sym, start, end):
    raise RuntimeError("連不到 nasdaq")


class VoteDailyTest(unittest.TestCase):
    def test_two_sources_agree(self):
        rows, m = common.vote_daily("ARKX", [("moneydj", bars(HIST + [30.53])), ("yahoo", bars(HIST + [30.53]))],
                                    referee=broken_referee)
        self.assertEqual(rows[-1][5], 30.53)
        self.assertEqual(m["last_bar_source"], "moneydj")
        self.assertFalse(m["last_bar_single_source"])
        self.assertNotIn("single_source_days", m)

    def test_single_source_moneydj_is_taken_and_flagged(self):
        # 10/20 早上的情況：只有 MoneyDJ 有 10/21，Yahoo 還沒有，Nasdaq 也沒有
        rows, m = common.vote_daily("VOO", [("moneydj", bars(HIST + [704.6])), ("yahoo", bars(HIST))],
                                    referee=no_referee)
        self.assertEqual(rows[-1][0], "2026-10-21")
        self.assertEqual(rows[-1][5], 704.6)
        self.assertTrue(m["last_bar_single_source"])
        self.assertEqual(m["last_bar_source"], "moneydj")
        self.assertEqual(m["single_source_days"], ["2026-10-21"])
        self.assertNotIn("last_bar_unresolved", m)
        meta = fetch.daily_meta(rows, dict(m))
        self.assertIsNone(meta["cross_check_ok"])  # 不是核對失敗，也不是已核對一致
        self.assertIn("單一來源", meta["cross_check"])

    def test_single_source_also_when_referee_unreachable(self):
        rows, m = common.vote_daily("VOO", [("moneydj", bars(HIST + [704.6])), ("yahoo", bars(HIST))],
                                    referee=broken_referee)
        self.assertTrue(m["last_bar_single_source"])
        self.assertIn("referee_error", m)

    def test_recheck_same_day_agree_clears_flag(self):
        # 下一次排程：Yahoo 有了，而且一致
        rows, m = common.vote_daily("VOO", [("moneydj", bars(HIST + [704.6])), ("yahoo", bars(HIST + [704.6]))],
                                    referee=broken_referee, recheck=["2026-10-21"])
        self.assertFalse(m["last_bar_single_source"])
        self.assertNotIn("single_source_days", m)
        self.assertEqual(fetch.daily_meta(rows, dict(m))["cross_check_ok"], True)

    def test_recheck_same_day_disagree_referee_corrects(self):
        rows, m = common.vote_daily("ARKX", [("moneydj", bars(HIST + [30.2])), ("yahoo", bars(HIST + [30.53]))],
                                    referee=referee_with("2026-10-21", 30.53), recheck=["2026-10-21"])
        self.assertEqual(rows[-1][5], 30.53)
        self.assertEqual(m["last_bar_source"], "nasdaq")
        self.assertNotIn("single_source_days", m)

    def test_recheck_same_day_disagree_no_referee_drops_bar(self):
        rows, m = common.vote_daily("ARKX", [("moneydj", bars(HIST + [30.2])), ("yahoo", bars(HIST + [30.53]))],
                                    referee=no_referee, recheck=["2026-10-21"])
        self.assertEqual(rows[-1][0], "2026-10-20")  # 維持原本行為：無法判定的最後一根先不收
        self.assertEqual(m["last_bar_unresolved"], "2026-10-21")
        self.assertIs(fetch.daily_meta(rows, dict(m))["cross_check_ok"], False)

    def test_recheck_previous_day_corrected_after_new_day(self):
        # 10/21 先以 MoneyDJ 單一來源收進來（30.2），隔天才核對：Yahoo 與裁判都說 30.53
        md = bars(HIST + [30.2, 31.0])
        yh = bars(HIST + [30.53, 31.0])

        def ref(sym, start, end):
            return {"2026-10-21": ("2026-10-21", 999, 30.53, 30.6, 30.4, 30.53)}

        rows, m = common.vote_daily("ARKX", [("moneydj", md), ("yahoo", yh)], referee=ref, recheck=["2026-10-21"])
        by_day = {r[0]: r for r in rows}
        self.assertEqual(by_day["2026-10-21"][5], 30.53)
        self.assertEqual(rows[-1][0], "2026-10-22")
        self.assertNotIn("single_source_days", m)
        self.assertIn("已改正", m["rechecked"][0])
        self.assertEqual(m["history_mismatch"], 0)  # 已重新核對的日子不重複計入

    def test_recheck_previous_day_still_unverified_kept_and_pending(self):
        md = bars(HIST + [30.2, 31.0])
        yh = bars(HIST + [31.0], start=1)[:20] + [("2026-10-22", 100, 31.0, 31.0, 31.0, 31.0)]  # Yahoo 缺 10/21
        rows, m = common.vote_daily("ARKX", [("moneydj", md), ("yahoo", yh)], referee=no_referee,
                                    recheck=["2026-10-21"])
        by_day = {r[0]: r for r in rows}
        self.assertEqual(by_day["2026-10-21"][5], 30.2)  # 保留 MoneyDJ 的值
        self.assertEqual(m["single_source_days"], ["2026-10-21"])
        self.assertFalse(m["last_bar_single_source"])  # 最後一根 10/22 兩家一致

    def test_stop_tracking_after_recheck_days(self):
        rows, m = common.vote_daily("VOO", [("moneydj", bars(HIST + [1.0])), ("yahoo", bars(HIST + [1.0]))],
                                    referee=broken_referee, recheck=["2026-10-05"])
        self.assertNotIn("single_source_days", m)
        self.assertIn("停止追蹤", m["rechecked"][0])

    def test_only_yahoo_is_not_trusted_alone(self):
        rows, m = common.vote_daily("VOO", [("moneydj", bars(HIST)), ("yahoo", bars(HIST + [704.6]))],
                                    referee=no_referee)
        self.assertEqual(rows[-1][0], "2026-10-20")
        self.assertEqual(m["last_bar_unresolved"], "2026-10-21")

    def test_single_moneydj_but_referee_disagrees(self):
        rows, m = common.vote_daily("VOO", [("moneydj", bars(HIST + [704.6])), ("yahoo", bars(HIST))],
                                    referee=referee_with("2026-10-21", 690.0))
        self.assertEqual(m["last_bar_unresolved"], "2026-10-21")
        self.assertFalse(m["last_bar_single_source"])

    def test_single_moneydj_referee_confirms(self):
        rows, m = common.vote_daily("VOO", [("moneydj", bars(HIST + [704.6])), ("yahoo", bars(HIST))],
                                    referee=referee_with("2026-10-21", 704.6))
        self.assertEqual(m["last_bar_source"], "nasdaq")
        self.assertFalse(m["last_bar_single_source"])


class FreshTodayTest(unittest.TestCase):
    def run_fresh(self, datasets):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "meta.json"), "w") as f:
                json.dump({"datasets": datasets}, f)
            with mock.patch.object(fetch, "DATA", tmp), mock.patch.object(fetch, "expected_last",
                                                                         return_value="2026-10-19"):
                return fetch.fresh_today()

    def base(self):
        return {"us/VOO": {"status": "ok", "last": "2026-10-19"}, "tw/00981A": {"status": "ok", "last": "2026-10-19"}}

    def test_complete_is_fresh(self):
        self.assertTrue(self.run_fresh(self.base()))

    def test_single_source_last_bar_is_not_fresh(self):
        ds = self.base()
        ds["us/VOO"].update(last_bar_single_source=True, single_source_days=["2026-10-19"])
        self.assertFalse(self.run_fresh(ds))

    def test_single_source_in_macro_tsm_is_not_fresh(self):
        ds = self.base()
        ds["macro/TSM"] = {"status": "ok", "last": "2026-10-19", "last_bar_single_source": True}
        self.assertFalse(self.run_fresh(ds))


if __name__ == "__main__":
    unittest.main()

"""topics.py（話題統計）測試：重跑不重複計數、舊日凍結、rise 公式、上升詞，全部用假資料、不連網。

執行：python -m unittest discover -s tests（在 repo 根目錄）
"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import topics  # noqa: E402
from common import TPE  # noqa: E402

RULES = {"fed": ["fed"], "tariff": ["tariff"]}
NOW = dt.datetime(2026, 11, 3, 8, 20, tzinfo=TPE)


def item(i, when, title, tps, tier=2):
    return {"id": f"id{i}", "published_taipei": when, "published_utc": when, "title": title, "topics": tps,
            "tier": tier}


class TopicsTest(unittest.TestCase):
    def test_title_terms(self):
        terms = topics.title_terms("Nvidia beats estimates as Blackwell sales surge 台積電先進封裝")
        self.assertTrue({"nvidia", "blackwell", "台積", "積電", "台積電", "先進封裝"[:3]} <= terms)
        self.assertNotIn("as", terms)

    def test_rerun_does_not_double_count_and_old_days_frozen(self):
        items = [item(1, "2026-11-02 10:00", "Fed holds rates", ["fed"]),
                 item(2, "2026-11-03 07:00", "Tariff talks resume", ["tariff"]),
                 item(3, "2026-10-31 09:00", "Fed speaker", ["fed"])]  # 窗開始那天（10/31 08:20 起）只有一部分
        hist = {"days": {"2026-10-31": {"items": 9, "topics": {"fed": 9}, "terms": {}}}}
        once = topics.update_history(hist, items, NOW, 72)
        twice = topics.update_history(once, items, NOW, 72)
        self.assertEqual(once, twice)
        self.assertEqual(twice["days"]["2026-10-31"]["topics"], {"fed": 9})  # 凍結，不被重算
        self.assertEqual(twice["days"]["2026-11-02"]["topics"], {"fed": 1})

    def test_rise_and_counts(self):
        days = {}
        for k in range(30):  # 30 天每天 fed 1 則，最近 7 天每天 3 則
            d = (NOW.date() - dt.timedelta(days=k)).isoformat()
            days[d] = {"items": 3, "topics": {"fed": 3 if k < 7 else 1}, "terms": {}}
        items = [item(1, "2026-11-03 07:00", "Fed decision", ["fed"]), item(2, "2026-11-01 07:00", "Fed old", ["fed"])]
        out = topics.summarize({"days": days}, items, NOW, RULES, {"fed": "Fed／利率"})
        fed = out["topics"][0]
        self.assertEqual((fed["label"], fed["count_1d"], fed["count_7d"], fed["count_30d"]), ("Fed／利率", 1, 21, 44))
        self.assertAlmostEqual(fed["rise"], round((21 / 7) / (44 / 30), 2))
        self.assertEqual(fed["sample_ids"], ["id1", "id2"])
        self.assertEqual(out["days_available"], 30)
        self.assertEqual(out["rising_topic"], "fed")
        self.assertIsNone(out["topics"][1]["rise"])

    def test_rising_terms_need_history(self):
        items = [item(i, "2026-11-03 07:00", "Anthropic files for IPO", []) for i in range(4)]
        one_day = {"days": {"2026-11-03": {"items": 4, "topics": {}, "terms": {"anthropic": 4}}}}
        self.assertEqual(topics.summarize(one_day, items, NOW, RULES, {})["rising_terms"], [])
        two = {"days": dict(one_day["days"], **{"2026-11-02": {"items": 50, "topics": {}, "terms": {"files": 4}}})}
        rising = topics.summarize(two, items, NOW, RULES, {})["rising_terms"]
        self.assertEqual(rising[0]["term"], "anthropic")  # 前一天沒出現過，上升最多
        self.assertEqual(rising[0]["count_1d"], 4)
        self.assertEqual(len(rising[0]["sample_ids"]), 4)


if __name__ == "__main__":
    unittest.main()

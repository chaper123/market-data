"""news.py 的測試：假的 Google News RSS 與 Fed RSS，不連網。

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

import news  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 22, 0, 0, tzinfo=UTC)  # 台北 08:00

CFG = {
    "window_hours": 72, "query_when": "2d", "low_tier_per_topic": 2, "off_title_per_topic": 1,
    "en": [{"id": "NVDA", "q": "Nvidia", "symbols": ["NVDA"], "match": ["nvidia"]},
           {"id": "TARIFF", "q": "tariffs", "symbols": [], "match": ["tariff"]},
           {"id": "TSMC", "q": "TSMC", "symbols": ["TSM", "2330"], "match": ["tsmc"]}],
    "zh": [{"id": "2330", "q": "台積電", "symbols": ["2330", "TSM"], "match": ["台積電"]}],
    "site_filter": {"en": ["reuters.com", "cnbc.com"], "zh": ["cna.com.tw"]},
    "tiers": {"A": ["reuters.com", "cnbc.com", "cna.com.tw"], "B": ["finance.yahoo.com", "money.udn.com"]},
    "fed_feeds": {"press": "https://www.federalreserve.gov/feeds/press_all.xml"}, "fed_days": 7,
}


def rss(items):
    body = "".join(
        f"<item><title>{t + ' - ' + s if s else t}</title><link>https://news.google.com/rss/articles/{i}</link>"
        f"<pubDate>{p}</pubDate>{f'<source url=\"{u}\">{s}</source>' if s else ''}</item>"
        for i, (t, s, u, p) in enumerate(items))
    return f"<?xml version=\"1.0\"?><rss><channel><title>x</title>{body}</channel></rss>".encode()


NVDA_RSS = rss([
    ("Nvidia shares rise on new AI deal", "Reuters", "https://www.reuters.com", "Wed, 21 Oct 2026 22:00:00 GMT"),
    ("Nvidia shares rise on new AI deal", "Yahoo Finance", "https://finance.yahoo.com", "Wed, 21 Oct 2026 22:30:00 GMT"),
    ("Nvidia shares rise on a new AI deal, report", "MarketBeat", "https://www.marketbeat.com",
     "Wed, 21 Oct 2026 23:00:00 GMT"),
    ("Nvidia and TSMC expand chip partnership", "CNBC", "https://www.cnbc.com", "Wed, 21 Oct 2026 20:00:00 GMT"),
    ("Old Nvidia story from last week", "Reuters", "https://www.reuters.com", "Mon, 12 Oct 2026 10:00:00 GMT"),
    ("Barclays to cut 5,000 jobs", "Reuters", "https://www.reuters.com", "Wed, 21 Oct 2026 21:00:00 GMT"),
    ("Banks rally as yields fall", "Reuters", "https://www.reuters.com", "Wed, 21 Oct 2026 21:30:00 GMT"),
    ("Nvidia blog: new GPU", "NVIDIA Blog", "https://blogs.nvidia.com", "Wed, 21 Oct 2026 19:00:00 GMT"),
    ("Nvidia stock: buy or sell?", "MarketBeat", "https://www.marketbeat.com", "Wed, 21 Oct 2026 18:00:00 GMT"),
    ("Nvidia price target raised", "Benzinga", "https://www.benzinga.com", "Wed, 21 Oct 2026 17:00:00 GMT"),
])
TARIFF_RSS = rss([("Trump threatens new tariffs on chips", "Reuters", "https://www.reuters.com",
                   "Wed, 21 Oct 2026 12:00:00 GMT")])
ZH_RSS = rss([("台積電法說會釋出樂觀展望", "中央社 CNA", "https://www.cna.com.tw", "Wed, 21 Oct 2026 08:00:00 GMT"),
              ("台積電 法說會 釋出 樂觀展望", "經濟日報", "https://money.udn.com", "Wed, 21 Oct 2026 08:30:00 GMT")])
FED_RSS = rss([("Federal Reserve issues FOMC statement", "", "", "Wed, 21 Oct 2026 18:00:00 GMT"),
               ("Old Fed release", "", "", "Mon, 05 Oct 2026 18:00:00 GMT")])


def fake_http(url, **kw):
    if "federalreserve.gov" in url:
        return 200, FED_RSS
    if "hl=zh-TW" in url:
        return 200, ZH_RSS
    if "tariffs" in url:
        return 200, TARIFF_RSS
    if "TSMC" in url:
        raise RuntimeError("HTTP 503")
    return 200, NVDA_RSS


class NewsTest(unittest.TestCase):
    def run_main(self, previous=None):
        with tempfile.TemporaryDirectory() as tmp:
            out_json, out_md = os.path.join(tmp, "news", "headlines.json"), os.path.join(tmp, "news", "headlines.md")
            if previous:
                os.makedirs(os.path.dirname(out_json))
                json.dump(previous, open(out_json, "w", encoding="utf-8"))
            cfg_path = os.path.join(tmp, "config.json")
            json.dump({"news": CFG}, open(cfg_path, "w", encoding="utf-8"))
            with mock.patch.object(news, "http", fake_http), mock.patch.object(news, "NOW", NOW), \
                    mock.patch.object(news, "OUT_JSON", out_json), mock.patch.object(news, "OUT_MD", out_md), \
                    mock.patch.object(news, "ROOT", tmp), mock.patch.object(news.time, "sleep", lambda s: None):
                news.main()
            return json.load(open(out_json, encoding="utf-8")), open(out_md, encoding="utf-8").read()

    def test_tiers_dedup_window_and_tags(self):
        doc, md = self.run_main()
        titles = [it["title"] for it in doc["items"]]
        # 同標題合併成一則，保留等級最高的 Reuters，其他來源列在 also
        deal = [it for it in doc["items"] if it["title"].startswith("Nvidia shares rise")]
        self.assertEqual(len(deal), 1)
        self.assertEqual((deal[0]["source"], deal[0]["tier"]), ("Reuters", "A"))
        self.assertEqual(sorted(deal[0]["also"]), ["MarketBeat", "Yahoo Finance"])
        # 超過 72 小時的不收
        self.assertNotIn("Old Nvidia story from last week", titles)
        # 標題提到 TSMC → 也標上 TSM、2330
        partner = next(it for it in doc["items"] if it["title"].startswith("Nvidia and TSMC"))
        self.assertEqual(partner["symbols"], ["2330", "NVDA", "TSM"])
        self.assertEqual(partner["title_topics"], ["NVDA", "TSMC"])
        # tariffs（複數）也算命中 tariff
        tariff = next(it for it in doc["items"] if "tariffs" in it["title"])
        self.assertTrue(tariff["title_match"])
        # 時間換成台北
        self.assertEqual(deal[0]["published_taipei"], "2026-10-22 06:00")
        # 標題沒提到主題的（Barclays、Banks）每個主題只留 1 則
        self.assertEqual(sum(1 for it in doc["items"] if not it["title_match"]), 1)
        # C 等級每個主題最多 2 則
        self.assertLessEqual(sum(1 for it in doc["items"] if it["tier"] == "C" and "NVDA" in it["topics"]), 2)
        # 中文：中央社與經濟日報字詞幾乎相同 → 合併
        zh = [it for it in doc["items"] if it["lang"] == "zh"]
        self.assertEqual(len(zh), 1)
        self.assertEqual(zh[0]["tier"], "A")

    def test_fed_partial_status_and_md(self):
        doc, md = self.run_main()
        self.assertEqual([f["title"] for f in doc["fed"]], ["Federal Reserve issues FOMC statement"])
        self.assertEqual(doc["status"], "partial")  # TSMC 兩次查詢失敗
        self.assertEqual(doc["stats"]["queries_ok"], doc["stats"]["queries"] - 2)
        self.assertEqual(doc["sec_8k"]["status"], "未啟用")
        self.assertIn("## NVDA", md)
        self.assertIn("A｜Reuters｜Nvidia shares rise on new AI deal", md)
        self.assertNotIn("Barclays", md.split("## NVDA")[1].split("##")[0])

    def test_previous_items_kept_within_window(self):
        prev = {"items": [
            {"title": "Nvidia earlier headline kept", "source": "Reuters", "source_url": "https://www.reuters.com",
             "tier": "A", "lang": "en", "topics": ["NVDA"], "symbols": ["NVDA"], "also": [], "link": "x",
             "published_utc": "2026-10-20T03:00:00Z", "published_taipei": "2026-10-20 11:00"},
            {"title": "Nvidia too old", "source": "Reuters", "source_url": "https://www.reuters.com",
             "tier": "A", "lang": "en", "topics": ["NVDA"], "symbols": ["NVDA"], "also": [], "link": "x",
             "published_utc": "2026-10-18T03:00:00Z", "published_taipei": "2026-10-18 11:00"}]}
        doc, _ = self.run_main(previous=prev)
        titles = [it["title"] for it in doc["items"]]
        self.assertIn("Nvidia earlier headline kept", titles)
        self.assertNotIn("Nvidia too old", titles)

    def test_all_queries_fail_keeps_old_file(self):
        def down(url, **kw):
            raise RuntimeError("HTTP 503")
        with tempfile.TemporaryDirectory() as tmp:
            json.dump({"news": CFG}, open(os.path.join(tmp, "config.json"), "w", encoding="utf-8"))
            with mock.patch.object(news, "http", down), mock.patch.object(news, "ROOT", tmp), \
                    mock.patch.object(news, "OUT_JSON", os.path.join(tmp, "h.json")), \
                    mock.patch.object(news.time, "sleep", lambda s: None):
                with self.assertRaises(SystemExit):
                    news.main()
            self.assertFalse(os.path.exists(os.path.join(tmp, "h.json")))

    def test_tier_and_title_helpers(self):
        self.assertEqual(news.tier_of("https://ca.finance.yahoo.com", CFG["tiers"]), "B")
        self.assertEqual(news.tier_of("https://www.reuters.com", CFG["tiers"]), "A")
        self.assertEqual(news.tier_of("https://www.marketbeat.com", CFG["tiers"]), "C")
        self.assertEqual(news.clean_title("Fed holds rates - Reuters", "Reuters"), "Fed holds rates")
        self.assertTrue(news.term_hit("fed", "fed's powell signals pause"))
        self.assertFalse(news.term_hit("apple", "pineapple prices jump"))


if __name__ == "__main__":
    unittest.main()

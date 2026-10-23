"""news.py 的測試：假的 Google News RSS、Fed RSS、文章頁與 batchexecute，不連網。輸出格式依 NEWS_ROUTINE.md 第 3.1 節。

執行：python -m unittest discover -s tests（在 repo 根目錄）
"""
import datetime as dt
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.parse
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
    "tiers": {"1": ["prnewswire.com"], "2A": ["reuters.com", "cnbc.com", "cna.com.tw"],
              "2B": ["finance.yahoo.com", "money.udn.com"], "3": ["blogs.nvidia.com"]},
    "topic_rules": {"fed": ["fed", "fomc", "聯準會"], "tariff": ["tariff", "關稅"], "china": ["china", "中國"],
                    "earnings": ["earnings", "法說"]},
    "fed_feeds": {"press": "https://www.federalreserve.gov/feeds/press_all.xml"}, "fed_days": 7,
    "url_hours": 36, "url_max_per_run": 600, "url_budget_seconds": 60, "url_workers": 2,
}
GN_BASE = "https://news.google.com/rss/articles/"
NOSIG = {"Banksrallyasyieldsfall"}  # 文章頁拿不到簽章的
PAGES = []  # 讀過的文章頁（檢查沿用時不會再讀）


def gid(title):
    return re.sub(r"\W", "", title)[:40]


def rss(items, base=GN_BASE):
    body = "".join(
        f"<item><title>{t + ' - ' + s if s else t}</title><link>{base}{gid(t)}</link>"
        f"<pubDate>{p}</pubDate>{f'<source url=\"{u}\">{s}</source>' if s else ''}</item>"
        for t, s, u, p in items)
    return f"<?xml version=\"1.0\"?><rss><channel><title>x</title>{body}</channel></rss>".encode()


NVDA_RSS = rss([
    ("Nvidia shares rise on new AI deal", "Reuters", "https://www.reuters.com", "Wed, 21 Oct 2026 22:00:00 GMT"),
    ("Nvidia shares rise on new AI deal", "Yahoo Finance", "https://finance.yahoo.com", "Wed, 21 Oct 2026 22:30:00 GMT"),
    ("Nvidia shares rise on a new AI deal, report", "MarketBeat", "https://www.marketbeat.com",
     "Wed, 21 Oct 2026 23:00:00 GMT"),
    ("Nvidia and TSMC expand chip partnership in China", "CNBC", "https://www.cnbc.com", "Wed, 21 Oct 2026 20:00:00 GMT"),
    ("Old Nvidia story from last week", "Reuters", "https://www.reuters.com", "Mon, 12 Oct 2026 10:00:00 GMT"),
    ("Barclays to cut 5,000 jobs", "Reuters", "https://www.reuters.com", "Wed, 21 Oct 2026 21:00:00 GMT"),
    ("Banks rally as yields fall", "Reuters", "https://www.reuters.com", "Wed, 21 Oct 2026 21:30:00 GMT"),
    ("Nvidia blog: new GPU", "NVIDIA Blog", "https://blogs.nvidia.com", "Wed, 21 Oct 2026 19:00:00 GMT"),
    ("Nvidia announces quarterly earnings date", "PR Newswire", "https://www.prnewswire.com",
     "Wed, 21 Oct 2026 18:30:00 GMT"),
    ("Nvidia stock: buy or sell?", "MarketBeat", "https://www.marketbeat.com", "Wed, 21 Oct 2026 18:00:00 GMT"),
    ("Nvidia price target raised", "Benzinga", "https://www.benzinga.com", "Wed, 21 Oct 2026 17:00:00 GMT"),
    ("Nvidia options activity", "Benzinga", "https://www.benzinga.com", "Wed, 21 Oct 2026 16:00:00 GMT"),
])
TARIFF_RSS = rss([("Trump threatens new tariffs on chips", "Reuters", "https://www.reuters.com",
                   "Wed, 21 Oct 2026 12:00:00 GMT")])
ZH_RSS = rss([("台積電法說會釋出樂觀展望", "中央社 CNA", "https://www.cna.com.tw", "Wed, 21 Oct 2026 08:00:00 GMT"),
              ("台積電 法說會 釋出 樂觀展望", "經濟日報", "https://money.udn.com", "Wed, 21 Oct 2026 08:30:00 GMT"),
              ("台光電擴產 外資喊買", "經濟日報", "https://money.udn.com", "Wed, 21 Oct 2026 09:00:00 GMT")])
FED_RSS = rss([("Federal Reserve issues FOMC statement", "", "", "Wed, 21 Oct 2026 18:00:00 GMT"),
               ("Old Fed release", "", "", "Mon, 05 Oct 2026 18:00:00 GMT")],
              base="https://www.federalreserve.gov/newsevents/pressreleases/")


def fake_page(url, timeout=20):
    g = url.rsplit("/", 1)[-1]
    PAGES.append(g)
    if g in NOSIG:
        return b"<html>no signature</html>"
    return f'<html>... data-n-a-ts="1792700000" data-n-a-sg="SIG{g}" ...</html>'.encode()


def fake_batch(data):
    """模擬 batchexecute：每個請求回 garturlres 與原文網址，依請求的編號對應。"""
    reqs = json.loads(urllib.parse.unquote(data.decode()[len("f.req="):]))[0]
    rows = []
    for rpc, args, _, idx in reqs:
        g = json.loads(args)[2]
        rows.append(["wrb.fr", rpc, json.dumps(["garturlres", f"https://publisher.example/{g}", 1]),
                     None, None, None, idx])
    return 200, (")]}'\n\n123\n" + json.dumps(rows) + "\n").encode()


def fake_http(url, **kw):
    if "batchexecute" in url:
        return fake_batch(kw["data"])
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
    def run_main(self, previous=None, page=fake_page):
        PAGES.clear()
        with tempfile.TemporaryDirectory() as tmp:
            paths = {n: os.path.join(tmp, "news", f) for n, f in
                     (("OUT_JSON", "headlines.json"), ("OUT_FED", "fed.json"), ("OUT_MD", "headlines.md"))}
            meta_path = os.path.join(tmp, "meta.json")
            json.dump({"datasets": {"us/VOO": {"status": "ok"}}, "errors": []}, open(meta_path, "w"))
            if previous:
                os.makedirs(os.path.join(tmp, "news"))
                json.dump(previous, open(paths["OUT_JSON"], "w", encoding="utf-8"))
            os.makedirs(os.path.join(tmp, "holdings"))
            json.dump({"top10": [{"code": "2383", "name": "台光電"}, {"code": "2327", "name": "國巨*"},
                                 {"code": "NVDA US", "name": "NVIDIA CORP"}]},
                      open(os.path.join(tmp, "holdings", "00981A.json"), "w", encoding="utf-8"))
            json.dump({"news": CFG}, open(os.path.join(tmp, "config.json"), "w", encoding="utf-8"))
            with mock.patch.object(news, "http", fake_http), mock.patch.object(news, "NOW", NOW), \
                    mock.patch.object(news, "OUT_JSON", paths["OUT_JSON"]), \
                    mock.patch.object(news, "OUT_FED", paths["OUT_FED"]), \
                    mock.patch.object(news, "OUT_MD", paths["OUT_MD"]), mock.patch.object(news, "META", meta_path), \
                    mock.patch.object(news, "ROOT", tmp), mock.patch.object(news, "DATA", tmp), \
                    mock.patch.object(news, "fetch_page", page), \
                    mock.patch.object(news.time, "sleep", lambda s: None):
                news.main()
            return (json.load(open(paths["OUT_JSON"], encoding="utf-8")),
                    json.load(open(paths["OUT_FED"], encoding="utf-8")),
                    open(paths["OUT_MD"], encoding="utf-8").read(), json.load(open(meta_path, encoding="utf-8")))

    def item(self, doc, prefix):
        return next(it for it in doc["items"] if it["title"].startswith(prefix))

    def test_agreed_format(self):
        doc, fed, md, meta = self.run_main()
        for key in ("generated_at_taipei", "window_hours", "feeds", "errors", "items"):
            self.assertIn(key, doc)
        self.assertEqual(doc["generated_at_taipei"], "2026-10-22 08:00")
        it = self.item(doc, "Nvidia shares rise")
        for key in ("id", "published_utc", "published_taipei", "source", "tier", "lang", "title", "link", "url",
                    "symbols", "topics"):
            self.assertIn(key, it)
        self.assertEqual(len(it["id"]), 16)
        self.assertEqual({f["name"] for f in doc["feeds"] if not f["ok"]},
                         {"Google News 白名單：TSMC", "Google News 全部：TSMC"})
        self.assertTrue(all("count" in f for f in doc["feeds"]))

    def test_tiers_dedup_symbols_topics(self):
        doc, *_ = self.run_main()
        titles = [it["title"] for it in doc["items"]]
        deal = [it for it in doc["items"] if it["title"].startswith("Nvidia shares rise")]
        self.assertEqual(len(deal), 1)  # 同標題與高度相似的合併
        self.assertEqual((deal[0]["source"], deal[0]["tier"], deal[0]["rank"]), ("Reuters", 2, "A"))
        self.assertEqual(sorted(deal[0]["also"]), ["MarketBeat", "Yahoo Finance"])
        self.assertEqual(deal[0]["published_taipei"], "2026-10-22 06:00")
        self.assertNotIn("Old Nvidia story from last week", titles)  # 超過 72 小時
        self.assertEqual(self.item(doc, "Nvidia announces")["tier"], 1)  # 新聞稿通路＝官方
        self.assertEqual(self.item(doc, "Nvidia blog")["tier"], 3)
        self.assertEqual(self.item(doc, "Nvidia stock")["tier"], 4)
        partner = self.item(doc, "Nvidia and TSMC")
        self.assertEqual(partner["symbols"], ["2330", "NVDA", "TSM"])
        self.assertEqual(partner["topics"], ["china"])
        self.assertEqual(self.item(doc, "Trump threatens")["topics"], ["tariff"])  # tariffs 也算
        self.assertEqual(self.item(doc, "Nvidia announces")["topics"], ["earnings"])
        # 標題沒提到查詢關鍵字的：symbols 留空但照樣保留，每個查詢只留 1 則
        off = [it for it in doc["items"] if not it["title_match"] and it["source"] != "Federal Reserve"]
        self.assertEqual(len(off), 1)
        self.assertEqual(off[0]["symbols"], [])
        self.assertEqual(off[0]["queries"], ["NVDA"])
        # tier 4 每個查詢最多 2 則
        self.assertLessEqual(sum(1 for it in doc["items"] if it["tier"] == 4 and "NVDA" in it["queries"]), 2)
        # 中文：相似標題合併；成分股名稱補代號（國巨* 的星號去掉、美股持股不算）
        zh = [it for it in doc["items"] if it["lang"] == "zh"]
        self.assertEqual(len(zh), 2)
        self.assertEqual(self.item(doc, "台光電")["symbols"], ["2383"])
        self.assertEqual(self.item(doc, "台積電法說會")["topics"], ["earnings"])

    def test_fed_files_and_meta(self):
        doc, fed, md, meta = self.run_main()
        self.assertEqual([(f["date"], f["title"], f["type"]) for f in fed["items"]],
                         [("2026-10-22", "Federal Reserve issues FOMC statement", "press")])
        official = self.item(doc, "Federal Reserve issues")
        self.assertEqual((official["source"], official["tier"], official["topics"]), ("Federal Reserve", 1, ["fed"]))
        self.assertEqual(doc["status"], "partial")
        self.assertEqual(doc["sec_8k"]["status"], "未啟用")
        ds = meta["datasets"]
        self.assertEqual(ds["us/VOO"], {"status": "ok"})  # 其他資料集不動
        self.assertEqual((ds["news/headlines"]["status"], ds["news/headlines"]["last"]), ("partial", "2026-10-22 08:00"))
        self.assertEqual((ds["news/fed"]["status"], ds["news/fed"]["last"]), ("ok", "2026-10-22"))
        self.assertIn("T2A｜Reuters｜Nvidia shares rise on new AI deal", md)
        self.assertNotIn("Barclays", md.split("## NVDA")[1].split("##")[0])

    def test_original_urls(self):
        doc, fed, md, meta = self.run_main()
        deal = self.item(doc, "Nvidia shares rise")
        self.assertEqual(deal["url"], "https://publisher.example/" + gid("Nvidia shares rise on new AI deal"))
        self.assertTrue(deal["link"].startswith(GN_BASE))  # 原本的 link 保留
        self.assertEqual(self.item(doc, "Trump threatens")["url"],
                         "https://publisher.example/" + gid("Trump threatens new tariffs on chips"))
        self.assertEqual(self.item(doc, "台積電法說會")["url"], "https://publisher.example/" + gid("台積電法說會釋出樂觀展望"))
        self.assertIsNone(self.item(doc, "Nvidia stock")["url"])  # tier 4 不解
        self.assertIsNone(self.item(doc, "Banks rally")["url"])  # 拿不到簽章 → null，不放 Google 網址
        fed_item = self.item(doc, "Federal Reserve issues")
        self.assertEqual(fed_item["url"], fed_item["link"])  # Fed 的 link 本來就是原文
        u = doc["stats"]["url"]
        self.assertEqual((u["failed"], u["stopped"]), (1, None))
        self.assertEqual(u["ok"], u["target"] - 1)
        h = meta["datasets"]["news/headlines"]
        self.assertEqual((h["url_ok"], h["url_total"]), (u["ok"], u["target"]))

    def test_url_reused_and_old_items_skipped(self):
        prev = {"items": [
            {"title": "Nvidia cached headline", "source": "Reuters", "source_url": "https://www.reuters.com", "tier": 2,
             "lang": "en", "queries": ["NVDA"], "also": [], "link": GN_BASE + "CachedReuters",
             "url": "https://www.reuters.com/cached", "published_utc": "2026-10-21T20:00:00Z"},
            {"title": "Nvidia earlier headline kept", "source": "Reuters", "source_url": "https://www.reuters.com",
             "tier": 2, "lang": "en", "queries": ["NVDA"], "also": [], "link": GN_BASE + "OlderThan36h",
             "published_utc": "2026-10-20T03:00:00Z"}]}
        doc, *_ = self.run_main(previous=prev)
        self.assertEqual(self.item(doc, "Nvidia cached headline")["url"], "https://www.reuters.com/cached")
        self.assertNotIn("CachedReuters", PAGES)  # 解過的不再讀文章頁
        self.assertIsNone(self.item(doc, "Nvidia earlier headline kept")["url"])  # 超過 36 小時不解
        self.assertNotIn("OlderThan36h", PAGES)
        self.assertGreaterEqual(doc["stats"]["url"]["cached"], 1)

    def test_url_stops_when_whole_batch_fails(self):
        def down(url, timeout=20):
            raise RuntimeError("HTTP 429")
        doc, *_ = self.run_main(page=down)
        u = doc["stats"]["url"]
        self.assertEqual(u["new"], 0)
        self.assertIn("限速", u["stopped"])
        self.assertTrue(u["notes"])
        self.assertFalse(any("限速" in e for e in doc["errors"]))  # 網址解不開不算進 errors
        self.assertTrue(all(it["url"] is None for it in doc["items"] if it["source"] != "Federal Reserve"))

    def test_previous_items_kept_within_window_including_old_format(self):
        prev = {"items": [
            {"title": "Nvidia earlier headline kept", "source": "Reuters", "source_url": "https://www.reuters.com",
             "tier": "A", "lang": "en", "topics": ["NVDA"], "symbols": ["NVDA"], "also": [], "link": "x",
             "published_utc": "2026-10-20T03:00:00Z"},
            {"title": "Nvidia too old", "source": "Reuters", "source_url": "https://www.reuters.com",
             "tier": 2, "lang": "en", "queries": ["NVDA"], "topics": [], "also": [], "link": "y",
             "published_utc": "2026-10-18T03:00:00Z"}]}
        doc, *_ = self.run_main(previous=prev)
        kept = self.item(doc, "Nvidia earlier headline kept")
        self.assertEqual((kept["tier"], kept["queries"], kept["symbols"]), (2, ["NVDA"], ["NVDA"]))
        self.assertNotIn("Nvidia too old", [it["title"] for it in doc["items"]])

    def test_all_google_queries_fail_keeps_old_file(self):
        def down(url, **kw):
            if "federalreserve.gov" in url:
                return 200, FED_RSS
            raise RuntimeError("HTTP 503")
        with tempfile.TemporaryDirectory() as tmp:
            json.dump({"news": CFG}, open(os.path.join(tmp, "config.json"), "w", encoding="utf-8"))
            with mock.patch.object(news, "http", down), mock.patch.object(news, "ROOT", tmp), \
                    mock.patch.object(news, "OUT_JSON", os.path.join(tmp, "h.json")), \
                    mock.patch.object(news.time, "sleep", lambda s: None):
                with self.assertRaises(SystemExit):
                    news.main()
            self.assertFalse(os.path.exists(os.path.join(tmp, "h.json")))

    def test_helpers(self):
        self.assertEqual(news.tier_of("https://ca.finance.yahoo.com", CFG["tiers"]), (2, "B"))
        self.assertEqual(news.tier_of("https://www.marketbeat.com", CFG["tiers"]), (4, None))
        self.assertEqual(news.clean_title("Fed holds rates - Reuters", "Reuters"), "Fed holds rates")
        self.assertTrue(news.term_hit("fed", "fed's powell signals pause"))
        self.assertFalse(news.term_hit("apple", "pineapple prices jump"))
        self.assertTrue(news.term_hit("export control", "us tightens export controls"))
        self.assertEqual(news.gn_article_id(GN_BASE + "CBMiabc?oc=5"), "CBMiabc")
        self.assertIsNone(news.gn_article_id("https://www.reuters.com/x"))
        self.assertFalse(news.good_url("https://news.google.com/rss/articles/x"))


if __name__ == "__main__":
    unittest.main()

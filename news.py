"""新聞三層架構的第一層：整理新聞標題給「新聞與社群晨報」排程與兩個晨報讀取（只用 Python 標準庫）。

由 .github/workflows/news.yml 執行（cron-job.org 約台北 08:20 觸發）；不寫 meta.json，狀態記在輸出檔裡。

來源：
  Google News RSS：config.json 的 news.en／news.zh 每個主題查兩次，一次限定白名單網站（site:，避免被
                   公司新聞稿、MarketBeat 這類擠掉），一次不限定。RSS 的 source 欄位網址對照 news.tiers 標等級：
                   A＝通訊社與一線財經媒體、B＝主要財經媒體、C＝白名單以外（照樣保留，每個主題最多留 low_tier_per_topic 則）
  Fed：federalreserve.gov 官方 RSS（新聞稿、演講），等級 official
  SEC 8-K：還沒做（要等存聯絡信箱的 Actions secret），輸出裡先標「未啟用」

整理：同標題或高度相似（字詞重疊 ≥ 80%）的合併成一則，保留等級最高、最早的那則並列出其他來源；
依主題與標題關鍵字標相關標的（title_match＝標題有提到；沒提到的是 Google 依內文找到的，每個主題最多留
off_title_per_topic 則）；時間換成台北時間；保留最近 window_hours 小時（會合併上一次的輸出）。

輸出：
  data/news/headlines.json  items（標題、來源、等級、台北時間、主題、相關標的、連結）、fed、sec_8k、各查詢狀態
  data/news/headlines.md    精簡版給人看：每個主題列 A／B 等級標題，C 等級只列幾則
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from common import DATA, NOW, ROOT, TPE, UTC, http, write_json

OUT_JSON = os.path.join(DATA, "news", "headlines.json")
OUT_MD = os.path.join(DATA, "news", "headlines.md")
GN = "https://news.google.com/rss/search?q={q}&{loc}"
LOC = {"en": "hl=en-US&gl=US&ceid=US:en", "zh": "hl=zh-TW&gl=TW&ceid=TW:zh-Hant"}
TIER_ORDER = {"official": 0, "A": 1, "B": 2, "C": 3}
SIMILAR = 0.8


# ---------------------------------------------------------------- 解析
def domain(url):
    host = urllib.parse.urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def tier_of(url, tiers):
    host = domain(url)
    for t in ("A", "B"):
        if any(host == d or host.endswith("." + d) for d in tiers.get(t, [])):
            return t
    return "C"


def clean_title(title, source):
    """Google News 會在標題後面接「 - 來源名稱」，去掉。"""
    title = (title or "").strip()
    if source and title.endswith(" - " + source):
        title = title[: -len(source) - 3]
    return re.sub(r"\s+", " ", title).strip()


def to_utc(text):
    try:
        t = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(UTC)


def parse_rss(raw):
    """回傳 [{title, link, source, source_url, published(datetime UTC)}]；沒有發布時間的略過。"""
    out = []
    for it in ET.fromstring(raw).findall("./channel/item"):
        src = it.find("source")
        name = (src.text or "").strip() if src is not None else ""
        pub = to_utc(it.findtext("pubDate"))
        if not pub:
            continue
        out.append({"title": clean_title(it.findtext("title"), name), "link": it.findtext("link"),
                    "source": name, "source_url": src.get("url") if src is not None else None, "published": pub})
    return out


# ---------------------------------------------------------------- 去重與標記
def tokens(title):
    """英文用單字（3 個字母以上），中文用相鄰兩字，用來算相似度。"""
    t = title.lower()
    words = set(re.findall(r"[a-z0-9][a-z0-9&'.-]{2,}", t))
    cjk = re.sub(r"[^一-鿿]", "", t)
    return words | {cjk[i:i + 2] for i in range(len(cjk) - 1)}


def similar(a, b):
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= SIMILAR


def term_hit(term, text):
    if re.fullmatch(r"[a-z0-9&. ]+", term):
        return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?:s|es|'s|’s)?(?![a-z0-9])", text) is not None
    return term in text


def tag(item, topics):
    """主題：查到它的主題，加上標題命中其他主題關鍵字的。相關標的＝這些主題的 symbols。
    title_topics：標題真的提到的主題；title_match：至少提到一個（Google 也會依內文找到標題無關的新聞，這類標 false）。"""
    text = item["title"].lower()
    hits = {t["id"] for t in topics if any(term_hit(m.lower(), text) for m in t.get("match", []))}
    ids = set(item["topics"]) | hits
    order = [t["id"] for t in topics]
    item["topics"] = sorted(ids, key=lambda i: order.index(i) if i in order else 99)
    item["symbols"] = sorted({s for t in topics if t["id"] in ids for s in t.get("symbols", [])})
    item["title_topics"] = [i for i in item["topics"] if i in hits]
    item["title_match"] = bool(hits)
    return item


def merge(items):
    """同標題或高度相似的合併：保留等級最高、其次最早的那則，其他來源列在 also。"""
    items = sorted(items, key=lambda x: (TIER_ORDER[x["tier"]], x["published"]))
    kept = []
    for it in items:
        tk = tokens(it["title"])
        for k in kept:
            if k["title"].lower() == it["title"].lower() or similar(k["_tokens"], tk):
                k["topics"] = sorted(set(k["topics"]) | set(it["topics"]))
                if it["source"] not in k["also"] and it["source"] != k["source"]:
                    k["also"].append(it["source"])
                break
        else:
            kept.append(dict(it, _tokens=tk, also=list(it.get("also", []))))
    for k in kept:
        k.pop("_tokens")
    return kept


def cap(items, low_tier_per_topic, off_title_per_topic):
    """標題沒提到主題的，每個主題只留最新 off_title_per_topic 則；C 等級每個主題只留最新 low_tier_per_topic 則
    （一則屬於多個主題時，任一主題還有名額就留）。"""
    used, out = {}, []
    for it in sorted(items, key=lambda x: (TIER_ORDER[x["tier"]], -x["published"].timestamp())):
        kind = "off" if not it["title_match"] else ("low" if it["tier"] == "C" else None)
        if kind is None:
            out.append(it)
            continue
        limit = off_title_per_topic if kind == "off" else low_tier_per_topic
        if any(used.get((kind, t), 0) < limit for t in it["topics"]):
            for t in it["topics"]:
                used[(kind, t)] = used.get((kind, t), 0) + 1
            out.append(it)
    return out


# ---------------------------------------------------------------- 抓取
def google_news(cfg, errors):
    """每個主題兩次查詢（限定白名單網站、不限定）。回傳 (items, 各查詢狀態)。"""
    items, status = [], []
    tiers = cfg["tiers"]
    for lang in ("en", "zh"):
        sites = " OR ".join(f"site:{d}" for d in cfg["site_filter"][lang])
        for topic in cfg[lang]:
            for kind, q in (("whitelist", f"({topic['q']}) ({sites}) when:{cfg['query_when']}"),
                            ("all", f"({topic['q']}) when:{cfg['query_when']}")):
                url = GN.format(q=urllib.parse.quote(q), loc=LOC[lang])
                try:
                    code, raw = http(url)
                    if code != 200:
                        raise RuntimeError(f"HTTP {code}")
                    got = parse_rss(raw)
                    status.append({"topic": topic["id"], "lang": lang, "kind": kind, "items": len(got)})
                    for g in got:
                        items.append(dict(g, tier=tier_of(g["source_url"], tiers), lang=lang, topics=[topic["id"]]))
                except Exception as ex:  # noqa: BLE001 — 單一查詢失敗不影響其他
                    status.append({"topic": topic["id"], "lang": lang, "kind": kind, "error": str(ex)[:200]})
                    errors.append(f"Google News {topic['id']} {kind}: {ex}")
                time.sleep(1)
    return items, status


def fed(cfg, errors):
    out, status = [], []
    since = NOW - dt.timedelta(days=cfg.get("fed_days", 7))
    for kind, url in cfg["fed_feeds"].items():
        try:
            code, raw = http(url)
            if code != 200:
                raise RuntimeError(f"HTTP {code}")
            got = [g for g in parse_rss(raw) if g["published"] >= since]
            status.append({"feed": kind, "items": len(got)})
            out += [{"title": g["title"], "link": g["link"], "feed": kind, "published": g["published"]} for g in got]
        except Exception as ex:  # noqa: BLE001
            status.append({"feed": kind, "error": str(ex)[:200]})
            errors.append(f"Fed {kind}: {ex}")
    return sorted(out, key=lambda x: x["published"], reverse=True), status


# ---------------------------------------------------------------- 輸出
def tpe(t):
    return t.astimezone(TPE).strftime("%Y-%m-%d %H:%M")


def serialize(it):
    out = {k: v for k, v in it.items() if k != "published"}
    out["published_taipei"], out["published_utc"] = tpe(it["published"]), it["published"].strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


def load_previous(path):
    """上一次輸出的 items（還原 published 成 datetime），用來保留 window 內、這次查詢沒再出現的標題。"""
    try:
        prev = json.load(open(path, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in prev.get("items", []):
        try:
            t = dt.datetime.strptime(it["published_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except (KeyError, ValueError):
            continue
        out.append(dict({k: v for k, v in it.items() if k not in ("published_taipei", "published_utc")}, published=t))
    return out


def build(cfg, fresh_items, previous, now):
    topics = cfg["en"] + cfg["zh"]
    since = now - dt.timedelta(hours=cfg["window_hours"])
    pool = [it for it in fresh_items + previous if it["published"] >= since]
    items = [tag(it, topics) for it in merge([tag(dict(it), topics) for it in pool])]
    items = cap(items, cfg["low_tier_per_topic"], cfg["off_title_per_topic"])
    return sorted(items, key=lambda x: x["published"], reverse=True)


def markdown(doc, topics):
    lines = [f"# 新聞標題（{doc['generated_at_taipei']} 台北）", "",
             f"最近 {doc['window_hours']} 小時，共 {doc['stats']['total']} 則（A {doc['stats']['by_tier'].get('A', 0)}、"
             f"B {doc['stats']['by_tier'].get('B', 0)}、C {doc['stats']['by_tier'].get('C', 0)}）。"
             "等級：A 通訊社與一線財經媒體、B 主要財經媒體、C 白名單以外。時間為台北時間。", ""]
    lines += ["## Fed（官方）", ""]
    lines += [f"- {f['published_taipei']} [{f['feed']}] {f['title']}" for f in doc["fed"]] or ["- 期間內沒有新發布"]
    lines.append("")
    for t in topics:
        mine = [it for it in doc["items"] if t["id"] in it["topics"]]
        if not mine:
            continue
        good = [it for it in mine if it["tier"] in ("A", "B") and t["id"] in it["title_topics"]][:15]
        low = [it for it in mine if it["tier"] == "C" and t["id"] in it["title_topics"]][:3]
        lines += [f"## {t['id']}（{t['q']}）", ""]
        lines += [f"- {it['published_taipei']} {it['tier']}｜{it['source']}｜{it['title']}" for it in good + low]
        rest = len(mine) - len(good) - len(low)
        if rest > 0:
            lines.append(f"- …另有 {rest} 則（見 headlines.json）")
        lines.append("")
    return "\n".join(lines)


def main():
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))["news"]
    errors = []
    fresh, gn_status = google_news(cfg, errors)
    items = build(cfg, fresh, load_previous(OUT_JSON), NOW)
    fed_items, fed_status = fed(cfg, errors)
    by_tier, by_symbol = {}, {}
    for it in items:
        by_tier[it["tier"]] = by_tier.get(it["tier"], 0) + 1
        for s in it["symbols"]:
            by_symbol[s] = by_symbol.get(s, 0) + 1
    ok_queries = sum(1 for s in gn_status if "error" not in s)
    doc = {"generated_at_taipei": tpe(NOW), "generated_at_utc": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "window_hours": cfg["window_hours"],
           "status": "fail" if not ok_queries else ("ok" if not errors else "partial"),
           "tiers": {"official": "Fed 官網", "A": cfg["tiers"]["A"], "B": cfg["tiers"]["B"], "C": "白名單以外"},
           "stats": {"total": len(items), "by_tier": by_tier, "by_symbol": by_symbol,
                     "queries_ok": ok_queries, "queries": len(gn_status)},
           "fed": [dict({k: v for k, v in f.items() if k != "published"}, published_taipei=tpe(f["published"]))
                   for f in fed_items],
           "sec_8k": {"status": "未啟用", "note": "等存聯絡信箱的 Actions secret 建好後加入"},
           "items": [serialize(it) for it in items],
           "query_status": gn_status, "fed_status": fed_status, "errors": errors}
    if not items and not ok_queries:
        print("所有查詢都失敗，保留上一次的檔案", file=sys.stderr)
        for e in errors:
            print(" -", e, file=sys.stderr)
        sys.exit(1)
    write_json(OUT_JSON, doc)
    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write(markdown(doc, cfg["en"] + cfg["zh"]) + "\n")
    print(json.dumps(doc["stats"], ensure_ascii=False), "fed", len(doc["fed"]), "errors", len(errors))
    for e in errors:
        print(" -", e)


if __name__ == "__main__":
    main()

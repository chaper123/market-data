"""新聞三層架構的第一層：整理新聞標題給「新聞與社群晨報」排程與兩個晨報讀取（只用 Python 標準庫）。

由 .github/workflows/news.yml 執行（cron-job.org 約台北 08:20 觸發）。輸出格式依 stock-dashboard 的
docs/NEWS_ROUTINE.md 第 3.1 節約定，差異寫在 README。

來源：
  Google News RSS：config.json 的 news.en／news.zh 每個查詢跑兩次，一次限定白名單網站（site:，避免被公司新聞稿、
                   MarketBeat 這類擠掉），一次不限定。依 RSS 的 source 網址對照 news.tiers 標等級：
                   1 官方（政府、央行、交易所、公司 IR 與新聞稿通路）、2 主要媒體（rank A 通訊社與一線財經媒體、
                   B 其他主要財經媒體）、3 第一手發言（公司部落格等）、4 其他（照樣保留，每個查詢最多 low_tier_per_topic 則）
  Fed：federalreserve.gov 官方 RSS（新聞稿、演講），tier 1，另外輸出 fed.json
  SEC 8-K：還沒做（要等存聯絡信箱的 Actions secret），先標「未啟用」

整理：同標題或高度相似（字詞重疊 ≥ 80%）的合併成一則，保留等級最高、最早的那則並把其他來源列在 also；
symbols 只依標題判斷（查詢關鍵字，以及兩檔主動 ETF 前十大的台股名稱）；topics 依 news.topic_rules 的標題關鍵字
（fed、tariff、export_controls、china、taiwan、oil、war、cpi、jobs 等）；queries 是找到它的查詢；
標題沒提到查詢關鍵字的（Google 依內文找到）每個查詢最多留 off_title_per_topic 則；時間換成台北時間；
保留最近 window_hours 小時（會合併上一次的輸出）。

輸出：
  data/news/headlines.json  generated_at_taipei、window_hours、status、feeds[]、errors[]、items[]（見 README）
  data/news/fed.json        Fed 新聞稿與演講（最近 fed_days 天）：items[].date、title、link、type
  data/news/headlines.md    精簡版給人看
  data/meta.json            只更新 datasets 的 news/headlines、news/fed（推回 repo 時由 publish.py 合併）
"""
import datetime as dt
import hashlib
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
OUT_FED = os.path.join(DATA, "news", "fed.json")
OUT_MD = os.path.join(DATA, "news", "headlines.md")
META = os.path.join(DATA, "meta.json")
GN = "https://news.google.com/rss/search?q={q}&{loc}"
LOC = {"en": "hl=en-US&gl=US&ceid=US:en", "zh": "hl=zh-TW&gl=TW&ceid=TW:zh-Hant"}
TIER_NAMES = {1: "官方", 2: "主要媒體", 3: "第一手發言", 4: "其他"}
SIMILAR = 0.8


# ---------------------------------------------------------------- 解析
def domain(url):
    host = urllib.parse.urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def tier_of(url, tiers):
    """回傳 (tier, rank)：tier 1～4；主要媒體再分 rank A／B，其他為 None。"""
    host = domain(url)
    for key, tier, rank in (("1", 1, None), ("2A", 2, "A"), ("2B", 2, "B"), ("3", 3, None)):
        if any(host == d or host.endswith("." + d) for d in tiers.get(key, [])):
            return tier, rank
    return 4, None


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
    """英文詞看字的邊界（可接 s／es／'s），中文詞直接找子字串。"""
    if re.fullmatch(r"[a-z0-9&. -]+", term):
        return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?:s|es|'s|’s)?(?![a-z0-9])", text) is not None
    return term in text


def constituents():
    """兩檔主動 ETF 前十大裡的台股：名稱 → 代號（名稱至少兩個字；去掉「國巨*」這類註記）。"""
    out = {}
    for code in ("00981A", "00411A"):
        try:
            snap = json.load(open(os.path.join(DATA, "holdings", f"{code}.json"), encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 沒有持股檔就不補成分股
            continue
        for h in snap.get("top10", []):
            name = re.sub(r"[*＊\s]", "", h.get("name") or "")
            if re.fullmatch(r"\d{4,6}", h.get("code") or "") and len(name) >= 2:
                out[name] = h["code"]
    return out


def tag(item, cfg, names):
    """依標題標 symbols、topics、title_queries（標題提到的查詢）與 title_match。"""
    text = item["title"].lower()
    queries = cfg["en"] + cfg["zh"]
    hits = [q["id"] for q in queries if any(term_hit(m.lower(), text) for m in q.get("match", []))]
    symbols = {s for q in queries if q["id"] in hits for s in q.get("symbols", [])}
    if item.get("lang") == "zh":
        symbols |= {code for name, code in names.items() if name in item["title"]}
    item["symbols"] = sorted(symbols)
    item["topics"] = [t for t, terms in cfg["topic_rules"].items() if any(term_hit(w.lower(), text) for w in terms)]
    item["title_queries"] = hits
    item["title_match"] = bool(hits or symbols)  # 標題提到查詢關鍵字或成分股
    return item


def merge(items):
    """同標題或高度相似的合併：保留等級最高、其次最早的那則，其他來源列在 also。"""
    items = sorted(items, key=lambda x: (x["tier"], x["published"]))
    kept = []
    for it in items:
        tk = tokens(it["title"])
        for k in kept:
            if k["title"].lower() == it["title"].lower() or similar(k["_tokens"], tk):
                k["queries"] = sorted(set(k["queries"]) | set(it["queries"]))
                for s in [it["source"]] + list(it.get("also", [])):
                    if s and s != k["source"] and s not in k["also"]:
                        k["also"].append(s)
                break
        else:
            kept.append(dict(it, _tokens=tk, also=list(it.get("also", []))))
    for k in kept:
        k.pop("_tokens")
    return kept


def cap(items, low_tier_per_topic, off_title_per_topic):
    """標題沒提到查詢關鍵字的，每個查詢只留最新 off_title_per_topic 則；tier 4 每個查詢只留最新 low_tier_per_topic 則
    （一則屬於多個查詢時，任一查詢還有名額就留）。官方（Fed）一律保留。"""
    used, out = {}, []
    for it in sorted(items, key=lambda x: (x["tier"], -x["published"].timestamp())):
        kind = None if it["tier"] == 1 else "off" if not it["title_match"] else "low" if it["tier"] == 4 else None
        if kind is None:
            out.append(it)
            continue
        limit = off_title_per_topic if kind == "off" else low_tier_per_topic
        if any(used.get((kind, q), 0) < limit for q in it["queries"]):
            for q in it["queries"]:
                used[(kind, q)] = used.get((kind, q), 0) + 1
            out.append(it)
    return out


# ---------------------------------------------------------------- 抓取
def google_news(cfg, errors):
    """每個查詢跑兩次（限定白名單網站、不限定）。回傳 (items, feeds)。"""
    items, feeds = [], []
    for lang in ("en", "zh"):
        sites = " OR ".join(f"site:{d}" for d in cfg["site_filter"][lang])
        for topic in cfg[lang]:
            for label, q in (("白名單", f"({topic['q']}) ({sites}) when:{cfg['query_when']}"),
                             ("全部", f"({topic['q']}) when:{cfg['query_when']}")):
                name = f"Google News {label}：{topic['id']}"
                url = GN.format(q=urllib.parse.quote(q), loc=LOC[lang])
                try:
                    code, raw = http(url)
                    if code != 200:
                        raise RuntimeError(f"HTTP {code}")
                    got = parse_rss(raw)
                    feeds.append({"name": name, "ok": True, "count": len(got)})
                    for g in got:
                        tier, rank = tier_of(g["source_url"], cfg["tiers"])
                        items.append(dict(g, tier=tier, rank=rank, lang=lang, queries=[topic["id"]]))
                except Exception as ex:  # noqa: BLE001 — 單一查詢失敗不影響其他
                    feeds.append({"name": name, "ok": False, "count": 0, "error": str(ex)[:200]})
                    errors.append(f"{name}: {ex}")
                time.sleep(1)
    return items, feeds


def fed(cfg, errors):
    out, feeds = [], []
    since = NOW - dt.timedelta(days=cfg.get("fed_days", 7))
    for kind, url in cfg["fed_feeds"].items():
        name = {"press": "Fed 新聞稿", "speech": "Fed 演講"}.get(kind, f"Fed {kind}")
        try:
            code, raw = http(url)
            if code != 200:
                raise RuntimeError(f"HTTP {code}")
            got = [g for g in parse_rss(raw) if g["published"] >= since]
            feeds.append({"name": name, "ok": True, "count": len(got)})
            out += [{"title": g["title"], "link": g["link"], "type": kind, "published": g["published"]} for g in got]
        except Exception as ex:  # noqa: BLE001
            feeds.append({"name": name, "ok": False, "count": 0, "error": str(ex)[:200]})
            errors.append(f"{name}: {ex}")
    return sorted(out, key=lambda x: x["published"], reverse=True), feeds


# ---------------------------------------------------------------- 輸出
def tpe(t):
    return t.astimezone(TPE).strftime("%Y-%m-%d %H:%M")


def utc_text(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def item_id(it):
    return hashlib.sha1((it.get("link") or it["title"]).encode("utf-8")).hexdigest()[:16]


def serialize(it):
    return {"id": item_id(it), "published_utc": utc_text(it["published"]), "published_taipei": tpe(it["published"]),
            "source": it["source"], "source_url": it.get("source_url"), "tier": it["tier"], "rank": it.get("rank"),
            "lang": it["lang"], "title": it["title"], "link": it["link"], "symbols": it["symbols"],
            "topics": it["topics"], "queries": it["queries"], "title_queries": it["title_queries"],
            "title_match": it["title_match"], "also": it["also"]}


def load_previous(path, tiers):
    """上一次輸出的 items（還原 published），用來保留時間窗內、這次查詢沒再出現的標題。等級依來源網址重算。"""
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
        if it.get("source") == "Federal Reserve":
            continue  # Fed 每次重抓
        tier, rank = tier_of(it.get("source_url"), tiers)
        # 舊格式（10/22 第一版）的 topics 是查詢名稱
        queries = it.get("queries") or (it.get("topics") if isinstance(it.get("tier"), str) else None) or []
        out.append({"title": it["title"], "link": it.get("link"), "source": it.get("source", ""),
                    "source_url": it.get("source_url"), "published": t, "tier": tier, "rank": rank,
                    "lang": it.get("lang", "en"), "queries": list(queries), "also": list(it.get("also", []))})
    return out


def build(cfg, fresh_items, previous, fed_items, now, names=None):
    names = constituents() if names is None else names
    since = now - dt.timedelta(hours=cfg["window_hours"])
    official = [{"title": f["title"], "link": f["link"], "source": "Federal Reserve",
                 "source_url": "https://www.federalreserve.gov", "published": f["published"], "tier": 1, "rank": None,
                 "lang": "en", "queries": [f"fed_{f['type']}"], "also": []} for f in fed_items]
    pool = [it for it in fresh_items + previous if it["published"] >= since] + official
    items = [tag(it, cfg, names) for it in merge([dict(it) for it in pool])]
    items = cap(items, cfg["low_tier_per_topic"], cfg["off_title_per_topic"])
    return sorted(items, key=lambda x: x["published"], reverse=True)


def markdown(doc, fed_doc, queries):
    by = doc["stats"]["by_tier"]
    lines = [f"# 新聞標題（{doc['generated_at_taipei']} 台北）", "",
             f"最近 {doc['window_hours']} 小時，共 {doc['stats']['total']} 則（官方 {by.get('1', 0)}、主要媒體 {by.get('2', 0)}、"
             f"第一手 {by.get('3', 0)}、其他 {by.get('4', 0)}）。T1 官方、T2A 通訊社與一線財經媒體、T2B 其他主要財經媒體、"
             "T3 第一手發言、T4 其他。時間為台北時間。", "", "## Fed（官方）", ""]
    lines += [f"- {f['published_taipei']} [{f['type']}] {f['title']}" for f in fed_doc["items"]] or ["- 期間內沒有新發布"]
    lines.append("")
    for q in queries:
        mine = [it for it in doc["items"] if q["id"] in it["queries"]]
        if not mine:
            continue
        rel = [it for it in mine if q["id"] in it["title_queries"]]
        good = [it for it in rel if it["tier"] <= 2][:15]
        low = [it for it in rel if it["tier"] >= 3][:3]
        lines += [f"## {q['id']}（{q['q']}）", ""]
        lines += [f"- {it['published_taipei']} T{it['tier']}{it['rank'] or ''}｜{it['source']}｜{it['title']}"
                  for it in good + low]
        rest = len(mine) - len(good) - len(low)
        if rest > 0:
            lines.append(f"- …另有 {rest} 則（見 headlines.json）")
        lines.append("")
    return "\n".join(lines)


def update_meta(doc, fed_doc):
    """只寫 news/headlines、news/fed 兩個資料集；其他內容不動（推回 repo 時 publish.py 會再合併一次）。"""
    try:
        meta = json.load(open(META, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        meta = {"datasets": {}, "errors": []}
    ds = meta.setdefault("datasets", {})
    ds["news/headlines"] = {"status": doc["status"], "source": "Google News RSS（news.py）",
                            "last": doc["generated_at_taipei"], "items": doc["stats"]["total"],
                            "by_tier": doc["stats"]["by_tier"],
                            "feeds_ok": f"{sum(f['ok'] for f in doc['feeds'])}/{len(doc['feeds'])}",
                            "errors": len(doc["errors"])}
    fed_ok = all(f["ok"] for f in fed_doc["feeds"])
    ds["news/fed"] = {"status": "ok" if fed_ok else "fail", "source": "federalreserve.gov RSS",
                      "last": fed_doc["items"][0]["date"] if fed_doc["items"] else None,
                      "checked_at_taipei": fed_doc["generated_at_taipei"], "items": len(fed_doc["items"])}
    write_json(META, meta)


def main():
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))["news"]
    errors = []
    fresh, feeds = google_news(cfg, errors)
    fed_items, fed_feeds = fed(cfg, errors)
    if not any(f["ok"] for f in feeds):
        print("Google News 查詢全部失敗，保留上一次的檔案", file=sys.stderr)
        for e in errors:
            print(" -", e, file=sys.stderr)
        sys.exit(1)
    items = build(cfg, fresh, load_previous(OUT_JSON, cfg["tiers"]), fed_items, NOW)
    by_tier, by_symbol, by_topic = {}, {}, {}
    for it in items:
        by_tier[str(it["tier"])] = by_tier.get(str(it["tier"]), 0) + 1
        for s in it["symbols"]:
            by_symbol[s] = by_symbol.get(s, 0) + 1
        for t in it["topics"]:
            by_topic[t] = by_topic.get(t, 0) + 1
    all_feeds = feeds + fed_feeds
    doc = {"generated_at_taipei": tpe(NOW), "generated_at_utc": utc_text(NOW), "window_hours": cfg["window_hours"],
           "status": "ok" if not errors else "partial", "tiers": {str(k): v for k, v in TIER_NAMES.items()},
           "feeds": all_feeds, "errors": errors,
           "stats": {"total": len(items), "by_tier": by_tier, "by_symbol": by_symbol, "by_topic": by_topic},
           "sec_8k": {"status": "未啟用", "note": "等存聯絡信箱的 Actions secret 建好後加入"},
           "items": [serialize(it) for it in items]}
    fed_doc = {"generated_at_taipei": tpe(NOW), "days": cfg.get("fed_days", 7), "feeds": fed_feeds,
               "items": [{"date": tpe(f["published"])[:10], "title": f["title"], "link": f["link"], "type": f["type"],
                          "published_taipei": tpe(f["published"]), "published_utc": utc_text(f["published"])}
                         for f in fed_items]}
    write_json(OUT_JSON, doc)
    write_json(OUT_FED, fed_doc)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write(markdown(doc, fed_doc, cfg["en"] + cfg["zh"]) + "\n")
    update_meta(doc, fed_doc)
    print(json.dumps(doc["stats"]["by_tier"], ensure_ascii=False), "total", len(items), "fed", len(fed_items),
          "feeds ok", f"{sum(f['ok'] for f in all_feeds)}/{len(all_feeds)}")
    for e in errors:
        print(" -", e)


if __name__ == "__main__":
    main()

"""新聞三層架構的第一層：整理新聞標題給「新聞與社群晨報」排程與兩個晨報讀取（只用 Python 標準庫）。

由 .github/workflows/news.yml 執行（cron-job.org 約台北 08:20 觸發）。輸出格式依 stock-dashboard 的
docs/NEWS_ROUTINE.md 第 3.1 節約定，差異寫在 README。

來源：
  Google News RSS：config.json 的 news.en／news.zh 每個查詢跑兩次，一次限定白名單網站（site:，避免被公司新聞稿、
                   MarketBeat 這類擠掉），一次不限定。依 RSS 的 source 網址對照 news.tiers 標等級：
                   1 官方（政府、央行、交易所、公司 IR 與新聞稿通路）、2 主要媒體（rank A 通訊社與一線財經媒體、
                   B 其他主要財經媒體）、3 第一手發言（公司部落格等）、4 其他（照樣保留，每個查詢最多 low_tier_per_topic 則）
  Fed：federalreserve.gov 官方 RSS（新聞稿、演講），tier 1，另外輸出 fed.json
  中央社：官方 RSS（news.cna_feeds，預設財經、科技），連結就是 cna.com.tw 原文（url 直接用），source 標「中央社」、
          tier 2 rank A。Google News 收到的同一篇以官方 RSS 為準。依中央社 RSS 使用規範（個人、非營利之非商業用途），
          只用標題與連結、不轉載全文，並標示出處「中央通訊社」（頂層 attribution）
  SEC 8-K：還沒做（要等存聯絡信箱的 Actions secret），先標「未啟用」

整理：同標題或高度相似（字詞重疊 ≥ 80%）的合併成一則，保留等級最高、最早的那則並把其他來源列在 also；
symbols 只依標題判斷（查詢關鍵字，以及兩檔主動 ETF 前十大的台股名稱）；topics 依 news.topic_rules 的標題關鍵字
（fed、tariff、export_controls、china、taiwan、oil、war、cpi、jobs 等）；queries 是找到它的查詢；
標題沒提到查詢關鍵字的（Google 依內文找到）每個查詢最多留 off_title_per_topic 則；時間換成台北時間；
保留最近 window_hours 小時（會合併上一次的輸出）。
url：Tier 1～2、最近 url_hours 小時的項目解開 Google News 轉址得到原文網址（讀文章頁拿簽章、再呼叫 batchexecute，
一次 10 則；解過的沿用；每次有數量與時間上限），解不開或範圍外是 null；Fed 的 link 本來就是原文。
發布時間可疑：同一次抓取中同一來源有 time_suspect_min 則以上發布時間完全相同（Google 重新索引時會把一批舊文標成同一個
時間），就讀原文頁（JSON-LD datePublished、article:published_time 等，最後看網址裡的日期）找回日期：找得回來就改正並記
time_fixed_from，改正後超出時間窗的拿掉；找不回來的移到頂層 suspect_items（附 suspect_reason）。結果存在 time_check_cache。

輸出：
  data/news/headlines.json  generated_at_taipei、window_hours、status、feeds[]、errors[]、items[]（見 README）
  data/news/fed.json        Fed 新聞稿與演講（最近 fed_days 天）：items[].date、title、link、type
  data/news/headlines.md    精簡版給人看
  data/meta.json            只更新 datasets 的 news/headlines、news/fed（推回 repo 時由 publish.py 合併）
"""
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime

from common import DATA, NOW, ROOT, TPE, UA, UTC, http, write_json

OUT_JSON = os.path.join(DATA, "news", "headlines.json")
OUT_FED = os.path.join(DATA, "news", "fed.json")
OUT_MD = os.path.join(DATA, "news", "headlines.md")
META = os.path.join(DATA, "meta.json")
GN = "https://news.google.com/rss/search?q={q}&{loc}"
LOC = {"en": "hl=en-US&gl=US&ceid=US:en", "zh": "hl=zh-TW&gl=TW&ceid=TW:zh-Hant"}
TIER_NAMES = {1: "官方", 2: "主要媒體", 3: "第一手發言", 4: "其他"}
GN_ARTICLE = "https://news.google.com/rss/articles/{}"
GN_BATCH = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
GN_REQ = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
          '"X","X",1,[1,1,1],1,1,null,0,0,null,0],{gid},{ts},{sg}]')
BATCH_SIZE = 10
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
    """同標題或高度相似的合併：保留等級最高、其次是原文連結（官方 RSS，例如中央社）、再其次最早的那則，
    其他來源列在 also。"""
    items = sorted(items, key=lambda x: (x["tier"], gn_article_id(x.get("link")) is not None, x["published"]))
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
    （一則屬於多個查詢時，任一查詢還有名額就留）。官方（Fed）與原文連結的官方 RSS（中央社）一律保留。"""
    used, out = {}, []
    for it in sorted(items, key=lambda x: (x["tier"], -x["published"].timestamp())):
        direct = it["tier"] == 1 or not gn_article_id(it.get("link"))  # 官方、中央社 RSS 這類原文連結不限
        kind = None if direct else "off" if not it["title_match"] else "low" if it["tier"] == 4 else None
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


def cna(cfg, errors):
    """中央社官方 RSS（news.cna_feeds）。回傳 (items, feeds)；失敗只記在 feeds／errors，上一次的中央社標題照常保留。"""
    items, feeds = [], []
    for name, url in cfg.get("cna_feeds", {}).items():
        label = f"中央社 RSS：{name}"
        try:
            code, raw = http(url)
            if code != 200:
                raise RuntimeError(f"HTTP {code}")
            got = [g for g in parse_rss(raw) if good_url(g["link"])]
            feeds.append({"name": label, "ok": True, "count": len(got)})
            for g in got:
                tier, rank = tier_of(g["link"], cfg["tiers"])
                items.append(dict(g, source="中央社", source_url="https://www.cna.com.tw", tier=tier, rank=rank,
                                  lang="zh", queries=[f"cna_{name}"], url=g["link"]))
        except Exception as ex:  # noqa: BLE001 — 中央社失敗不影響其他 feed
            feeds.append({"name": label, "ok": False, "count": 0, "error": str(ex)[:200]})
            errors.append(f"{label}: {ex}")
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


# ---------------------------------------------------------------- 原文網址（解開 Google News 轉址）
def gn_article_id(link):
    p = urllib.parse.urlparse(link or "")
    if p.netloc != "news.google.com" or "/articles/" not in p.path:
        return None
    return p.path.rsplit("/", 1)[-1]


def good_url(url):
    return isinstance(url, str) and url.startswith(("http://", "https://")) and domain(url) != "news.google.com"


def fetch_page(url, timeout=20):
    """要 gzip（約 600 KB 的頁面壓成 120 KB）。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return gzip.decompress(raw) if r.headers.get("Content-Encoding") == "gzip" else raw


def signature(gid):
    """新版連結（CBMi…）要先讀文章頁取得簽章 data-n-a-ts、data-n-a-sg（在頁面最後面）。"""
    html = fetch_page(GN_ARTICLE.format(gid)).decode("utf-8", "replace")
    ts, sg = re.search(r'data-n-a-ts="(\d+)"', html), re.search(r'data-n-a-sg="([^"]+)"', html)
    if not ts or not sg:
        raise RuntimeError("文章頁沒有簽章")
    return ts.group(1), sg.group(1)


def batch_decode(entries):
    """entries：[(gid, ts, sg)]。一次呼叫 batchexecute 解多則，回傳 {gid: 原文網址}（解不開的不列）。"""
    req = [[["Fbv4je", GN_REQ.format(gid=json.dumps(g), ts=ts, sg=json.dumps(sg)), None, str(k + 1)]
            for k, (g, ts, sg) in enumerate(entries)]]
    body = ("f.req=" + urllib.parse.quote(json.dumps(req, separators=(",", ":")))).encode()
    code, raw = http(GN_BATCH, data=body, tries=2,
                     headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    if code != 200:
        raise RuntimeError(f"batchexecute HTTP {code}")
    out = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip().startswith("["):
            continue
        try:
            rows = json.loads(line)
        except ValueError:
            continue
        for e in rows:
            if not (isinstance(e, list) and len(e) > 6 and e[:2] == ["wrb.fr", "Fbv4je"] and e[2]):
                continue
            try:
                payload, k = json.loads(e[2]), int(e[6]) - 1
            except (ValueError, TypeError):
                continue
            if 0 <= k < len(entries) and len(payload) > 1 and good_url(payload[1]):
                out[entries[k][0]] = payload[1]
    return out


def decode_chunk(pool, ids, notes):
    """一批（最多 10 則）Google News 文章 id：並行讀文章頁拿簽章，再一次 batchexecute。回傳 {id: 原文網址}。"""
    sigs = []
    for g, f in zip(ids, [pool.submit(signature, g) for g in ids]):
        try:
            sigs.append((g, *f.result()))
        except Exception:  # noqa: BLE001 — 單則失敗就留 null
            pass
    if not sigs:
        return {}
    try:
        return batch_decode(sigs)
    except Exception as ex:  # noqa: BLE001
        notes.append(f"batchexecute：{ex}")
        return {}


def resolve_urls(items, cfg, now, cache, notes):
    """替 Tier 1～2、最近 url_hours 小時的 Google News 連結解出原文網址（url）。解過的依 id 沿用（cache）；
    每次最多 url_max_per_run 則、url_budget_seconds 秒，優先順序：等級、rank A、標題相關、時間新。
    整批 10 則都失敗（多半是被限速）就停，剩下的留到下次。狀況記在 notes（不算進 errors）。回傳統計。"""
    for it in items:
        if not it.get("url"):
            it["url"] = cache.get(item_id(it)) or (it["link"] if good_url(it.get("link")) else None)
    since = now - dt.timedelta(hours=cfg.get("url_hours", 36))
    target = [it for it in items if it["tier"] <= 2 and it["published"] >= since]
    cached = sum(1 for it in target if it["url"])
    todo = sorted((it for it in target if not it["url"] and gn_article_id(it.get("link"))),
                  key=lambda x: (x["tier"], x.get("rank") or "Z", not x["title_match"], -x["published"].timestamp()))
    limit = cfg.get("url_max_per_run", 600)
    left, todo = max(0, len(todo) - limit), todo[:limit]
    deadline = time.monotonic() + cfg.get("url_budget_seconds", 240)
    new = failed = 0
    stopped = None
    with ThreadPoolExecutor(max_workers=cfg.get("url_workers", 5)) as pool:
        for i in range(0, len(todo), BATCH_SIZE):
            if time.monotonic() > deadline:
                stopped = "超過時間上限"
                break
            chunk = todo[i:i + BATCH_SIZE]
            ids = [gn_article_id(it["link"]) for it in chunk]
            urls = decode_chunk(pool, ids, notes)
            for it, g in zip(chunk, ids):
                if urls.get(g):
                    it["url"], new = urls[g], new + 1
                else:
                    failed += 1
            if not urls:
                stopped = "整批 10 則都解不開（可能被 Google 限速）"
                break
    left += len(todo) - new - failed
    if stopped:
        notes.append(f"{stopped}，剩 {left} 則留到下次")
    ok = sum(1 for it in target if it["url"])
    return {"hours": cfg.get("url_hours", 36), "target": len(target), "ok": ok, "cached": cached, "new": new,
            "failed": failed, "left": left, "stopped": stopped}


# ---------------------------------------------------------------- 發布時間可疑（Google 重新索引）
DATE_PATTERNS = (  # 原文頁的發布時間，依序找
    ("datePublished", r'"datePublished"\s*:\s*"([^"]+)"'),
    ("article:published_time", r'<meta[^>]+(?:property|name)="(?:article:published_time|og:published_time)"[^>]*'
                               r'content="([^"]+)"'),
    ("article:published_time", r'<meta[^>]+content="([^"]+)"[^>]+(?:property|name)="(?:article:published_time|'
                               r'og:published_time)"'),
    ("itemprop datePublished", r'<meta[^>]+itemprop="datePublished"[^>]+content="([^"]+)"'),
    ("meta pubdate", r'<meta[^>]+name="(?:pubdate|publishdate|date)"[^>]+content="([^"]+)"'),
)


def batch_keys(fresh, minimum):
    """同一次抓取（加上上一次留下、還沒檢查過的）中，同一來源有 minimum 則以上（不同標題）的發布時間完全相同
    → {(來源, 時間): 則數}。官方不算。
    Google News 重新索引時會把一批舊文標成同一個時間（10/26 經濟日報 52 則都是 10/24 17:00，實際是 10/13～10/22）。"""
    groups = {}
    for it in fresh:
        if it.get("tier") != 1:
            groups.setdefault((it["source"], it["published"]), set()).add(it["title"].lower())
    return {k: len(v) for k, v in groups.items() if len(v) >= minimum}


def parse_when(text, default_tz):
    """ISO 8601（+08:00、+0800、Z）或「YYYY-MM-DD HH:MM[:SS]」；沒有時區就用 default_tz。"""
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text.strip().replace("Z", "+00:00"))
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d"):
        try:
            t = dt.datetime.fromisoformat(s) if fmt is None else dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
        return (t if t.tzinfo else t.replace(tzinfo=default_tz)).astimezone(UTC)
    return None


def page_date(html, default_tz):
    for method, pat in DATE_PATTERNS:
        m = re.search(pat, html, re.I)
        t = parse_when(m.group(1), default_tz) if m else None
        if t:
            return t, method
    return None, None


def url_date(url, default_tz):
    """網址裡的日期（/2026/10/26/、/news/20261025700017），只有日期，時間記當地 00:00。"""
    path = urllib.parse.urlparse(url or "").path
    m = (re.search(r"/(20\d{2})/(\d{1,2})/(\d{1,2})(?:/|$)", path)
         or re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})", path))
    if not m:
        return None
    try:
        return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=default_tz).astimezone(UTC)
    except ValueError:
        return None


def recover_date(url, default_tz, now):
    """讀原文頁找發布時間；找不到再看網址裡的日期。回傳 (UTC 時間, 方法)，都找不到就丟例外。"""
    t, method = None, None
    try:
        t, method = page_date(fetch_page(url).decode("utf-8", "replace"), default_tz)
    except Exception as ex:  # noqa: BLE001 — 讀不到原文頁就看網址
        method = f"原文頁讀取失敗（{str(ex)[:80]}）"
    if not t:
        t = url_date(url, default_tz)
        method = "網址日期" if t else method
    if not t:
        raise RuntimeError(method or "原文頁與網址都沒有日期")
    if not now - dt.timedelta(days=400) <= t <= now + dt.timedelta(hours=2):
        raise RuntimeError(f"找到的日期不合理（{utc_text(t)}）")
    return t, method


def check_times(items, fresh, cfg, now, cache, notes):
    """偵測批次相同時間的標題（time_suspect），從原文找回日期：找得回來就改正 published 並記 time_fixed_from，
    改正後超出時間窗的直接拿掉；找不回來的移出 items（回傳 moved，放在頂層 suspect_items）。
    沒有原文網址的先解開；結果存在 cache（依 id），下次沿用。每次最多 time_check_max 則、time_check_seconds 秒。"""
    minimum = cfg.get("time_suspect_min", 5)
    keys = batch_keys(fresh, minimum)
    stats = {"threshold": minimum,
             "groups": [{"source": s, "published_taipei": tpe(p), "count": n} for (s, p), n in sorted(keys.items())],
             "detected": 0, "fixed": 0, "changed": 0, "fixed_out_of_window": 0, "removed": 0, "cached": 0}

    def suspect(it):
        return it["tier"] != 1 and not it.get("time_fixed_from") and (it["source"], it["published"]) in keys

    suspects = [it for it in items if suspect(it)]
    stats["detected"] = len(suspects)
    if not suspects:
        return items, [], stats
    results, todo = {}, []  # results：id → (時間或 None, 方法或原因)
    for it in suspects:
        c = cache.get(item_id(it)) or {}
        if c.get("published_utc"):
            t = dt.datetime.strptime(c["published_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            results[item_id(it)] = (t, c["by"])
            stats["cached"] += 1
        elif c.get("attempts", 0) >= 3:
            results[item_id(it)] = (None, c.get("reason", "找不回日期"))
            stats["cached"] += 1
        else:
            todo.append(it)
    limit = cfg.get("time_check_max", 200)
    for it in todo[limit:]:
        results[item_id(it)] = (None, "超過每次檢查的數量上限，下次再試")
    todo = todo[:limit]
    tried = {item_id(it) for it in todo}
    deadline = time.monotonic() + cfg.get("time_check_seconds", 120)
    with ThreadPoolExecutor(max_workers=cfg.get("url_workers", 5)) as pool:
        need = [it for it in todo if not it.get("url") and gn_article_id(it.get("link"))]
        for i in range(0, len(need), BATCH_SIZE):
            if time.monotonic() > deadline:
                notes.append("解原文網址超過時間上限")
                break
            chunk = need[i:i + BATCH_SIZE]
            urls = decode_chunk(pool, [gn_article_id(it["link"]) for it in chunk], notes)
            for it in chunk:
                it["url"] = urls.get(gn_article_id(it["link"]))
        futures = {}
        for it in todo:
            if it.get("url"):
                tz = TPE if it["lang"] == "zh" or domain(it["url"]).endswith(".tw") else UTC
                futures[item_id(it)] = pool.submit(recover_date, it["url"], tz, now)
        for it in todo:
            f = futures.get(item_id(it))
            if f is None:
                results[item_id(it)] = (None, "沒有原文網址")
                continue
            try:
                results[item_id(it)] = f.result(timeout=max(1.0, deadline - time.monotonic()))
            except Exception as ex:  # noqa: BLE001
                results[item_id(it)] = (None, str(ex)[:120] or type(ex).__name__)
    since = now - dt.timedelta(hours=cfg["window_hours"])
    kept, moved = [], []
    for it in items:
        if not suspect(it):
            kept.append(it)
            continue
        t, how = results[item_id(it)]
        old = cache.get(item_id(it)) or {}
        if t is None:
            cache[item_id(it)] = {"reason": how, "attempts": old.get("attempts", 0) + (item_id(it) in tried),
                                  "checked_at": utc_text(now)}
            moved.append(dict(it, time_suspect=True, suspect_reason=how))
            continue
        cache[item_id(it)] = {"published_utc": utc_text(t), "by": how, "checked_at": utc_text(now)}
        stats["fixed"] += 1
        if abs((t - it["published"]).total_seconds()) >= 60:
            stats["changed"] += 1
        it["time_fixed_from"] = {"published_utc": utc_text(it["published"]), "published_taipei": tpe(it["published"]),
                                 "by": how}
        it["published"] = t
        if t >= since:
            kept.append(it)
        else:
            stats["fixed_out_of_window"] += 1
    stats["removed"] = len(moved)
    return sorted(kept, key=lambda x: x["published"], reverse=True), moved, stats


def load_check_cache(path, now, days=14):
    """上一次輸出的 time_check_cache，只留最近 days 天檢查過的。"""
    try:
        cache = json.load(open(path, encoding="utf-8")).get("time_check_cache", {})
    except Exception:  # noqa: BLE001
        return {}
    since = utc_text(now - dt.timedelta(days=days))
    return {k: v for k, v in cache.items() if v.get("checked_at", "") >= since}


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
            "lang": it["lang"], "title": it["title"], "link": it["link"], "url": it.get("url"), "symbols": it["symbols"],
            "topics": it["topics"], "queries": it["queries"], "title_queries": it["title_queries"],
            "title_match": it["title_match"], "also": it["also"], "time_fixed_from": it.get("time_fixed_from")}


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
                    "lang": it.get("lang", "en"), "queries": list(queries), "also": list(it.get("also", [])),
                    "url": it.get("url"), "time_fixed_from": it.get("time_fixed_from")})
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
    official_cna = [it for it in doc["items"] if it["source"] == "中央社" and any(q.startswith("cna_") for q in it["queries"])]
    if official_cna:
        lines += ["## 中央社（官方 RSS，資料出處：中央通訊社）", ""]
        lines += [f"- {it['published_taipei']} {it['title']}" for it in official_cna[:20]]
        if len(official_cna) > 20:
            lines.append(f"- …另有 {len(official_cna) - 20} 則（見 headlines.json）")
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
                            "errors": len(doc["errors"]), "url_ok": doc["stats"]["url"]["ok"],
                            "url_total": doc["stats"]["url"]["target"],
                            "time_suspect_detected": doc["stats"]["time_check"]["detected"],
                            "time_fixed": doc["stats"]["time_check"]["fixed"],
                            "time_removed": doc["stats"]["time_check"]["removed"]}
    cna_feeds = [f for f in doc["feeds"] if f["name"].startswith("中央社 RSS")]
    if cna_feeds:
        mine = [it for it in doc["items"] if it["source"] == "中央社" and any(q.startswith("cna_") for q in it["queries"])]
        ok = all(f["ok"] for f in cna_feeds)
        ds["news/cna"] = {"status": "ok" if ok else ("stale" if mine else "fail"), "source": "中央社官方 RSS",
                          "last": max((it["published_taipei"] for it in mine), default=None), "items": len(mine),
                          "feeds_ok": f"{sum(f['ok'] for f in cna_feeds)}/{len(cna_feeds)}",
                          "checked_at_taipei": doc["generated_at_taipei"]}
    fed_ok = all(f["ok"] for f in fed_doc["feeds"])
    ds["news/fed"] = {"status": "ok" if fed_ok else "fail", "source": "federalreserve.gov RSS",
                      "last": fed_doc["items"][0]["date"] if fed_doc["items"] else None,
                      "checked_at_taipei": fed_doc["generated_at_taipei"], "items": len(fed_doc["items"])}
    write_json(META, meta)


def main():
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))["news"]
    errors = []
    fresh, feeds = google_news(cfg, errors)
    cna_items, cna_feeds = cna(cfg, errors)
    fed_items, fed_feeds = fed(cfg, errors)
    if not any(f["ok"] for f in feeds):
        print("Google News 查詢全部失敗，保留上一次的檔案", file=sys.stderr)
        for e in errors:
            print(" -", e, file=sys.stderr)
        sys.exit(1)
    previous = load_previous(OUT_JSON, cfg["tiers"])
    fresh += cna_items
    items = build(cfg, fresh, previous, fed_items, NOW)
    check_cache, time_notes = load_check_cache(OUT_JSON, NOW), []
    # 上一次留下、還沒檢查過的也一起算：同一批舊文下一次可能被標成另一個時間（10/26 08:20 是 10/24 17:00，
    # 17:06 變成 10/25 17:00），合併時會保留較早的那個時間
    unchecked = [p for p in previous if not p.get("time_fixed_from")]
    items, suspects, time_stats = check_times(items, fresh + unchecked, cfg, NOW, check_cache, time_notes)
    time_stats["notes"] = time_notes
    url_notes = []
    url_stats = resolve_urls(items, cfg, NOW, {item_id(p): p["url"] for p in previous if p.get("url")}, url_notes)
    url_stats["notes"] = url_notes
    by_tier, by_symbol, by_topic = {}, {}, {}
    for it in items:
        by_tier[str(it["tier"])] = by_tier.get(str(it["tier"]), 0) + 1
        for s in it["symbols"]:
            by_symbol[s] = by_symbol.get(s, 0) + 1
        for t in it["topics"]:
            by_topic[t] = by_topic.get(t, 0) + 1
    all_feeds = feeds + cna_feeds + fed_feeds
    doc = {"generated_at_taipei": tpe(NOW), "generated_at_utc": utc_text(NOW), "window_hours": cfg["window_hours"],
           "status": "ok" if not errors else "partial", "tiers": {str(k): v for k, v in TIER_NAMES.items()},
           "feeds": all_feeds, "errors": errors,
           "stats": {"total": len(items), "by_tier": by_tier, "by_symbol": by_symbol, "by_topic": by_topic,
                     "url": url_stats, "time_check": time_stats},
           "sec_8k": {"status": "未啟用", "note": "等存聯絡信箱的 Actions secret 建好後加入"},
           "attribution": {"中央社": "資料出處：中央通訊社。依中央社 RSS 使用規範（個人、非營利之非商業用途），"
                                     "只用標題與連結，不轉載全文"},
           "items": [serialize(it) for it in items],
           "suspect_items": [dict(serialize(it), time_suspect=True, suspect_reason=it["suspect_reason"])
                             for it in suspects],
           "time_check_cache": check_cache}
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
          "feeds ok", f"{sum(f['ok'] for f in all_feeds)}/{len(all_feeds)}",
          "url", json.dumps(url_stats, ensure_ascii=False),
          "time_check", json.dumps({k: v for k, v in time_stats.items() if k != "groups"}, ensure_ascii=False))
    for e in errors:
        print(" -", e)


if __name__ == "__main__":
    main()

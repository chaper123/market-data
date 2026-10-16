"""fetch.py 與各資料模組共用的常數與工具（只用 Python 標準庫）。

fetch.py 以腳本執行，模組若 import fetch 會再載入一份（META 變成兩份），所以共用的東西放這裡。
"""
import csv
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
TPE = ZoneInfo("Asia/Taipei")
NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
NOW = dt.datetime.now(UTC)


# ---------------------------------------------------------------- HTTP
def http(url, data=None, headers=None, tries=3, timeout=25, opener=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data
    if body is not None:
        h.setdefault("Content-Type", "application/json")
    open_url = opener.open if opener else urllib.request.urlopen
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with open_url(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            if e.code in (400, 404):
                return e.code, raw
            last = f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001 — 任何網路錯誤都重試
            last = f"{type(e).__name__}: {e}"
        time.sleep(2 + 3 * i)
    raise RuntimeError(f"{url} -> {last}")


def cookie_opener():
    """需要先拿 cookie 的網站（Yahoo crumb、統一投信）用。"""
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def get_json(url, **kw):
    status, raw = http(url, **kw)
    if status != 200:
        raise RuntimeError(f"{url} -> HTTP {status}: {raw[:200]!r}")
    return json.loads(raw.decode("utf-8-sig"))


def num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace(",", "").strip()
    if s in ("", "--", "---", "-", "X", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------- 共用：日線整理
def tz_by_weekday(ts, candidates):
    """依「週末日期最少」選出 unix 時間戳要用哪個時區轉成日期（各家 API 的 K 棒時間錨點不同）。"""
    best, best_bad = candidates[0], None
    for tz in candidates:
        bad = sum(1 for t in ts if dt.datetime.fromtimestamp(t, tz).weekday() >= 5)
        if best_bad is None or bad < best_bad:
            best, best_bad = tz, bad
    return best


def finalize(rows, cutoff, min_date=None):
    """rows: {date: (date, vol, o, h, l, c)} → 去掉未收盤與上市前資料，排序。"""
    out = []
    for d, r in sorted(rows.items()):
        if d > cutoff or (min_date and d < min_date):
            continue
        if None in r[2:] or r[5] <= 0:
            continue
        out.append(r)
    return out


def write_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        for d, v, o, h, l, c in rows:
            w.writerow([d, int(v or 0), o, h, l, c])
    os.replace(tmp, path)


def us_cutoff():
    ny = NOW.astimezone(NY)
    day = ny if (ny.hour, ny.minute) >= (16, 15) else ny - dt.timedelta(days=1)
    return day.strftime("%Y-%m-%d")


def tw_cutoff():
    tp = NOW.astimezone(TPE)
    day = tp if (tp.hour, tp.minute) >= (14, 30) else tp - dt.timedelta(days=1)
    return day.strftime("%Y-%m-%d")


def summary(rows, source):
    return {"status": "ok", "source": source, "rows": len(rows),
            "first": rows[0][0] if rows else None, "last": rows[-1][0] if rows else None,
            "last_close": rows[-1][5] if rows else None,
            "prev_close": rows[-2][5] if len(rows) >= 2 else None}


def cross_check(meta, rows_a, rows_b, name_b):
    """主來源與備援來源比對同一天的收盤。"""
    if not rows_a or not rows_b:
        meta["cross_check"] = f"{name_b} 無資料，未核對"
        return
    b = {r[0]: r[5] for r in rows_b}
    d = rows_a[-1][0]
    if d not in b:
        meta["cross_check"] = f"{name_b} 無 {d} 資料（{name_b} 最後 {rows_b[-1][0]}）"
        meta["cross_check_ok"] = rows_b[-1][0] < d  # 備援較舊不算衝突
        return
    diff = (rows_a[-1][5] / b[d] - 1) * 100
    meta["cross_check"] = f"{d} 收盤 {rows_a[-1][5]} vs {name_b} {b[d]}（差 {diff:+.2f}%）"
    meta["cross_check_ok"] = abs(diff) <= 0.5


# ---------------------------------------------------------------- 鉅亨網日線
def cnyes_daily(symbol, tz_candidates):
    j = get_json(f"https://ws.api.cnyes.com/ws/api/v1/charting/history?resolution=D&symbol={symbol}&quote=1")
    d = j.get("data") or {}
    t = d.get("t") or []
    if not t:
        raise RuntimeError(f"cnyes {symbol}: 無資料")
    tz = tz_by_weekday(t, tz_candidates)
    rows = {}
    for i, ts in enumerate(t):
        day = dt.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")
        rows[day] = (day, num(d["v"][i]) or 0, num(d["o"][i]), num(d["h"][i]), num(d["l"][i]), num(d["c"][i]))
    quote = d.get("quote") or {}
    return rows, {"prev_close_field21": num(quote.get("21")), "tz_used": str(tz)}


# ---------------------------------------------------------------- Yahoo 日線
def yahoo_daily(sym):
    q_sym = urllib.parse.quote(sym, safe="")  # ^GSPC、GC=F 這類代號要編碼
    j = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{q_sym}?range=2y&interval=1d&includePrePost=false")
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        raise RuntimeError(f"yahoo {sym}: {(j.get('chart') or {}).get('error')}")
    r = res[0]
    tzname = (r.get("meta") or {}).get("exchangeTimezoneName") or "America/New_York"
    tz = ZoneInfo(tzname)
    q = r["indicators"]["quote"][0]
    rows = {}
    for i, ts in enumerate(r.get("timestamp") or []):
        day = dt.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")
        rows[day] = (day, num(q["volume"][i]) or 0, num(q["open"][i]), num(q["high"][i]),
                     num(q["low"][i]), num(q["close"][i]))
    return rows


# ---------------------------------------------------------------- Phase 1 資料模組共用
def read_rows(path):
    """讀無表頭 CSV；檔案不存在回傳空串列。"""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return [r for r in csv.reader(f) if r]
    except FileNotFoundError:
        return []


def write_rows(path, rows):
    """寫無表頭 CSV（先寫 .tmp 再取代，避免寫一半）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    os.replace(tmp, path)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


_PREV_META = None


def prev_meta(key):
    """上一次 meta.json 裡這個資料集的紀錄（本次失敗時沿用它的 source、last）。"""
    global _PREV_META
    if _PREV_META is None:
        try:
            _PREV_META = json.load(open(os.path.join(DATA, "meta.json"), encoding="utf-8")).get("datasets", {})
        except Exception:  # noqa: BLE001
            _PREV_META = {}
    return _PREV_META.get(key) or {}


def mark_failed(meta, key, path, label, ex):
    """失敗：保留舊檔，meta 標 stale（有舊檔）或 fail，並帶上一次成功的 source、last。"""
    meta["errors"].append(f"{label}: {ex}")
    m = {"status": "stale" if os.path.exists(path) else "fail", "error": str(ex)[:300]}
    old = prev_meta(key)
    for k in ("source", "last"):
        if old.get(k):
            m[k] = old[k]
    meta["datasets"][key] = m

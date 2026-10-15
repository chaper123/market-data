"""抓取行情數據，輸出成排程任務 monitor.py / monitor_us.py 直接能讀的 CSV。

只用 Python 標準庫。每個資料集都有主來源與備援來源；兩個來源都抓得到時互相核對最後收盤。
抓取失敗時保留上一次的檔案，並在 data/meta.json 標示 stale，排程任務據此決定是否改用舊方法。

輸出：
  data/us/<代號>.csv       date,volume,open,high,low,close（無表頭，只含已收盤的交易日）
  data/tw/<代號>_norm.csv  同上（只含已收盤的交易日；盤中價由排程任務另外抓）
  data/fx/usdtwd_mid.csv   date,rate（Yahoo 美元兌台幣收盤，市場中間價；台銀即期賣出由排程任務自己抓）
  data/tv/snapshot.json    TradingView 日線技術面快照（radar.py 使用）
  data/meta.json           每個資料集的來源、列數、日期、核對結果與錯誤

用法：python fetch.py [--skip-if-fresh]
  --skip-if-fresh：meta.json 顯示今天（台北時間）已成功抓過就直接結束（給第二次排程當重試用）。
"""
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
TPE = ZoneInfo("Asia/Taipei")
NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
NOW = dt.datetime.now(UTC)

META = {"generated_at_utc": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_taipei": NOW.astimezone(TPE).strftime("%Y-%m-%d %H:%M"),
        "datasets": {}, "errors": []}


# ---------------------------------------------------------------- HTTP
def http(url, data=None, headers=None, tries=3, timeout=25):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data
    if body is not None:
        h.setdefault("Content-Type", "application/json")
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
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


# ---------------------------------------------------------------- Yahoo 日線（美股備援 / 核對）
def yahoo_daily(sym):
    j = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=2y&interval=1d&includePrePost=false")
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


def fetch_us(cfg):
    cut = us_cutoff()
    META["us_cutoff_ny"] = cut
    for sym, e in cfg["us"].items():
        m, rows, alt = {}, None, None
        path = os.path.join(DATA, "us", f"{sym}.csv")
        try:
            raw, extra = cnyes_daily(f"USS:{sym}:STOCK", [UTC, NY, TPE])
            rows = finalize(raw, cut, e.get("min_date"))
            m = summary(rows, "cnyes")
            m.update(extra)
        except Exception as ex:  # noqa: BLE001
            META["errors"].append(f"US {sym} cnyes: {ex}")
        try:
            alt = finalize(yahoo_daily(sym), cut, e.get("min_date"))
        except Exception as ex:  # noqa: BLE001
            META["errors"].append(f"US {sym} yahoo: {ex}")
        if (not rows or len(rows) < 15) and alt:
            rows, m = alt, summary(alt, "yahoo")
            alt = None
        if rows:
            if alt:
                cross_check(m, rows, alt, "yahoo")
            write_csv(path, rows)
        else:
            m = {"status": "stale" if os.path.exists(path) else "fail", "source": None}
        META["datasets"][f"us/{sym}"] = m
        time.sleep(0.8)


# ---------------------------------------------------------------- 台股日線
def months_back(n):
    tp = NOW.astimezone(TPE)
    y, mo = tp.year, tp.month
    out = []
    for _ in range(n):
        out.append((y, mo))
        mo -= 1
        if mo == 0:
            y, mo = y - 1, 12
    return out


def roc_date(s):
    m = re.match(r"\s*(\d{2,3})/(\d{1,2})/(\d{1,2})", str(s))
    if not m:
        return None
    return f"{int(m.group(1)) + 1911:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def pick(fields, *keys):
    for i, f in enumerate(fields):
        if any(k in f for k in keys):
            return i
    return None


def twse_daily(code, n_months):
    rows = {}
    for y, mo in months_back(n_months):
        j = None
        for url in (f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={y}{mo:02d}01&stockNo={code}&response=json",
                    f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={y}{mo:02d}01&stockNo={code}"):
            try:
                j = get_json(url, headers={"Referer": "https://www.twse.com.tw/"})
                if j.get("stat") == "OK" or j.get("data"):
                    break
            except Exception:  # noqa: BLE001
                j = None
            time.sleep(1.5)
        if not j or not j.get("data"):
            time.sleep(2)
            continue
        f = j.get("fields") or ["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價"]
        idx = dict(d=pick(f, "日期"), v=pick(f, "成交股數"), o=pick(f, "開盤"), h=pick(f, "最高"),
                   l=pick(f, "最低"), c=pick(f, "收盤"))
        for r in j["data"]:
            day = roc_date(r[idx["d"]])
            if day:
                rows[day] = (day, num(r[idx["v"]]) or 0, num(r[idx["o"]]), num(r[idx["h"]]),
                             num(r[idx["l"]]), num(r[idx["c"]]))
        time.sleep(2.5)  # 證交所有頻率限制
    if not rows:
        raise RuntimeError(f"TWSE {code}: 無資料")
    return rows


def fix_volume_units(rows):
    """鉅亨網台股日線偶爾某天用「股」而不是「張」。成交量是前後中位數的 300 倍以上、
    除以 1000 後又落在正常範圍時，視為單位錯誤並除以 1000。回傳修正後的列與被修正的日期。"""
    vols = [r[1] for r in rows]
    out, fixed = [], []
    for i, r in enumerate(rows):
        near = sorted(v for v in vols[max(0, i - 10):i] + vols[i + 1:i + 11] if v)
        med = near[len(near) // 2] if near else 0
        if med and r[1] > 300 * med and 0.1 * med <= r[1] / 1000 <= 10 * med:
            out.append((r[0], r[1] / 1000, *r[2:]))
            fixed.append(r[0])
        else:
            out.append(r)
    return out, fixed


def fetch_tw(cfg):
    cut = tw_cutoff()
    META["tw_cutoff_taipei"] = cut
    n = cfg.get("tw_months", 7)
    for code, e in cfg["tw"].items():
        path = os.path.join(DATA, "tw", f"{code}_norm.csv")
        got, extra = [], {}
        # 依序嘗試：證交所（上市）→ 鉅亨網 → Yahoo；第一個列數足夠的當主來源，下一個用來核對
        if e["market"] == "TWSE":
            try:
                got.append(("TWSE", finalize(twse_daily(code, n), cut, e.get("min_date"))))
            except Exception as ex:  # noqa: BLE001
                META["errors"].append(f"TW {code} TWSE: {ex}")
        try:
            raw, extra = cnyes_daily(f"TWS:{code}:STOCK", [TPE, UTC])
            fixed_rows = finalize(raw, cut, e.get("min_date"))
            fixed_rows, fixed = fix_volume_units(fixed_rows)
            if fixed:
                extra["volume_units_fixed"] = fixed
            got.append(("cnyes", fixed_rows))
        except Exception as ex:  # noqa: BLE001
            META["errors"].append(f"TW {code} cnyes: {ex}")
        try:
            suffix = ".TW" if e["market"] == "TWSE" else ".TWO"
            got.append(("yahoo", finalize(yahoo_daily(code + suffix), cut, e.get("min_date"))))
        except Exception as ex:  # noqa: BLE001
            META["errors"].append(f"TW {code} yahoo: {ex}")
        got = [(name, rows) for name, rows in got if rows]
        most = max((len(r) for _, r in got), default=0)
        rows, m = None, {}
        for i, (name, r) in enumerate(got):
            if len(r) >= min(60, most - 2):
                rows, m = r, summary(r, name)
                if name == "cnyes":
                    m.update(extra)
                others = [x for j, x in enumerate(got) if j != i]
                if others:
                    cross_check(m, rows, others[0][1], others[0][0])
                else:
                    m["cross_check"] = "無第二來源，未核對"
                break
        if rows:
            write_csv(path, rows)
        else:
            m = {"status": "stale" if os.path.exists(path) else "fail", "source": None}
        META["datasets"][f"tw/{code}"] = m


def fetch_tw_quotes(cfg):
    """盤中快照（備援用；排程任務仍以執行當下的即時報價為主）。"""
    syms = [f"TWS:{c}:STOCK" for c in cfg["tw"]] + [f"TWS:{cfg['tw_index']}:INDEX"]
    try:
        j = get_json("https://ws.api.cnyes.com/ws/api/v1/quote/quotes/" + ",".join(syms))
        out = {}
        for q in j.get("data") or []:
            sym = q.get("0") or ""
            ts = q.get("200007")
            out[sym] = {"price": num(q.get("6")), "change": num(q.get("11")), "change_pct": num(q.get("56")),
                        "open": num(q.get("19")), "high": num(q.get("12")), "low": num(q.get("13")),
                        "prev_close": num(q.get("21")), "volume_lots": num(q.get("200013")),
                        "time_taipei": dt.datetime.fromtimestamp(int(ts), TPE).strftime("%Y-%m-%d %H:%M") if ts else None}
        os.makedirs(os.path.join(DATA, "tw"), exist_ok=True)
        with open(os.path.join(DATA, "tw", "quotes.json"), "w") as f:
            json.dump({"fetched_at_taipei": META["generated_at_taipei"], "quotes": out}, f, ensure_ascii=False, indent=1)
        META["datasets"]["tw/quotes"] = {"status": "ok", "source": "cnyes", "symbols": len(out)}
    except Exception as ex:  # noqa: BLE001
        META["errors"].append(f"TW quotes: {ex}")
        META["datasets"]["tw/quotes"] = {"status": "fail"}



# ---------------------------------------------------------------- ETF 淨值（證交所基本市況報導網站）
def prev_trading_day(code, day):
    """用 repo 的台股日線找 day 之前最近一個交易日。"""
    path = os.path.join(DATA, "tw", f"{code}_norm.csv")
    try:
        days = [r[0] for r in csv.reader(open(path)) if r and r[0] < day]
        return max(days) if days else None
    except OSError:
        return None


def close_on(code, day):
    path = os.path.join(DATA, "tw", f"{code}_norm.csv")
    try:
        for r in csv.reader(open(path)):
            if r and r[0] == day:
                return float(r[5])
    except OSError:
        pass
    return None


def fetch_nav(cfg):
    """mis.twse.com.tw 的 ETF 淨值資訊（上市、上櫃 ETF 都有）。欄位 h 是「前一營業日單位淨值」。
    每次把各檔的 (淨值日期, 淨值) 累積到 data/tw/nav_history.csv，再用同一天的官方收盤算折溢價。"""
    raw_path = os.path.join(DATA, "tw", "nav_raw.json")
    hist_path = os.path.join(DATA, "tw", "nav_history.csv")
    try:
        ts = int(NOW.timestamp() * 1000)
        status, raw = http(f"https://mis.twse.com.tw/stock/data/all_etf.txt?{ts}",
                           headers={"Referer": "https://mis.twse.com.tw/stock/etf_nav.jsp?ex=tse"})
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        j = json.loads(raw.decode("utf-8-sig"))
        found = {}
        for grp in j.get("a1") or []:
            for m in grp.get("msgArray") or []:
                if m.get("a") in cfg["tw"]:
                    found[m["a"]] = m
        os.makedirs(os.path.dirname(raw_path), exist_ok=True)
        with open(raw_path, "w") as fh:
            json.dump({"fetched_at_taipei": META["generated_at_taipei"], "items": found}, fh, ensure_ascii=False, indent=1)
        hist = {}
        if os.path.exists(hist_path):
            for r in csv.reader(open(hist_path)):
                if len(r) >= 3:
                    hist[(r[0], r[1])] = r
        added = {}
        for code, m in found.items():
            nav = num(m.get("h"))
            day = m.get("i") or ""
            day = f"{day[:4]}-{day[4:6]}-{day[6:8]}" if re.fullmatch(r"\d{8}", day) else None
            nav_day = prev_trading_day(code, day) if day else None
            if nav and nav_day:
                px = close_on(code, nav_day)
                prem = round((px / nav - 1) * 100, 2) if px else ""
                hist[(nav_day, code)] = [nav_day, code, nav, px if px else "", prem, "mis.twse 前一營業日淨值"]
                added[code] = {"nav_date": nav_day, "nav": nav, "close": px, "premium_pct": prem}
        rows = sorted(hist.values())
        with open(hist_path, "w", newline="") as fh:
            w = csv.writer(fh)
            for r in rows:
                w.writerow(r)
        META["datasets"]["tw/nav"] = {"status": "ok" if added else "fail", "source": "mis.twse.com.tw all_etf",
                                      "latest": added}
        if not added:
            META["errors"].append(f"NAV: 找到 {list(found)}，但沒有可用的淨值欄位")
    except Exception as ex:  # noqa: BLE001
        META["errors"].append(f"NAV: {ex}")
        META["datasets"]["tw/nav"] = {"status": "stale" if os.path.exists(hist_path) else "fail"}


# ---------------------------------------------------------------- 台銀匯率
def fetch_fx():
    """台銀網站會對 GitHub 伺服器回傳防機器人驗證頁，不嘗試繞過。
    這裡改抓 Yahoo 的 TWD=X（市場中間價）當備援；排程任務仍以台銀即期賣出為主。"""
    path = os.path.join(DATA, "fx", "usdtwd_mid.csv")
    try:
        raw = yahoo_daily("TWD=X")
        rows = {d: r[5] for d, r in raw.items() if r[5]}
        if len(rows) < 100:
            raise RuntimeError(f"只有 {len(rows)} 列")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            for d in sorted(rows):
                w.writerow([d, round(rows[d], 4)])
        last = max(rows)
        META["datasets"]["fx/usdtwd_mid"] = {
            "status": "ok", "source": "Yahoo TWD=X 收盤（市場中間價，不是台銀即期賣出）",
            "rows": len(rows), "first": min(rows), "last": last, "last_rate": round(rows[last], 4)}
    except Exception as ex:  # noqa: BLE001
        META["errors"].append(f"FX yahoo: {ex}")
        META["datasets"]["fx/usdtwd_mid"] = {"status": "stale" if os.path.exists(path) else "fail"}


# ---------------------------------------------------------------- TradingView 技術面快照
TV_COLS = [
    "name", "description", "close", "open", "high", "low", "volume", "change", "change_abs",
    "Recommend.All", "Recommend.MA", "Recommend.Other",
    "RSI", "Stoch.K", "Stoch.D", "Stoch.RSI.K", "W.R", "MACD.macd", "MACD.signal",
    "ADX", "ADX+DI", "ADX-DI", "CCI20", "AO", "Mom", "BBPower", "UO", "ATR",
    "EMA10", "SMA10", "EMA20", "SMA20", "EMA30", "SMA30", "EMA50", "SMA50",
    "EMA100", "SMA100", "EMA200", "SMA200", "Ichimoku.BLine", "VWMA", "HullMA9",
    "Pivot.M.Classic.R1", "Pivot.M.Classic.Middle", "Pivot.M.Classic.S1",
    "BB.upper", "BB.lower", "Perf.W", "Perf.1M", "Perf.3M", "Perf.Y",
]


def tv_scan(market, tickers, cols):
    cols = list(cols)
    for _ in range(8):
        status, raw = http(f"https://scanner.tradingview.com/{market}/scan",
                           data={"symbols": {"tickers": tickers, "query": {"types": []}}, "columns": cols},
                           headers={"Origin": "https://www.tradingview.com", "Referer": "https://www.tradingview.com/"})
        if status == 200:
            j = json.loads(raw)
            return {row["s"]: dict(zip(cols, row["d"])) for row in j.get("data") or []}, cols
        msg = raw.decode("utf-8", "replace")
        bad = re.findall(r"[Uu]nknown field[^\"']*[\"']?([A-Za-z0-9_.+\-\[\]|]+)", msg)
        if bad and bad[0] in cols:
            cols.remove(bad[0])
            continue
        raise RuntimeError(f"TradingView {market}: HTTP {status} {msg[:200]}")
    raise RuntimeError(f"TradingView {market}: 欄位錯誤過多")


def fetch_tv(cfg):
    path = os.path.join(DATA, "tv", "snapshot.json")
    out, used_cols = {}, None
    plans = [("america", {s: [f"NASDAQ:{s}", f"NYSE:{s}", f"AMEX:{s}", f"CBOE:{s}"] for s in cfg["us"]}),
             ("taiwan", {c: [f"TWSE:{c}", f"TPEX:{c}"] for c in cfg["tw"]})]
    for market, cand in plans:
        try:
            got, used_cols = tv_scan(market, [t for v in cand.values() for t in v], TV_COLS)
            for code, opts in cand.items():
                hit = next((t for t in opts if t in got), None)
                if hit:
                    out[code] = {"tv_symbol": hit, "market": market, **got[hit]}
                else:
                    META["errors"].append(f"TradingView 找不到 {code}（試過 {', '.join(opts)}）")
        except Exception as ex:  # noqa: BLE001
            META["errors"].append(str(ex))
        time.sleep(1)
    if out:
        tp = NOW.astimezone(TPE)
        tw_session = tp.weekday() < 5 and (9, 0) <= (tp.hour, tp.minute) <= (13, 35)
        ny = NOW.astimezone(NY)
        us_session = ny.weekday() < 5 and (9, 30) <= (ny.hour, ny.minute) < (16, 0)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"fetched_at_taipei": META["generated_at_taipei"],
                       "tw_bar_is_intraday": tw_session,
                       "us_bar_is_intraday": us_session,
                       "note": "TradingView 匿名存取只有日線。*_bar_is_intraday=true 代表該市場當日 K 棒尚未收盤。",
                       "missing_columns": sorted(set(TV_COLS) - set(used_cols or TV_COLS)),
                       "symbols": out}, f, ensure_ascii=False, indent=1)
        META["datasets"]["tv/snapshot"] = {"status": "ok", "symbols": len(out), "tw_bar_is_intraday": tw_session,
                                           "us_bar_is_intraday": us_session}
    else:
        META["datasets"]["tv/snapshot"] = {"status": "stale" if os.path.exists(path) else "fail"}



# ---------------------------------------------------------------- main
def fresh_today():
    try:
        m = json.load(open(os.path.join(DATA, "meta.json")))
    except Exception:  # noqa: BLE001
        return False
    today = NOW.astimezone(TPE).strftime("%Y-%m-%d")
    core = [k for k in m.get("datasets", {}) if k.startswith(("us/", "tw/0"))]
    return (m.get("generated_at_taipei", "").startswith(today) and core
            and all(m["datasets"][k].get("status") == "ok" for k in core))


def main():
    if "--skip-if-fresh" in sys.argv and fresh_today():
        print("今天已成功抓取，略過")
        return
    cfg = json.load(open(os.path.join(ROOT, "config.json")))
    for step in (fetch_us, fetch_tw, fetch_tw_quotes, fetch_nav, fetch_tv):
        try:
            step(cfg)
        except Exception as ex:  # noqa: BLE001 — 單一步驟失敗不影響其他
            META["errors"].append(f"{step.__name__}: {ex}")
    for step in (fetch_fx,):
        try:
            step()
        except Exception as ex:  # noqa: BLE001
            META["errors"].append(f"{step.__name__}: {ex}")
    ds = META["datasets"]
    META["summary"] = {s: sum(1 for v in ds.values() if v.get("status") == s) for s in ("ok", "stale", "fail")}
    META["cross_check_conflicts"] = [k for k, v in ds.items() if v.get("cross_check_ok") is False]
    with open(os.path.join(DATA, "meta.json"), "w") as f:
        json.dump(META, f, ensure_ascii=False, indent=1)
    print(json.dumps(META["summary"], ensure_ascii=False), "errors:", len(META["errors"]))
    for e in META["errors"]:
        print(" -", e)


if __name__ == "__main__":
    main()

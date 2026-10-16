"""抓取行情數據，輸出成排程任務 monitor.py / monitor_us.py 直接能讀的 CSV。

只用 Python 標準庫。每個資料集都有主來源與備援來源；兩個來源都抓得到時互相核對最後收盤。
抓取失敗時保留上一次的檔案，並在 data/meta.json 標示 stale，排程任務據此決定是否改用舊方法。

輸出：
  data/us/<代號>.csv       date,volume,open,high,low,close（無表頭，只含已收盤的交易日）
  data/tw/<代號>_norm.csv  同上（只含已收盤的交易日；盤中價由排程任務另外抓）
  data/fx/usdtwd_mid.csv   date,rate（Yahoo 美元兌台幣收盤，市場中間價；台銀即期賣出由排程任務自己抓）
  data/tv/snapshot.json    TradingView 日線技術面快照（radar.py 使用）
  data/meta.json           每個資料集的來源、列數、日期、核對結果與錯誤
Phase 1 新資料由各模組產生（只在完整抓取時執行，失敗不影響上面的檔案）：
  macro.py                 data/macro/、data/sentiment/（指數與總經收盤、CNN 恐懼貪婪、台指 VIX）
  fxbot.py                 data/fx/usdtwd_bot_sell.csv（台銀美元即期賣出，FinMind）
  valuation.py             data/valuation/（估值 stockanalysis、下次財報日 Yahoo）
  calendars.py             data/calendar/calendar.json（台股休市、FOMC、財報日；CPI 未取得）
  chips.py                 data/chips/（關注代號三大法人、集中市場三大法人金額、融資）

用法：python fetch.py [--skip-if-fresh]
  --skip-if-fresh：meta.json 顯示日線已完整（美股、台股最後日期都到台北今天的前一個平日且狀態 ok）就只更新淨值（給後續排程當重試用）。
"""
import csv
import datetime as dt
import json
import os
import re
import sys
import time

from common import (DATA, NOW, NY, ROOT, TPE, UTC, cnyes_daily, cross_check, finalize, get_json, http, num,
                    summary, tw_cutoff, us_cutoff, write_csv, yahoo_daily)
import calendars
import chips
import fxbot
import macro
import valuation

# Phase 1 新資料：放在既有日線與匯率之後，每步各自 try，簽名都是 step(cfg, META)
PHASE1_STEPS = (macro.fetch_macro, macro.fetch_cnn, macro.fetch_taifex_vix, fxbot.fetch_bot_fx,
                valuation.fetch_valuation, calendars.fetch_calendar,
                chips.fetch_chips)

META = {"generated_at_utc": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_taipei": NOW.astimezone(TPE).strftime("%Y-%m-%d %H:%M"),
        "datasets": {}, "errors": []}


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
def expected_last(today):
    """台北日期 today 之前最近的平日：美股應收盤到這天（前一個美東工作日），台股也是。
    不考慮假日；遇假日資料永遠比這天舊，只會多做幾次完整抓取，不會出錯。"""
    d = today - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def fresh_today():
    """日線已經完整才略過：us/*、tw/0* 全部 ok，且最後日期都到了 expected_last。
    只看 meta 產生日期不夠：2026-10-16 02:19 手動執行時美股 10/15 還沒收盤，
    之後三次排程都被略過，晨報讀到 10/14 的資料。"""
    try:
        m = json.load(open(os.path.join(DATA, "meta.json")))
    except Exception:  # noqa: BLE001
        return False
    ds = m.get("datasets", {})
    core = [k for k in ds if k.startswith(("us/", "tw/0"))]
    if not core or any(ds[k].get("status") != "ok" for k in core):
        return False
    want = expected_last(NOW.astimezone(TPE).date())
    return all((ds[k].get("last") or "") >= want for k in core)


def main():
    if "--skip-if-fresh" in sys.argv and fresh_today():
        # 日線今天已成功；淨值由投信在收盤後才公布，仍每次更新（晨報前的 08:41、09:47 都會跑到）
        global META
        meta_path = os.path.join(DATA, "meta.json")
        META = json.load(open(meta_path))
        META.setdefault("errors", [])
        fetch_nav(json.load(open(os.path.join(ROOT, "config.json"))))
        META["nav_refreshed_at_taipei"] = NOW.astimezone(TPE).strftime("%Y-%m-%d %H:%M")
        with open(meta_path, "w") as fh:
            json.dump(META, fh, ensure_ascii=False, indent=1)
        print("日線今天已成功抓取，只更新淨值")
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
    for step in PHASE1_STEPS:
        try:
            step(cfg, META)
        except Exception as ex:  # noqa: BLE001 — 新資料失敗不影響既有日線
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

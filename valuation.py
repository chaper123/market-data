"""Phase 1 ③：估值（stockanalysis）與下次財報日（Yahoo），由 fetch.py 在完整抓取時呼叫。

stockanalysis 與排程原本讀的是同一個網站，數字不會因換來源而跳動；Yahoo 的 Forward PE 預估期間不同
（NVDA 13.6 vs 18.0），不拿來核對本益比，只取財報日。核對改成兩件事：
  1. 頁面股價 vs repo 美股日線同一天收盤（頁面是收盤後版本時）
  2. 隱含預估 EPS（股價 ÷ 本益比）與一週前相比，變動超過 WEEK_LIMIT% 就標記，提醒人工確認

輸出：
  data/valuation/us.json         各檔最新估值：kind、fwd_pe、peg、pe、頁面股價與交易日、隱含 EPS 與一週前比較，
                                 以及 Yahoo 的下次財報日（earnings_dates、is_estimate）
  data/valuation/us_history.csv  date,symbol,price,fwd_pe,peg,pe（逐日累積；date 是頁面股價的交易日，空字串＝n/a）
"""
import datetime as dt
import json
import os
import re
import time
import urllib.parse

from common import (DATA, NOW, TPE, cookie_opener, get_json, http, mark_failed, num, read_rows, write_json,
                    write_rows)

SA_URL = {"stock": "https://stockanalysis.com/stocks/{}/statistics/", "etf": "https://stockanalysis.com/etf/{}/"}
QS_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{}?modules=calendarEvents&crumb={}"
WEEK_LIMIT = 20
JSON_PATH = os.path.join(DATA, "valuation", "us.json")
HIST_PATH = os.path.join(DATA, "valuation", "us_history.csv")


def sa_field(h, label):
    """stockanalysis 表格：標籤所在的 td 後面第一個 td 就是數值（n/a、- 回傳 None）。"""
    m = re.search(r">(?:<!---->)?\s*" + re.escape(label) + r"\s*(?:<!---->)?<.{0,400}?<td[^>]*>([^<]+)</td>", h, re.S)
    return num(m.group(1)) if m else None


def sa_quote(h):
    """頁面內嵌的報價：p 股價、u 時間文字、td 交易日、ms 市場狀態（open／closed）。"""
    m = re.search(r"quote:\{([^}]*)\}", h)
    if not m:
        return {}
    return {k: v.strip('"') for k, v in re.findall(r'(\w+):("[^"]*"|[-\d.]+)', m.group(1))}


def repo_close(sym, day):
    for r in read_rows(os.path.join(DATA, "us", f"{sym}.csv")):
        if r[0] == day:
            return float(r[5])
    return None


def week_ago(hist, sym, day):
    """歷史中同一檔、日期在 day 的 7 天前（含）以前最近的一列。"""
    limit = (dt.date.fromisoformat(day) - dt.timedelta(days=7)).isoformat()
    rows = [r for r in hist.values() if r[1] == sym and r[0] <= limit]
    return max(rows) if rows else None


def implied_eps(price, ratio):
    return price / ratio if price and ratio and ratio > 0 else None


def yahoo_earnings(symbols, meta):
    """Yahoo quoteSummary 要 cookie＋crumb：先到 fc.yahoo.com 拿 cookie（回 404 也會設），再換 crumb。"""
    key, out = "valuation/earnings", {}
    try:
        opener = cookie_opener()
        try:
            http("https://fc.yahoo.com/", tries=1, opener=opener)
        except Exception:  # noqa: BLE001 — 拿不到 cookie 時下一步會失敗，錯誤記在那裡
            pass
        status, raw = http("https://query1.finance.yahoo.com/v1/test/getcrumb", opener=opener)
        crumb = raw.decode("utf-8", "replace").strip()
        if status != 200 or not crumb or len(crumb) > 40 or "<" in crumb:
            raise RuntimeError(f"拿不到 crumb（HTTP {status}）")
        for sym in symbols:
            try:
                j = get_json(QS_URL.format(sym, urllib.parse.quote(crumb)), opener=opener)
                e = j["quoteSummary"]["result"][0]["calendarEvents"].get("earnings") or {}
                out[sym] = {"earnings_dates": [x["fmt"] for x in e.get("earningsDate") or []],
                            "is_estimate": e.get("isEarningsDateEstimate")}
            except Exception as ex:  # noqa: BLE001
                meta["errors"].append(f"earnings {sym}: {ex}")
            time.sleep(0.5)
        if not out:
            raise RuntimeError("沒有任何一檔取得財報日")
        meta["datasets"][key] = {"status": "ok", "source": "Yahoo quoteSummary calendarEvents",
                                 "symbols": len(out), "missing": [s for s in symbols if s not in out],
                                 "next": {s: (v["earnings_dates"] or [None])[0] for s, v in out.items()}}
    except Exception as ex:  # noqa: BLE001
        mark_failed(meta, key, JSON_PATH, "Yahoo earnings", ex)
    return out


def fetch_valuation(cfg, meta):
    syms = cfg["valuation"]
    try:
        old = json.load(open(JSON_PATH, encoding="utf-8")).get("symbols", {})
    except Exception:  # noqa: BLE001
        old = {}
    hist = {(r[0], r[1]): r for r in read_rows(HIST_PATH)}
    earnings = yahoo_earnings([s for s, k in syms.items() if k == "stock"], meta)
    snap = {}
    for sym, kind in syms.items():
        key, url = f"valuation/{sym}", SA_URL[kind].format(sym.lower())
        try:
            status, raw = http(url)
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            h = raw.decode("utf-8", "replace")
            fwd_pe, peg, pe = (sa_field(h, lab) for lab in ("Forward PE", "PEG Ratio", "PE Ratio"))
            q = sa_quote(h)
            price, day = num(q.get("p")), q.get("td")
            if not price or not day:
                raise RuntimeError("頁面沒有股價或交易日")
            ratio = fwd_pe if kind == "stock" else pe  # 排程的估值閘門：個股看 Forward PE，ETF 看本益比
            if ratio is None:
                raise RuntimeError(f"沒有{'Forward PE' if kind == 'stock' else 'PE Ratio'}")
            rec = {"kind": kind, "fwd_pe": fwd_pe, "peg": peg, "pe": pe, "price": price, "price_date": day,
                   "price_time": q.get("u"), "market_state": q.get("ms"), "source": url,
                   "implied_eps": round(implied_eps(price, ratio), 4)}
            m = {"status": "ok", "source": "stockanalysis", "last": day, "price": price,
                 "fwd_pe": fwd_pe, "peg": peg, "pe": pe}
            # 核對 1：頁面股價 vs repo 日線收盤
            close = repo_close(sym, day)
            if q.get("ms") != "closed":
                m["cross_check"] = f"頁面為盤中價（{q.get('u')}），未核對"
            elif close:
                diff = (price / close - 1) * 100
                m["cross_check"] = f"{day} 頁面股價 {price} vs repo 收盤 {close}（差 {diff:+.2f}%）"
                m["cross_check_ok"] = abs(diff) <= 0.5
            else:
                m["cross_check"] = f"repo 日線沒有 {day}，未核對"
            # 核對 2：隱含預估 EPS 與一週前比較
            ref = week_ago(hist, sym, day)
            ref_eps = implied_eps(num(ref[2]), num(ref[3] if kind == "stock" else ref[5])) if ref else None
            if ref_eps:
                chg = (rec["implied_eps"] / ref_eps - 1) * 100
                rec["week"] = {"ref_date": ref[0], "ref_ratio": num(ref[3] if kind == "stock" else ref[5]),
                               "eps_change_pct": round(chg, 2)}
                m["week_check"] = f"隱含 EPS 較 {ref[0]} {chg:+.1f}%（超過 ±{WEEK_LIMIT}% 要人工確認）"
                m["week_check_ok"] = abs(chg) <= WEEK_LIMIT
            else:
                m["week_check"] = "沒有一週前的紀錄，未比較"
            if sym in earnings:
                rec.update(earnings[sym])
            elif old.get(sym, {}).get("earnings_dates"):
                rec.update({k: old[sym].get(k) for k in ("earnings_dates", "is_estimate")}, earnings_stale=True)
            snap[sym] = rec
            hist[(day, sym)] = [day, sym, price] + ["" if v is None else v for v in (fwd_pe, peg, pe)]
            meta["datasets"][key] = m
        except Exception as ex:  # noqa: BLE001
            if sym in old:
                snap[sym] = dict(old[sym], stale=True)
            mark_failed(meta, key, JSON_PATH, f"valuation {sym}", ex)
        time.sleep(1)
    if snap:
        write_json(JSON_PATH, {"fetched_at_taipei": NOW.astimezone(TPE).strftime("%Y-%m-%d %H:%M"),
                               "week_limit_pct": WEEK_LIMIT, "symbols": snap})
        write_rows(HIST_PATH, sorted(hist.values()))

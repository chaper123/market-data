"""美股配息（由 fetch.py 在完整抓取時呼叫）。

來源：Yahoo chart API 加 events=div、range=5y（和 common.yahoo_daily 同一個端點）。
代號清單：config.json 的 us 裡 role 是 buy 的。

輸出 data/dividends/us.json：
  symbols[代號].events  每次配息 {ex_date, cash}，依除息日排序
      ex_date 是美東日期（Yahoo 的時間戳是除息日美東開盤時間，轉 America/New_York 才是正確的日子）
      cash 是每股美元，稅前，已依拆股調整（Yahoo 的數字）
  symbols[代號].ttm_cash、ttm_count  除息日在最近 365 天（含今天，美東日期）的合計與次數
  symbols[代號].last                 最近一次已除息的事件（沒有則 null）
Yahoo 沒有發放日也沒有未來的除息日，所以沒有 pay_date、next。
某一檔抓不到（或這次回來的事件比上一次的檔案少到全空）就沿用上一次那一檔的事件，事件不會消失。
"""
import datetime as dt
import json
import os
import time
import urllib.parse

from common import DATA, NOW, NY, TPE, get_json, mark_failed, num, write_json

OUT = os.path.join(DATA, "dividends", "us.json")
YEARS = 5


def load_previous(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("symbols", {})
    except (OSError, ValueError):
        return {}


def fetch_chart(sym):
    q_sym = urllib.parse.quote(sym, safe="")
    return get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{q_sym}"
                    f"?range={YEARS}y&interval=1d&events=div&includePrePost=false")


def parse_events(j, sym):
    """Yahoo chart 回應 → 依除息日排序的 [{ex_date, cash}]。沒有 events.dividends 就是沒有配息（空陣列）。
    回應裡沒有 result 當作失敗（丟例外），由呼叫端沿用上一次。"""
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        raise RuntimeError(f"yahoo {sym}: {(j.get('chart') or {}).get('error') or '沒有 result'}")
    divs = ((res[0].get("events") or {}).get("dividends")) or {}
    by_day = {}
    for key, d in divs.items():
        cash = num(d.get("amount"))
        ts = d.get("date", key)
        if not cash or cash <= 0:
            continue
        day = dt.datetime.fromtimestamp(int(ts), NY).strftime("%Y-%m-%d")
        by_day[day] = {"ex_date": day, "cash": round(cash, 6)}
    return [by_day[k] for k in sorted(by_day)]


def summarize(events, today):
    """events 已排序；today 是美東日期字串。"""
    past = [e for e in events if e["ex_date"] <= today]
    year_ago = (dt.date.fromisoformat(today) - dt.timedelta(days=365)).isoformat()
    ttm = [e for e in past if e["ex_date"] > year_ago]
    return {"events": events, "ttm_cash": round(sum(e["cash"] for e in ttm), 6), "ttm_count": len(ttm),
            "last": past[-1] if past else None}


def pick_symbols(cfg):
    return [s for s, e in cfg["us"].items() if e.get("role") == "buy"]


def fetch_us_dividends(cfg, meta, fetcher=fetch_chart, out=OUT, pause=0.8):
    key = "dividends/us"
    try:
        codes = pick_symbols(cfg)
        today = NOW.astimezone(NY).date().isoformat()
        previous, warn, failed, symbols = load_previous(out), [], [], {}
        for i, sym in enumerate(codes):
            prev = (previous.get(sym) or {}).get("events") or []
            try:
                events = parse_events(fetcher(sym), sym)
                if prev and not events:  # Yahoo 不會把已發的配息刪掉，全空視為這次回應不可信
                    raise RuntimeError(f"yahoo {sym}: 回應沒有配息事件，但上一次有 {len(prev)} 筆")
            except Exception as ex:  # noqa: BLE001 — 這一檔沿用上一次
                failed.append(sym)
                warn.append(f"{sym}：{ex}")
                events = prev
            symbols[sym] = summarize(events, today)
            if pause and i < len(codes) - 1:
                time.sleep(pause)
        if codes and len(failed) == len(codes):
            raise RuntimeError("；".join(warn))
        doc = {"generated_at_taipei": NOW.astimezone(TPE).strftime("%Y-%m-%d %H:%M"),
               "source": "Yahoo chart API events=div（range=5y）",
               "symbols": symbols,
               "note": "cash 是每股美元（稅前）；ex_date 是美東日期；ttm_cash 是除息日在最近 365 天的合計。"
                       "沒有配息紀錄的代號 events 是空的。Yahoo 沒有發放日與未來的除息日"}
        write_json(out, doc)
        past = [e for v in symbols.values() for e in v["events"] if e["ex_date"] <= today]
        m = {"status": "stale" if failed else "ok", "source": doc["source"], "codes": len(codes),
             "events": sum(len(v["events"]) for v in symbols.values()),
             "last": max((e["ex_date"] for e in past), default=None)}
        if failed:
            m["failed_codes"] = failed
        if warn:
            m["warnings"] = warn
        meta["datasets"][key] = m
    except Exception as ex:  # noqa: BLE001
        mark_failed(meta, key, out, "美股配息", ex)

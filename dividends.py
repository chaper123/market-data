"""台股 ETF 配息（由 fetch.py 在完整抓取時呼叫）。

來源：
  FinMind TaiwanStockDividend（除息交易日、現金股利發放日、每單位現金），近 4 年
  證交所 OpenAPI TWT48U_ALL（上市股票除權除息預告表，政府資料開放授權）：核對即將除息的日期與金額，
      FinMind 還沒有的預告事件也補進來（發放日留空）
代號清單在 config.json 的 dividend_etfs。

輸出 data/dividends/tw_etf.json：
  etfs[代號].events  每次配息 {ex_date, pay_date, cash, year（民國年度）, source, official_checked}，依除息日排序
  etfs[代號].ttm_cash、ttm_count  除息日在最近 365 天（含今天）的現金合計與次數（算殖利率用）
  etfs[代號].last、next  最近一次已除息、下一次還沒除息的事件
FinMind 視窗以前的事件沿用上一次的檔案，不會因為視窗往後移而消失。
"""
import datetime as dt
import json
import os

from common import DATA, NOW, TPE, finmind, mark_failed, num, roc_ymd, twse_openapi, write_json

OUT = os.path.join(DATA, "dividends", "tw_etf.json")
YEARS = 4


def load_previous(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("etfs", {})
    except (OSError, ValueError):
        return {}


def from_finmind(rows):
    """FinMind 的列 → {除息日: 事件}；只收有現金股利與除息日的。"""
    out = {}
    for x in rows:
        ex = (x.get("CashExDividendTradingDate") or "").strip()
        cash = (num(x.get("CashEarningsDistribution")) or 0) + (num(x.get("CashStatutorySurplus")) or 0)
        if ex and cash > 0:
            out[ex] = {"ex_date": ex, "pay_date": (x.get("CashDividendPaymentDate") or "").strip() or None,
                       "cash": round(cash, 6), "year": x.get("year"), "source": "FinMind", "official_checked": False}
    return out


def from_twse(rows, codes):
    """預告表 → {代號: {除息日: (現金或 None)}}；只收「息」或「權息」。"""
    out = {}
    for r in rows:
        code = (r.get("Code") or "").strip()
        if code in codes and "息" in (r.get("Exdividend") or ""):
            out.setdefault(code, {})[roc_ymd(r["Date"])] = num(r.get("CashDividend"))
    return out


def build(code, fm_events, official, previous, start, today):
    """合併一檔：上一次（視窗以前）← FinMind ← 預告表核對與補充。回傳 (結果, 不一致清單)。"""
    events = {e["ex_date"]: e for e in previous if e["ex_date"] < start}
    events.update(fm_events)
    mismatch = []
    for ex, cash in (official or {}).items():
        if ex in events:
            e = events[ex]
            if cash is not None and abs(cash - e["cash"]) > 1e-6:
                mismatch.append(f"{code} {ex} 預告表 {cash} vs FinMind {e['cash']}")
            else:
                e["official_checked"] = True
        else:
            events[ex] = {"ex_date": ex, "pay_date": None, "cash": cash, "year": None,
                          "source": "證交所除權除息預告表", "official_checked": True}
    evs = [events[k] for k in sorted(events)]
    past = [e for e in evs if e["ex_date"] <= today]
    year_ago = (dt.date.fromisoformat(today) - dt.timedelta(days=365)).isoformat()
    ttm = [e for e in past if e["ex_date"] > year_ago and e["cash"] is not None]
    nxt = [e for e in evs if e["ex_date"] > today]
    return {"events": evs, "ttm_cash": round(sum(e["cash"] for e in ttm), 6), "ttm_count": len(ttm),
            "last": past[-1] if past else None, "next": nxt[0] if nxt else None}, mismatch


def fetch_dividends(cfg, meta):
    key = "dividends/tw_etf"
    try:
        codes = cfg.get("dividend_etfs") or []
        today = NOW.astimezone(TPE).date()
        start = (today - dt.timedelta(days=round(365.25 * YEARS))).isoformat()
        previous, warn = load_previous(OUT), []
        try:
            official = from_twse(twse_openapi("exchangeReport/TWT48U_ALL"), set(codes))
        except Exception as ex:  # noqa: BLE001 — 預告表失敗就只用 FinMind
            official = {}
            warn.append(f"證交所 OpenAPI TWT48U_ALL：{ex}")
        etfs, failed, mismatch = {}, [], []
        for code in codes:
            prev = (previous.get(code) or {}).get("events") or []
            try:
                fm = from_finmind(finmind("TaiwanStockDividend", code, start))
            except Exception as ex:  # noqa: BLE001 — 這一檔沿用上一次
                failed.append(code)
                warn.append(f"FinMind {code}：{ex}")
                fm = {e["ex_date"]: e for e in prev if e["ex_date"] >= start}
            etfs[code], bad = build(code, fm, official.get(code), prev, start, today.isoformat())
            mismatch += bad
        if len(failed) == len(codes) and codes:
            raise RuntimeError("；".join(warn))
        doc = {"generated_at_taipei": NOW.astimezone(TPE).strftime("%Y-%m-%d %H:%M"),
               "source": "FinMind TaiwanStockDividend（歷史、發放日）＋證交所 OpenAPI TWT48U_ALL（除權除息預告）",
               "since": start, "etfs": etfs,
               "note": "cash 是每單位現金（元）；ttm_cash 是除息日在最近 365 天的合計。沒有配息紀錄的代號 events 是空的"}
        write_json(OUT, doc)
        past = [e for v in etfs.values() for e in v["events"] if e["ex_date"] <= today.isoformat()]
        upcoming = sorted((v["next"]["ex_date"], c, v["next"]["cash"]) for c, v in etfs.items() if v["next"])
        checked = sum(1 for c in official.values() for _ in c)
        m = {"status": "stale" if failed else "ok", "source": doc["source"], "codes": len(codes),
             "events": sum(len(v["events"]) for v in etfs.values()),
             "last": max((e["ex_date"] for e in past), default=None),
             "next": [f"{c} {d} {cash if cash is not None else '金額未公布'}" for d, c, cash in upcoming],
             "cross_check": (f"預告表 {checked} 筆，與 FinMind 不一致 {len(mismatch)} 筆" if checked
                             else "預告表目前沒有這幾檔，未核對"),
             "cross_check_ok": (not mismatch) if checked else None}
        if mismatch:
            m["mismatch"] = mismatch
        if failed:
            m["failed_codes"] = failed
        if warn:
            m["warnings"] = warn
        meta["datasets"][key] = m
    except Exception as ex:  # noqa: BLE001
        mark_failed(meta, key, OUT, "ETF 配息", ex)

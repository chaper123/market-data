"""台股日線的官方開放來源，以及追蹤用的 ETF 日線（config.json 的 tw_watch：0056、00878、00919；由 fetch.py 在完整抓取時呼叫）。

證交所、櫃買中心網站的使用條款都禁止未經同意以程式下載，所以日線不抓網站報表頁：
  歷史：FinMind TaiwanStockPrice（上市、上櫃都有）
  最新一天：上市用證交所 OpenAPI STOCK_DAY_ALL、上櫃用櫃買 OpenAPI tpex_mainboard_daily_close_quotes
            （政府資料開放授權），同一天以它為準並核對 FinMind
成交量：上市是股；上櫃換成張（和 2026-11 以前讀櫃買網站時的舊檔一致）。
official_daily() 也給 fetch.py 的 tw/ 日線用（009816、00981A、00411A）。
tw_watch 輸出 data/tw/<代號>_norm.csv（date,volume,open,high,low,close，無表頭，格式同 009816 等），
meta 用 tw_watch/<代號>（不放在 tw/ 底下，不影響 fetch.py 的「日線已完整」判斷）。
"""
import datetime as dt
import os

from common import (DATA, NOW, TPE, finalize, finmind, mark_failed, num, read_rows, roc_ymd, summary, tpex_openapi,
                    tw_cutoff, twse_openapi, write_csv)

SOURCE = {"TWSE": "FinMind（歷史）＋證交所 OpenAPI STOCK_DAY_ALL（最新一天）",
          "TPEX": "FinMind（歷史）＋櫃買 OpenAPI tpex_mainboard_daily_close_quotes（最新一天）"}


def bar(day, v, o, h, l, c):
    return (day, num(v) or 0, num(o), num(h), num(l), num(c))


def lots(v):
    return round(v / 1000)


def openapi_latest(codes_by_market):
    """{代號: 最新一天的 bar}；回傳 (bars, 錯誤清單)。上櫃的成交量換成張。"""
    out, errors = {}, []
    if codes_by_market.get("TWSE"):
        try:
            for r in twse_openapi("exchangeReport/STOCK_DAY_ALL"):
                if r.get("Code") in codes_by_market["TWSE"]:
                    out[r["Code"]] = bar(roc_ymd(r["Date"]), r["TradeVolume"], r["OpeningPrice"], r["HighestPrice"],
                                         r["LowestPrice"], r["ClosingPrice"])
        except Exception as ex:  # noqa: BLE001
            errors.append(f"證交所 OpenAPI STOCK_DAY_ALL：{ex}")
    if codes_by_market.get("TPEX"):
        try:
            for r in tpex_openapi("tpex_mainboard_daily_close_quotes"):
                code = (r.get("SecuritiesCompanyCode") or "").strip()
                if code in codes_by_market["TPEX"]:
                    b = bar(roc_ymd(r["Date"]), r["TradingShares"], r["Open"], r["High"], r["Low"], r["Close"])
                    out[code] = (b[0], lots(b[1]), *b[2:])
        except Exception as ex:  # noqa: BLE001
            errors.append(f"櫃買 OpenAPI tpex_mainboard_daily_close_quotes：{ex}")
    return out, errors


def official_daily(code, market, start, latest, previous=None):
    """FinMind 歷史＋OpenAPI 最新一天。回傳 ({date: bar}, 核對說明, 核對結果, 警告)。
    previous：舊檔的列（FinMind 失敗時沿用）。兩個都沒有就丟例外。"""
    rows, warn = dict(previous or {}), []
    try:
        fm = {}
        for x in finmind("TaiwanStockPrice", code, start):
            v = x.get("Trading_Volume") or 0
            fm[x["date"]] = bar(x["date"], lots(v) if market == "TPEX" else v, x["open"], x["max"], x["min"], x["close"])
    except Exception as ex:  # noqa: BLE001 — FinMind 失敗就沿用舊檔＋OpenAPI 最新一天
        fm = {}
        warn.append(f"FinMind：{ex}")
    rows.update(fm)
    check, check_ok = "OpenAPI 沒有這一檔，未核對", None
    ob = latest.get(code)
    if ob and ob[5]:
        day = ob[0]
        if day in fm:
            check, check_ok = f"{day} 收盤 OpenAPI {ob[5]} vs FinMind {fm[day][5]}", abs(fm[day][5] - ob[5]) < 0.005
        else:
            check = f"FinMind 還沒有 {day}，用 OpenAPI 補上"
        rows[day] = ob
    if not fm and not ob:
        raise RuntimeError("；".join(warn) or "FinMind 與 OpenAPI 都沒有資料")
    return rows, check, check_ok, warn


def fetch_watch(cfg, meta):
    watch = cfg.get("tw_watch") or {}
    if not watch:
        return
    cut = tw_cutoff()
    start = (NOW.astimezone(TPE).date() - dt.timedelta(days=31 * cfg.get("tw_months", 13))).isoformat()
    by_market = {}
    for code, e in watch.items():
        by_market.setdefault(e["market"], set()).add(code)
    latest, oa_err = openapi_latest(by_market)
    for code, e in watch.items():
        key, path = f"tw_watch/{code}", os.path.join(DATA, "tw", f"{code}_norm.csv")
        try:
            prev = {r[0]: bar(*r[:6]) for r in read_rows(path) if len(r) >= 6}
            rows, check, check_ok, warn = official_daily(code, e["market"], start, latest, prev)
            out = [r for r in finalize(rows, cut) if r[0] >= start]
            if len(out) < 60:
                raise RuntimeError(f"只有 {len(out)} 列")
            m = summary(out, SOURCE[e["market"]])
            m.update({"cross_check": check, "cross_check_ok": check_ok})
            if oa_err or warn:
                m["warnings"] = oa_err + warn
            write_csv(path, out)
            meta["datasets"][key] = m
        except Exception as ex:  # noqa: BLE001
            mark_failed(meta, key, path, f"TW {code}（tw_watch）", ex)

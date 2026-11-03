"""追蹤用的台股 ETF 日線（config.json 的 tw_watch：0056、00878、00919，算殖利率用；由 fetch.py 在完整抓取時呼叫）。

證交所網站報表頁的使用條款禁止未經同意以程式下載，所以不走既有 tw/ 那套（STOCK_DAY）：
  歷史：FinMind TaiwanStockPrice（約 13 個月，和既有 tw_months 相同）
  最新一天：證交所 OpenAPI STOCK_DAY_ALL（上市個股日成交，政府資料開放授權），同一天以它為準並核對 FinMind
輸出 data/tw/<代號>_norm.csv（date,volume,open,high,low,close，無表頭，格式同 009816 等）。
meta 用 tw_watch/<代號>（不放在 tw/ 底下，不影響 fetch.py 的「日線已完整」判斷）。
"""
import datetime as dt
import os

from common import (DATA, NOW, TPE, finalize, finmind, mark_failed, num, read_rows, roc_ymd, summary, tw_cutoff,
                    twse_openapi, write_csv)


def bar(day, v, o, h, l, c):
    return (day, num(v) or 0, num(o), num(h), num(l), num(c))


def fetch_watch(cfg, meta):
    codes = list(cfg.get("tw_watch") or {})
    if not codes:
        return
    cut = tw_cutoff()
    start = (NOW.astimezone(TPE).date() - dt.timedelta(days=31 * cfg.get("tw_months", 13))).isoformat()
    try:
        latest = {r["Code"]: r for r in twse_openapi("exchangeReport/STOCK_DAY_ALL") if r.get("Code") in codes}
        oa_err = None
    except Exception as ex:  # noqa: BLE001 — OpenAPI 失敗就只用 FinMind
        latest, oa_err = {}, f"證交所 OpenAPI STOCK_DAY_ALL：{ex}"
    for code in codes:
        key, path = f"tw_watch/{code}", os.path.join(DATA, "tw", f"{code}_norm.csv")
        try:
            rows = {r[0]: bar(*r[:6]) for r in read_rows(path) if len(r) >= 6}
            warn = [oa_err] if oa_err else []
            try:
                fm = {x["date"]: bar(x["date"], x.get("Trading_Volume"), x["open"], x["max"], x["min"], x["close"])
                      for x in finmind("TaiwanStockPrice", code, start)}
            except Exception as ex:  # noqa: BLE001 — FinMind 失敗就沿用舊檔＋OpenAPI 最新一天
                fm = {}
                warn.append(f"FinMind：{ex}")
            rows.update(fm)
            oa = latest.get(code)
            check, check_ok = "OpenAPI 沒有這一檔，未核對", None
            if oa:
                day = roc_ymd(oa["Date"])
                ob = bar(day, oa["TradeVolume"], oa["OpeningPrice"], oa["HighestPrice"], oa["LowestPrice"],
                         oa["ClosingPrice"])
                if day in fm:
                    diff = abs(fm[day][5] - ob[5])
                    check, check_ok = f"{day} 收盤 OpenAPI {ob[5]} vs FinMind {fm[day][5]}", diff < 0.005
                else:
                    check = f"FinMind 還沒有 {day}，用 OpenAPI 補上"
                if ob[5]:
                    rows[day] = ob
            if not fm and not oa:
                raise RuntimeError("；".join(warn) or "兩個來源都沒有資料")
            out = [r for r in finalize(rows, cut) if r[0] >= start]
            if len(out) < 60:
                raise RuntimeError(f"只有 {len(out)} 列")
            m = summary(out, "FinMind（歷史）＋證交所 OpenAPI STOCK_DAY_ALL（最新一天）")
            m.update({"cross_check": check, "cross_check_ok": check_ok})
            if warn:
                m["warnings"] = warn
            write_csv(path, out)
            meta["datasets"][key] = m
        except Exception as ex:  # noqa: BLE001
            mark_failed(meta, key, path, f"TW {code}（tw_watch）", ex)

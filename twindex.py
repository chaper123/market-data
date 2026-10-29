"""台股加權指數與加權報酬指數日線（由 fetch.py 在完整抓取時呼叫）。

證交所網站報表頁的使用條款禁止未經同意以程式下載，所以不抓 www.twse.com.tw：
  當月：證交所 OpenAPI（政府資料開放授權條款第 1 版）MI_5MINS_HIST（開高低收）、FMTQIK（成交股數、金額）、
        MFI94U（報酬指數）；OpenAPI 只給當月，同一天以它為準
  歷史：FinMind TaiwanStockPrice（TAIEX）、TaiwanStockTotalReturnIndex（TAIEX），2003 年起；
        和 OpenAPI 重疊的日子逐日核對收盤

輸出（無表頭）：
  data/index/TAIEX.csv     date,open,high,low,close,volume_shares,value_twd（成交股數、成交金額元）
  data/index/TAIEX_TR.csv  date,close
"""
import os

from common import DATA, finmind, mark_failed, num, read_rows, roc_ymd, tw_cutoff, twse_openapi, write_rows

START = "2003-01-01"  # 報酬指數 2003 年起才有，兩份對齊
TAIEX_CSV = os.path.join(DATA, "index", "TAIEX.csv")
TR_CSV = os.path.join(DATA, "index", "TAIEX_TR.csv")
MIN_ROWS = 4000
TOL = 0.011  # 指數兩位小數，差 0.01 以內算一致


def official_month():
    """OpenAPI 當月：({date: [o, h, l, c, 股數, 金額]}, {date: 報酬指數})。"""
    vol = {roc_ymd(r["Date"]): (num(r.get("TradeVolume")), num(r.get("TradeValue")))
           for r in twse_openapi("exchangeReport/FMTQIK")}
    ohlc = {}
    for r in twse_openapi("indicesReport/MI_5MINS_HIST"):
        d = roc_ymd(r["Date"])
        v, val = vol.get(d, (None, None))
        ohlc[d] = [num(r["OpeningIndex"]), num(r["HighestIndex"]), num(r["LowestIndex"]), num(r["ClosingIndex"]), v, val]
    tr = {roc_ymd(r["Date"]): num(r["TAIEXTotalReturnIndex"]) for r in twse_openapi("indicesReport/MFI94U")}
    return ohlc, tr


def finmind_history():
    ohlc = {x["date"]: [x["open"], x["max"], x["min"], x["close"], x.get("Trading_Volume"), x.get("Trading_money")]
            for x in finmind("TaiwanStockPrice", "TAIEX", START)}
    tr = {x["date"]: x["price"] for x in finmind("TaiwanStockTotalReturnIndex", "TAIEX", START)}
    return ohlc, tr


def merge(previous, history, official, cut, close=lambda v: v):
    """上一次的檔案 ← FinMind ← OpenAPI（後蓋前），去掉未收盤的日子。回傳 (列, 重疊天數, 收盤不一致的日子)。
    close：從一列取出收盤（開高低收那份是 v[3]）。"""
    out = dict(previous)
    out.update(history)
    both = sorted(set(history) & set(official))
    diff = [d for d in both if close(history[d]) is not None and close(official[d]) is not None
            and abs(close(history[d]) - close(official[d])) > TOL]
    out.update(official)
    return {d: v for d, v in sorted(out.items()) if d <= cut}, len(both), diff


def _int(x):
    return "" if x in (None, "") else int(float(x))


def fetch_index(cfg, meta):
    cut = tw_cutoff()
    warn, official, history = [], ({}, {}), ({}, {})
    try:
        official = official_month()
    except Exception as ex:  # noqa: BLE001 — OpenAPI 失敗就只用 FinMind
        warn.append(f"證交所 OpenAPI：{ex}")
    try:
        history = finmind_history()
    except Exception as ex:  # noqa: BLE001 — FinMind 失敗就沿用舊檔＋OpenAPI 當月
        warn.append(f"FinMind：{ex}")
    jobs = (("index/TAIEX", TAIEX_CSV, 0, "加權指數"), ("index/TAIEX_TR", TR_CSV, 1, "加權報酬指數"))
    for key, path, i, label in jobs:
        try:
            if not official[i] and not history[i]:
                raise RuntimeError("；".join(warn) or "兩個來源都沒有資料")
            if i == 0:
                prev = {r[0]: [num(x) for x in r[1:7]] for r in read_rows(path) if len(r) >= 5}
            else:
                prev = {r[0]: num(r[1]) for r in read_rows(path) if len(r) >= 2}
            rows, compared, diff = merge(prev, history[i], official[i], cut, (lambda v: v[3]) if i == 0 else (lambda v: v))
            if len(rows) < MIN_ROWS:
                raise RuntimeError(f"只有 {len(rows)} 列")
            if i == 0:
                out = [[d, v[0], v[1], v[2], v[3], _int(v[4]), _int(v[5])] for d, v in rows.items()]
                last_close = out[-1][4]
            else:
                out = [[d, v] for d, v in rows.items()]
                last_close = out[-1][1]
            m = {"status": "ok", "source": "證交所 OpenAPI（當月）＋FinMind（歷史）", "rows": len(out),
                 "first": out[0][0], "last": out[-1][0], "last_close": last_close,
                 "official_last": max(official[i]) if official[i] else None,
                 "finmind_last": max(history[i]) if history[i] else None,
                 "cross_check": (f"OpenAPI 與 FinMind 重疊 {compared} 天，收盤不一致 {len(diff)} 天" if compared
                                 else "沒有重疊的日子，未核對"),
                 "cross_check_ok": (not diff) if compared else None}
            if diff:
                m["diff_days"] = diff[:20]
            if warn:
                m["warnings"] = warn
            write_rows(path, out)
            meta["datasets"][key] = m
        except Exception as ex:  # noqa: BLE001
            mark_failed(meta, key, path, label, ex)

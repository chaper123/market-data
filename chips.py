"""Phase 1 ⑤：法人買賣超與融資（由 fetch.py 在完整抓取時呼叫）。

輸出（都是逐日累積、無表頭）：
  data/chips/tw_insti.csv         date,code,foreign,foreign_dealer,trust,dealer,total
                                  關注代號（config.json 的 tw）三大法人買賣超股數。上市取證交所 T86，上櫃取櫃買中心；
                                  foreign＝外資及陸資（不含外資自營商），dealer＝自營商合計（自行買賣＋避險）。
                                  當天沒有法人交易的代號記 0
  data/chips/tw_market_insti.csv  date,dealer_self,dealer_hedge,trust,foreign,foreign_dealer,total
                                  集中市場三大法人買賣差額（元，證交所 BFI82U）
  data/chips/tw_margin.csv        date,margin_lots,short_lots,margin_amount_k
                                  集中市場融資餘額（張）、融券餘額（張）、融資金額（仟元，證交所 MI_MARGN 信用交易統計）

每次檢查最近 LOOKBACK_DAYS 天內的平日（略過 calendar.json 的休市日），CSV 缺的日子才抓，漏跑會自動補。
台北 22:00 前不抓當天（融資資料晚上才公布）。核對：每個來源的「合計」要等於分項加總，不一致代表欄位變了。
"""
import datetime as dt
import json
import os
import time

from common import DATA, NOW, TPE, get_json, mark_failed, num, read_rows, write_rows

LOOKBACK_DAYS = 9
REF = {"Referer": "https://www.twse.com.tw/"}
T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86?date={:%Y%m%d}&selectType=ALL&response=json"
TPEX_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={:%Y/%m/%d}&response=json"
BFI_URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&dayDate={:%Y%m%d}&response=json"
MARGIN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={:%Y%m%d}&selectType=MS&response=json"
T86_COLS = ("外陸資買賣超股數(不含外資自營商)", "外資自營商買賣超股數", "投信買賣超股數", "自營商買賣超股數",
            "三大法人買賣超股數")
# 櫃買的欄位名稱重複（每組都叫買進／賣出／買賣超），只能用位置：外資不含自營商、外資自營商、投信、自營商合計、三大法人合計
TPEX_IDX = (4, 7, 13, 22, 23)
BFI_ROWS = ("自營商(自行買賣)", "自營商(避險)", "投信", "外資及陸資(不含外資自營商)", "外資自營商", "合計")
PATHS = {k: os.path.join(DATA, "chips", f"{k}.csv") for k in ("tw_insti", "tw_market_insti", "tw_margin")}


def candidate_days():
    tp = NOW.astimezone(TPE)
    last = tp.date() if tp.hour >= 22 else tp.date() - dt.timedelta(days=1)
    try:
        closed = set(json.load(open(os.path.join(DATA, "calendar", "calendar.json"),
                                    encoding="utf-8"))["tw_market"]["closed"])
    except Exception:  # noqa: BLE001 — 沒有行事曆就每個平日都試，休市日抓不到資料會自動略過
        closed = set()
    days = [last - dt.timedelta(days=i) for i in range(LOOKBACK_DAYS)]
    return sorted(d for d in days if d.weekday() < 5 and d.isoformat() not in closed)


def twse(url):
    """證交所 rwd：stat 不是 OK（假日、還沒公布）回傳 None。證交所有頻率限制，每次間隔 2 秒。"""
    try:
        j = get_json(url, headers=REF)
    finally:
        time.sleep(2)
    return j if j.get("stat") == "OK" else None


def check_sum(parts, total, label, problems):
    if abs(sum(parts) - total) > 0.5:
        problems.append(f"{label} 分項加總 {sum(parts):,.0f} ≠ 合計 {total:,.0f}")


def t86(day, codes, problems):
    j = twse(T86_URL.format(day))
    if not j:
        return None
    f = j["fields"]
    idx = [f.index(c) for c in T86_COLS]  # 欄位改名會在這裡丟例外
    by_code = {r[0].strip(): r for r in j["data"]}
    out = {}
    for c in codes:
        r = by_code.get(c)
        vals = [num(r[i]) or 0 for i in idx] if r else [0] * 5
        check_sum(vals[:4], vals[4], f"T86 {day} {c}", problems)
        out[c] = vals
    return out


def tpex(day, codes, problems):
    try:
        j = get_json(TPEX_URL.format(day))
    finally:
        time.sleep(2)
    t = (j.get("tables") or [{}])[0]
    if j.get("date") != f"{day:%Y%m%d}" or not t.get("data"):
        return None
    if (t.get("fields") or [""])[-1] != "三大法人買賣超股數合計":
        raise RuntimeError(f"櫃買欄位變了：{(t.get('fields') or [''])[-1]}")
    by_code = {r[0].strip(): r for r in t["data"]}
    out = {}
    for c in codes:
        r = by_code.get(c)
        vals = [num(r[i]) or 0 for i in TPEX_IDX] if r else [0] * 5
        check_sum(vals[:4], vals[4], f"櫃買 {day} {c}", problems)
        out[c] = vals
    return out


def bfi82u(day, problems):
    j = twse(BFI_URL.format(day))
    if not j:
        return None
    rows = {r[0]: num(r[3]) for r in j["data"]}
    vals = [rows[k] for k in BFI_ROWS]  # 少了某一列會在這裡丟例外
    check_sum(vals[:5], vals[5], f"BFI82U {day}", problems)
    return vals


def margin(day):
    j = twse(MARGIN_URL.format(day))
    if not j:
        return None
    rows = {r[0]: num(r[5]) for r in j["tables"][0]["data"]}
    return [rows["融資(交易單位)"], rows["融券(交易單位)"], rows["融資金額(仟元)"]]


def fetch_chips(cfg, meta):
    days = candidate_days()
    groups = {"TWSE": [c for c, e in cfg["tw"].items() if e["market"] == "TWSE"],
              "TPEX": [c for c, e in cfg["tw"].items() if e["market"] == "TPEX"]}

    # 關注代號三大法人
    key, path = "chips/tw_insti", PATHS["tw_insti"]
    try:
        hist = {(r[0], r[1]): r for r in read_rows(path)}
        added, problems = [], []
        for market, codes in groups.items():
            if not codes:
                continue
            for d in days:
                if all((d.isoformat(), c) in hist for c in codes):
                    continue
                got = t86(d, codes, problems) if market == "TWSE" else tpex(d, codes, problems)
                if got:
                    for c, vals in got.items():
                        hist[(d.isoformat(), c)] = [d.isoformat(), c] + [int(v) for v in vals]
                    added.append(f"{d} {market}")
        rows = sorted(hist.values())
        if not rows:
            raise RuntimeError("沒有任何資料")
        write_rows(path, rows)
        last = rows[-1][0]
        meta["datasets"][key] = {
            "status": "ok", "source": "證交所 T86（上市）、櫃買 dailyTrade（上櫃）", "rows": len(rows),
            "first": rows[0][0], "last": last, "added": added,
            "latest_total": {r[1]: int(r[6]) for r in rows if r[0] == last},
            "cross_check": "；".join(problems) or "各代號合計＝分項加總", "cross_check_ok": not problems}
    except Exception as ex:  # noqa: BLE001
        mark_failed(meta, key, path, "chips 三大法人（個股）", ex)

    # 集中市場三大法人金額、信用交易統計
    for key, path, fn, label in (("chips/tw_market_insti", PATHS["tw_market_insti"], bfi82u, "BFI82U"),
                                 ("chips/tw_margin", PATHS["tw_margin"], None, "MI_MARGN")):
        try:
            hist = {r[0]: r for r in read_rows(path)}
            added, problems = [], []
            for d in days:
                if d.isoformat() in hist:
                    continue
                vals = fn(d, problems) if fn else margin(d)
                if vals:
                    hist[d.isoformat()] = [d.isoformat()] + [int(v) for v in vals]
                    added.append(d.isoformat())
            rows = sorted(hist.values())
            if not rows:
                raise RuntimeError("沒有任何資料")
            write_rows(path, rows)
            m = {"status": "ok", "source": f"證交所 {label}", "rows": len(rows), "first": rows[0][0],
                 "last": rows[-1][0], "added": added}
            if fn:
                m["latest_foreign"], m["latest_total"] = int(rows[-1][4]), int(rows[-1][6])
                m["cross_check"] = "；".join(problems) or "合計＝分項加總"
                m["cross_check_ok"] = not problems
            else:
                m["latest_margin_amount_k"] = int(rows[-1][3])
                m["cross_check"] = "單一來源，未核對"
            meta["datasets"][key] = m
        except Exception as ex:  # noqa: BLE001
            mark_failed(meta, key, path, f"chips {label}", ex)

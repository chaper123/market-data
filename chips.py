"""Phase 1 ⑤：法人買賣超與融資（由 fetch.py 在完整抓取時呼叫）。

輸出（都是逐日累積、無表頭）：
  data/chips/tw_insti.csv         date,code,foreign,foreign_dealer,trust,dealer,total
                                  關注代號（config.json 的 tw）三大法人買賣超股數。foreign＝外資及陸資（不含外資自營商），
                                  dealer＝自營商合計（自行買賣＋避險），total＝四項加總。當天沒有法人交易的代號記 0
  data/chips/tw_market_insti.csv  date,dealer_self,dealer_hedge,trust,foreign,foreign_dealer,total
                                  集中市場三大法人買賣差額（元）
  data/chips/tw_margin.csv        date,margin_lots,short_lots,margin_amount_k
                                  集中市場融資餘額（張）、融券餘額（張）、融資金額（仟元）

來源：FinMind TaiwanStockInstitutionalInvestorsBuySell（個股，上市上櫃都有）、TaiwanStockTotalInstitutionalInvestors
（集中市場金額）、TaiwanStockTotalMarginPurchaseShortSale（集中市場信用交易）。證交所、櫃買網站的使用條款禁止未經同意
以程式下載，2026-11 起不再抓 T86、櫃買 dailyTrade、BFI82U、MI_MARGN（換之前逐筆比對 2026-10-07 起的全部歷史，完全一致）；
證交所 OpenAPI 沒有這幾份的市場合計。

每次檢查最近 LOOKBACK_DAYS 天內的平日（略過 calendar.json 的休市日），CSV 缺的日子才抓（一份資料一次請求），漏跑會自動補。
台北 22:00 前不抓當天（融資資料晚上才公布）。核對：集中市場金額的「合計」要等於分項加總。
"""
import datetime as dt
import json
import os

from common import DATA, NOW, TPE, finmind, mark_failed, read_rows, write_rows

LOOKBACK_DAYS = 9
SOURCE = "FinMind（證交所、櫃買資料）"
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
    return sorted(d.isoformat() for d in days if d.weekday() < 5 and d.isoformat() not in closed)


def check_sum(parts, total, label, problems):
    if abs(sum(parts) - total) > 0.5:
        problems.append(f"{label} 分項加總 {sum(parts):,.0f} ≠ 合計 {total:,.0f}")


def net_by_day(rows):
    """FinMind 法人資料（每列一個身分的 buy、sell）→ {date: {身分: 買賣超}}。"""
    out = {}
    for x in rows:
        out.setdefault(x["date"], {})[x["name"]] = (x.get("buy") or 0) - (x.get("sell") or 0)
    return out


def stock_insti(code, start):
    """{date: [foreign, foreign_dealer, trust, dealer, total]}（股）。"""
    out = {}
    for d, v in net_by_day(finmind("TaiwanStockInstitutionalInvestorsBuySell", code, start)).items():
        vals = [v.get("Foreign_Investor", 0), v.get("Foreign_Dealer_Self", 0), v.get("Investment_Trust", 0),
                v.get("Dealer_self", 0) + v.get("Dealer_Hedging", 0)]
        out[d] = vals + [sum(vals)]
    return out


def market_insti(start, problems):
    """{date: [dealer_self, dealer_hedge, trust, foreign, foreign_dealer, total]}（元）。"""
    out = {}
    for d, v in net_by_day(finmind("TaiwanStockTotalInstitutionalInvestors", "", start)).items():
        vals = [v["Dealer_self"], v["Dealer_Hedging"], v["Investment_Trust"], v["Foreign_Investor"],
                v.get("Foreign_Dealer_Self", 0), v["total"]]  # 少了某一項會在這裡丟例外
        check_sum(vals[:5], vals[5], f"集中市場法人 {d}", problems)
        out[d] = vals
    return out


def market_margin(start):
    """{date: [融資餘額張, 融券餘額張, 融資金額仟元]}。"""
    by_day = {}
    for x in finmind("TaiwanStockTotalMarginPurchaseShortSale", "", start):
        by_day.setdefault(x["date"], {})[x["name"]] = x["TodayBalance"]
    return {d: [v["MarginPurchase"], v["ShortSale"], v["MarginPurchaseMoney"] // 1000]
            for d, v in by_day.items() if {"MarginPurchase", "ShortSale", "MarginPurchaseMoney"} <= set(v)}


def fetch_chips(cfg, meta):
    days = candidate_days()
    codes = list(cfg["tw"])
    trading_days = set()

    # 集中市場三大法人金額、信用交易統計（先抓：有資料的日子就是交易日，個股沒有法人交易時補 0 用）
    for key, fn, label in (("chips/tw_market_insti", market_insti, "集中市場法人"),
                           ("chips/tw_margin", None, "集中市場信用交易")):
        path = PATHS[key.split("/")[1]]
        try:
            hist = {r[0]: r for r in read_rows(path)}
            missing = [d for d in days if d not in hist]
            added, problems = [], []
            if missing:
                got = fn(missing[0], problems) if fn else market_margin(missing[0])
                trading_days |= set(got)
                for d in missing:
                    if d in got:
                        hist[d] = [d] + [int(v) for v in got[d]]
                        added.append(d)
            trading_days |= {d for d in days if d in hist}
            rows = sorted(hist.values())
            if not rows:
                raise RuntimeError("沒有任何資料")
            write_rows(path, rows)
            m = {"status": "ok", "source": SOURCE, "rows": len(rows), "first": rows[0][0], "last": rows[-1][0],
                 "added": added}
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

    # 關注代號三大法人
    key, path = "chips/tw_insti", PATHS["tw_insti"]
    try:
        hist = {(r[0], r[1]): r for r in read_rows(path)}
        missing = [d for d in days if not all((d, c) in hist for c in codes)]
        added = []
        if missing:
            got = {c: stock_insti(c, missing[0]) for c in codes}
            have = trading_days | {d for g in got.values() for d in g}
            for d in missing:
                if d not in have:
                    continue
                for c in codes:
                    hist[(d, c)] = [d, c] + [int(v) for v in got[c].get(d, [0] * 5)]
                added.append(d)
        rows = sorted(hist.values())
        if not rows:
            raise RuntimeError("沒有任何資料")
        write_rows(path, rows)
        last = rows[-1][0]
        meta["datasets"][key] = {
            "status": "ok", "source": SOURCE, "rows": len(rows), "first": rows[0][0], "last": last, "added": added,
            "latest_total": {r[1]: int(r[6]) for r in rows if r[0] == last},
            "cross_check": "單一來源（換來源前與證交所 T86、櫃買 dailyTrade 逐筆比對一致）", "cross_check_ok": None}
    except Exception as ex:  # noqa: BLE001
        mark_failed(meta, key, path, "chips 三大法人（個股）", ex)

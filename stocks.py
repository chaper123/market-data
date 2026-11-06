"""個股資料（stock-dashboard docs/INSIGHTS.md 第 7 節，2026-11-07）：代號在 config.json 的 stocks（目前只有台積電 2330）。

輸出（逐日／逐週／逐月累積、無表頭）：
  data/chips/tw_stock_margin.csv    date,code,margin_lots,short_lots     個股融資餘額（張）、融券餘額（張）
  data/chips/tw_holders.csv         date,code,level,people,shares,pct    集保戶股權分散表（每週一份）：分級 1～15 是
                                    1～999 股、1,000～5,000 股 …… 1,000,001 股以上；16 是差異數調整、17 是合計
  data/fundamentals/tw_revenue.csv  month,code,revenue                   月營收（元），month＝營收所屬月份 YYYY-MM

來源：
- 融資融券、月營收：FinMind TaiwanStockMarginPurchaseShortSale、TaiwanStockMonthRevenue（免費、教育非商業；
  使用者 2026-11-03 決定照常使用）。
- 股權分散：集保結算所開放資料 https://opendata.tdcc.com.tw/getOD.ashx?id=1-5（政府資料開放平臺 dataset 11452
  「集保戶股權分散表」，政府資料開放授權條款第 1 版，提供機關金管會證期局）。只給最新一週，歷史從 2026-11 起累積。
  集保的憑證在 Python 3.13 起會被嚴格檢查擋下（Missing Subject Key Identifier），workflow 固定 3.12。
- 個股日線與三大法人：代號另外加在 config.json 的 tw，由 fetch.py fetch_tw 與 chips.py 處理。

每次都重抓最近一段（融資 LOOKBACK_DAYS 天、營收兩年）並跟舊檔合併，漏跑會自動補；同一天（週、月）以新抓的為準。
核對：股權分散的分級 1～15 股數加總扣掉（或加上）16 差異數調整要等於合計；占比合計要是 100。
"""
import csv
import datetime as dt
import io
import os

from common import DATA, NOW, TPE, finmind, http, mark_failed, read_rows, write_rows

LOOKBACK_DAYS = 14
FINMIND_SOURCE = "FinMind（證交所、公開資訊觀測站資料）"
TDCC_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
TDCC_SOURCE = "集保結算所開放資料（政府資料開放授權條款第 1 版）"
PATHS = {
    "chips/tw_stock_margin": os.path.join(DATA, "chips", "tw_stock_margin.csv"),
    "chips/tw_holders": os.path.join(DATA, "chips", "tw_holders.csv"),
    "fundamentals/tw_revenue": os.path.join(DATA, "fundamentals", "tw_revenue.csv"),
}


def merge(path, new_rows, key_len):
    """舊檔＋新列（同一個 key 以新的為準），排序後寫回。回傳 (全部列, 新增或改變的 key)。"""
    hist = {tuple(r[:key_len]): r for r in read_rows(path)}
    changed = []
    for r in new_rows:
        r = [str(x) for x in r]
        k = tuple(r[:key_len])
        if hist.get(k) != r:
            changed.append(k)
        hist[k] = r
    rows = sorted(hist.values())
    write_rows(path, rows)
    return rows, changed


def stock_margin(codes):
    old = read_rows(PATHS["chips/tw_stock_margin"])
    out = []
    for code in codes:
        # 歷史不到 60 個交易日（新代號）先補 120 天，之後每次只重抓最近 LOOKBACK_DAYS 天
        days = LOOKBACK_DAYS if sum(r[1] == code for r in old) >= 60 else 120
        start = (NOW.astimezone(TPE).date() - dt.timedelta(days=days)).isoformat()
        for x in finmind("TaiwanStockMarginPurchaseShortSale", code, start):
            out.append([x["date"], code, int(x["MarginPurchaseTodayBalance"]), int(x["ShortSaleTodayBalance"])])
    return out


def revenue(codes):
    tp = NOW.astimezone(TPE).date()
    start = f"{tp.year - 2}-{tp.month:02d}-01"  # 兩年：最新月份也算得出年增率
    out = []
    for code in codes:
        for x in finmind("TaiwanStockMonthRevenue", code, start):
            out.append([f"{int(x['revenue_year'])}-{int(x['revenue_month']):02d}", code, int(x["revenue"])])
    return out


def parse_tdcc(text, codes):
    """集保 CSV（資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%）→ 指定代號的列；日期轉 YYYY-MM-DD。"""
    out = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 6 or not r[0].strip().isdigit():
            continue
        code = r[1].strip()
        if code not in codes:
            continue
        d = r[0].strip()
        out.append([f"{d[:4]}-{d[4:6]}-{d[6:8]}", code, int(r[2]), int(r[3]), int(r[4]), float(r[5])])
    return out


def check_holders(rows):
    """每個（日期, 代號）：分級 1～16 股數加總＝合計（17），占比合計 100。回傳問題清單。"""
    groups = {}
    for d, code, level, _, shares, pct in rows:
        groups.setdefault((d, code), {})[int(level)] = (int(shares), float(pct))
    problems = []
    for (d, code), g in groups.items():
        if 17 not in g:
            problems.append(f"{code} {d} 沒有合計列")
            continue
        # 分級 1～15 加總再扣掉（或加上）16 差異數調整要等於合計；2026-10-30 實際資料是扣掉
        parts, adj = sum(v[0] for lv, v in g.items() if lv <= 15), g.get(16, (0, 0))[0]
        if g[17][0] not in (parts - adj, parts + adj):
            problems.append(f"{code} {d} 分級股數加總 {parts}（差異數 {adj}）≠ 合計 {g[17][0]}")
        if abs(g[17][1] - 100) > 0.01:
            problems.append(f"{code} {d} 占比合計 {g[17][1]}")
    return problems


def holders(codes):
    status, raw = http(TDCC_URL, timeout=60)
    if status != 200:
        raise RuntimeError(f"集保開放資料 HTTP {status}")
    rows = parse_tdcc(raw.decode("utf-8-sig", errors="replace"), set(codes))
    if not rows:
        raise RuntimeError("集保開放資料沒有指定代號的列")
    return rows


def fetch_stocks(cfg, meta):
    codes = list(cfg.get("stocks") or [])
    if not codes:
        return
    jobs = (
        ("chips/tw_stock_margin", lambda: stock_margin(codes), 2, FINMIND_SOURCE, "個股融資融券"),
        ("chips/tw_holders", lambda: holders(codes), 3, TDCC_SOURCE, "集保股權分散"),
        ("fundamentals/tw_revenue", lambda: revenue(codes), 2, FINMIND_SOURCE, "月營收"),
    )
    for key, fn, key_len, source, label in jobs:
        path = PATHS[key]
        try:
            new = fn()
            if not new:
                raise RuntimeError("來源沒有回傳資料")
            rows, changed = merge(path, new, key_len)
            m = {"status": "ok", "source": source, "rows": len(rows), "first": rows[0][0], "last": rows[-1][0],
                 "codes": codes, "changed": len(changed)}
            if key == "chips/tw_holders":
                problems = check_holders(new)
                m["cross_check"] = "；".join(problems) or "分級加總＝合計、占比 100%"
                m["cross_check_ok"] = not problems
            else:
                m["cross_check"] = "單一來源，未核對"
            meta["datasets"][key] = m
        except Exception as ex:  # noqa: BLE001 — 單一份失敗保留舊檔
            mark_failed(meta, key, path, f"stocks {label}", ex)

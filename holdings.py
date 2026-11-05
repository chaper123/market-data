"""ETF 每日持股（00981A 統一投信、009816 凱基投信）。2026-11-06 起只寫進私人 repo，不再放公開的 market-data。

統一投信網站寫明「任何人未經同意，不得將網站全部或部分內容轉載」，凱基投信網站寫明「未經授權不得複製、修改或散發引用」，
所以 fetch.py 不再呼叫這個模組（見 fetch.DISABLED），改由私人 repo chaper123/market-data-private 的 workflow 每天執行：
  python holdings.py --out <私人 repo 的 data 資料夾>
公開 repo 的 data/holdings/ 保留到停用前，新聞晨報排程改讀私人 repo 後刪除。

config.json 的 holdings：代號 → {"source": "ezmoney" 或 "kgifund", "fund": 投信的基金代碼}（舊格式的字串＝ezmoney）。
  ezmoney：基金頁內嵌 DataAsset JSON（淨資產、單位數、每單位淨值、各資產與持股明細）。第一次請求回 302 並設 cookie，
           帶 cookie 再進同一頁才有內容，所以用 cookie opener。持有美股的 ETF 資料日通常晚一天。
  kgifund：基金頁「持股比重」區塊的 HTML（基金資產、股票與期貨表格），另讀「定審頻率」表。沒有每檔金額（amount＝null）。

輸出（<out>/holdings/）：
  <代號>.json         最新持股：date（持股資料日）、nav、units、net_assets、holdings（code、name、shares、amount、weight、
                      currency）、futures、other_assets、top10、reviews（定審紀錄，kgifund 才有），以及與前一個資料日的比較
                      changes：top10_in、top10_out、weight_moves（權重變動 ≥ WEIGHT_MOVE 個百分點）、added、removed
  <代號>_history.csv  date,code,name,shares,weight（逐日累積）
  meta.json           這次執行各檔的狀態與核對（只有 --out 執行時寫）
核對：每單位淨值 × 流通單位數 vs 淨資產（差 0.1% 以內）；持股權重加總 vs 股票市值 ÷ 淨資產（差 0.5 個百分點以內）。
"""
import argparse
import html
import json
import os
import re
import sys
import time

from common import DATA, NOW, ROOT, TPE, cookie_opener, http, mark_failed, read_rows, write_json, write_rows

SOURCES = {
    "ezmoney": ("統一投信 ezmoney", "https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode={}"),
    "kgifund": ("凱基投信", "https://www.kgifund.com.tw/Fund/Detail?fundID={}"),
}
TOP_N = 10
WEIGHT_MOVE = 1.0


def fund_spec(v):
    """config 的值 → (source, fund)。舊格式是 ezmoney 的基金代碼字串。"""
    return ("ezmoney", v) if isinstance(v, str) else (v["source"], v["fund"])


def parse_ezmoney(raw):
    m = re.search(r'id="DataAsset" data-content="([^"]*)"', raw.decode("utf-8", "replace"))
    if not m:
        raise RuntimeError("頁面沒有 DataAsset")
    assets = json.loads(html.unescape(m.group(1)))
    head = {a["AssetCode"]: a.get("Value") for a in assets if not a.get("Details")}
    rows = [d for a in assets for d in (a.get("Details") or []) if a.get("AssetCode") != "GD"]
    if not rows:
        raise RuntimeError("沒有持股明細")
    day = max(d["TranDate"][:10] for d in rows)
    holdings = sorted(({"code": d["DetailCode"].strip(), "name": (d.get("DetailName") or "").strip(),
                        "shares": d.get("Share"), "amount": d.get("Amount"), "weight": d.get("NavRate"),
                        "currency": d.get("MoneyType")} for d in rows), key=lambda x: -(x["weight"] or 0))
    futures = [{"code": d["DetailCode"], "name": d.get("DetailName"), "position": d.get("Position"),
                "contracts": d.get("Share"), "notional": d.get("Amount"), "weight": d.get("NavRate"), "month": d.get("MTH")}
               for a in assets if a.get("AssetCode") == "GD" for d in (a.get("Details") or [])]
    other = {a["AssetName"]: a.get("Value") for a in assets
             if a.get("AssetCode") not in ("NAV", "OUT_UNIT", "P_UNIT") and a.get("Value")}
    stock_value = sum(a.get("Value") or 0 for a in assets if a.get("AssetCode") == "ST")
    return {"date": day, "nav": head.get("P_UNIT"), "units": head.get("OUT_UNIT"), "net_assets": head.get("NAV"),
            "holdings": holdings, "futures": futures, "other_assets": other, "stock_value": stock_value, "reviews": []}


def num(s):
    """「TWD$207,633,126,266」「38,342,000」「41.36」→ 數字；沒有數字回傳 None。"""
    s = re.sub(r"[^\d.\-]", "", s or "")
    return float(s) if re.search(r"\d", s) else None


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s).strip()


def tables(text):
    """HTML 裡每個表格：(表頭文字, [每列的儲存格文字])。"""
    out = []
    for t in re.findall(r"<table[^>]*>(.*?)</table>", text, re.S):
        head = " ".join(strip_tags(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", t, re.S))
        rows = [[strip_tags(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)]
                for r in re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S)]
        out.append((head, [r for r in rows if r]))
    return out


def parse_kgifund(raw):
    text = html.unescape(raw.decode("utf-8", "replace"))
    m = re.search(r"持股比重\s*</div>\s*<p[^>]*>\s*\((\d{4})/(\d{2})/(\d{2})\)\s*</p>", text)
    if not m:
        raise RuntimeError("頁面沒有「持股比重」與資料日")
    rest = text[m.end():]
    nxt = re.search(r'id="FundDetail-\d+"', rest)
    body = rest[:nxt.start()] if nxt else rest
    pairs = re.findall(r'<span class="fund-asset__list-cell">(.*?)</span>\s*<span class="fund-asset__list-cell">(.*?)</span>',
                       body, re.S)
    assets = {strip_tags(k): num(v) for k, v in pairs}
    tbl = tables(body)
    # 同一份表格有電腦版、手機版兩份，取第一份
    stock = next((rows for head, rows in tbl if "股票代號" in head), None)
    fut = next((rows for head, rows in tbl if "期貨代號" in head), [])
    if not stock:
        raise RuntimeError("沒有股票持股表")
    holdings = sorted(({"code": r[0], "name": r[1], "shares": num(r[2]), "amount": None, "weight": num(r[3]),
                        "currency": "NTD"} for r in stock if len(r) >= 4), key=lambda x: -(x["weight"] or 0))
    futures = [{"code": r[0], "name": r[1], "position": None, "contracts": num(r[2]), "notional": None,
                "weight": num(r[3]), "month": r[4] if len(r) > 4 else None} for r in fut if len(r) >= 4]
    reviews = []
    for head, rows in tables(text):
        if "定審生效日" in head:
            reviews = [{"effective": r[0].replace("/", "-"), "before": num(r[1]), "added": num(r[2]),
                        "removed": num(r[3]), "after": num(r[4])} for r in rows if len(r) >= 5]
            break
    main_keys = ("基金淨值資產價值", "基金在外流通單位數", "基金每單位淨值")
    return {"date": "-".join(m.groups()), "nav": assets.get("基金每單位淨值"), "units": assets.get("基金在外流通單位數"),
            "net_assets": assets.get("基金淨值資產價值"), "holdings": holdings, "futures": futures,
            "other_assets": {k: v for k, v in assets.items() if k not in main_keys and v},
            "stock_value": assets.get("股票") or 0, "reviews": reviews}


PARSERS = {"ezmoney": parse_ezmoney, "kgifund": parse_kgifund}


def compare(now, prev_rows, prev_day):
    """與前一個資料日比較前十大與權重。prev_rows: {code: (name, weight)}。"""
    if not prev_rows:
        return {"compare_date": None, "note": "沒有前一日資料，未比較"}
    cur = {h["code"]: (h["name"], h["weight"] or 0) for h in now}
    top_now = [h["code"] for h in now[:TOP_N]]
    top_prev = [c for c, _ in sorted(prev_rows.items(), key=lambda x: -x[1][1])[:TOP_N]]
    moves = []
    for c in sorted(set(top_now) | set(top_prev)):
        w0, w1 = prev_rows.get(c, ("", 0))[1], cur.get(c, ("", 0))[1]
        if abs(w1 - w0) >= WEIGHT_MOVE:
            moves.append({"code": c, "name": (cur.get(c) or prev_rows.get(c))[0], "prev": w0, "now": w1,
                          "change": round(w1 - w0, 2)})
    name = lambda c: (cur.get(c) or prev_rows.get(c))[0]  # noqa: E731
    return {"compare_date": prev_day,
            "top10_in": [{"code": c, "name": name(c)} for c in top_now if c not in top_prev],
            "top10_out": [{"code": c, "name": name(c)} for c in top_prev if c not in top_now],
            "weight_moves": sorted(moves, key=lambda x: -abs(x["change"])),
            "added": [{"code": c, "name": name(c)} for c in cur if c not in prev_rows],
            "removed": [{"code": c, "name": name(c)} for c in prev_rows if c not in cur]}


def cross_check(p):
    """回傳 (說明, 是否通過)。"""
    checks, ok = [], True
    if p["nav"] and p["units"] and p["net_assets"]:
        diff = (p["nav"] * p["units"] - p["net_assets"]) / p["net_assets"] * 100
        checks.append(f"淨值 {p['nav']} × 單位數 vs 淨資產（差 {diff:+.3f}%）")
        ok = abs(diff) <= 0.1
    else:
        checks.append("缺淨值、單位數或淨資產，未核對")
        ok = False
    if p["net_assets"]:
        wsum = sum(h["weight"] or 0 for h in p["holdings"])
        ratio = p["stock_value"] / p["net_assets"] * 100
        checks.append(f"權重加總 {wsum:.2f}% vs 股票市值／淨資產 {ratio:.2f}%")
        ok = ok and abs(wsum - ratio) <= 0.5
    return checks, ok


def fetch_holdings(cfg, meta, out=DATA):
    for code, spec in cfg["holdings"].items():
        source, fund = fund_spec(spec)
        label, url = SOURCES[source][0], SOURCES[source][1].format(fund)
        key = f"holdings/{code}"
        jpath = os.path.join(out, "holdings", f"{code}.json")
        hpath = os.path.join(out, "holdings", f"{code}_history.csv")
        try:
            status, raw = http(url, opener=cookie_opener())
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            p = PARSERS[source](raw)
            day = p["date"]
            hist = [r for r in read_rows(hpath) if r[0] != day]  # 同一資料日重抓時以新的為準
            prev_day = max((r[0] for r in hist if r[0] < day), default=None)
            prev_rows = {r[1]: (r[2], float(r[4])) for r in hist if r[0] == prev_day}
            changes = compare(p["holdings"], prev_rows, prev_day)
            hist += [[day, h["code"], h["name"], int(h["shares"] or 0), h["weight"]] for h in p["holdings"]]
            checks, ok = cross_check(p)
            snap = {"code": code, "fund_code": fund, "source": url, "source_name": label,
                    "fetched_at_taipei": NOW.astimezone(TPE).strftime("%Y-%m-%d %H:%M"),
                    **{k: p[k] for k in ("date", "nav", "units", "net_assets")},
                    "top10": [{k: h[k] for k in ("code", "name", "weight")} for h in p["holdings"][:TOP_N]],
                    "changes": changes, "holdings": p["holdings"], "futures": p["futures"],
                    "other_assets": p["other_assets"], "reviews": p["reviews"]}
            write_json(jpath, snap)
            write_rows(hpath, sorted(hist, key=lambda r: (r[0], -float(r[4]))))
            meta["datasets"][key] = {
                "status": "ok", "source": label, "last": day, "holdings": len(p["holdings"]),
                "nav": p["nav"], "top1": f"{p['holdings'][0]['code']} {p['holdings'][0]['weight']}%",
                "compare_date": changes.get("compare_date"),
                "top10_in": len(changes.get("top10_in", [])), "top10_out": len(changes.get("top10_out", [])),
                "weight_moves": len(changes.get("weight_moves", [])),
                "cross_check": "；".join(checks), "cross_check_ok": ok}
            if not ok:
                meta["errors"].append(f"holdings {code}: 核對不符（{'；'.join(checks)}）")
        except Exception as ex:  # noqa: BLE001
            mark_failed(meta, key, jpath, f"holdings {code}", ex)
        time.sleep(1)


def main(argv=None):
    ap = argparse.ArgumentParser(description="ETF 每日持股（私人 repo 的 workflow 用）")
    ap.add_argument("--out", required=True, help="輸出的 data 資料夾（寫到 <out>/holdings/）")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    a = ap.parse_args(argv)
    with open(a.config, encoding="utf-8") as f:
        cfg = json.load(f)
    meta = {"generated_at_taipei": NOW.astimezone(TPE).strftime("%Y-%m-%d %H:%M"), "datasets": {}, "errors": []}
    fetch_holdings(cfg, meta, a.out)
    write_json(os.path.join(a.out, "holdings", "meta.json"), meta)
    print(json.dumps(meta, ensure_ascii=False, indent=1))
    return 1 if meta["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())

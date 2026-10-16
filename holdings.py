"""Phase 1 ⑥：主動式 ETF 每日持股（統一投信 ezmoney，由 fetch.py 在完整抓取時呼叫）。

基金頁內嵌 DataAsset JSON（淨資產、單位數、每單位淨值、各資產與持股明細）。第一次請求回 302 並設 cookie，
帶 cookie 再進同一頁才有內容，所以用 cookie opener。持有美股的 00411A 資料日通常比 00981A 晚一天。

輸出：
  data/holdings/<代號>.json         最新持股：date（持股資料日）、nav、units、net_assets、holdings（code、name、shares、
                                    amount、weight、currency）、other_assets（期貨、現金等）、top10，以及與前一個資料日的比較
                                    changes：top10_in、top10_out、weight_moves（權重變動 ≥ WEIGHT_MOVE 個百分點）、added、removed
  data/holdings/<代號>_history.csv  date,code,name,shares,weight（逐日累積）
核對：每單位淨值 vs repo 的 tw/nav_history.csv 同一天淨值（證交所 mis）；持股權重加總 vs 股票市值 ÷ 淨資產。
"""
import html
import json
import os
import re
import time

from common import DATA, NOW, TPE, cookie_opener, http, mark_failed, read_rows, write_json, write_rows

URL = "https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode={}"
TOP_N = 10
WEIGHT_MOVE = 1.0


def parse(raw):
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
            "holdings": holdings, "futures": futures, "other_assets": other, "stock_value": stock_value}


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


def mis_nav(code, day):
    for r in read_rows(os.path.join(DATA, "tw", "nav_history.csv")):
        if r[0] == day and r[1] == code:
            return float(r[2])
    return None


def fetch_holdings(cfg, meta):
    for code, fund in cfg["holdings"].items():
        key = f"holdings/{code}"
        jpath = os.path.join(DATA, "holdings", f"{code}.json")
        hpath = os.path.join(DATA, "holdings", f"{code}_history.csv")
        try:
            status, raw = http(URL.format(fund), opener=cookie_opener())
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            p = parse(raw)
            day = p["date"]
            hist = [r for r in read_rows(hpath) if r[0] != day]  # 同一資料日重抓時以新的為準
            prev_day = max((r[0] for r in hist if r[0] < day), default=None)
            prev_rows = {r[1]: (r[2], float(r[4])) for r in hist if r[0] == prev_day}
            changes = compare(p["holdings"], prev_rows, prev_day)
            hist += [[day, h["code"], h["name"], int(h["shares"] or 0), h["weight"]] for h in p["holdings"]]
            # 核對
            checks, ok = [], True
            ref = mis_nav(code, day)
            if ref is None:
                checks.append(f"nav_history 沒有 {day}，淨值未核對")
            else:
                diff = p["nav"] - ref
                checks.append(f"{day} 淨值 {p['nav']} vs 證交所 mis {ref}（差 {diff:+.2f}）")
                ok = ok and abs(diff) <= 0.011
            wsum = sum(h["weight"] or 0 for h in p["holdings"])
            ratio = p["stock_value"] / p["net_assets"] * 100 if p["net_assets"] else None
            if ratio is not None:
                checks.append(f"權重加總 {wsum:.2f}% vs 股票市值／淨資產 {ratio:.2f}%")
                ok = ok and abs(wsum - ratio) <= 0.5
            snap = {"code": code, "fund_code": fund, "source": URL.format(fund),
                    "fetched_at_taipei": NOW.astimezone(TPE).strftime("%Y-%m-%d %H:%M"),
                    **{k: p[k] for k in ("date", "nav", "units", "net_assets")},
                    "top10": [{k: h[k] for k in ("code", "name", "weight")} for h in p["holdings"][:TOP_N]],
                    "changes": changes, "holdings": p["holdings"], "futures": p["futures"],
                    "other_assets": p["other_assets"]}
            write_json(jpath, snap)
            write_rows(hpath, sorted(hist, key=lambda r: (r[0], -float(r[4]))))
            meta["datasets"][key] = {
                "status": "ok", "source": "統一投信 ezmoney", "last": day, "holdings": len(p["holdings"]),
                "nav": p["nav"], "top1": f"{p['holdings'][0]['code']} {p['holdings'][0]['weight']}%",
                "compare_date": changes.get("compare_date"),
                "top10_in": len(changes.get("top10_in", [])), "top10_out": len(changes.get("top10_out", [])),
                "weight_moves": len(changes.get("weight_moves", [])),
                "cross_check": "；".join(checks), "cross_check_ok": ok}
        except Exception as ex:  # noqa: BLE001
            mark_failed(meta, key, jpath, f"holdings {code}", ex)
        time.sleep(1)

"""技術面雷達：把 TradingView 日線快照用固定規則讀成「結構、位置、訊號矛盾、關鍵價位」。

這一層只做描述，不產生買賣訊號，也不改變排程任務 monitor.py / monitor_us.py 的買區判定。
規則寫死在這裡，每天的結論可以互相比較。

輸入：data/tv/snapshot.json；輸出：data/radar.json（結構化）、data/radar.md（給報告引用）。
"""
import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")

PERIODS = [10, 20, 30, 50, 100, 200]
TREND_ADX, RANGE_ADX = 25, 20


def rec_label(v):
    """TradingView 綜合評分區間。"""
    if v is None:
        return "無資料"
    if v > 0.5:
        return "強力買進"
    if v > 0.1:
        return "買進"
    if v >= -0.1:
        return "中立"
    if v >= -0.5:
        return "賣出"
    return "強力賣出"


def f(x, nd=2):
    return "—" if x is None else f"{x:,.{nd}f}"


def analyse(code, s):
    px = s.get("close")
    adx, pdi, mdi = s.get("ADX"), s.get("ADX+DI"), s.get("ADX-DI")
    rsi, srsi, wr = s.get("RSI"), s.get("Stoch.RSI.K"), s.get("W.R")
    rec_all, rec_ma, rec_osc = s.get("Recommend.All"), s.get("Recommend.MA"), s.get("Recommend.Other")
    hull = s.get("HullMA9")
    r1, p, s1 = s.get("Pivot.M.Classic.R1"), s.get("Pivot.M.Classic.Middle"), s.get("Pivot.M.Classic.S1")

    # 1. 結構：ADX 決定均線排列有沒有意義
    if adx is None:
        structure, trend = "ADX 無資料", None
    elif adx > TREND_ADX:
        direction = "多方" if (pdi or 0) >= (mdi or 0) else "空方"
        structure, trend = f"有趨勢（ADX {adx:.1f} > {TREND_ADX}，{direction}主導）", "trend"
    elif adx < RANGE_ADX:
        structure, trend = f"盤整（ADX {adx:.1f} < {RANGE_ADX}）：均線排列參考性低，改看 Pivot 區間上下緣", "range"
    else:
        structure, trend = f"趨勢不明（ADX {adx:.1f}，介於 {RANGE_ADX}～{TREND_ADX}）", "weak"

    # 2. 位置：站上哪些均線、分界點在哪
    mas, missing = [], []
    for n in PERIODS:
        for kind in ("EMA", "SMA"):
            v = s.get(f"{kind}{n}")
            if v is None:
                missing.append(f"{kind}{n}")
            else:
                mas.append((f"{kind}{n}", n, v, px is not None and px > v))
    above = [m for m in mas if m[3]]
    below = [m for m in mas if not m[3]]
    if not mas or px is None:
        position = "均線無資料"
    elif not below:
        position = f"{len(mas)} 條均線全數站上"
    elif not above:
        position = f"{len(mas)} 條均線全數跌破"
    else:
        short_above = all(n <= 30 for _, n, _, _ in above)
        long_below = any(n >= 50 for _, n, _, _ in below)
        lowest_broken = min(below, key=lambda m: m[1])
        position = f"站上 {len(above)}/{len(mas)} 條；分界在 {lowest_broken[0]}（{f(lowest_broken[2])}）"
        if short_above and long_below:
            position += "；只站上短均線，屬反彈，不是翻多"
        elif not short_above and any(n <= 20 for _, n, _, _ in below):
            position += "；長均線之上但短均線失守，短線轉弱"
    if missing:
        position += f"（樣本不足：{'、'.join(missing)}）"

    # 3. 訊號矛盾：明確指出哪兩個訊號在打架；沒有就說訊號一致
    c = []
    if rec_all is not None and rec_all > 0.1 and trend == "range":
        c.append(f"綜合評分{rec_label(rec_all)}，但 ADX {adx:.1f} < 20 → 沒有趨勢支撐，突破容易失敗")
    if rec_all is not None and rec_all < -0.1 and trend == "range":
        c.append(f"綜合評分{rec_label(rec_all)}，但 ADX {adx:.1f} < 20 → 下跌也缺乏趨勢，容易在區間內反彈")
    hot = [x for x in (f"RSI {rsi:.1f}" if rsi and rsi > 70 else None,
                       f"StochRSI {srsi:.1f}" if srsi and srsi > 90 else None,
                       f"W%R {wr:.1f}" if wr is not None and wr > -10 else None) if x]
    cold = [x for x in (f"RSI {rsi:.1f}" if rsi and rsi < 30 else None,
                        f"StochRSI {srsi:.1f}" if srsi is not None and srsi < 10 else None,
                        f"W%R {wr:.1f}" if wr is not None and wr < -90 else None) if x]
    if rec_all is not None and rec_all > 0.1 and hot:
        c.append(f"綜合評分{rec_label(rec_all)}，但 {'、'.join(hot)} 超買 → 短線過熱，追高風險高")
    if rec_all is not None and rec_all < -0.1 and cold:
        c.append(f"綜合評分{rec_label(rec_all)}，但 {'、'.join(cold)} 超賣 → 短線殺過頭，追空風險高")
    if rec_ma is not None and rec_osc is not None and abs(rec_ma) > 0.1 and abs(rec_osc) > 0.1 \
            and rec_ma * rec_osc < 0:
        c.append(f"均線評分{rec_label(rec_ma)}、振盪評分{rec_label(rec_osc)} → 趨勢與動能方向相反")
    if hull is not None and px is not None and mas:
        long_ok = all(m[3] for m in mas if m[1] >= 20)
        long_bad = all(not m[3] for m in mas if m[1] >= 20)
        if long_ok and px < hull:
            c.append(f"中長均線都在下方，唯獨 Hull(9) {f(hull)} 在現價之上 → 最快的均線先轉弱，常是回檔第一個訊號")
        if long_bad and px > hull:
            c.append(f"中長均線都在上方壓著，唯獨 Hull(9) {f(hull)} 翻上 → 最快的均線先轉強，可能是止跌第一個訊號")
    if r1 and px and px > r1:
        c.append(f"現價已在月 R1 {f(r1)} 之上 {((px / r1 - 1) * 100):.1f}% → 本月上漲空間已走完不少")
    if s1 and px and px < s1:
        c.append(f"現價已跌破月 S1 {f(s1)} {((px / s1 - 1) * 100):.1f}% → 本月跌幅偏大，留意是否止穩")
    contradictions = c or ["訊號一致"]

    # 4. 關鍵價位：最近的上方壓力與下方支撐（均線 + 月 Pivot + Hull）
    levels = [(name, v) for name, _, v, _ in mas]
    levels += [(n, v) for n, v in (("月 R1", r1), ("月 Pivot", p), ("月 S1", s1), ("Hull(9)", hull)) if v]
    sup = sorted([lv for lv in levels if px and lv[1] < px], key=lambda lv: -lv[1])[:2]
    res = sorted([lv for lv in levels if px and lv[1] >= px], key=lambda lv: lv[1])[:2]

    if trend == "trend" and (pdi or 0) >= (mdi or 0) and not hot:
        one = "多頭趨勢成立，動能未過熱"
    elif trend == "trend" and (pdi or 0) >= (mdi or 0):
        one = "多頭趨勢成立，但短線過熱，不宜追高、回檔才有位置"
    elif trend == "trend":
        one = "空方趨勢成立，不宜接刀"
    elif trend == "range":
        one = "盤整格局，看區間上下緣，不看均線排列"
    else:
        one = "趨勢不明，等方向確認"

    return {
        "code": code, "tv_symbol": s.get("tv_symbol"), "close": px, "change_pct": s.get("change"),
        "rating": {"all": rec_label(rec_all), "ma": rec_label(rec_ma), "osc": rec_label(rec_osc),
                   "all_value": rec_all},
        "one_line": one, "structure": structure, "position": position, "contradictions": contradictions,
        "support": [{"name": n, "price": round(v, 2)} for n, v in sup],
        "resistance": [{"name": n, "price": round(v, 2)} for n, v in res],
        "pivot_m": {"R1": r1, "P": p, "S1": s1},
        "osc": {"RSI": rsi, "StochRSI": srsi, "W%R": wr, "ADX": adx, "+DI": pdi, "-DI": mdi,
                "MACD": s.get("MACD.macd"), "MACD_signal": s.get("MACD.signal"), "CCI": s.get("CCI20"),
                "ATR": s.get("ATR")},
    }


def md(a):
    lv = lambda xs: "、".join(f"{x['name']} {f(x['price'])}" for x in xs) or "—"
    lines = [f"**{a['code']}** {f(a['close'])}（{f(a['change_pct'])}%）｜綜合 {a['rating']['all']}"
             f"（均線 {a['rating']['ma']}／振盪 {a['rating']['osc']}）",
             f"- 一句話：{a['one_line']}",
             f"- 結構：{a['structure']}",
             f"- 位置：{a['position']}"]
    lines += [f"- 矛盾：{x}" for x in a["contradictions"]]
    lines.append(f"- 關鍵價位：上檔 {lv(a['resistance'])}；下檔 {lv(a['support'])}")
    return "\n".join(lines)


def main():
    snap = json.load(open(os.path.join(DATA, "tv", "snapshot.json")))
    out = {"source_fetched_at_taipei": snap.get("fetched_at_taipei"),
           "tw_bar_is_intraday": snap.get("tw_bar_is_intraday"),
           "rules": {"trend_adx": TREND_ADX, "range_adx": RANGE_ADX},
           "symbols": {code: analyse(code, s) for code, s in snap["symbols"].items()}}
    with open(os.path.join(DATA, "radar.json"), "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    head = [f"# 技術面雷達（TradingView 日線，抓取於台北 {out['source_fetched_at_taipei']}）",
            "", "只描述技術面結構，不改變買區判定。"]
    if out["tw_bar_is_intraday"]:
        head.append("台股當日 K 棒尚未收盤，數值會隨盤中變動。")
    body = [md(a) for a in out["symbols"].values()]
    with open(os.path.join(DATA, "radar.md"), "w") as fh:
        fh.write("\n".join(head) + "\n\n" + "\n\n".join(body) + "\n")
    print(f"radar: {len(body)} symbols")


if __name__ == "__main__":
    main()

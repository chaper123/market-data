"""技術面雷達：用固定規則把日線讀成「結構、位置、訊號矛盾、關鍵價位」。

這一層只做描述，不產生買賣訊號，也不改變排程任務 monitor.py / monitor_us.py 的買區判定。
規則寫死在這裡，每天的結論可以互相比較。

指標（均線、Hull、RSI、Stoch RSI、W%R、ADX/DI、ATR、月 Pivot）一律由 repo 自己的官方日線計算
（indicators.py，定義與 TradingView 預設相同，已實測數值一致）。
TradingView 只提供「綜合評分」，而且只有在它的收盤、SMA20、ATR 與 repo 日線一致時才採用：
TradingView 偶爾有錯誤的 K 棒（例如 00411A 的高低點），會讓 ADX、ATR、Pivot 全部失真。

輸入：data/us、data/tw 日線與 data/tv/snapshot.json；輸出：data/radar.json、data/radar.md。
"""
import json
import os

import indicators

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
        short_above_only = all(n <= 30 for _, n, _, _ in above) and any(n >= 50 for _, n, _, _ in below)
        long_all_above = all(m[3] for m in mas if m[1] >= 50)
        short_broken = any(n <= 20 for _, n, _, _ in below)
        above_names = "、".join(m[0] for m in sorted(above, key=lambda m: m[1]))
        position = f"站上 {len(above)}/{len(mas)} 條（{above_names}）"
        if short_above_only:
            position += "；只站上短均線，屬反彈，不是翻多"
        elif long_all_above and short_broken:
            position += "；中長均線之上但短均線失守，短線轉弱"
        elif not long_all_above and not any(n <= 20 for _, n, _, _ in above):
            position += "；短均線全數失守、只剩部分長均線撐著，結構偏弱"
        else:
            position += "；長短均線交錯"
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
    if a.get("rating_skipped"):
        rating = f"綜合評分略過（{a['rating_skipped']}）"
    else:
        rating = f"綜合 {a['rating']['all']}（均線 {a['rating']['ma']}／振盪 {a['rating']['osc']}）"
    lines = [f"**{a['code']}** {f(a['close'])}（{a.get('last_date', '')}，{f(a['change_pct'])}%）｜{rating}",
             f"- 一句話：{a['one_line']}",
             f"- 結構：{a['structure']}",
             f"- 位置：{a['position']}"]
    lines += [f"- 矛盾：{x}" for x in a["contradictions"]]
    lines.append(f"- 關鍵價位：上檔 {lv(a['resistance'])}；下檔 {lv(a['support'])}")
    return "\n".join(lines)


RATING_KEYS = ("Recommend.All", "Recommend.MA", "Recommend.Other")


def tv_ratings(mine, tv):
    """TradingView 評分只在它的數據與 repo 日線一致時採用；回傳 (評分 dict, 略過原因)。"""
    if not tv:
        return {}, "TradingView 無此標的"
    checks = []
    if tv.get("close") and abs(tv["close"] / mine["close"] - 1) > 0.003:
        checks.append(f"收盤 {f(tv['close'])} ≠ {f(mine['close'])}（可能是盤中數字）")
    if tv.get("SMA20") and mine.get("SMA20") and abs(tv["SMA20"] / mine["SMA20"] - 1) > 0.01:
        checks.append("SMA20 不一致")
    if tv.get("ATR") and mine.get("ATR") and not 0.8 <= tv["ATR"] / mine["ATR"] <= 1.25:
        checks.append(f"ATR {f(tv['ATR'])} vs {f(mine['ATR'])}（TradingView 高低點資料有誤）")
    if checks:
        return {}, "；".join(checks)
    return {k: tv.get(k) for k in RATING_KEYS}, None


def main():
    cfg = json.load(open(os.path.join(ROOT, "config.json")))
    try:
        snap = json.load(open(os.path.join(DATA, "tv", "snapshot.json")))
    except Exception:  # noqa: BLE001 — TradingView 失效時雷達照算，只是沒有評分
        snap = {"symbols": {}}
    paths = {c: os.path.join(DATA, "us", f"{c}.csv") for c in cfg["us"]}
    paths.update({c: os.path.join(DATA, "tw", f"{c}_norm.csv") for c in cfg["tw"]})
    symbols = {}
    for code, path in paths.items():
        if not os.path.exists(path):
            continue
        mine = indicators.compute(path)
        rating, skip = tv_ratings(mine, snap["symbols"].get(code))
        a = analyse(code, {**mine, **rating})
        a.update(last_date=mine["last_date"], rows=mine["rows"], rating_skipped=skip)
        symbols[code] = a
    out = {"generated_from": "repo 日線（indicators.py）；綜合評分來自 TradingView",
           "tv_fetched_at_taipei": snap.get("fetched_at_taipei"),
           "rules": {"trend_adx": TREND_ADX, "range_adx": RANGE_ADX},
           "symbols": symbols}
    with open(os.path.join(DATA, "radar.json"), "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    head = ["# 技術面雷達（repo 官方日線計算，只含已收盤的交易日）",
            "", "只描述技術面結構，不改變買區判定。綜合評分來自 TradingView，與官方日線不一致時略過。"]
    body = [md(a) for a in out["symbols"].values()]
    with open(os.path.join(DATA, "radar.md"), "w") as fh:
        fh.write("\n".join(head) + "\n\n" + "\n\n".join(body) + "\n")
    print(f"radar: {len(body)} symbols")


if __name__ == "__main__":
    main()

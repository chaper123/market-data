"""從 repo 自己的日線 CSV 計算技術指標（只用標準庫）。

定義對齊 TradingView 的預設值：RSI(14)、ATR(14)、ADX/DI(14) 用 Wilder 平滑；
Stoch RSI K = RSI(14) 在 14 根內的隨機值再取 3 日均；W%R(14)；Hull(9)；
月 Pivot（Classic）取上一個日曆月的高、低、收。
"""
import csv
import math


def load(path):
    rows = []
    with open(path) as f:
        for r in csv.reader(f):
            if len(r) < 6:
                continue
            rows.append((r[0], float(r[1] or 0), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
    rows.sort()
    return rows


def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def ema_series(xs, n):
    if len(xs) < n:
        return []
    k = 2 / (n + 1)
    e = sum(xs[:n]) / n
    out = [e]
    for x in xs[n:]:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def wma(xs, n):
    if len(xs) < n:
        return None
    w = range(1, n + 1)
    return sum(a * b for a, b in zip(xs[-n:], w)) / sum(w)


def rma_series(xs, n):
    """Wilder 平滑：第一個值是前 n 個的平均。"""
    if len(xs) < n:
        return []
    v = sum(xs[:n]) / n
    out = [v]
    for x in xs[n:]:
        v = (v * (n - 1) + x) / n
        out.append(v)
    return out


def rsi_series(closes, n=14):
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = rma_series(gains, n), rma_series(losses, n)
    return [100.0 if l == 0 else 100 - 100 / (1 + g / l) for g, l in zip(ag, al)]


def hull(closes, n=9):
    half, root = n // 2, int(round(math.sqrt(n)))
    if len(closes) < n + root:
        return None
    diff = [2 * wma(closes[:i], half) - wma(closes[:i], n) for i in range(len(closes) - root + 1, len(closes) + 1)]
    return wma(diff, root)


def adx(rows, n=14):
    if len(rows) < 2 * n + 1:
        return None, None, None
    tr, pdm, mdm = [], [], []
    for i in range(1, len(rows)):
        h, l, pc = rows[i][3], rows[i][4], rows[i - 1][5]
        ph, pl = rows[i - 1][3], rows[i - 1][4]
        up, dn = h - ph, pl - l
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    atr, sp, sm = rma_series(tr, n), rma_series(pdm, n), rma_series(mdm, n)
    pdi = [100 * p / a if a else 0 for p, a in zip(sp, atr)]
    mdi = [100 * m / a if a else 0 for m, a in zip(sm, atr)]
    dx = [100 * abs(p - m) / (p + m) if p + m else 0 for p, m in zip(pdi, mdi)]
    a = rma_series(dx, n)
    return (a[-1] if a else None), pdi[-1], mdi[-1]


def atr(rows, n=14):
    tr = [max(rows[i][3] - rows[i][4], abs(rows[i][3] - rows[i - 1][5]), abs(rows[i][4] - rows[i - 1][5]))
          for i in range(1, len(rows))]
    s = rma_series(tr, n)
    return s[-1] if s else None


def stoch_rsi_k(closes, n=14, k=3):
    r = rsi_series(closes, n)
    if len(r) < n + k - 1:
        return None
    vals = []
    for i in range(len(r) - k + 1, len(r) + 1):
        win = r[i - n:i]
        lo, hi = min(win), max(win)
        vals.append(100 * (win[-1] - lo) / (hi - lo) if hi > lo else 50.0)
    return sum(vals) / k


def williams_r(rows, n=14):
    if len(rows) < n:
        return None
    hh = max(r[3] for r in rows[-n:])
    ll = min(r[4] for r in rows[-n:])
    return -100 * (hh - rows[-1][5]) / (hh - ll) if hh > ll else -50.0


def monthly_pivot(rows):
    """上一個日曆月（相對最後一筆日期）的 Classic Pivot。"""
    last = rows[-1][0][:7]
    months = sorted({r[0][:7] for r in rows if r[0][:7] < last})
    if not months:
        return None
    m = [r for r in rows if r[0][:7] == months[-1]]
    h, l, c = max(r[3] for r in m), min(r[4] for r in m), m[-1][5]
    p = (h + l + c) / 3
    return {"R1": 2 * p - l, "P": p, "S1": 2 * p - h, "month": months[-1]}


def compute(path):
    rows = load(path)
    closes = [r[5] for r in rows]
    out = {"rows": len(rows), "last_date": rows[-1][0], "close": closes[-1],
           "change_pct": (closes[-1] / closes[-2] - 1) * 100 if len(closes) >= 2 else None}
    out["change"] = out["change_pct"]
    for n in (10, 20, 30, 50, 100, 200):
        out[f"SMA{n}"] = sma(closes, n)
        e = ema_series(closes, n)
        out[f"EMA{n}"] = e[-1] if e else None
    out["HullMA9"] = hull(closes)
    r = rsi_series(closes)
    out["RSI"] = r[-1] if r else None
    out["Stoch.RSI.K"] = stoch_rsi_k(closes)
    out["W.R"] = williams_r(rows)
    out["ADX"], out["ADX+DI"], out["ADX-DI"] = adx(rows)
    out["ATR"] = atr(rows)
    pv = monthly_pivot(rows)
    if pv:
        out["Pivot.M.Classic.R1"], out["Pivot.M.Classic.Middle"], out["Pivot.M.Classic.S1"] = pv["R1"], pv["P"], pv["S1"]
        out["pivot_month"] = pv["month"]
    return out

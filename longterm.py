"""長週期總經資料（回測用）：只由 longterm.yml 手動觸發，不在 fetch.yml 的定期抓取裡。

  國發會景氣對策信號（月）：data.gov.tw 開放資料「景氣指標及燈號」（資料集 6099，zip，政府資料開放授權條款第 1 版），
      另用國發會景氣指標查詢系統的 JSON 核對，並補 zip 還沒更新的月份
  巴菲特指標（季）：FRED 的美國非金融企業股權市值（NCBEILQ027S，Fed Z.1）÷ 名目 GDP（GDP，BEA），
      公布日期取 Fed Z.1 的發布日（1996Q2 起有清單），更早的用估計

輸出（無表頭 CSV，欄位見 README）：
  data/longterm/tw_business_cycle.csv
  data/longterm/buffett_indicator.csv
meta.json 只寫 longterm/ 兩個資料集（推回 repo 用 publish.py --meta-own longterm/）。

注意：兩份都是「最新修正值」，不是當時公布的初值（回測會有些微的前視偏誤）。景氣燈號從這支第一次跑起，
每次發布的最新月份會另外記下初值（score_first），要每月跑一次才記得到。
"""
import calendar
import csv
import datetime as dt
import io
import json
import math
import os
import re
import sys
import zipfile

from common import DATA, NOW, TPE, cookie_opener, get_json, http, mark_failed, read_rows, write_json, write_rows

OUT = os.path.join(DATA, "longterm")
META = os.path.join(DATA, "meta.json")
CYCLE_CSV = os.path.join(OUT, "tw_business_cycle.csv")
BUFFETT_CSV = os.path.join(OUT, "buffett_indicator.csv")
BOT_UA = "market-data/1.0 (+https://github.com/chaper123/market-data)"  # FRED 會擋瀏覽器 UA（連線被重置）

# ---------------------------------------------------------------- 國發會景氣對策信號
DGTW_API = "https://data.gov.tw/api/v2/rest/dataset/6099"
NDC_ZIP = ("https://ws.ndc.gov.tw/Download.ashx?u=LzAwMS9hZG1pbmlzdHJhdG9yLzEwL3JlbGZpbGUvNTc4MS82MzkyL2VhMjM1YmQ5LWQw"
           "NTItNGE2OS1hYmZjLWQ1Yzc4NWQzZDBlMi56aXA%3d&n=5pmv5rCj5oyH5qiZ5Y%2bK54eI6JmfLnppcA%3d%3d&icon=.zip")  # data.gov.tw 查不到時用
NDC_PAGE = "https://index.ndc.gov.tw/n/zh_tw/data/eco"
NDC_API = "https://index.ndc.gov.tw/n/json/data/eco/indicators"
LIGHTS = ((38, "紅"), (32, "黃紅"), (23, "綠"), (17, "黃藍"), (9, "藍"))  # 綜合判斷分數下限 → 燈號（9～45 分）
LIGHT_CODE = {1: "藍", 2: "黃藍", 3: "綠", 4: "黃紅", 5: "紅"}  # 查詢系統 JSON 的燈號代碼（SR0030）
RELEASE_DAY = 30  # 估計發布日：次月 30 日（2 月用月底）遇週末順延。2025-10～2026-09 實際都在 27～30 日，所以偏保守
MIN_MONTHS = 400


def light_of(score):
    return next(name for lo, name in LIGHTS if score >= lo)


def release_est(month):
    """'YYYY-MM' 的估計發布日（不會早於實際發布；國定假日沒考慮）。"""
    y, m = map(int, month.split("-"))
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    d = dt.date(y, m, min(RELEASE_DAY, calendar.monthrange(y, m)[1]))
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d.isoformat()


def parse_ndc_csv(text):
    """zip 裡「景氣指標與燈號」CSV → {YYYY-MM: (分數, 燈號文字)}；沒有分數的月份（1984 年以前）略過。"""
    rows = list(csv.reader(io.StringIO(text)))
    i, j = rows[0].index("景氣對策信號綜合分數"), rows[0].index("景氣對策信號")
    return {f"{r[0][:4]}-{r[0][4:]}": (int(r[i]), r[j].strip())
            for r in rows[1:] if len(r) > j and re.fullmatch(r"\d{6}", r[0]) and r[i].strip().isdigit()}


def ndc_zip():
    """回傳 ({YYYY-MM: (分數, 燈號)}, 檔案日期)。檔名在 zip 裡是亂碼，所以用表頭找檔案。"""
    url = NDC_ZIP
    try:
        url = get_json(DGTW_API)["result"]["distribution"][0]["resourceDownloadUrl"]
    except Exception:  # noqa: BLE001 — data.gov.tw 查不到就用已知網址
        pass
    status, raw = http(url, timeout=60)
    if status != 200:
        raise RuntimeError(f"zip HTTP {status}")
    z = zipfile.ZipFile(io.BytesIO(raw))
    for info in z.infolist():
        text = z.read(info).decode("utf-8-sig", "replace")
        if "景氣對策信號綜合分數" in text.split("\n", 1)[0]:
            return parse_ndc_csv(text), dt.date(*info.date_time[:3]).isoformat()
    raise RuntimeError("zip 裡找不到「景氣對策信號綜合分數」欄")


def ndc_api():
    """國發會景氣指標查詢系統 JSON（先拿頁面的 CSRF token 再 POST）。回傳 ({YYYY-MM: (分數, 燈號)}, 發布日)。"""
    op = cookie_opener()
    status, raw = http(NDC_PAGE, opener=op)
    m = re.search(r'name="csrf-token" content="([^"]+)"', raw.decode("utf-8", "replace"))
    if status != 200 or not m:
        raise RuntimeError(f"查詢系統頁面 HTTP {status}，沒有 CSRF token")
    status, raw = http(NDC_API, data=b"", opener=op,
                       headers={"X-CSRF-TOKEN": m.group(1), "X-Requested-With": "XMLHttpRequest",
                                "Content-Type": "application/json;charset=utf-8", "Referer": NDC_PAGE})
    if status != 200:
        raise RuntimeError(f"查詢系統 JSON HTTP {status}")
    j = json.loads(raw.decode("utf-8-sig"))
    lines = {v.get("code"): v for v in j["line"].values()}
    score = {p["x"]: p["y"] for p in lines["SR0005"]["data"] if p["y"] is not None}
    light = {p["x"]: p["y"] for p in lines["SR0030"]["data"] if p["y"] is not None}
    return {f"{x[:4]}-{x[4:]}": (int(s), LIGHT_CODE.get(light.get(x))) for x, s in score.items()}, j.get("latest_date")


def build_cycle(zip_rows, api_rows, release_date, previous):
    """合併 zip 與 JSON（zip 為主，JSON 補較新的月份），回傳 (rows, notes)。

    列：month, score, light, release_est, release_actual, score_first。
    release_actual／score_first 只在某個月第一次以「最新一期」出現時記下（那時的值就是初值），之後沿用 previous。"""
    base = dict(zip_rows or {})
    added = sorted(set(api_rows or {}) - set(base))
    for k in added:
        base[k] = api_rows[k]
    both = sorted(set(zip_rows or {}) & set(api_rows or {}))
    diff = [k for k in both if zip_rows[k][0] != api_rows[k][0]]
    bad_light = [k for k, (s, lt) in base.items() if lt and lt != light_of(s)]
    out_of_range = [k for k, (s, _) in base.items() if not 9 <= s <= 45]
    if out_of_range:
        raise RuntimeError(f"分數超出 9～45：{out_of_range[:5]}")
    prev = {r[0]: r for r in previous if len(r) >= 6}
    latest, rows = max(base), []
    for month in sorted(base):
        score = base[month][0]
        actual, first = (prev[month][4], prev[month][5]) if month in prev else ("", "")
        if month == latest and not first:
            actual, first = release_date or "", score
        rows.append([month, score, light_of(score), release_est(month), actual, first])
    notes = {"added_from_api": added, "compared": len(both), "diff": diff, "bad_light": bad_light}
    return rows, notes


def fetch_cycle(meta):
    key = "longterm/tw_business_cycle"
    try:
        errors, zip_rows, api_rows, zip_date, api_date = [], None, None, None, None
        try:
            zip_rows, zip_date = ndc_zip()
        except Exception as ex:  # noqa: BLE001 — zip 失敗就只用 JSON
            errors.append(f"data.gov.tw zip: {ex}")
        try:
            api_rows, api_date = ndc_api()
        except Exception as ex:  # noqa: BLE001 — JSON 失敗就只用 zip
            errors.append(f"國發會查詢系統 JSON: {ex}")
        if not zip_rows and not api_rows:
            raise RuntimeError("；".join(errors))
        # 最新一期的發布日：zip 與 JSON 最新月份相同時取 JSON 的 latest_date，否則取較新那份的日期
        zip_last, api_last = max(zip_rows or {"": 0}), max(api_rows or {"": 0})
        release_date = api_date if api_rows and api_last >= zip_last else zip_date
        rows, notes = build_cycle(zip_rows, api_rows, release_date, read_rows(CYCLE_CSV))
        if len(rows) < MIN_MONTHS:
            raise RuntimeError(f"只有 {len(rows)} 個月")
        last = rows[-1]
        nxt = f"{int(last[0][:4]) + (last[0][5:] == '12')}-{int(last[0][5:]) % 12 + 1:02d}"
        m = {"status": "ok",
             "source": "data.gov.tw 資料集 6099（國發會景氣指標及燈號 zip）＋國發會景氣指標查詢系統 JSON 核對",
             "rows": len(rows), "first": rows[0][0], "last": last[0], "last_score": last[1], "last_light": last[2],
             "last_release": last[4] or None, "next_release_est": release_est(nxt),
             "zip_last": zip_last or None, "api_last": api_last or None, "api_latest_date": api_date,
             "cross_check": (f"zip 與 JSON 重疊 {notes['compared']} 個月，分數不同 {len(notes['diff'])} 個月"
                             if zip_rows and api_rows else "只有一個來源，未核對"),
             "cross_check_ok": not notes["diff"] and not notes["bad_light"] if zip_rows and api_rows else None,
             "revised": "歷史值是最新修正值，不是初值；score_first 從 2026-08 起記錄",
             "frequency": "月；次月 27～30 日下午發布（release_est 用次月 30 日估計）"}
        if notes["added_from_api"]:
            m["added_from_api"] = notes["added_from_api"]
        if notes["diff"]:
            m["diff_months"] = notes["diff"][:20]
        if notes["bad_light"]:
            m["light_mismatch"] = notes["bad_light"][:20]
        if errors:
            m["warnings"] = errors
        write_rows(CYCLE_CSV, rows)
        meta["datasets"][key] = m
    except Exception as ex:  # noqa: BLE001
        mark_failed(meta, key, CYCLE_CSV, "國發會景氣對策信號", ex)


# ---------------------------------------------------------------- 巴菲特指標
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
EQUITY, GDP = "NCBEILQ027S", "GDP"  # 非金融企業股權市值（百萬美元，季末）；名目 GDP（十億美元，季調年率）
Z1_DATES = "https://www.federalreserve.gov/releases/z1/release-dates.htm"
Z1_EST_DAYS = 90  # 1996Q2 以前沒有發布日清單：季末＋90 天（1996Q2 起實際 64～107 天，中位數 71）
START = "1951Q4"  # 股權市值 1951Q4 起才是連續的季資料（更早只有年資料）
TREND_MIN = 40  # 擴張視窗趨勢至少 40 季（10 年）才算
MIN_QUARTERS = 250


def quarter_of(day):
    """'2026-04-01' → '2026Q2'。"""
    return f"{day[:4]}Q{(int(day[5:7]) - 1) // 3 + 1}"


def quarter_end(q):
    y, n = int(q[:4]), int(q[5])
    return dt.date(y, 12, 31) if n == 4 else dt.date(y, 3 * n + 1, 1) - dt.timedelta(days=1)


def fred_series(sid):
    status, raw = http(FRED.format(sid), headers={"User-Agent": BOT_UA}, timeout=40)
    if status != 200:
        raise RuntimeError(f"FRED {sid} HTTP {status}")
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    if not rows or rows[0][-1] != sid:
        raise RuntimeError(f"FRED {sid} 格式不對：{rows[:1]}")
    return {quarter_of(r[0]): float(r[1]) for r in rows[1:] if len(r) == 2 and r[1] not in ("", ".")}


def parse_z1_dates(html):
    """Fed Z.1 Release Dates 頁 → {'2026Q2': '2026-09-11'}（同一季有多次發布取最早）。目前這期的連結是 current，
    日期用頁尾的 Last Update。"""
    cur = re.search(r"Last Update:\s*([A-Z][a-z]+ \d{1,2}, \d{4})", html)
    out = {}
    for y, n, folder in re.findall(r'(\d{4}):Q(\d)</div>\s*<div class="col-xs-5">\s*<a href="/releases/z1/(\d{8}|current)/',
                                   html):
        if folder == "current":
            if not cur:
                continue
            day = dt.datetime.strptime(cur.group(1), "%B %d, %Y").date().isoformat()
        else:
            day = f"{folder[:4]}-{folder[4:6]}-{folder[6:]}"
        q = f"{y}Q{n}"
        out[q] = min(out.get(q, day), day)
    return out


def fit(xs, ys):
    """最小平方直線 y = a + b·x，回傳 (a, b, 殘差標準差)。"""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    a = my - b * mx
    sd = math.sqrt(sum((y - a - b * x) ** 2 for x, y in zip(xs, ys)) / (n - 2))
    return a, b, sd


def build_buffett(equity, gdp, z1):
    """回傳 (rows, 趨勢參數)。列：quarter, equities_bn, gdp_bn, ratio_pct, trend_pct, dev_sigma,
    dev_sigma_expanding, release, release_basis。

    trend／dev_sigma：ln(比率) 對時間的全期間對數線性趨勢（用到未來資料，只適合看歷史位置）；
    dev_sigma_expanding：只用到該季為止的資料估趨勢與標準差（回測用，前 TREND_MIN 季留空）。"""
    qs = sorted(q for q in equity.keys() & gdp.keys() if q >= START)
    ratio = {q: equity[q] / 1000 / gdp[q] * 100 for q in qs}
    xs = [int(q[:4]) + (int(q[5]) - 1) / 4 for q in qs]
    ys = [math.log(ratio[q]) for q in qs]
    a, b, sd = fit(xs, ys)
    rows = []
    for i, q in enumerate(qs):
        trend = math.exp(a + b * xs[i])
        dev = (ys[i] - a - b * xs[i]) / sd
        exp_dev = ""
        if i + 1 >= TREND_MIN:
            ea, eb, esd = fit(xs[:i + 1], ys[:i + 1])
            exp_dev = round((ys[i] - ea - eb * xs[i]) / esd, 3)
        if q in z1:
            release, basis = z1[q], "z1"
        else:
            release, basis = (quarter_end(q) + dt.timedelta(days=Z1_EST_DAYS)).isoformat(), "est"
        rows.append([q, round(equity[q] / 1000, 3), gdp[q], round(ratio[q], 2), round(trend, 2), round(dev, 3),
                     exp_dev, release, basis])
    return rows, {"a": a, "b": b, "sd": sd, "first": qs[0], "last": qs[-1]}


def fetch_buffett(meta):
    key = "longterm/buffett_indicator"
    try:
        equity, gdp = fred_series(EQUITY), fred_series(GDP)
        warn = []
        try:
            status, raw = http(Z1_DATES, headers={"User-Agent": BOT_UA})
            z1 = parse_z1_dates(raw.decode("utf-8", "replace")) if status == 200 else {}
            if not z1:
                warn.append(f"Z.1 發布日清單沒抓到（HTTP {status}），全部用估計")
        except Exception as ex:  # noqa: BLE001 — 發布日清單失敗就全部用估計
            z1 = {}
            warn.append(f"Z.1 發布日清單：{ex}")
        rows, tr = build_buffett(equity, gdp, z1)
        if len(rows) < MIN_QUARTERS:
            raise RuntimeError(f"只有 {len(rows)} 季")
        if not all(20 <= r[3] <= 400 for r in rows):
            raise RuntimeError("比率超出 20%～400%，資料可能有誤")
        last = rows[-1]
        m = {"status": "ok", "source": "FRED NCBEILQ027S（Fed Z.1 非金融企業股權市值）÷ GDP（BEA 名目 GDP）",
             "rows": len(rows), "first": rows[0][0], "last": last[0], "last_ratio_pct": last[3],
             "last_trend_pct": last[4], "last_dev_sigma": last[5], "last_dev_sigma_expanding": last[6],
             "last_release": last[7], "gdp_last": max(gdp), "equity_last": max(equity),
             "trend": (f"{tr['first']}～{tr['last']} ln(比率) 對數線性：每年 {math.expm1(tr['b']) * 100:+.2f}%，"
                       f"殘差標準差 {tr['sd']:.3f}"),
             "frequency": "季；Fed Z.1 約季末後 10 週發布（1996Q2 起 64～107 天，中位數 71）",
             "revised": "股權市值與 GDP 都是最新修正值，不是當時公布的初值"}
        if warn:
            m["warnings"] = warn
        write_rows(BUFFETT_CSV, rows)
        meta["datasets"][key] = m
    except Exception as ex:  # noqa: BLE001
        mark_failed(meta, key, BUFFETT_CSV, "巴菲特指標（FRED）", ex)


def main():
    try:
        meta = json.load(open(META, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        meta = {"datasets": {}, "errors": []}
    meta.setdefault("datasets", {})
    meta.setdefault("errors", [])
    fetch_cycle(meta)
    fetch_buffett(meta)
    keys = ("longterm/tw_business_cycle", "longterm/buffett_indicator")
    for k in keys:
        meta["datasets"][k]["checked_at_taipei"] = NOW.astimezone(TPE).strftime("%Y-%m-%d %H:%M")
        print(k, json.dumps(meta["datasets"][k], ensure_ascii=False))
    write_json(META, meta)
    if all(meta["datasets"][k]["status"] != "ok" for k in keys):
        sys.exit(1)


if __name__ == "__main__":
    main()

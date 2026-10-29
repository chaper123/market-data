"""長週期總經資料（回測用）：由 longterm.yml 排程（景氣燈號每月 27 日～次月 3 日、巴菲特指標每週六）或手動觸發，
不在 fetch.yml 的抓取裡。

  國發會景氣對策信號（月）：data.gov.tw 開放資料「景氣指標及燈號」（資料集 6099，zip，政府資料開放授權條款第 1 版），
      另用國發會景氣指標查詢系統的 JSON 核對，並補 zip 還沒更新的月份
  巴菲特指標（季）：FRED 的美國非金融企業股權市值（NCBEILQ027S，Fed Z.1）÷ 名目 GDP（GDP，BEA），
      公布日期取 Fed Z.1 的發布日（1996Q2 起有清單），更早的用估計

輸出（無表頭 CSV，欄位見 README）：
  data/longterm/tw_business_cycle.csv            景氣燈號（修正值＋初值）
  data/longterm/tw_business_cycle_revisions.csv  景氣燈號修正紀錄（逐次累加）
  data/longterm/buffett_indicator.csv            巴菲特指標（修正值＋第一次看到的值）
meta.json 只寫 longterm/ 兩個資料集（推回 repo 用 publish.py --meta-own longterm/）。

用法：python longterm.py [--only cycle|buffett] [--skip-if-current]
  --skip-if-current：已經有這次該有的月份／季度就不抓、不寫（排程用，避免每天 commit）

注意：主欄位是「最新修正值」，不是當時公布的初值。新月份（新季度）第一次出現時另存初值欄，之後修正只改主欄位；
景氣燈號從 2026-08、巴菲特指標從 2026Q3 起才有初值。
"""
import argparse
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

from common import (DATA, NOW, TPE, cookie_opener, get_json, http, mark_failed, prev_meta, read_rows, write_json,
                    write_rows)

OUT = os.path.join(DATA, "longterm")
META = os.path.join(DATA, "meta.json")
CYCLE_CSV = os.path.join(OUT, "tw_business_cycle.csv")
REVISIONS_CSV = os.path.join(OUT, "tw_business_cycle_revisions.csv")
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


def taipei_now():
    return NOW.astimezone(TPE).strftime("%Y-%m-%d %H:%M")


def light_of(score):
    return next(name for lo, name in LIGHTS if score >= lo)


def expected_month(today):
    """today（台北日期）時應該已經發布的最新月份：27 日起是上個月，之前是上上個月。"""
    y, m = today.year, today.month - (1 if today.day >= 27 else 2)
    if m <= 0:
        y, m = y - 1, m + 12
    return f"{y}-{m:02d}"


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


def upgrade_cycle_row(r, legacy_seen):
    """舊格式（6 欄：…, release_actual, score_first）→ 8 欄（…, release_actual, initial_score, initial_light,
    first_seen_taipei）；legacy_seen 是舊格式那次執行的時間（meta 的 checked_at_taipei）。"""
    if len(r) >= 8:
        return r[:8]
    first = r[5] if len(r) > 5 else ""
    return r[:5] + [first, light_of(int(first)) if first else "", legacy_seen if first else ""]


def build_cycle(zip_rows, api_rows, release_date, previous, now, legacy_seen=""):
    """合併 zip 與 JSON，回傳 (rows, revisions, notes)。兩份最新月份相同時以 zip 為主（JSON 補不到的月份），
    JSON 新一期時以 JSON 為主（zip 還是上一期的修正前數字）。

    列：month, score, light, release_est, release_actual, initial_score, initial_light, first_seen_taipei。
    score／light 永遠是最新修正值。新月份第一次出現時：若它是這次的最新一期，當時的分數就是初值，記
    initial_*、release_actual 與 first_seen_taipei；若不是（中間漏跑了一期，已被修正過）只記 first_seen_taipei、
    初值留空（notes 的 missed_initial）。第一次建檔（previous 空）只有最新一期記初值。
    已有的月份分數改變時，主欄位更新、初值不動，另回傳修正紀錄 revisions：
    [now, month, old_score, new_score, old_light, new_light, release_date]。同樣的輸入重跑不會再產生紀錄。"""
    zip_rows, api_rows = zip_rows or {}, api_rows or {}
    api_newer = bool(api_rows) and (not zip_rows or max(api_rows) > max(zip_rows))
    primary, other = (api_rows, zip_rows) if api_newer else (zip_rows, api_rows)
    base = dict(primary)
    added = sorted(set(other) - set(base)) if not api_newer else []
    for k in added:
        base[k] = other[k]
    both = sorted(set(zip_rows) & set(api_rows))
    diff = [k for k in both if zip_rows[k][0] != api_rows[k][0]]
    bad_light = [k for k, (s, lt) in base.items() if lt and lt != light_of(s)]
    out_of_range = [k for k, (s, _) in base.items() if not 9 <= s <= 45]
    if out_of_range:
        raise RuntimeError(f"分數超出 9～45：{out_of_range[:5]}")
    prev = {r[0]: upgrade_cycle_row(r, legacy_seen) for r in previous if len(r) >= 5}
    latest, rows, revisions, missed = max(base), [], [], []
    for month in sorted(set(base) | set(prev)):
        if month not in base:  # 來源少了這個月（不該發生）：沿用上一次的列
            rows.append(prev[month])
            continue
        score = base[month][0]
        light = light_of(score)
        if month in prev:
            p = prev[month]
            actual, init_s, init_l, seen = p[4], p[5], p[6], p[7]
            if str(p[1]) != str(score):
                revisions.append([now, month, p[1], score, p[2], light, release_date or ""])
        elif month == latest:
            actual, init_s, init_l, seen = release_date or "", score, light, now
        else:
            actual, init_s, init_l, seen = "", "", "", now if prev else ""
            if prev:
                missed.append(month)
        rows.append([month, score, light, release_est(month), actual, init_s, init_l, seen])
    notes = {"added_from_api": added, "compared": len(both), "diff": diff, "bad_light": bad_light,
             "api_newer": api_newer, "missed_initial": missed, "missing_from_source": sorted(set(prev) - set(base))}
    return rows, revisions, notes


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
        old = prev_meta(key)
        rows, revisions, notes = build_cycle(zip_rows, api_rows, release_date, read_rows(CYCLE_CSV), taipei_now(),
                                             legacy_seen=old.get("checked_at_taipei", ""))
        if len(rows) < MIN_MONTHS:
            raise RuntimeError(f"只有 {len(rows)} 個月")
        last = rows[-1]
        nxt = f"{int(last[0][:4]) + (last[0][5:] == '12')}-{int(last[0][5:]) % 12 + 1:02d}"
        if not zip_rows or not api_rows:
            check, check_ok = "只有一個來源，未核對", None
        elif notes["api_newer"]:
            check, check_ok = f"JSON 已是 {api_last}、zip 還在 {zip_last}：以 JSON 為主，zip 是上一期的數字不比對", None
        else:
            check = f"zip 與 JSON 重疊 {notes['compared']} 個月，分數不同 {len(notes['diff'])} 個月"
            check_ok = not notes["diff"] and not notes["bad_light"]
        m = {"status": "ok",
             "source": "data.gov.tw 資料集 6099（國發會景氣指標及燈號 zip）＋國發會景氣指標查詢系統 JSON 核對",
             "rows": len(rows), "first": rows[0][0], "last": last[0], "last_score": last[1], "last_light": last[2],
             "last_initial_score": int(last[5]) if last[5] != "" else None, "last_first_seen_taipei": last[7] or None,
             "last_release": last[4] or None, "next_release_est": release_est(nxt),
             "zip_last": zip_last or None, "api_last": api_last or None, "api_latest_date": api_date,
             "cross_check": check, "cross_check_ok": check_ok,
             "revisions_this_run": [f"{r[1]} {r[2]}→{r[3]}" for r in revisions],
             "revisions_total": len(read_rows(REVISIONS_CSV)) + len(revisions),
             "revised": "score／light 是最新修正值；initial_* 從 2026-08 起記錄，修正紀錄在 tw_business_cycle_revisions.csv",
             "frequency": "月；次月 27～30 日下午發布（release_est 用次月 30 日估計）"}
        for k in ("added_from_api", "missed_initial", "missing_from_source"):
            if notes[k]:
                m[k] = notes[k]
        if notes["diff"] and not notes["api_newer"]:
            m["diff_months"] = notes["diff"][:20]
        if notes["bad_light"]:
            m["light_mismatch"] = notes["bad_light"][:20]
        if errors:
            m["warnings"] = errors
        write_rows(CYCLE_CSV, rows)
        if revisions:
            write_rows(REVISIONS_CSV, read_rows(REVISIONS_CSV) + revisions)
        meta["datasets"][key] = m
    except Exception as ex:  # noqa: BLE001
        mark_failed(meta, key, CYCLE_CSV, "國發會景氣對策信號", ex)


# ---------------------------------------------------------------- 巴菲特指標
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
EQUITY, GDP = "NCBEILQ027S", "GDP"  # 非金融企業股權市值（百萬美元，季末）；名目 GDP（十億美元，季調年率）
Z1_DATES = "https://www.federalreserve.gov/releases/z1/release-dates.htm"
Z1_EST_DAYS = 90  # 1996Q2 以前沒有發布日清單：季末＋90 天（1996Q2 起實際 64～107 天，中位數 71）
Z1_MIN_DAYS = 64  # 季末後最快 64 天發布：排程在這之前不必檢查新一季
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


def expected_quarter(today):
    """today 時可能已經發布的最新一季：季末後 Z1_MIN_DAYS 天以上的最近一季。"""
    q = quarter_of(today.isoformat())
    while quarter_end(q) + dt.timedelta(days=Z1_MIN_DAYS) > today:
        y, n = int(q[:4]), int(q[5])
        q = f"{y - 1}Q4" if n == 1 else f"{y}Q{n - 1}"
    return q


def build_buffett(equity, gdp, z1, previous=(), now=""):
    """回傳 (rows, 趨勢參數, notes)。列：quarter, equities_bn, gdp_bn, ratio_pct, trend_pct, dev_sigma,
    dev_sigma_expanding, release, release_basis, initial_equities_bn, initial_gdp_bn, initial_ratio_pct,
    initial_dev_sigma_expanding, first_seen_taipei。

    trend／dev_sigma：ln(比率) 對時間的全期間對數線性趨勢（用到未來資料，只適合看歷史位置）；
    dev_sigma_expanding：只用到該季為止的資料估趨勢與標準差（回測用，前 TREND_MIN 季留空）。
    initial_*：這一季第一次出現在 CSV 時的值，之後修正不覆蓋（第一次建檔、舊格式留下的季度都留空）。"""
    prev = {r[0]: r for r in previous if r}
    qs = sorted(q for q in equity.keys() & gdp.keys() if q >= START)
    ratio = {q: equity[q] / 1000 / gdp[q] * 100 for q in qs}
    xs = [int(q[:4]) + (int(q[5]) - 1) / 4 for q in qs]
    ys = [math.log(ratio[q]) for q in qs]
    a, b, sd = fit(xs, ys)
    rows, new = [], []
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
        row = [q, round(equity[q] / 1000, 3), gdp[q], round(ratio[q], 2), round(trend, 2), round(dev, 3),
               exp_dev, release, basis]
        if q in prev:
            init = (prev[q][9:14] + [""] * 5)[:5]
        elif prev:
            init = [row[1], row[2], row[3], row[6], now]
            new.append(q)
        else:
            init = [""] * 5
        rows.append(row + init)
    missing = sorted(set(prev) - set(qs))
    rows = sorted(rows + [(prev[q] + [""] * 14)[:14] for q in missing])  # 來源少了的季（不該發生）沿用上一次
    return rows, {"a": a, "b": b, "sd": sd, "first": qs[0], "last": qs[-1]}, {"new": new, "missing_from_source": missing}


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
        rows, tr, notes = build_buffett(equity, gdp, z1, read_rows(BUFFETT_CSV), taipei_now())
        if len(rows) < MIN_QUARTERS:
            raise RuntimeError(f"只有 {len(rows)} 季")
        if not all(20 <= float(r[3]) <= 400 for r in rows):
            raise RuntimeError("比率超出 20%～400%，資料可能有誤")
        last = rows[-1]
        m = {"status": "ok", "source": "FRED NCBEILQ027S（Fed Z.1 非金融企業股權市值）÷ GDP（BEA 名目 GDP）",
             "rows": len(rows), "first": rows[0][0], "last": last[0], "last_ratio_pct": last[3],
             "last_trend_pct": last[4], "last_dev_sigma": last[5], "last_dev_sigma_expanding": last[6],
             "last_release": last[7], "last_initial_ratio_pct": float(last[11]) if last[11] != "" else None,
             "last_first_seen_taipei": last[13] or None,
             "new_quarters": notes["new"], "gdp_last": max(gdp), "equity_last": max(equity),
             "trend": (f"{tr['first']}～{tr['last']} ln(比率) 對數線性：每年 {math.expm1(tr['b']) * 100:+.2f}%，"
                       f"殘差標準差 {tr['sd']:.3f}"),
             "frequency": "季；Fed Z.1 約季末後 10 週發布（1996Q2 起 64～107 天，中位數 71）",
             "revised": "主欄位是最新修正值；initial_* 是該季第一次出現時的值（2026Q3 起）"}
        if notes["missing_from_source"]:
            m["missing_from_source"] = notes["missing_from_source"]
        if warn:
            m["warnings"] = warn
        write_rows(BUFFETT_CSV, rows)
        meta["datasets"][key] = m
    except Exception as ex:  # noqa: BLE001
        mark_failed(meta, key, BUFFETT_CSV, "巴菲特指標（FRED）", ex)


def is_current(name, today):
    """排程用：CSV 已經有 today 時該有的最新月份（季度）就不必再抓。"""
    path, want = ((CYCLE_CSV, expected_month(today)) if name == "cycle" else (BUFFETT_CSV, expected_quarter(today)))
    rows = read_rows(path)
    have = max((r[0] for r in rows), default="")
    return have >= want, have, want


STEPS = {"cycle": ("longterm/tw_business_cycle", fetch_cycle), "buffett": ("longterm/buffett_indicator", fetch_buffett)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", choices=sorted(STEPS))
    ap.add_argument("--skip-if-current", action="store_true")
    a = ap.parse_args(argv)
    try:
        meta = json.load(open(META, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        meta = {"datasets": {}, "errors": []}
    meta.setdefault("datasets", {})
    meta.setdefault("errors", [])
    today, ran = NOW.astimezone(TPE).date(), []
    for name in [a.only] if a.only else list(STEPS):
        key, step = STEPS[name]
        if a.skip_if_current:
            ok, have, want = is_current(name, today)
            if ok:
                print(f"{key}：已有 {have}（今天應有 {want}），略過")
                continue
        step(meta)
        meta["datasets"][key]["checked_at_taipei"] = taipei_now()
        print(key, json.dumps(meta["datasets"][key], ensure_ascii=False))
        ran.append(key)
    if not ran:
        return 0
    write_json(META, meta)
    return 1 if any(meta["datasets"][k]["status"] != "ok" for k in ran) else 0


if __name__ == "__main__":
    sys.exit(main())

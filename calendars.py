"""Phase 1 ④：行事曆（由 fetch.py 在完整抓取時呼叫；檔名避開標準庫 calendar）。

輸出 data/calendar/calendar.json：
  tw_market  證交所開休市日期：entries（date、name、description、trading＝當天其實有交易，例如「開始交易日」），
             closed（週一～五休市的日期）。今年用 OpenAPI，並與 rwd 版核對；明年公布後一併收錄
  fomc       FOMC 會議（今年與已公布的未來年度）：start、end、sep（Fed 標 *，會公布經濟預測）、note
  earnings   可買個股下次財報日（取自 data/valuation/us.json，來源 Yahoo，is_estimate＝尚未正式公告）
  cpi        未取得：BLS 對 GitHub 機房回 403，尚未找到替代來源
某一段失敗時保留上一次的內容，meta 標 stale。
"""
import datetime as dt
import json
import os
import re

from common import DATA, NOW, TPE, get_json, http, mark_failed, write_json

PATH = os.path.join(DATA, "calendar", "calendar.json")
TWSE_OPENAPI = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
TWSE_RWD = "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?response=json&date={}0101"
FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                      "nov", "dec"), 1)}


def clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def tw_entry(day, name, desc):
    return {"date": day, "name": clean(name), "description": clean(desc),
            "trading": "開始交易" in name or "最後交易" in name}


def tw_market(today):
    year = today.year
    ref = {"Referer": "https://www.twse.com.tw/"}
    api = get_json(TWSE_OPENAPI)
    entries = [tw_entry(f"{int(x['Date'][:-4]) + 1911}-{x['Date'][-4:-2]}-{x['Date'][-2:]}", x["Name"],
                        x.get("Description")) for x in api]
    if not entries or entries[0]["date"][:4] != str(year):
        raise RuntimeError(f"OpenAPI 不是 {year} 年的資料")
    rwd = get_json(TWSE_RWD.format(year), headers=ref)
    rwd_days = {r[0] for r in rwd.get("data") or []}
    api_days = {e["date"] for e in entries}
    nxt = get_json(TWSE_RWD.format(year + 1), headers=ref)  # 明年通常年底才公布，沒有就是 0 筆
    entries += [tw_entry(r[0], r[1], r[2] if len(r) > 2 else "") for r in nxt.get("data") or []]
    entries.sort(key=lambda e: e["date"])
    closed = sorted({e["date"] for e in entries if not e["trading"]
                     and dt.date.fromisoformat(e["date"]).weekday() < 5})
    m = {"status": "ok", "source": "證交所 OpenAPI holidaySchedule", "rows": len(entries),
         "years": sorted({e["date"][:4] for e in entries}), "last": entries[-1]["date"],
         "next_closed": next((d for d in closed if d >= today.isoformat()), None)}
    if rwd_days == api_days:
        m["cross_check"], m["cross_check_ok"] = f"{year} 年與 rwd 版一致（{len(api_days)} 筆）", True
    else:
        m["cross_check"] = f"{year} 年與 rwd 版不同：只在 OpenAPI {sorted(api_days - rwd_days)}，只在 rwd {sorted(rwd_days - api_days)}"
        m["cross_check_ok"] = False
    return {"source": TWSE_OPENAPI, "entries": entries, "closed": closed}, m


def fomc(today):
    status, raw = http(FED_URL)
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    h = raw.decode("utf-8", "replace")
    out = []
    for panel in h.split('<div class="panel panel-default">')[1:]:
        y = re.search(r"(\d{4}) FOMC Meetings", panel)
        if not y or int(y.group(1)) < today.year:
            continue
        year = int(y.group(1))
        for mon, day in re.findall(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?'
                                   r'fomc-meeting__date[^>]*>([^<]+)<', panel, re.S):
            ms = [MONTHS.get(x.strip()[:3].lower()) for x in mon.split("/")]
            ds = [int(x) for x in re.findall(r"\d+", day)]
            if not ds or None in ms:
                continue
            out.append({"start": f"{year}-{ms[0]:02d}-{ds[0]:02d}", "end": f"{year}-{ms[-1]:02d}-{ds[-1]:02d}",
                        "sep": "*" in day, "note": clean(re.sub(r"[\d\-*]", "", day)) or None})
    if not any(x["start"][:4] == str(today.year) for x in out):
        raise RuntimeError(f"找不到 {today.year} 年的會議")
    out.sort(key=lambda x: x["start"])
    nxt = next((x for x in out if x["end"] >= today.isoformat()), None)
    m = {"status": "ok", "source": "federalreserve.gov FOMC calendars", "rows": len(out),
         "years": sorted({x["start"][:4] for x in out}), "last": out[-1]["end"],
         "next": f"{nxt['start']}～{nxt['end']}{'（SEP）' if nxt['sep'] else ''}" if nxt else None,
         "cross_check": "官方單一來源，未核對"}
    return out, m


def earnings():
    snap = json.load(open(os.path.join(DATA, "valuation", "us.json"), encoding="utf-8"))
    out = {s: {"dates": v.get("earnings_dates"), "is_estimate": v.get("is_estimate"),
               "stale": bool(v.get("stale") or v.get("earnings_stale"))}
           for s, v in snap.get("symbols", {}).items() if v.get("earnings_dates")}
    if not out:
        raise RuntimeError("valuation/us.json 沒有財報日")
    return {"source": "Yahoo quoteSummary（經 data/valuation/us.json）", "fetched_at_taipei": snap.get("fetched_at_taipei"),
            "symbols": out}


def fetch_calendar(cfg, meta):
    today = NOW.astimezone(TPE).date()
    try:
        old = json.load(open(PATH, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        old = {}
    cal = {"generated_at_taipei": NOW.astimezone(TPE).strftime("%Y-%m-%d %H:%M")}
    for name, key, fn in (("tw_market", "calendar/tw_holidays", tw_market), ("fomc", "calendar/fomc", fomc)):
        try:
            cal[name], meta["datasets"][key] = fn(today)
        except Exception as ex:  # noqa: BLE001
            if name in old:
                cal[name] = old[name]
            mark_failed(meta, key, PATH, f"calendar {name}", ex)
    try:
        cal["earnings"] = earnings()
    except Exception as ex:  # noqa: BLE001
        meta["errors"].append(f"calendar earnings: {ex}")
        if "earnings" in old:
            cal["earnings"] = dict(old["earnings"], stale=True)
    cal["cpi"] = {"status": "未取得", "reason": "BLS 時程頁對 GitHub 機房回 403，尚未找到替代來源"}
    write_json(PATH, cal)

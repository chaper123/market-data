"""證交所 MIS 盤中快照，給台股晨報取代鉅亨網的盤中價（只用 Python 標準庫）。

由 .github/workflows/intraday.yml 執行（cron-job.org 約台北 10:05 觸發）；不寫 meta.json，狀態記在輸出檔裡。
來源：mis.twse.com.tw 的 getStockInfo（抓 ETF 淨值用的同一個站）。上市用 tse_、上櫃用 otc_，加權指數是 tse_t00。

輸出 data/tw/intraday.json：
  session      盤前／盤中／收盤後／休市日（依台北時間與 data/calendar/calendar.json 的休市日）
  is_trading   只有盤中是 true；其他時段 note 會寫清楚價格是哪一天、幾點的資料
  quotes       代號 → name、market、data_date、time（一般交易最後成交時間，台北）、updated（MIS 最後更新，收盤後的
               ETF 會是 14:30 盤後定價交易）、price、price_field（z＝最後成交價；
               這一刻沒有成交時 MIS 給 "-"，改用 pz）、change、change_pct、open、high、low、prev_close、
               volume_lots（累積成交張數，指數為 null）、limit_up、limit_down、is_today（資料日期是不是今天）
全部失敗時保留上一次的檔案並以非 0 結束。
"""
import datetime as dt
import json
import os
import sys
import time

from common import DATA, NOW, ROOT, TPE, http, num, write_json

URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={}&json=1&delay=0&_={}"
OUT = os.path.join(DATA, "tw", "intraday.json")
INDEX = ("t00", "tse")  # 發行量加權股價指數
OPEN, CLOSE = (9, 0), (13, 30)


def closed_days():
    try:
        return set(json.load(open(os.path.join(DATA, "calendar", "calendar.json"),
                                  encoding="utf-8"))["tw_market"]["closed"])
    except Exception:  # noqa: BLE001 — 沒有行事曆就只看週末
        return set()


def session(now, closed):
    tp = now.astimezone(TPE)
    if tp.weekday() >= 5 or tp.date().isoformat() in closed:
        return "休市日"
    hm = (tp.hour, tp.minute)
    if hm < OPEN:
        return "盤前"
    if hm <= CLOSE:
        return "盤中"
    return "收盤後"


def parse(item, today):
    """MIS 的一筆 msgArray → 快照欄位。"""
    price, field = num(item.get("z")), "z"
    if price is None:
        price, field = num(item.get("pz")), "pz"
    prev = num(item.get("y"))
    d = item.get("d") or ""
    data_date = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else None
    tlong = item.get("tlong")
    when = f"{data_date} {item['t']}" if data_date and item.get("t") else None
    updated = dt.datetime.fromtimestamp(int(tlong) / 1000, TPE).strftime("%Y-%m-%d %H:%M:%S") if tlong else None
    return {"name": item.get("n"), "market": item.get("ex"), "data_date": data_date, "time": when, "updated": updated,
            "price": price, "price_field": field if price is not None else None,
            "change": round(price - prev, 4) if price is not None and prev else None,
            "change_pct": round((price / prev - 1) * 100, 2) if price is not None and prev else None,
            "open": num(item.get("o")), "high": num(item.get("h")), "low": num(item.get("l")), "prev_close": prev,
            "volume_lots": num(item.get("v")), "limit_up": num(item.get("u")), "limit_down": num(item.get("w")),
            "is_today": data_date == today}


def note_for(sess, quotes, today):
    if sess == "盤中":
        return "盤中：價格是最後成交價，time 是成交時間"
    stamps = sorted({q["time"] for q in quotes.values() if q.get("time")})
    when = f"{stamps[0]}～{stamps[-1]}" if len(stamps) > 1 else (stamps[0] if stamps else "不明")
    if sess == "盤前":
        return f"非交易時段（盤前）：價格是前一交易日的收盤資料（{when}），不是今天的盤中價"
    if sess == "收盤後":
        return f"非交易時段（收盤後）：價格是今天 {today} 的收盤資料（{when}）"
    return f"非交易時段（休市日）：價格是最近一個交易日的資料（{when}）"


def snapshot(cfg, now):
    today = now.astimezone(TPE).date().isoformat()
    targets = [(c, "otc" if e["market"] == "TPEX" else "tse") for c, e in cfg["tw"].items()] + [INDEX]
    ex_ch = "|".join(f"{ex}_{c}.tw" for c, ex in targets)
    code, raw = http(URL.format(ex_ch, int(now.timestamp() * 1000)),
                     headers={"Referer": "https://mis.twse.com.tw/stock/index.jsp"})
    if code != 200:
        raise RuntimeError(f"HTTP {code}")
    j = json.loads(raw.decode("utf-8-sig"))
    if j.get("rtcode") != "0000":
        raise RuntimeError(f"rtcode {j.get('rtcode')}: {j.get('rtmessage')}")
    got = {m.get("c"): m for m in j.get("msgArray") or []}
    quotes, errors = {}, []
    for c, ex in targets:
        if c in got:
            quotes["TAIEX" if c == INDEX[0] else c] = parse(got[c], today)
        else:
            errors.append(f"MIS 沒有回傳 {ex}_{c}")
    if not quotes:
        raise RuntimeError("MIS 沒有回傳任何一檔")
    sess = session(now, closed_days())
    sys_time = (j.get("queryTime") or {})
    return {"fetched_at_taipei": now.astimezone(TPE).strftime("%Y-%m-%d %H:%M:%S"),
            "mis_time": f"{sys_time.get('sysDate', '')} {sys_time.get('sysTime', '')}".strip(),
            "session": sess, "is_trading": sess == "盤中", "trading_day": sess != "休市日",
            "note": note_for(sess, quotes, today), "status": "partial" if errors else "ok",
            "source": "mis.twse.com.tw getStockInfo", "quotes": quotes, "errors": errors}


def main():
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    last = None
    for i in range(2):  # MIS 偶爾回空的 msgArray，隔幾秒再試一次
        try:
            doc = snapshot(cfg, NOW)
            break
        except Exception as ex:  # noqa: BLE001
            last = ex
            time.sleep(5)
    else:
        print(f"盤中快照失敗，保留上一次的檔案：{last}", file=sys.stderr)
        sys.exit(1)
    write_json(OUT, doc)
    print(doc["session"], doc["status"], {k: (q["price"], q["time"]) for k, q in doc["quotes"].items()})
    for e in doc["errors"]:
        print(" -", e)


if __name__ == "__main__":
    main()

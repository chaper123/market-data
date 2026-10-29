"""證交所 MIS 盤中快照，給台股晨報取代鉅亨網的盤中價（只用 Python 標準庫）。

由 .github/workflows/intraday.yml 執行（cron-job.org 約台北 10:05 與 12:00 觸發，兩次都寫同一個檔，用 slot 分辨）；
meta.json 只寫 intraday/tw（推回 repo 用 publish.py --meta-own intraday/），不影響 fetch.yml 的略過判斷。
來源：mis.twse.com.tw 的 getStockInfo（抓 ETF 淨值用的同一個站）。上市用 tse_、上櫃用 otc_，加權指數是 tse_t00。

輸出 data/tw/intraday.json：
  session      盤前／盤中／收盤後／休市日（依台北時間與 data/calendar/calendar.json 的休市日）
  is_trading   只有盤中是 true；其他時段 note 會寫清楚價格是哪一天、幾點的資料
  slot         早盤（盤中 11:00 前，10:05 那次）／午間（盤中 11:00 起，12:00 那次）／盤外（不是盤中）
  quotes       代號 → name、market、data_date、time（一般交易最後成交時間，台北）、updated（MIS 最後更新，收盤後的
               ETF 會是 14:30 盤後定價交易）、price、price_field、price_note、price_retries、bid1、ask1、change、
               change_pct、open、high、low、prev_close、volume_lots（累積成交張數，指數為 null）、limit_up、limit_down、
               limit_note、is_today（資料日期是不是今天）
price 的備援順序（price_field）：
  z            最後成交價
  z_retry      那一刻沒有成交（MIS 的 z 是 "-"），隔幾秒重抓後拿到的最後成交價（price_retries 是第幾次）
  pz           MIS 的前一筆成交價
  bid_ask_mid  重抓 RETRIES 次仍沒有成交價：最佳一檔買價（b）與賣價（a）的中間價
  bid1／ask1   只有單邊報價時用那一邊
  null         都沒有，原因寫在 price_note
change、change_pct 用採用的 price 對 prev_close 計算。limit_up／limit_down 不合理（例如上櫃無漲跌幅限制的 ETF，
MIS 給 9999.95）就改成 null，原因寫在 limit_note。
全部失敗時保留上一次的檔案、meta 標 stale，並以非 0 結束。
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
RETRIES, RETRY_WAIT = 3, 8  # 沒有成交價時重抓的次數與間隔（秒），總共約 30 秒內


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


def slot_of(now, sess):
    """這次快照是哪一個時段：盤中 11:00 前是早盤（10:05 那次），11:00 起是午間（12:00 那次），其他是盤外。"""
    if sess != "盤中":
        return "盤外"
    tp = now.astimezone(TPE)
    return "早盤" if (tp.hour, tp.minute) < (11, 0) else "午間"


def first_level(text):
    """MIS 的五檔報價「27.4900_27.5000_…_」→ 第一檔；0 或 "-" 當成沒有。"""
    v = num((text or "").split("_")[0])
    return v if v and v > 0 else None


def has_trade(item):
    return num(item.get("z")) is not None or num(item.get("pz")) is not None


def sane_limits(u, w, prev):
    """台股漲跌幅 10%：漲停價應在昨收之上 25% 內、跌停價在昨收之下 25% 內，否則改 null 並說明。"""
    notes = []
    if u is not None and not (prev and prev < u <= prev * 1.25):
        notes.append(f"漲停價 {u:g} 不合理（MIS 對無漲跌幅限制的商品給 9999.95），改成 null")
        u = None
    if w is not None and not (prev and prev * 0.75 <= w < prev):
        notes.append(f"跌停價 {w:g} 不合理，改成 null")
        w = None
    return u, w, "；".join(notes) or None


def parse(item, today, retries=0, index=False):
    """MIS 的一筆 msgArray → 快照欄位。retries：這筆是第幾次重抓拿到的。"""
    bid, ask = first_level(item.get("b")), first_level(item.get("a"))
    note = None
    price, field = num(item.get("z")), "z_retry" if retries else "z"
    if price is None:
        price, field = num(item.get("pz")), "pz"
    if price is None:
        if bid and ask:
            price, field = round((bid + ask) / 2, 4), "bid_ask_mid"
            note = f"重抓 {RETRIES} 次仍沒有成交價，用最佳一檔買賣價的中間價" if not index else None
        elif bid or ask:
            price, field = (bid, "bid1") if bid else (ask, "ask1")
            note = f"沒有成交價、只有單邊報價，用最佳一檔{'買價' if bid else '賣價'}"
        else:
            field, note = None, "沒有成交價，也沒有買賣報價"
    prev = num(item.get("y"))
    d = item.get("d") or ""
    data_date = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else None
    tlong = item.get("tlong")
    when = f"{data_date} {item['t']}" if data_date and item.get("t") else None
    updated = dt.datetime.fromtimestamp(int(tlong) / 1000, TPE).strftime("%Y-%m-%d %H:%M:%S") if tlong else None
    q = {"name": item.get("n"), "market": item.get("ex"), "data_date": data_date, "time": when, "updated": updated,
         "price": price, "price_field": field, "price_note": note, "price_retries": retries,
         "change": round(price - prev, 4) if price is not None and prev else None,
         "change_pct": round((price / prev - 1) * 100, 2) if price is not None and prev else None,
         "open": num(item.get("o")), "high": num(item.get("h")), "low": num(item.get("l")), "prev_close": prev,
         "volume_lots": num(item.get("v")), "is_today": data_date == today}
    if not index:
        q["bid1"], q["ask1"] = bid, ask
        q["limit_up"], q["limit_down"], q["limit_note"] = sane_limits(num(item.get("u")), num(item.get("w")), prev)
    else:
        q["limit_up"] = q["limit_down"] = q["limit_note"] = None
    return q


def note_for(sess, quotes, today):
    if sess == "盤中":
        fallback = [k for k, q in quotes.items() if q["price_field"] not in ("z", "z_retry")]
        extra = f"；{'、'.join(fallback)} 那一刻沒有成交價，改用備援（見各檔 price_field）" if fallback else ""
        return "盤中：價格是最後成交價，time 是成交時間" + extra
    stamps = sorted({q["time"] for q in quotes.values() if q.get("time")})
    when = f"{stamps[0]}～{stamps[-1]}" if len(stamps) > 1 else (stamps[0] if stamps else "不明")
    if sess == "盤前":
        return f"非交易時段（盤前）：價格是前一交易日的收盤資料（{when}），不是今天的盤中價"
    if sess == "收盤後":
        return f"非交易時段（收盤後）：價格是今天 {today} 的收盤資料（{when}）"
    return f"非交易時段（休市日）：價格是最近一個交易日的資料（{when}）"


def fetch(targets, now):
    ex_ch = "|".join(f"{ex}_{c}.tw" for c, ex in targets)
    code, raw = http(URL.format(ex_ch, int(now.timestamp() * 1000)),
                     headers={"Referer": "https://mis.twse.com.tw/stock/index.jsp"})
    if code != 200:
        raise RuntimeError(f"HTTP {code}")
    j = json.loads(raw.decode("utf-8-sig"))
    if j.get("rtcode") != "0000":
        raise RuntimeError(f"rtcode {j.get('rtcode')}: {j.get('rtmessage')}")
    return j, {m.get("c"): m for m in j.get("msgArray") or []}


def snapshot(cfg, now):
    today = now.astimezone(TPE).date().isoformat()
    targets = [(c, "otc" if e["market"] == "TPEX" else "tse") for c, e in cfg["tw"].items()] + [INDEX]
    j, got = fetch(targets, now)
    tries = {c: 0 for c in got}
    for n in range(1, RETRIES + 1):  # 那一刻沒有成交（z、pz 都是 "-"）就隔幾秒重抓
        missing = [c for c, _ in targets if c in got and not has_trade(got[c])]
        if not missing:
            break
        time.sleep(RETRY_WAIT)
        try:
            _, again = fetch(targets, now)
        except Exception:  # noqa: BLE001 — 重抓失敗就用手上的
            continue
        for c in missing:
            if c in again:
                got[c] = again[c]  # 沒有成交時也換成較新的買賣報價
                tries[c] = n if has_trade(again[c]) else 0
    quotes, errors = {}, []
    for c, ex in targets:
        if c in got:
            quotes["TAIEX" if c == INDEX[0] else c] = parse(got[c], today, tries.get(c, 0), index=c == INDEX[0])
        else:
            errors.append(f"MIS 沒有回傳 {ex}_{c}")
    if not quotes:
        raise RuntimeError("MIS 沒有回傳任何一檔")
    sess = session(now, closed_days())
    sys_time = (j.get("queryTime") or {})
    return {"fetched_at_taipei": now.astimezone(TPE).strftime("%Y-%m-%d %H:%M:%S"),
            "mis_time": f"{sys_time.get('sysDate', '')} {sys_time.get('sysTime', '')}".strip(),
            "session": sess, "slot": slot_of(now, sess), "is_trading": sess == "盤中", "trading_day": sess != "休市日",
            "note": note_for(sess, quotes, today), "status": "partial" if errors else "ok",
            "source": "mis.twse.com.tw getStockInfo", "quotes": quotes, "errors": errors}


def update_meta(doc=None, error=None):
    """只寫 meta.json 的 intraday/tw；其他資料集不動（推回 repo 時 publish.py 會再合併一次）。"""
    path = os.path.join(DATA, "meta.json")
    try:
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
    except Exception:  # noqa: BLE001
        meta = {"datasets": {}, "errors": []}
    old = meta.setdefault("datasets", {}).get("intraday/tw") or {}
    if doc is None:
        m = {"status": "stale" if os.path.exists(OUT) else "fail", "error": str(error)[:300],
             "last": old.get("last"), "slot": old.get("slot")}
    else:
        m = {"status": "ok", "source": doc["source"], "last": doc["fetched_at_taipei"], "slot": doc["slot"],
             "session": doc["session"], "quotes": len(doc["quotes"]),
             "no_price": [k for k, q in doc["quotes"].items() if q["price"] is None], "errors": len(doc["errors"])}
    meta["datasets"]["intraday/tw"] = m
    write_json(path, meta)


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
        update_meta(error=last)
        sys.exit(1)
    write_json(OUT, doc)
    update_meta(doc)
    print(doc["session"], doc["slot"], doc["status"],
          {k: (q["price"], q["price_field"], q["time"]) for k, q in doc["quotes"].items()})
    for e in doc["errors"]:
        print(" -", e)


if __name__ == "__main__":
    main()

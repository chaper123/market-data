"""Phase 1 ②：台銀美元即期賣出（由 fetch.py 在完整抓取時呼叫）。

台銀網站對 GitHub 機房回防機器人驗證頁，不嘗試繞過；改用 FinMind 的 TaiwanExchangeRate（台灣銀行牌告，免 token）。

輸出：
  data/fx/usdtwd_bot_sell.csv  date,rate（台銀美元即期賣出，約 13 個月，無表頭）。格式同 usdtwd_mid.csv，可直接當排程的 fx_csv
"""
import datetime as dt
import os

from common import DATA, NOW, TPE, get_json, mark_failed, num, read_rows, write_rows

FINMIND = "https://api.finmindtrade.com/api/v4/data?dataset=TaiwanExchangeRate&data_id=USD&start_date={}"
# 台銀即期賣出 − Yahoo 中間價（同一天）：2025-09～2026-10 中位數 +0.07 元，絕大多數在 −0.18～+0.24 元
DIFF_RANGE = (-0.3, 0.4)


def fx_cutoff():
    """台銀牌告營業時間內會變動，16:30 前不收當天的值。"""
    tp = NOW.astimezone(TPE)
    day = tp if (tp.hour, tp.minute) >= (16, 30) else tp - dt.timedelta(days=1)
    return day.strftime("%Y-%m-%d")


def fetch_bot_fx(cfg, meta):
    key, path = "fx/usdtwd_bot_sell", os.path.join(DATA, "fx", "usdtwd_bot_sell.csv")
    try:
        start = NOW.astimezone(TPE).date() - dt.timedelta(days=400)
        j = get_json(FINMIND.format(start))
        if j.get("status") != 200:
            raise RuntimeError(f"FinMind status {j.get('status')}: {j.get('msg')}")
        cut, rows, last = fx_cutoff(), {}, None
        for x in j.get("data") or []:
            day, sell = x.get("date"), num(x.get("spot_sell"))
            if day and day <= cut and sell and sell > 0:
                rows[day] = [day, sell]
                if last is None or day > last["date"]:
                    last = x
        if len(rows) < 100:
            raise RuntimeError(f"只有 {len(rows)} 列")
        out = sorted(rows.values())
        m = {"status": "ok", "source": "FinMind TaiwanExchangeRate（台灣銀行牌告）", "rows": len(out),
             "first": out[0][0], "last": out[-1][0], "last_rate": out[-1][1],
             "last_spot_buy": num(last.get("spot_buy")), "last_cash_sell": num(last.get("cash_sell"))}
        # 與同一次抓到的 Yahoo 中間價（usdtwd_mid.csv）比對最近的同一天
        mid = {r[0]: float(r[1]) for r in read_rows(os.path.join(DATA, "fx", "usdtwd_mid.csv"))}
        both = [d for d in rows if d in mid]
        if both:
            d = max(both)
            diff = rows[d][1] - mid[d]
            lo, hi = DIFF_RANGE
            m["cross_check"] = (f"{d} 即期賣出 {rows[d][1]} vs Yahoo 中間價 {mid[d]}"
                                f"（差 {diff:+.3f} 元，正常 {lo:+}～{hi:+}）")
            m["cross_check_ok"] = lo <= diff <= hi
        else:
            m["cross_check"] = "usdtwd_mid 沒有同一天的資料，未核對"
        write_rows(path, out)
        meta["datasets"][key] = m
    except Exception as ex:  # noqa: BLE001
        mark_failed(meta, key, path, "FX 台銀（FinMind）", ex)

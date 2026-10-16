"""Phase 1 ①：美股指數與總經收盤、CNN 恐懼貪婪、台指 VIX（由 fetch.py 在完整抓取時呼叫）。

輸出：
  data/macro/<名稱>.csv               date,volume,open,high,low,close（無表頭，只含已收盤的交易日）
                                      名稱與 Yahoo 代號見 config.json 的 macro；有鉅亨網代號的會核對收盤
  data/sentiment/cnn_fear_greed.json  CNN 恐懼貪婪目前分數、評等、前收／一週／一月／一年前，以及 7 個分項
  data/sentiment/cnn_fear_greed.csv   date,score,rating（逐日累積，只含已收盤的美股交易日）
  data/sentiment/taifex_vix.csv       date,vix（臺指選擇權波動率指數每日收盤，逐日累積）
"""
import datetime as dt
import os
import re
import time

from common import (DATA, NOW, NY, TPE, UTC, cnyes_daily, cross_check, finalize, get_json, http, mark_failed,
                    read_rows, summary, tw_cutoff, us_cutoff, write_csv, write_json, write_rows, yahoo_daily)

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
CNN_PARTS = ("market_momentum_sp500", "stock_price_strength", "stock_price_breadth", "put_call_options",
             "market_volatility_vix", "junk_bond_demand", "safe_haven_demand")
VIX_URL = "https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/{:%Y%m}new.txt"


def fetch_macro(cfg, meta):
    """Yahoo 日線（2 年）為主；config 有 cnyes 代號的再用鉅亨網核對最後收盤。"""
    cut = us_cutoff()
    for name, e in cfg["macro"].items():
        key, path = f"macro/{name}", os.path.join(DATA, "macro", f"{name}.csv")
        try:
            rows = finalize(yahoo_daily(e["yahoo"]), cut)
            if len(rows) < 100:
                raise RuntimeError(f"只有 {len(rows)} 列")
            m = summary(rows, f"yahoo {e['yahoo']}")
            m["name"] = e.get("name")
            if e.get("cnyes"):
                try:
                    raw, _ = cnyes_daily(e["cnyes"], [UTC, NY, TPE])
                    cross_check(m, rows, finalize(raw, cut), "cnyes")
                except Exception as ex:  # noqa: BLE001 — 核對來源失敗不影響主資料
                    m["cross_check"] = f"cnyes 失敗，未核對：{ex}"[:200]
            else:
                m["cross_check"] = "無第二來源，未核對"
            write_csv(path, rows)
            meta["datasets"][key] = m
        except Exception as ex:  # noqa: BLE001
            mark_failed(meta, key, path, f"macro {name}", ex)
        time.sleep(0.8)


def _utc_text(ts):
    """CNN 的時間有時是 ISO 字串、有時是毫秒時間戳。"""
    if isinstance(ts, (int, float)):
        return dt.datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ts


def fetch_cnn(cfg, meta):
    """CNN 恐懼貪婪。歷史點的時間是隔天 00:00 UTC（＝美東前一晚），換成美東日期就是那個交易日的收盤；
    最後一點是盤中即時值，日期晚於收盤截止就不寫進 CSV（JSON 的目前分數照存，附時間）。"""
    key = "sentiment/cnn_fear_greed"
    jpath = os.path.join(DATA, "sentiment", "cnn_fear_greed.json")
    cpath = os.path.join(DATA, "sentiment", "cnn_fear_greed.csv")
    try:
        j = get_json(CNN_URL, headers={"Referer": "https://edition.cnn.com/", "Accept": "application/json"})
        fg = j["fear_and_greed"]
        if not 0 <= fg["score"] <= 100:
            raise RuntimeError(f"分數超出範圍：{fg['score']}")
        cut = us_cutoff()
        hist = {r[0]: r for r in read_rows(cpath)}
        pts = (j.get("fear_and_greed_historical") or {}).get("data") or []
        if len(pts) < 100:
            raise RuntimeError(f"歷史只有 {len(pts)} 點")
        for p in pts:
            day = dt.datetime.fromtimestamp(p["x"] / 1000, NY).strftime("%Y-%m-%d")
            if day <= cut:
                hist[day] = [day, round(p["y"], 2), p.get("rating", "")]
        rows = sorted(hist.values())
        parts = {}
        for k in CNN_PARTS:
            v = j.get(k) or {}
            data = v.get("data") or []
            parts[k] = {"score": v.get("score"), "rating": v.get("rating"), "time_utc": _utc_text(v.get("timestamp")),
                        "value": data[-1].get("y") if data else None}
        snap = {"fetched_at_taipei": NOW.astimezone(TPE).strftime("%Y-%m-%d %H:%M"),
                "time_utc": _utc_text(fg.get("timestamp")), "score": round(fg["score"], 2), "rating": fg.get("rating"),
                **{k: round(fg[k], 2) for k in ("previous_close", "previous_1_week", "previous_1_month",
                                                "previous_1_year") if fg.get(k) is not None},
                "components": parts, "source": CNN_URL}
        write_json(jpath, snap)
        write_rows(cpath, rows)
        meta["datasets"][key] = {
            "status": "ok", "source": "CNN dataviz graphdata", "rows": len(rows), "first": rows[0][0],
            "last": rows[-1][0], "last_score": float(rows[-1][1]), "score_now": snap["score"],
            "rating_now": snap["rating"], "time_utc": snap["time_utc"],
            "components": sum(1 for v in parts.values() if v["score"] is not None),
            "cross_check": "無第二來源，未核對"}
    except Exception as ex:  # noqa: BLE001
        mark_failed(meta, key, cpath, "CNN fear & greed", ex)


def fetch_taifex_vix(cfg, meta):
    """期交所「前 3 個月每日收盤之臺指選擇權波動率指數」月檔（Big5 文字檔）。
    平常抓本月與上月；CSV 還不存在時多抓兩個月補歷史。期交所從 GitHub 偶爾第一次逾時，所以放寬逾時。"""
    key, path = "sentiment/taifex_vix", os.path.join(DATA, "sentiment", "taifex_vix.csv")
    try:
        hist = {r[0]: r for r in read_rows(path)}
        month = NOW.astimezone(TPE).date().replace(day=1)
        months = [month]
        for _ in range(1 if hist else 3):
            months.append((months[-1] - dt.timedelta(days=1)).replace(day=1))
        cut, got = tw_cutoff(), 0
        for m in months:
            status, raw = http(VIX_URL.format(m), timeout=40)
            if status == 200:
                for d, v in re.findall(r"^(\d{8})\s+\d+\s+([\d.]+)", raw.decode("big5", "replace"), re.M):
                    day = f"{d[:4]}-{d[4:6]}-{d[6:]}"
                    if day <= cut and 3 < float(v) < 150:
                        hist[day] = [day, float(v)]
                        got += 1
            time.sleep(1)
        if not got:
            raise RuntimeError("月檔沒有資料列")
        rows = sorted(hist.values())
        write_rows(path, rows)
        meta["datasets"][key] = {"status": "ok", "source": "期交所 vix/log2data 月檔", "rows": len(rows),
                                 "first": rows[0][0], "last": rows[-1][0], "last_vix": float(rows[-1][1]),
                                 "cross_check": "無第二來源，未核對"}
    except Exception as ex:  # noqa: BLE001
        mark_failed(meta, key, path, "TAIFEX VIX", ex)

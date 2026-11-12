"""候選 ETF（stock-dashboard 機會頁第二版，docs/OPPORTUNITY.md 第 3 節；使用者 2026-11-12 決定照現有做法放公開 repo）。

從上市 ETF 裡挑出「台股股票型與海外股票型、上市滿一年、近 60 個交易日日均成交金額最大的 30 檔」，每月重排一次，
輸出（data/candidates/）：
  universe.json       候選名單：items（名次、代號、名稱、類別 tw／overseas、上市日、日均成交金額（元））與 excluded（被排除的檔和原因）
  turnover.csv        date,code,value（無表頭）：通過類別與排除規則的上市 ETF 每天的成交金額（元），保留最近 90 個交易日
  <代號>_norm.csv     每檔日線 date,volume,open,high,low,close（無表頭，格式同 data/tw/<代號>_norm.csv），約 15 個月；
                      代號已在 config.json 的 tw 或 tw_watch 的不重抓，沿用 data/tw/ 的檔；退出名單的檔保留、不再更新
  closes.csv          收盤合併檔：表頭 date,<代號…>（照名次排），每列一個交易日，沒有資料留空，最近 300 個交易日

來源（只用 OpenAPI 與 FinMind，不抓證交所、櫃買的網站報表頁）：
- 名單：證交所 OpenAPI opendata/t187ap47_L（基金基本資料彙總表：基金代號、簡稱、基金類型、上市日期（民國 YYYMMDD））。
  只有上市，上櫃 ETF 不列入。
- 每天的成交金額：證交所 OpenAPI exchangeReport/STOCK_DAY_ALL 的 TradeValue（元）；同一次執行重用 twwatch 已經抓的那份回應。
- 第一次（turnover.csv 不存在，或某檔不到一半窗口的天數）：FinMind TaiwanStockPrice 的 Trading_money（元，
  2026-11-12 實測：0050 在 2026-11-11 是 12,595,528,482，和 OpenAPI TradeValue 完全一致）補近 60 個交易日，每檔一次請求。
- 日線：FinMind（歷史）＋ STOCK_DAY_ALL（最新一天），做法同 twwatch.official_daily。價格沒有還原配息與分割。

FinMind 免費額度每小時約 300 次，所以這裡自己節流：兩次請求至少隔 1.5 秒、一次執行最多 250 次，碰到額度用完就整個停、
不重試；已補到的成交金額先寫檔，下次只補缺的。每檔每天最多向 FinMind 抓一次日線歷史（記在 meta 的 history_fetched），
同一天後面幾次執行只用 OpenAPI 補最新一天。

用法：由 fetch.py 的 PHASE1_STEPS 呼叫 fetch_candidates(cfg, meta)；也可以單獨跑：python candidates.py（結果印出來，不動 meta.json）。
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time

import twwatch
from common import (DATA, NOW, ROOT, TPE, finalize, finmind, mark_failed, num, prev_meta, read_rows, roc_ymd,
                    tw_cutoff, twse_openapi, write_csv, write_json, write_rows)

LIST_PATH = "opendata/t187ap47_L"
DAY_ALL_PATH = "exchangeReport/STOCK_DAY_ALL"

TOP_N = 30             # 取日均成交金額前幾名
WINDOW_DAYS = 60       # 算日均成交金額的交易日數
MIN_DAYS = 5           # 累積不到 60 天就用現有天數，但至少要有幾天
KEEP_DAYS = 90         # turnover.csv 保留幾個交易日
LISTED_DAYS = 365      # 上市滿幾天才算
HISTORY_MONTHS = 15    # 每檔日線至少幾個月
CLOSES_ROWS = 300      # closes.csv 放幾個交易日
SEED_START_MONTHS = HISTORY_MONTHS  # 補歷史時一次抓日線的月數（同一個請求也拿來當日線歷史）
MIN_DAILY_ROWS = 60    # 日線不到這個列數就不寫檔
FINMIND_GAP = 1.5      # 兩次 FinMind 請求至少間隔幾秒
FINMIND_LIMIT = 250    # 一次執行最多幾次 FinMind 請求（免費額度每小時約 300 次，其他步驟也要用）

RULE = f"上市 ETF、台股與海外股票型、上市滿一年、日均成交金額前 {TOP_N}"
SOURCE = "證交所 OpenAPI（名單、成交金額）；日線 FinMind＋證交所 OpenAPI"

# 排除規則（任一成立就排除；原因都記進 universe.json 的 excluded）
EXCLUDE_SUFFIX = "BLRUKCTD"
EXCLUDE_TYPE = ("槓桿", "反向", "期貨", "平衡", "債券", "連結", "境外", "外幣")
EXCLUDE_NAME = ("黃金", "白銀", "原油", "石油", "不動產", "REIT", "債")
QUOTA = re.compile(r"\b(402|429)\b|upper limit|too many|rate limit", re.I)


# ---------------------------------------------------------------- 名單：類別與排除
def classify(rec):
    """基金基本資料的一列 → (類別 'tw'／'overseas'／None, 排除原因清單)。
    基金類型要是指數股票型基金或主動式（股票）：含「國內成分」是 tw、含「國外成分」是 overseas。"""
    code = (rec.get("基金代號") or "").strip()
    name = html.unescape((rec.get("基金簡稱") or "").strip())  # 這支 API 的名稱是跳脫過的（元大S&amp;P500）
    typ = (rec.get("基金類型") or "").strip()
    reasons = []
    if code[-1:].isalpha() and code[-1] in EXCLUDE_SUFFIX:
        reasons.append(f"尾碼 {code[-1]}")
    reasons += [f"基金類型含 {w}" for w in EXCLUDE_TYPE if w in typ]
    reasons += [f"名稱含 {w}" for w in EXCLUDE_NAME if w in name.upper()]
    stock = "指數股票型基金" in typ or ("主動式" in typ and "股票" in typ)
    category = None
    if stock and "國內成分" in typ:
        category = "tw"
    elif stock and "國外成分" in typ:
        category = "overseas"
    if category is None and not reasons:
        reasons.append(f"基金類型不是台股或海外的股票型（{typ or '空白'}）")
    return (None if reasons else category), reasons


def parse_listing(rows):
    """t187ap47_L 的回應 → ({代號: {name, category, listed}}（通過規則的）, [{code, name, reason}]（被排除的，依代號排）)。"""
    ok, excluded = {}, []
    for r in rows:
        code = (r.get("基金代號") or "").strip()
        if not code:
            continue
        name = html.unescape((r.get("基金簡稱") or "").strip())
        category, reasons = classify(r)
        try:
            listed = roc_ymd(r["上市日期"])
        except Exception:  # noqa: BLE001
            listed = None
            reasons = reasons + [f"上市日期無法解析（{r.get('上市日期')!r}）"]
            category = None
        if category:
            ok[code] = {"name": name, "category": category, "listed": listed}
        else:
            excluded.append({"code": code, "name": name, "reason": "、".join(reasons)})
    excluded.sort(key=lambda x: x["code"])
    return ok, excluded


def listed_enough(info, day):
    """上市日期在 day 往前 LISTED_DAYS 天以前（含當天）才算上市滿一年。"""
    return info["listed"] <= (dt.date.fromisoformat(day) - dt.timedelta(days=LISTED_DAYS)).isoformat()


# ---------------------------------------------------------------- 成交金額
def load_turnover(path):
    """turnover.csv → {(date, code): 成交金額（元）}。"""
    out = {}
    for r in read_rows(path):
        if len(r) >= 3:
            try:
                out[(r[0], r[1])] = int(float(r[2]))
            except ValueError:
                pass
    return out


def trim_turnover(turnover, keep=KEEP_DAYS):
    """只留最近 keep 個交易日（依日期，所有代號合起來算）。"""
    days = set(sorted({d for d, _ in turnover})[-keep:])
    return {k: v for k, v in turnover.items() if k[0] in days}


def save_turnover(path, turnover):
    write_rows(path, sorted((d, c, v) for (d, c), v in trim_turnover(turnover).items()))


def add_day_all(turnover, rows, codes, cut):
    """STOCK_DAY_ALL 的回應 → 把 codes 的成交金額（元）寫進 turnover（同一天同一檔以新的為準）。回傳寫入的列數。"""
    n = 0
    for r in rows:
        if r.get("Code") not in codes:
            continue
        v, day = num(r.get("TradeValue")), roc_ymd(r["Date"])
        if v is not None and day <= cut:
            turnover[(day, r["Code"])] = int(round(v))
            n += 1
    return n


def thin(turnover, code, dates):
    """這一檔的成交金額天數不到一半窗口（至少 MIN_DAYS 天）→ 要向 FinMind 補歷史。"""
    n = sum(1 for d in dates[-WINDOW_DAYS:] if (d, code) in turnover)
    return n < max(MIN_DAYS, min(len(dates), WINDOW_DAYS) / 2)


# ---------------------------------------------------------------- FinMind 節流
class FinMindGate:
    """FinMind 的節流與額度：兩次請求至少隔 gap 秒、一次執行最多 limit 次。
    額度用完（402、429）就整個停掉、不再重試；連線等其他錯誤連續 3 次也停。stopped＝停掉的原因（沒停是 None）。"""

    def __init__(self, gap=None, limit=None, sleep=None, clock=None):
        self.gap = FINMIND_GAP if gap is None else gap
        self.limit = FINMIND_LIMIT if limit is None else limit
        self.sleep, self.clock = sleep or time.sleep, clock or time.monotonic
        self.used, self.fails, self.stopped, self._last = 0, 0, None, None

    def __call__(self, dataset, data_id, start):
        if self.stopped:
            raise RuntimeError(f"FinMind 已停止使用：{self.stopped}")
        if self.used >= self.limit:
            self.stopped = f"本次已用 {self.used} 次（上限 {self.limit}）"
            raise RuntimeError(f"FinMind 已停止使用：{self.stopped}")
        if self._last is not None and self.gap > 0:
            wait = self.gap - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self.used += 1
        try:
            data = finmind(dataset, data_id, start)
            self.fails = 0
            return data
        except Exception as ex:  # noqa: BLE001
            self.fails += 1
            if QUOTA.search(str(ex)):
                self.stopped = f"額度用完（{str(ex)[:150]}）"
            elif self.fails >= 3:
                self.stopped = f"連續 {self.fails} 次失敗（{str(ex)[:150]}）"
            raise
        finally:
            self._last = self.clock()


def seed_turnover(turnover, codes, gate, cut, start, save):
    """向 FinMind 補 codes 近 WINDOW_DAYS 個交易日的成交金額（Trading_money，元），每檔一次請求。
    start 同時拿來當日線歷史的起點，所以請求的回應一併回傳（之後不必再抓一次）。
    回傳 ({代號: FinMind 回應}, 沒補成的 {代號: 原因})。已補到的每 10 檔先寫檔，中途停下來也不會白費。"""
    got, failed = {}, {}
    try:
        for i, code in enumerate(codes):
            try:
                data = gate("TaiwanStockPrice", code, start)
            except Exception as ex:  # noqa: BLE001
                failed[code] = str(ex)[:200]
                if gate.stopped:
                    failed.update({rest: f"FinMind 已停止使用：{gate.stopped}" for rest in codes[i + 1:]})
                    break
                continue
            got[code] = data
            days = sorted((x["date"], num(x.get("Trading_money"))) for x in data
                          if x.get("date", "") <= cut and num(x.get("Trading_money")) is not None)
            for day, v in days[-WINDOW_DAYS:]:
                turnover[(day, code)] = int(round(v))
            if len(got) % 10 == 0:
                save()
    finally:
        save()
    return got, failed


# ---------------------------------------------------------------- 排名
def rank_universe(ok, excluded, turnover, ranked_on, now_text):
    """通過規則的 ETF + 成交金額 → universe.json 的內容。
    窗口＝turnover 裡最近 WINDOW_DAYS 個交易日；每檔的日均＝它在窗口內有資料的那幾天的平均。
    上市未滿一年、窗口內成交資料不到 MIN_DAYS 天的不排名，連同原因放進 excluded。"""
    dates = sorted({d for d, _ in turnover})
    win = set(dates[-WINDOW_DAYS:])
    per = {}
    for (d, c), v in turnover.items():
        if d in win and c in ok:
            per.setdefault(c, []).append(v)
    scored, dropped = [], []
    for code, info in sorted(ok.items()):
        vals = per.get(code, [])
        if not listed_enough(info, ranked_on):
            dropped.append({"code": code, "name": info["name"], "reason": f"上市未滿一年（上市日 {info['listed']}）"})
        elif len(vals) < MIN_DAYS:
            dropped.append({"code": code, "name": info["name"],
                            "reason": f"近 {len(win)} 個交易日只有 {len(vals)} 天成交金額（至少 {MIN_DAYS} 天）"})
        else:
            scored.append((sum(vals) / len(vals), code))
    scored.sort(key=lambda x: (-x[0], x[1]))
    items = [{"code": c, "name": ok[c]["name"], "category": ok[c]["category"], "listed": ok[c]["listed"],
              "avg_value": int(round(avg)), "rank": i} for i, (avg, c) in enumerate(scored[:TOP_N], 1)]
    return {"generated_at_taipei": now_text, "ranked_on": ranked_on, "window_days": len(win), "rule": RULE,
            "source": SOURCE, "items": items,
            "excluded": sorted(excluded + dropped, key=lambda x: x["code"])}


def need_rank(universe, latest_day):
    """每月第一個交易日重排（實作成「這個月還沒排過就排」，漏跑時下一次補排），或 universe.json 不存在。"""
    prev = (universe or {}).get("ranked_on")
    return not prev or prev[:7] < latest_day[:7]


# ---------------------------------------------------------------- 日線與合併檔
def norm_path(code, shared):
    return os.path.join(DATA, "tw" if code in shared else "candidates", f"{code}_norm.csv")


def update_daily(code, path, start, cut, latest, fetch, skip_history):
    """一檔日線：FinMind 歷史＋OpenAPI 最新一天（official_daily），寫 path。
    skip_history：今天已經向 FinMind 抓過，這次只用 OpenAPI 補最新一天。
    回傳 (有向 FinMind 抓成功, 警告清單)；列數不夠就丟例外（舊檔保留）。"""
    prev = {r[0]: twwatch.bar(*r[:6]) for r in read_rows(path) if len(r) >= 6}
    if skip_history and code not in latest:
        return True, []  # 今天抓過歷史、OpenAPI 又沒有新的一天，不必動
    rows, _, _, warn = twwatch.official_daily(code, "TWSE", start, latest, prev,
                                              fetch=(lambda *a: []) if skip_history else fetch)
    out = [r for r in finalize(rows, cut) if r[0] >= start]
    if len(out) < MIN_DAILY_ROWS:
        raise RuntimeError(f"只有 {len(out)} 列")
    write_csv(path, out)
    return skip_history or not any(w.startswith("FinMind") for w in warn), warn


def build_closes(codes, path_of, keep=CLOSES_ROWS):
    """每檔日線的收盤合併成一張表。回傳 (表頭, 列)；某天某檔沒有資料就留空。"""
    series = {c: {r[0]: r[5] for r in read_rows(path_of(c)) if len(r) >= 6} for c in codes}
    days = sorted({d for s in series.values() for d in s})[-keep:]
    return ["date", *codes], [[d, *[series[c].get(d, "") for c in codes]] for d in days]


# ---------------------------------------------------------------- 主流程
def fetch_candidates(cfg, meta, gate=None):
    """gate：FinMind 節流器（沒給就建一個；main() 傳進來是為了跑完印出請求次數）。"""
    base = os.path.join(DATA, "candidates")
    p_universe, p_turn, p_closes = (os.path.join(base, n) for n in ("universe.json", "turnover.csv", "closes.csv"))
    cut = tw_cutoff()
    today = NOW.astimezone(TPE).date()
    notes = []  # 這一步的警告（FinMind 停用、OpenAPI 失敗…），放進 meta
    gate = gate or FinMindGate()

    # 1. 名單與今天的成交金額（STOCK_DAY_ALL 重用 twwatch 同一次執行抓的那份，沒抓過才自己抓）
    ok = None
    try:
        ok, excluded = parse_listing(twse_openapi(LIST_PATH))
    except Exception as ex:  # noqa: BLE001
        notes.append(f"證交所 OpenAPI {LIST_PATH}：{ex}")
    day_rows = twwatch.LAST_DAY_ALL
    if day_rows is None:
        try:
            day_rows = twwatch.LAST_DAY_ALL = twse_openapi(DAY_ALL_PATH)
        except Exception as ex:  # noqa: BLE001
            day_rows = []
            notes.append(f"證交所 OpenAPI {DAY_ALL_PATH}：{ex}")
    elif not day_rows:
        notes.append(f"證交所 OpenAPI {DAY_ALL_PATH}：這次執行稍早已經失敗")

    turnover = load_turnover(p_turn)
    codes = set(ok) if ok is not None else {c for _, c in turnover}  # 名單失敗就只更新 turnover.csv 裡已有的代號
    try:
        if add_day_all(turnover, day_rows, codes, cut):
            save_turnover(p_turn, turnover)
    except Exception as ex:  # noqa: BLE001
        notes.append(f"成交金額解析：{ex}")

    # 2. 補歷史：上市滿一年、成交金額天數不到一半窗口的，向 FinMind 補（第一次就是全部）
    dates = sorted({d for d, _ in turnover})
    latest_day = dates[-1] if dates else cut
    start = (today - dt.timedelta(days=31 * SEED_START_MONTHS)).isoformat()
    fm_cache, seed_failed = {}, {}
    if ok:
        # 基金清單裡還留著、但已經不在 STOCK_DAY_ALL 的（下市、終止交易，2026-11-12 實測有 14 檔）永遠補不到，不要每次都去問 FinMind
        trading = {r.get("Code") for r in day_rows}
        need = [c for c in sorted(ok) if listed_enough(ok[c], latest_day) and thin(turnover, c, dates)
                and (not trading or c in trading)]
        if need:
            # FinMind 常比 OpenAPI 早一天有當天資料；補到 OpenAPI 的最後一天為止，讓每一檔的窗口日期一致
            api_days = [roc_ymd(r["Date"]) for r in day_rows if r.get("Date")]
            fm_cache, seed_failed = seed_turnover(turnover, need, gate, min([cut] + api_days[:1]), start,
                                                  lambda: save_turnover(p_turn, turnover))
            dates = sorted({d for d, _ in turnover})
            latest_day = dates[-1] if dates else cut
        if seed_failed:
            names = sorted(seed_failed)
            notes.append(f"FinMind 補成交金額沒補成 {len(names)} 檔（{', '.join(names[:5])}{'…' if len(names) > 5 else ''}）："
                         f"{seed_failed[names[0]]}")
        if gate.stopped:
            notes.append(f"FinMind 已停止使用：{gate.stopped}")

    # 3. 排名：這個月還沒排過、或沒有名單就重排；失敗時保留舊名單
    universe = None
    try:
        with open(p_universe, encoding="utf-8") as f:
            universe = json.load(f)
    except (OSError, ValueError):
        pass
    if need_rank(universe, latest_day):
        try:
            if ok is None:
                raise RuntimeError("沒有基金基本資料（" + "；".join(notes) + "）")
            if len(dates) < MIN_DAYS:
                raise RuntimeError(f"成交金額只有 {len(dates)} 個交易日（至少 {MIN_DAYS} 天）")
            if seed_failed:
                raise RuntimeError(f"{len(seed_failed)} 檔的成交金額歷史還沒補完，下次再排名")
            new = rank_universe(ok, excluded, turnover, latest_day, NOW.astimezone(TPE).strftime("%Y-%m-%d %H:%M"))
            if not new["items"]:
                raise RuntimeError("沒有符合資格的 ETF")
            write_json(p_universe, new)
            universe = new
        except Exception as ex:  # noqa: BLE001
            mark_failed(meta, "candidates/universe", p_universe, "候選 ETF 名單", ex)
    if universe and "candidates/universe" not in meta["datasets"]:
        meta["datasets"]["candidates/universe"] = {
            "status": "ok", "source": SOURCE, "last": universe["ranked_on"], "items": len(universe["items"]),
            "window_days": universe["window_days"]}
    if not universe:
        mark_failed(meta, "candidates/closes", p_closes, "候選 ETF 收盤合併檔", RuntimeError("沒有名單"))
        meta["errors"] += [f"候選 ETF：{n}" for n in notes]
        return

    # 4. 每檔日線：歷史每檔每天最多向 FinMind 抓一次（記在 meta 的 history_fetched），其餘只用 OpenAPI 補最新一天
    shared = set(cfg.get("tw") or {}) | set(cfg.get("tw_watch") or {})
    own = [it["code"] for it in universe["items"] if it["code"] not in shared]
    try:
        latest = twwatch.twse_day_bars(day_rows, set(own))
    except Exception as ex:  # noqa: BLE001 — 最新一天補不上，日線還是照 FinMind 的歷史更新
        latest = {}
        notes.append(f"STOCK_DAY_ALL 解析：{ex}")
    done_before = prev_meta("candidates/closes").get("history_fetched") or {}
    fetched, bad = {}, []
    for code in own:
        path = norm_path(code, shared)
        try:
            cached = fm_cache.get(code)  # 這次補成交金額時已經抓過這一檔（同一個請求），直接用
            same_day = cached is None and done_before.get(code) == today.isoformat() and os.path.exists(path)
            got_fm, warn = update_daily(code, path, start, cut, latest, gate if cached is None else (lambda *a: cached),
                                        skip_history=same_day)
            if got_fm:
                fetched[code] = today.isoformat()
            bad += [f"{code}：{w}" for w in warn]
        except Exception as ex:  # noqa: BLE001
            bad.append(f"{code}：{ex}")
    if gate.stopped and not any(gate.stopped in n for n in notes):  # 補日線歷史時才停的
        notes.append(f"FinMind 已停止使用：{gate.stopped}")
    notes += bad[:5] + ([f"…共 {len(bad)} 則"] if len(bad) > 5 else [])

    # 5. 合併檔
    try:
        header, rows = build_closes([it["code"] for it in universe["items"]], lambda c: norm_path(c, shared))
        if not rows:
            raise RuntimeError("每檔日線都是空的")
        write_rows(p_closes, [header] + rows)
        m = {"status": "ok", "source": SOURCE, "rows": len(rows), "first": rows[0][0], "last": rows[-1][0],
             "history_fetched": fetched}
        if notes:
            m["warnings"] = notes
        meta["datasets"]["candidates/closes"] = m
    except Exception as ex:  # noqa: BLE001
        mark_failed(meta, "candidates/closes", p_closes, "候選 ETF 收盤合併檔", ex)
    meta["errors"] += [f"候選 ETF：{n}" for n in notes]


def main():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    meta, gate = {"datasets": {}, "errors": []}, FinMindGate()
    fetch_candidates(cfg, meta, gate)
    print(json.dumps(meta, ensure_ascii=False, indent=1))
    print(f"FinMind 請求 {gate.used} 次" + (f"，已停用：{gate.stopped}" if gate.stopped else ""), file=sys.stderr)


if __name__ == "__main__":
    main()

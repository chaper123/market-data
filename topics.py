"""新聞話題統計（news.py 每次執行後呼叫），固定規則計數、不做預測。

data/news/topic_history.json  每天（台北日期）各主題與標題詞的則數，保留 35 天。完全落在這次 72 小時新聞窗內的日子
                              每次重算（所以重跑不會重複計數），更早的日子凍結不動
data/news/topics.json         topics[]：現有固定主題（config 的 news.topic_rules）的 count_1d（最近 24 小時）、count_7d、
                              count_30d、rise＝(count_7d/7)÷(count_30d/30)、sample_ids；歷史不足 30 天時分母改用
                              已有天數（days_available），並照實標出
                              rising_terms[]：標題詞（英文單字、中文 2～3 字詞）近 1 天相對 30 天平均上升最多的，
                              去掉常用字，用來抓固定主題沒有的新話題（盡力而為）
"""
import datetime as dt
import json
import os
import re

KEEP_DAYS = 35
MAX_TERMS_PER_DAY = 600
MIN_TERM_1D = 3
EN_STOP = set("""a about above after again against all also amid an and any are as at back be been before being between
both but by can could day days did do does down during each early first for from had has have he her his how if in into
is it its just last late like make may more most much new news no not now of off on one only or other our out over says
said see set she should so some still such than that the their them then there these they this those through to today
top under up us vs was we week weeks were what when where which while who why will with would year years you your
stock stocks share shares market markets price prices report reports update live latest inc corp co ltd plc group
billion million percent rise rises fall falls gain gains drop drops higher lower record big says""".split())
ZH_STOP = set("""台股 指數 今天 今日 表示 美國 台灣 公司 市場 股價 上漲 下跌 外資 投資 布局 一個 不是 什麼 我們 他們
這個 那個 可能 以及 成為 進行 持續 預估 預期 分析 報導 消息 新聞 最新 第一 一次 年度 今年 去年 明年 本週 上週 下週
收盤 開盤 大盤 加權 漲幅 跌幅 個股 族群 盤中 盤後 法人 營收 獲利 財報 億元 萬元 美元 台幣""".split())
CJK = re.compile(r"[一-鿿]+")
EN_WORD = re.compile(r"[a-z][a-z0-9&'\-]{2,}")


def title_terms(title):
    """標題裡的詞：英文小寫單字（去常用字）、中文連續字的 2～3 字片段（去常用詞）。同一則只算一次。"""
    text = title.lower()
    terms = {w.strip("'-") for w in EN_WORD.findall(text)} - EN_STOP
    for run in CJK.findall(title):
        for n in (2, 3):
            terms |= {run[i:i + n] for i in range(len(run) - n + 1)}
    return {t for t in terms if len(t) >= 2 and t not in ZH_STOP}


def day_of(it):
    return it["published_taipei"][:10]


def update_history(history, items, now_tpe, window_hours):
    """重算完全落在新聞窗內的日子（窗開始那天只有一部分，不動），回傳新的 history。"""
    days = dict(history.get("days", {}))
    first_full = (now_tpe - dt.timedelta(hours=window_hours)).date() + dt.timedelta(days=1)
    fresh = {}
    for it in items:
        d = day_of(it)
        if d >= first_full.isoformat():
            rec = fresh.setdefault(d, {"items": 0, "topics": {}, "terms": {}})
            rec["items"] += 1
            for t in it.get("topics", []):
                rec["topics"][t] = rec["topics"].get(t, 0) + 1
            for w in title_terms(it["title"]):
                rec["terms"][w] = rec["terms"].get(w, 0) + 1
    for d, rec in fresh.items():
        top = sorted(((w, c) for w, c in rec["terms"].items() if c >= 2), key=lambda kv: (-kv[1], kv[0]))
        top = top[:MAX_TERMS_PER_DAY]  # 只存出現 2 次以上的前幾百個詞，30 天合計是下限
        rec["terms"] = dict(top)
        days[d] = rec
    oldest = (now_tpe.date() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    return {"days": {d: days[d] for d in sorted(days) if d > oldest}}


def samples(items, pred, n=5):
    """符合條件的新聞 id：等級高的優先、再來最新。"""
    hit = [it for it in items if pred(it)]
    hit.sort(key=lambda it: it["published_utc"], reverse=True)
    hit.sort(key=lambda it: it.get("tier", 9))
    return [it["id"] for it in hit[:n]]


def summarize(history, items, now_tpe, rules, labels):
    today = now_tpe.date()
    in_range = lambda k: [d for d in history["days"] if (today - dt.date.fromisoformat(d)).days < k]  # noqa: E731
    d7, d30 = in_range(7), in_range(30)
    avail = len(d30)
    since = (now_tpe - dt.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    last24 = [it for it in items if it["published_taipei"] >= since]

    def total(days, kind, key):
        return sum(history["days"][d][kind].get(key, 0) for d in days)

    topics = []
    for t in rules:
        c7, c30 = total(d7, "topics", t), total(d30, "topics", t)
        rise = round((c7 / max(1, min(7, avail))) / (c30 / max(1, min(30, avail))), 2) if c30 else None
        topics.append({"topic": t, "label": labels.get(t, t), "count_1d": sum(t in it.get("topics", []) for it in last24),
                       "count_7d": c7, "count_30d": c30, "rise": rise,
                       "sample_ids": samples(items, lambda it, t=t: t in it.get("topics", []))})
    terms_1d = {}
    for it in last24:
        for w in title_terms(it["title"]):
            terms_1d[w] = terms_1d.get(w, 0) + 1
    rising = []
    if avail >= 2:
        base_days = max(1, avail - 1)
        for w, c1 in terms_1d.items():
            if c1 < MIN_TERM_1D:
                continue
            c30 = max(total(d30, "terms", w), c1)
            base = max(0, c30 - c1) / base_days  # 今天以外的日平均
            rising.append((c1 / (base + 1), w, c1, c30))
        rising.sort(key=lambda x: (-x[0], -x[2], x[1]))
        picked = []
        for score, w, c1, c30 in rising:
            # 中文片段互相包含時留較長、次數差不多的那個
            if any((w in p["term"] or p["term"] in w) and p["count_1d"] >= c1 * 0.8 for p in picked):
                continue
            picked.append({"term": w, "count_1d": c1, "count_30d": c30, "score": round(score, 2),
                           "sample_ids": samples(last24, lambda it, w=w: w in title_terms(it["title"]))})
            if len(picked) >= 15:
                break
        rising = picked
    ranked = [x for x in topics if x["rise"] is not None and x["count_7d"] >= 5]
    return {"days_available": avail, "history_since": min(history["days"]) if history["days"] else None,
            "topics": topics,
            "rising_topic": max(ranked, key=lambda x: x["rise"])["topic"] if ranked else None,
            "rising_terms": rising}


def update(items, now_tpe, cfg, hist_path, out_path):
    """news.py 呼叫：更新歷史與 topics.json，回傳 topics.json 的內容。"""
    try:
        with open(hist_path, encoding="utf-8") as fh:
            history = json.load(fh)
    except (OSError, ValueError):
        history = {"days": {}}
    history = update_history(history, items, now_tpe, cfg["window_hours"])
    doc = {"generated_at_taipei": now_tpe.strftime("%Y-%m-%d %H:%M"),
           "rule": ("count_1d＝最近 24 小時；count_7d／count_30d＝最近 7／30 個台北日曆日（含今天）；"
                    "rise＝(count_7d/7)÷(count_30d/30)，歷史不足時分母用已有天數；rising_terms 的 score＝"
                    "近 24 小時則數÷(今天以外的日平均＋1)。固定規則計數，不做預測"),
           **summarize(history, items, now_tpe, cfg["topic_rules"], cfg.get("topic_labels", {}))}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    for path, obj in ((hist_path, history), (out_path, doc)):
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)
    return doc

"""來源探測（Phase 1）：檢查候選資料來源從 GitHub Actions 能否取得資料。

只用 Python 標準庫；不寫入 data/、不 commit。每個來源記錄 HTTP 狀態、位元組數、延遲，
以及是否取得關鍵欄位（例如最後日期與數值）。結果印成 Markdown 表格；在 GitHub Actions 中
另外寫進 job summary（$GITHUB_STEP_SUMMARY），最後印一行 JSON 方便從 log 讀取。

只做一般的 GET，遇到防機器人驗證頁就記為失敗，不嘗試繞過。

用法：python probe.py [分組 ...]（分組見 STEPS，例如 daily；不給就全部）
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar
from zoneinfo import ZoneInfo

TPE = ZoneInfo("Asia/Taipei")
UTC = dt.timezone.utc
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
NOW = dt.datetime.now(UTC)
TODAY = NOW.astimezone(TPE).date()
RESULTS = []


# ---------------------------------------------------------------- HTTP（沿用 fetch.py 的重試寫法與 UA）
def http(url, headers=None, tries=3, timeout=20, opener=None):
    """回傳 (status, raw, 秒數, 請求次數, 錯誤)。

    與 fetch.py 不同：4xx（429 除外）直接回傳，被擋就是被擋，不必重試；5xx、429 與網路錯誤才重試。
    全部失敗也不丟例外，探測要把失敗原因記下來。"""
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    open_url = (opener or urllib.request.build_opener()).open
    t0 = time.monotonic()
    status, raw, err = None, b"", None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with open_url(req, timeout=timeout) as r:
                return r.status, r.read(), time.monotonic() - t0, i + 1, None
        except urllib.error.HTTPError as e:
            status, raw, err = e.code, e.read(), f"HTTP {e.code}"
            if e.code < 500 and e.code != 429:
                return status, raw, time.monotonic() - t0, i + 1, err
        except Exception as e:  # noqa: BLE001 — 任何網路錯誤都重試
            status, raw, err = None, b"", f"{type(e).__name__}: {e}"
        if i < tries - 1:
            time.sleep(2 + 3 * i)
    return status, raw, time.monotonic() - t0, tries, err


def cookie_opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def jload(raw):
    return json.loads(raw.decode("utf-8-sig"))


def snippet(raw):
    """失敗時附上回應的標題或開頭，用來分辨防機器人頁、錯誤訊息等。"""
    s = raw[:4000].decode("utf-8", "replace")
    m = re.search(r"<title[^>]*>(.*?)</title>", s, re.S | re.I)
    s = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1) if m else s)).strip()
    return f"｜內容：{s[:80]}" if s else ""


def probe(group, name, urls, check, show_url=None, pause=0, **kw):
    """依序試 urls（例如往回找有資料的交易日），第一個通過 check 的就算成功。

    check(raw) 回傳關鍵欄位的說明字串；取不到就丟例外。HTTP 不是 200 就不再試下一個網址（被擋了換日期也沒用）。"""
    if isinstance(urls, str):
        urls = [urls]
    reqs, key, ok = 0, "", False
    for n, url in enumerate(urls):
        if n and pause:
            time.sleep(pause)
        status, raw, secs, tries, err = http(url, **kw)
        reqs += tries
        try:
            if status != 200:
                raise RuntimeError(err or f"HTTP {status}")
            key, ok = check(raw), True
            break
        except Exception as e:  # noqa: BLE001 — 探測要記下所有失敗
            key = f"{type(e).__name__}: {e}{snippet(raw)}"
            if status != 200:
                break
    row = {"group": group, "name": name, "url": show_url or url, "status": status, "bytes": len(raw),
           "ms": round(secs * 1000), "requests": reqs, "ok": ok, "key": key}
    RESULTS.append(row)
    print(f"{'OK ' if ok else 'NG '} {status} {name}: {key}", flush=True)
    return row


def recent_weekdays(n=6):
    """今天（台北）往回 n 個週一～五，給要指定日期的日報用。"""
    out, d = [], TODAY
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------- 1. 指數與總經收盤（Yahoo chart）
YAHOO = ["^GSPC", "^IXIC", "^SOX", "^VIX", "^TNX", "DX-Y.NYB", "GC=F", "CL=F", "TSM", "^TWII"]


def yahoo_chart(raw):
    r = jload(raw)["chart"]["result"][0]
    tz = ZoneInfo(r["meta"].get("exchangeTimezoneName") or "America/New_York")
    q = r["indicators"]["quote"][0]
    pts = [(t, c) for t, c in zip(r.get("timestamp") or [], q.get("close") or []) if c is not None]
    if not pts:
        raise ValueError("沒有收盤價")
    t, c = pts[-1]
    reg = (r["meta"].get("currentTradingPeriod") or {}).get("regular") or {}
    live = "（盤中）" if reg.get("start", 0) <= NOW.timestamp() < reg.get("end", 0) else ""
    return f"{dt.datetime.fromtimestamp(t, tz):%Y-%m-%d}{live} 收 {c:,.2f}（{len(pts)} 列）"


def probe_yahoo_chart():
    for sym in YAHOO:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym, safe='')}"
               "?range=1mo&interval=1d")
        probe("指數收盤", f"Yahoo chart {sym}", url, yahoo_chart)


# ---------------------------------------------------------------- 2. 恐懼貪婪（CNN）、台指 VIX（期交所）
CNN_SUBS = ("market_momentum_sp500", "stock_price_strength", "stock_price_breadth", "put_call_options",
            "market_volatility_vix", "junk_bond_demand", "safe_haven_demand")


def cnn_fg(raw):
    j = jload(raw)
    fg = j["fear_and_greed"]
    hist = (j.get("fear_and_greed_historical") or {}).get("data") or []
    subs = sum(1 for k in CNN_SUBS if (j.get(k) or {}).get("score") is not None)
    return (f"{fg['timestamp'][:10]} 分數 {fg['score']:.1f}（{fg['rating']}）；"
            f"歷史 {len(hist)} 點；分項 {subs}/{len(CNN_SUBS)}")


def taifex_vix(raw):
    # 每月一檔：日期、時間、收盤 VIX、收盤前 1 分鐘平均（Big5 表頭）
    rows = re.findall(r"^(\d{8})\s+\d+\s+([\d.]+)", raw.decode("big5", "replace"), re.M)
    if not rows:
        raise ValueError("沒有資料列")
    d, v = rows[-1]
    return f"{d[:4]}-{d[4:6]}-{d[6:]} 收 {float(v):.2f}（本月 {len(rows)} 列）"


def probe_sentiment():
    probe("恐懼貪婪", "CNN Fear & Greed", "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
          cnn_fg, headers={"Referer": "https://edition.cnn.com/", "Accept": "application/json"})
    months = [TODAY.replace(day=1), (TODAY.replace(day=1) - dt.timedelta(days=1)).replace(day=1)]
    probe("恐懼貪婪", "期交所 台指VIX 月檔",
          [f"https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/{m:%Y%m}new.txt" for m in months],
          taifex_vix)


# ---------------------------------------------------------------- 3. 台銀美元即期（FinMind；台銀本站當對照組）
def finmind_fx(raw):
    j = jload(raw)
    data = [x for x in j.get("data") or [] if (x.get("spot_sell") or 0) > 0]
    if not data:
        raise ValueError(j.get("msg") or "沒有資料")
    last = data[-1]
    return f"{last['date']} 即期買 {last['spot_buy']} 賣 {last['spot_sell']}（{len(data)} 列）"


def bot_csv(raw):
    rows = [ln for ln in raw.decode("utf-8-sig", "replace").splitlines() if re.match(r"\d{8},", ln)]
    if not rows:
        raise ValueError("沒有資料列")
    return f"{rows[0][:8]} 起 {len(rows)} 列"


def probe_fx():
    start = TODAY - dt.timedelta(days=190)
    probe("匯率", "FinMind TaiwanExchangeRate USD",
          f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanExchangeRate&data_id=USD&start_date={start}",
          finmind_fx)
    probe("匯率", "台銀 6 個月 CSV（對照組）", "https://rate.bot.com.tw/xrt/flcsv/0/L6M/USD", bot_csv)


# ---------------------------------------------------------------- 4. 估值（stockanalysis、Yahoo quoteSummary）
def stockanalysis(*labels):
    def check(raw):
        h = raw.decode("utf-8", "replace")
        got = {}
        for lab in labels:
            m = re.search(r">(?:<!---->)?\s*" + re.escape(lab) + r"\s*(?:<!---->)?<.{0,400}?<td[^>]*>([^<]+)</td>",
                          h, re.S)
            got[lab] = m.group(1).strip() if m else None
        q = re.search(r'quote:\{[^}]*?\bp:([\d.]+)[^}]*?\bu:"([^"]+)"', h)
        parts = [f"{k} {v or '缺'}" for k, v in got.items()]
        if q:
            parts.append(f"價 {q.group(1)}（{q.group(2)}）")
        if not all(got.values()):
            raise ValueError("；".join(parts))
        return "；".join(parts)
    return check


def yahoo_summary(raw):
    r = jload(raw)["quoteSummary"]["result"][0]
    ks, sd, ce = (r.get(m) or {} for m in ("defaultKeyStatistics", "summaryDetail", "calendarEvents"))
    fpe = (ks.get("forwardPE") or sd.get("forwardPE") or {}).get("raw")
    peg = (ks.get("pegRatio") or {}).get("raw")
    ed = [x.get("fmt") for x in ((ce.get("earnings") or {}).get("earningsDate") or [])]
    if fpe is None:
        raise ValueError("沒有 forwardPE")
    return f"Forward PE {fpe:.2f}；PEG {peg if peg is not None else '缺'}；財報日 {'～'.join(ed) or '缺'}"


def probe_valuation():
    probe("估值", "stockanalysis NVDA statistics", "https://stockanalysis.com/stocks/nvda/statistics/",
          stockanalysis("Forward PE", "PEG Ratio"))
    probe("估值", "stockanalysis VOO", "https://stockanalysis.com/etf/voo/", stockanalysis("PE Ratio"))

    base = ("https://query2.finance.yahoo.com/v10/finance/quoteSummary/NVDA"
            "?modules=defaultKeyStatistics,summaryDetail,calendarEvents")
    probe("估值", "Yahoo quoteSummary NVDA（無 crumb）", base, yahoo_summary)

    # 2023 年起 quoteSummary 要 cookie＋crumb：先拿 Yahoo 的 cookie，再換 crumb
    opener = cookie_opener()
    http("https://fc.yahoo.com/", tries=1, opener=opener)  # 回 404 也會設 cookie
    crumb = {}

    def got_crumb(raw):
        s = raw.decode("utf-8", "replace").strip()
        if not s or len(s) > 40 or "<" in s:
            raise ValueError("回應不是 crumb")
        crumb["v"] = s
        return f"取得 crumb（{len(s)} 字元）"

    probe("估值", "Yahoo getcrumb（cookie）", "https://query1.finance.yahoo.com/v1/test/getcrumb", got_crumb,
          opener=opener)
    if crumb:
        probe("估值", "Yahoo quoteSummary NVDA（cookie＋crumb）",
              base + "&crumb=" + urllib.parse.quote(crumb["v"]), yahoo_summary, show_url=base + "&crumb=…",
              opener=opener)


# ---------------------------------------------------------------- 5. 行事曆（證交所休市、FOMC、CPI）
def roc_to_iso(s):
    return f"{int(s[:-4]) + 1911}-{s[-4:-2]}-{s[-2:]}"


def openapi_holiday(raw):
    data = jload(raw)
    rows = sorted((roc_to_iso(x["Date"]), x["Name"]) for x in data)
    nxt = next((r for r in rows if r[0] >= f"{TODAY}"), None)
    return f"{rows[0][0][:4]} 年 {len(rows)} 筆；下一筆 {nxt[0] + ' ' + nxt[1] if nxt else '無'}"


def twse_holiday(raw):
    j = jload(raw)
    rows = j.get("data") or []
    if not rows:
        raise ValueError(j.get("stat") or "沒有資料")
    nxt = next((r for r in rows if r[0] >= f"{TODAY}"), None)
    return f"{j.get('title')}：{len(rows)} 筆；下一筆 {nxt[0] + ' ' + nxt[1] if nxt else '無'}"


MONTHS = {m: i for i, m in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
     "November", "December"), 1)}


def fed_fomc(raw):
    h = raw.decode("utf-8", "replace")
    k = h.find(f"{TODAY.year} FOMC Meetings")
    if k < 0:
        raise ValueError(f"找不到 {TODAY.year} FOMC Meetings")
    end = h.find(f"{TODAY.year - 1} FOMC Meetings", k)
    seg = h[k:end if end > 0 else k + 60000]
    mt = re.findall(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)<',
                    seg, re.S)
    if not mt:
        raise ValueError("找不到會議日期")
    meetings = [(m.split("/")[0].strip(), d.strip()) for m, d in mt]
    upcoming = [f"{m} {d}" for m, d in meetings
                if MONTHS.get(m, 0) > TODAY.month
                or (MONTHS.get(m, 0) == TODAY.month and int(re.findall(r"\d+", d)[-1]) >= TODAY.day)]
    return f"{TODAY.year} 年 {len(meetings)} 次；接下來 {', '.join(upcoming) or '無'}"


def bls_cpi(raw):
    h = raw.decode("utf-8", "replace")
    ds = re.findall(r"([A-Z][a-z]{2})\.? (\d{1,2}), (\d{4})", h)
    days = sorted({dt.datetime.strptime(f"{m} {d} {y}", "%b %d %Y").date() for m, d, y in ds
                   if m in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")})
    nxt = next((d for d in days if d >= TODAY), None)
    if not nxt:
        raise ValueError("找不到日期")
    return f"{len(days)} 個日期；下一次 {nxt}"


def probe_calendar():
    probe("行事曆", "證交所 OpenAPI holidaySchedule", "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule",
          openapi_holiday)
    time.sleep(1)
    probe("行事曆", "證交所 rwd holidaySchedule",
          "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?response=json", twse_holiday,
          headers={"Referer": "https://www.twse.com.tw/"})
    probe("行事曆", "Fed FOMC 行事曆", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", fed_fomc)
    probe("行事曆", "BLS CPI 發布時程", "https://www.bls.gov/schedule/news_release/cpi.htm", bls_cpi)


# ---------------------------------------------------------------- 6. 法人買賣超、融資（證交所、櫃買）
def twse_ok(j):
    if j.get("stat") != "OK":
        raise ValueError(j.get("stat") or "沒有資料")
    return j


def twse_t86(raw):
    j = twse_ok(jload(raw))
    rows = {r[0].strip(): r for r in j["data"]}
    out = [f"{j['date']} 共 {len(rows)} 檔（末欄：{j['fields'][-1]}）"]
    out += [f"{c} {rows[c][-1]}" for c in ("00981A", "009816") if c in rows]
    return "；".join(out)


def twse_bfi82u(raw):
    j = twse_ok(jload(raw))
    rows = {r[0]: r for r in j["data"]}
    foreign = next(v for k, v in rows.items() if k.startswith("外資及陸資"))
    return f"{j['date']} 外資買賣差額 {foreign[3]} 元；合計 {rows['合計'][3]} 元"


def twse_margin(raw):
    j = twse_ok(jload(raw))
    t = j["tables"][0]
    fin = next(r for r in t["data"] if r[0].startswith("融資金額"))
    return f"{j['date']} 融資餘額 {fin[5]} 仟元（前日 {fin[4]}）"


def openapi_margin(raw):
    data = jload(raw)
    r = next((x for x in data if x.get("股票代號") == "2330"), None)
    if not r:
        raise ValueError("找不到 2330")
    return f"{len(data)} 檔（無日期欄位）；2330 融資今日餘額 {r['融資今日餘額']}"


def tpex_insti(raw):
    j = jload(raw)
    t = (j.get("tables") or [{}])[0]
    rows = {r[0].strip(): r for r in t.get("data") or []}
    if not rows:
        raise ValueError(j.get("stat") or "沒有資料")
    r = rows.get("00411A")
    return (f"{t.get('date')} 共 {len(rows)} 檔（末欄：{(t.get('fields') or ['?'])[-1]}）；"
            f"00411A {r[-1] if r else '無此檔'}")


def probe_institutional():
    days = recent_weekdays()
    ref = {"Referer": "https://www.twse.com.tw/"}
    probe("法人與融資", "證交所 T86 三大法人（個股）",
          [f"https://www.twse.com.tw/rwd/zh/fund/T86?date={d:%Y%m%d}&selectType=ALL&response=json" for d in days],
          twse_t86, pause=1.5, headers=ref)
    time.sleep(1.5)
    probe("法人與融資", "證交所 BFI82U 三大法人（金額）",
          [f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&dayDate={d:%Y%m%d}&response=json" for d in days],
          twse_bfi82u, pause=1.5, headers=ref)
    time.sleep(1.5)
    probe("法人與融資", "證交所 MI_MARGN 信用交易統計",
          [f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={d:%Y%m%d}&selectType=MS&response=json"
           for d in days], twse_margin, pause=1.5, headers=ref)
    probe("法人與融資", "證交所 OpenAPI MI_MARGN（個股融資）", "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN",
          openapi_margin)
    probe("法人與融資", "櫃買 三大法人（00411A）",
          [f"https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={d:%Y/%m/%d}&response=json"
           for d in days], tpex_insti, pause=1.5)


# ---------------------------------------------------------------- 7. 主動式 ETF 每日持股（統一投信）
EZMONEY = {"00981A": "49YTW", "00411A": "64YTW"}


def ezmoney(raw):
    h = raw.decode("utf-8", "replace")
    m = re.search(r'id="DataAsset" data-content="([^"]*)"', h)
    if not m:
        raise ValueError("找不到 DataAsset")
    assets = json.loads(html.unescape(m.group(1)))
    details = [d for a in assets for d in (a.get("Details") or [])]
    stocks = [d for d in details if d.get("AssetCode") not in ("GD",)]  # GD = 期貨名目本金
    if not stocks:
        raise ValueError("沒有持股明細")
    top = max(stocks, key=lambda d: d.get("NavRate") or 0)
    kinds = sorted({d.get("AssetCode") for d in stocks})
    return (f"{stocks[0]['TranDate'][:10]} 持股 {len(stocks)} 檔（{'/'.join(kinds)}）；"
            f"最大 {top['DetailCode']} {top['DetailName']} {top['NavRate']}%")


def probe_active_etf():
    for code, fund in EZMONEY.items():
        # 第一次回 302 並設 cookie，帶 cookie 再進同一頁才是內容，所以要 cookie opener
        probe("主動ETF持股", f"統一投信 {code}", f"https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode={fund}", ezmoney,
              opener=cookie_opener())


# ---------------------------------------------------------------- 8. 新聞預篩（SEC EDGAR、Google News RSS）
SEC_CIK = {"NVDA": 1045810, "AAPL": 320193, "GOOGL": 1652044, "TSLA": 1318605}


def sec_submissions(raw):
    j = jload(raw)
    rec = j["filings"]["recent"]
    pairs = list(zip(rec["form"], rec["filingDate"]))
    k8 = next((d for f, d in pairs if f == "8-K"), "無")
    return f"最新 {pairs[0][0]} {pairs[0][1]}；最近 8-K {k8}"


def rss(raw):
    items = ET.fromstring(raw).findall("./channel/item")
    if not items:
        raise ValueError("沒有 item")
    dates = [parsedate_to_datetime(i.findtext("pubDate")) for i in items if i.findtext("pubDate")]
    return f"{len(items)} 則；最新 {max(dates).astimezone(TPE):%Y-%m-%d %H:%M}（台北）"


def probe_news():
    for sym, cik in SEC_CIK.items():
        probe("新聞預篩", f"SEC EDGAR {sym}", f"https://data.sec.gov/submissions/CIK{cik:010d}.json", sec_submissions)
        time.sleep(0.5)
    q_en = urllib.parse.quote("NVIDIA OR Alphabet OR Tesla OR Apple when:1d")
    probe("新聞預篩", "Google News RSS（英文）", f"https://news.google.com/rss/search?q={q_en}&hl=en-US&gl=US&ceid=US:en",
          rss)
    q_tw = urllib.parse.quote("台積電 when:1d")
    probe("新聞預篩", "Google News RSS（中文）",
          f"https://news.google.com/rss/search?q={q_tw}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant", rss)


# ---------------------------------------------------------------- 9. 日線替代來源（鉅亨網最後一根 K 棒停在盤中價）
def nasdaq_hist(raw):
    rows = ((jload(raw).get("data") or {}).get("tradesTable") or {}).get("rows") or []
    if not rows:
        raise ValueError("沒有 rows")
    r = rows[0]  # 由新到舊
    m, d, y = r["date"].split("/")
    return f"{y}-{m}-{d} 收 {r['close']} 量 {r['volume']}（{len(rows)} 列）"


def moneydj_us(raw):
    res = jload(raw)["ResultSet"]["Result"][0]
    days, closes, vols = res["V1"].split(","), res["V5"].split(","), res["V6"].split(",")
    return f"{days[-1].replace('/', '-')} 收 {closes[-1]} 量 {vols[-1]}（{len(days)} 列）"


def moneydj_tw(raw):
    parts = raw.decode("utf-8", "replace").split(" ")
    days, closes = parts[0].split(","), parts[4].split(",")
    if len(days) != len(closes) or not days[0]:
        raise ValueError("欄位數不符")
    return f"{days[-1].replace('/', '-')} 收 {closes[-1]}（{len(days)} 列）"


def tpex_daily(raw):
    t = (jload(raw).get("tables") or [{}])[0]
    rows = t.get("data") or []
    if not rows:
        raise ValueError("沒有資料")
    return f"{rows[-1][0]} 收 {rows[-1][6]}（本月 {len(rows)} 列）"


def probe_daily_sources():
    start = TODAY - dt.timedelta(days=40)
    hdr = {"Accept": "application/json", "Referer": "https://www.nasdaq.com/"}
    for sym, cls in (("ARKX", "etf"), ("TSM", "stocks")):
        probe("日線替代", f"Nasdaq historical {sym}",
              f"https://api.nasdaq.com/api/quote/{sym}/historical?assetclass={cls}&fromdate={start}"
              f"&todate={TODAY}&limit=9999", nasdaq_hist, headers=hdr)
    for sym in ("ARKX", "TSM"):
        probe("日線替代", f"MoneyDJ 美股日線 {sym}",
              f"https://www.moneydj.com/JsonData/USStock/USStockJsonData.xdjjson?x=canvas0002&a={sym}&b=D", moneydj_us)
    probe("日線替代", "MoneyDJ 台股日線 00411A", "https://www.moneydj.com/Z/BCD/czkc1.djbcd?a=00411A&b=D&c=300&E=1&ver=5",
          moneydj_tw)
    probe("日線替代", "櫃買 個股日成交 00411A",
          f"https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?code=00411A&date={TODAY:%Y/%m}/01&response=json",
          tpex_daily)


# ---------------------------------------------------------------- 報告
def cell(s):
    return str(s).replace("|", "\\|").replace("<", "‹").replace(">", "›").replace("\n", " ")


def report():
    ok = sum(r["ok"] for r in RESULTS)
    where = "GitHub Actions" if os.environ.get("GITHUB_ACTIONS") else "本機"
    lines = ["## 來源探測結果", "",
             f"執行：{NOW:%Y-%m-%d %H:%M} UTC（台北 {NOW.astimezone(TPE):%Y-%m-%d %H:%M}），環境：{where}；"
             f"通過 {ok}/{len(RESULTS)}", "",
             "| 分組 | 來源 | HTTP | 位元組 | 延遲 ms | 請求數 | 結果 | 關鍵欄位／錯誤 |",
             "|---|---|---|---:|---:|---:|---|---|"]
    for r in RESULTS:
        lines.append(f"| {r['group']} | [{cell(r['name'])}]({r['url']}) | {r['status'] or '—'} | {r['bytes']:,} | "
                     f"{r['ms']:,} | {r['requests']} | {'✅' if r['ok'] else '❌'} | {cell(r['key'])} |")
    md = "\n".join(lines) + "\n"
    print("\n" + md)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md)
    print("PROBE_JSON=" + json.dumps(RESULTS, ensure_ascii=False))


# ---------------------------------------------------------------- 10. 中央社官方 RSS（Google News 收得很少）
def cna_rss(raw):
    items = ET.fromstring(raw).findall("./channel/item")
    if not items:
        raise ValueError("沒有 item")
    pubs = [parsedate_to_datetime(i.findtext("pubDate")) for i in items if i.findtext("pubDate")]
    links = [i.findtext("link") or "" for i in items]
    direct = sum(1 for u in links if u.startswith("https://www.cna.com.tw/news/"))
    return (f"{len(items)} 則；最新 {max(pubs).astimezone(TPE):%Y-%m-%d %H:%M}、最舊 {min(pubs).astimezone(TPE):%m-%d %H:%M}"
            f"（台北，原始時區 {pubs[0].strftime('%z')}）；cna.com.tw 原文連結 {direct}/{len(items)}")


def probe_cna():
    for name in ("finance", "technology", "intworld", "mainland"):
        probe("中央社 RSS", f"中央社 {name}", f"https://feeds.feedburner.com/rsscna/{name}", cna_rss)


STEPS = {"chart": "probe_yahoo_chart", "sentiment": "probe_sentiment", "fx": "probe_fx",
         "valuation": "probe_valuation", "calendar": "probe_calendar", "institutional": "probe_institutional",
         "etf": "probe_active_etf", "news": "probe_news", "daily": "probe_daily_sources",
         "cna": "probe_cna"}


def main():
    # 不給參數就全部探測；給分組名稱（例如 python probe.py daily）只探測那幾組
    names = [a for a in sys.argv[1:] if a in STEPS] or list(STEPS)
    for step in (globals()[STEPS[n]] for n in names):
        try:
            step()
        except Exception as ex:  # noqa: BLE001 — 單一步驟出錯不影響其他探測
            RESULTS.append({"group": step.__name__, "name": "（步驟例外）", "url": "", "status": None, "bytes": 0,
                            "ms": 0, "requests": 0, "ok": False, "key": f"{type(ex).__name__}: {ex}"})
    report()


if __name__ == "__main__":
    main()

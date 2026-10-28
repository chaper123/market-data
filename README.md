# market-data

每天由 GitHub Actions 抓取行情，給 Claude 的排程任務（台股 ETF 晨報、美股晨報）直接讀取。
排程任務的雲端環境連不到證交所、鉅亨網、台銀與 TradingView，但連得到 GitHub，所以由這裡代抓。

只放公開行情數據，不放帳本、持股或成本。

## 排程

台北時間週一～五 05:13、06:37、07:53、08:41、09:47 各排一次。日線完整（美股、台股與 24 小時交易的總經品項最後日期都到台北今天的前一個平日，且沒有待核對的單一來源 K 棒）之後，其餘幾次只更新 ETF 淨值（投信收盤後才公布，晨報前要再抓一次）。冬令時間 05:13 是美東 16:13，期貨還沒收，會由 06:37（美東 17:37）那次補上。

GitHub 的排程常延遲（實測 2.5～5 小時），偶爾整個漏跑，所以排多次。另外由 cron-job.org 在**美東 17:15（週一～五，America/New_York）**用 workflow_dispatch 準時觸發一次完整抓取（2026-10-20 設定；token 只限本 repo 的 Actions 讀寫，2027-10-20 到期），期貨能在 17:10～18:00 收到當天收盤。需要立刻更新時，到 Actions → fetch-market-data → Run workflow。

另外兩個只由 cron-job.org（或手動）觸發的 workflow，不排程，也不影響上面的略過判斷（news 只更新 meta.json 的 news/*，intraday 不動 meta.json）：
- `news-headlines`（`news.yml` → `news.py`）：台北 08:20（週一～五），更新 `data/news/`，給 08:40 的「新聞與社群晨報」排程讀。
- `intraday-snapshot`（`intraday.yml` → `intraday.py`）：台北 10:05（週一～五），更新 `data/tw/intraday.json`，給 10:17 的台股晨報讀。
- `longterm-macro`（`longterm.yml` → `longterm.py`）：**只手動觸發**（cron-job.org 也沒設），更新 `data/longterm/`（回測用的長週期資料），meta 只更新 longterm/*。要不要定期抓由使用者決定。

四個 workflow 都用 `publish.py` 推送：每次從最新的 main 重新套用自己的檔案與自己負責的 meta 資料集（fetch 負責 news/*、longterm/* 以外的全部，news 只負責 news/*，longterm 只負責 longterm/*），被拒就重來（最多 5 次），不會衝突，也不會蓋掉別人的更新。

## 檔案

| 路徑 | 內容 | 來源（主 / 核對） |
|---|---|---|
| `data/us/<代號>.csv` | 美股日線，已收盤的交易日，最近一年 | MoneyDJ / Yahoo；最後一根 K 棒兩家一致才採用，不一致或缺一家時用 Nasdaq 官方當裁判 |
| `data/tw/<代號>_norm.csv` | 台股日線，已收盤的交易日 | 上市：證交所 / 鉅亨網；00411A（上櫃）：櫃買中心 / MoneyDJ |
| `data/tw/quotes.json` | 抓取當下的盤中快照（備援用） | 鉅亨網 |
| `data/tw/intraday.json` | 證交所盤中快照：三檔 ETF 與加權指數（TAIEX）的現價 `price`、漲跌、開高低、昨收、累積成交張數、最後成交時間（`time`），ETF 另有最佳一檔買賣價 `bid1`／`ask1`。`price_field` 標價格來源：`z` 最後成交價、`z_retry` 那一刻沒成交（MIS 的 z 是「-」）隔幾秒重抓拿到的（`price_retries` 第幾次，最多 3 次、約 30 秒）、`pz` 前一筆成交價、`bid_ask_mid` 仍沒成交時用買賣中間價、`bid1`／`ask1` 只有單邊報價、`null` 都沒有（原因在 `price_note`）；change 用採用的價格計算。`limit_up`／`limit_down` 不合理就是 null（上櫃的 00411A 無漲跌幅限制，MIS 給 9999.95），原因在 `limit_note`。`session` 標盤前／盤中／收盤後／休市日，只有盤中 `is_trading=true`，其他時段 `note` 寫明價格是哪一天幾點的資料；每檔 `is_today` 標資料日期是不是今天 | 證交所 mis.twse.com.tw getStockInfo；`intraday.py`，由 cron-job.org 約台北 10:05 觸發 |
| `data/tw/nav_history.csv` | ETF 每日淨值與當日收盤折溢價：date,code,nav,close,premium_pct,source（逐日累積） | 證交所基本市況報導網站 mis.twse.com.tw |
| `data/tw/nav_raw.json` | 最新一次抓到的淨值原始資料，含盤中預估淨值（f）與預估折溢價（g） | 同上 |
| `data/fx/usdtwd_mid.csv` | 美元兌台幣收盤（市場中間價），約 2 年。台銀網站擋 GitHub 伺服器，即期賣出由排程任務自己抓，這份是備援 | Yahoo |
| `data/fx/usdtwd_bot_sell.csv` | 台銀美元即期賣出：date,rate，約 13 個月，16:30 前不收當天。格式同上，可直接當排程的 fx_csv（台銀網站擋 GitHub，改由 FinMind 取得，已與台銀網頁 6 個月表逐筆比對一致） | FinMind TaiwanExchangeRate / 與 usdtwd_mid 核對價差 |
| `data/tv/snapshot.json` | TradingView 日線技術面快照 | TradingView scanner |
| `data/radar.json`、`data/radar.md` | 技術面雷達判讀（結構、位置、訊號矛盾、關鍵價位）。指標由 repo 日線自己算，TradingView 只提供綜合評分，且只在數據一致時採用 | `radar.py`、`indicators.py` |
| `data/macro/<名稱>.csv` | 美股指數與總經日線，已收盤的交易日，約 2 年：SPX、IXIC、SOX、VIX、TNX（10 年期殖利率 %）、DXY、GOLD、WTI、TSM（代號對照見 `config.json` 的 `macro`）。DXY、GOLD、WTI 是 24 小時交易：不收週末列；交易日在美東 17:00 結束、18:00 晚盤開盤後 Yahoo 的當天 K 棒會變成晚盤未完成資料，所以只在美東 17:10～18:00 收當天，其餘時間只到前一天（meta 的 `cutoff`、`cutoff_rule`；晚盤重抓時沿用先前收到的乾淨 K 棒，記在 `last_kept_from_previous`） | Yahoo / 鉅亨網（SPX、IXIC、SOX、DXY）、MoneyDJ（TSM，最後一根多數決同 us/） |
| `data/sentiment/cnn_fear_greed.json` | CNN 恐懼貪婪目前分數與評等、前收／一週／一月／一年前，以及 7 個分項 | CNN dataviz |
| `data/sentiment/cnn_fear_greed.csv` | 恐懼貪婪每日收盤分數：date,score,rating（逐日累積） | 同上 |
| `data/sentiment/taifex_vix.csv` | 臺指選擇權波動率指數每日收盤：date,vix（逐日累積） | 期交所月檔 |
| `data/valuation/us.json` | 可買標的最新估值：個股 Forward PE、PEG，VOO 本益比；頁面股價與交易日、隱含預估 EPS 與一週前比較（變動超過 ±20% 標記）、Yahoo 下次財報日 | stockanalysis / 頁面股價與 repo 日線核對；財報日 Yahoo |
| `data/valuation/us_history.csv` | 估值每日紀錄：date,symbol,price,fwd_pe,peg,pe（逐日累積，空白＝n/a） | 同上 |
| `data/calendar/calendar.json` | 行事曆：台股開休市（entries 含「開始交易日」等其實有交易的日子，closed 是平日休市日）、FOMC 會議（今年與已公布的明年，sep＝有經濟預測）、可買個股下次財報日；CPI 目前標「未取得」 | 證交所 OpenAPI / rwd 版核對；Fed 官網；財報日同 valuation |
| `data/chips/tw_insti.csv` | 關注代號三大法人買賣超（股）：date,code,foreign,foreign_dealer,trust,dealer,total（foreign 不含外資自營商，dealer＝自行買賣＋避險；逐日累積） | 上市：證交所 T86；上櫃：櫃買中心 / 合計＝分項加總 |
| `data/chips/tw_market_insti.csv` | 集中市場三大法人買賣差額（元）：date,dealer_self,dealer_hedge,trust,foreign,foreign_dealer,total（逐日累積） | 證交所 BFI82U / 合計＝分項加總 |
| `data/chips/tw_margin.csv` | 集中市場信用交易：date,margin_lots,short_lots,margin_amount_k（融資餘額張、融券餘額張、融資金額仟元；逐日累積） | 證交所 MI_MARGN |
| `data/holdings/<代號>.json` | 主動式 ETF（00981A、00411A）最新持股：資料日、每單位淨值、全部持股與權重、期貨與現金、前十大，以及與前一資料日比較（前十大新進／退出、權重變動 ≥1 個百分點、新增／出清）。00411A 持有美股，資料日通常晚一天 | 統一投信 ezmoney / 淨值與 nav_history 核對、權重加總與股票市值核對 |
| `data/holdings/<代號>_history.csv` | 每日持股：date,code,name,shares,weight（逐日累積） | 同上 |
| `data/news/headlines.json` | 新聞標題（最近 72 小時），格式見下方「新聞標題格式」。約 0.7 MB，讀取時請用程式篩選 | Google News RSS（每個查詢跑兩次：限定白名單網站與不限定）、中央社官方 RSS（財經、科技）、federalreserve.gov RSS；`news.py`，查詢詞、來源等級與主題詞在 `config.json` 的 `news` |
| `data/news/fed.json` | Fed 新聞稿與演講（最近 7 天）：`items[]` 的 `date`（台北日期）、`title`、`link`、`type`（press／speech）、`published_taipei`、`published_utc` | federalreserve.gov RSS |
| `data/news/headlines.md` | 精簡版給人看：每個查詢列標題提到它的 T1～T2 新聞（最多 15 則）與 T3～T4 3 則 | 同上 |
| `data/longterm/tw_business_cycle.csv` | 國發會景氣對策信號（月，1984-01 起）：month,score,light,release_est,release_actual,score_first，說明見下方「長週期資料」 | data.gov.tw 資料集 6099（國發會 zip）/ 國發會景氣指標查詢系統 JSON |
| `data/longterm/buffett_indicator.csv` | 巴菲特指標（季，1951Q4 起）：quarter,equities_bn,gdp_bn,ratio_pct,trend_pct,dev_sigma,dev_sigma_expanding,release,release_basis，說明見下方「長週期資料」 | FRED NCBEILQ027S ÷ GDP；發布日取 Fed Z.1 Release Dates |
| `data/meta.json` | 每個資料集的狀態、列數、最後日期、雙來源核對結果、錯誤 | — |

CSV 欄位：`date,volume,open,high,low,close`（無表頭），與排程任務的 `monitor.py`、`monitor_us.py` 相同。
`macro/`、`sentiment/`、`fx/usdtwd_bot_sell.csv`、`valuation/`、`calendar/`、`chips/`、`holdings/` 等 Phase 1 新資料由 `macro.py` 等模組產生，只在完整抓取時更新（只更新淨值的那幾次不動）；某一項失敗時保留舊檔、meta 標 stale，不影響日線。

`meta.json` 的 `status`：`ok` 本次成功；`stale` 本次失敗，檔案是上一次的；`fail` 從未成功。
`cross_check_ok=false` 代表主來源與核對來源的收盤差超過 0.5%。美股（us/、macro/TSM）另有 `last_bar_source`（最後一根 K 棒採用的來源）、`last_bar_referee`（是否動用 Nasdaq 官方裁判）、`last_bar_votes`（各來源的值）、`history_mismatch`（近一年兩家收盤差超過 0.5% 的根數）；只有 MoneyDJ 一票、Yahoo 與 Nasdaq 都還沒有那一天時，先收 MoneyDJ 的值並標 `last_bar_single_source=true`、`cross_check_ok=null`（尚未核對，不是核對失敗），日期記在 `single_source_days`；之後的抓取等 Yahoo 有了再核對（一致就清除標記，不一致照常請 Nasdaq 裁判、必要時改正，結果記在 `rechecked`；超過 10 天仍無法核對就停止追蹤）。有待核對的日子時不算「日線已完整」，後面的排程會再做一次完整抓取。裁判也無法判定時，那一根先不收，`last_bar_unresolved` 記日期、`cross_check_ok=false`。

鉅亨網的美股最後一根 K 棒常停在盤中價（2026-10-16 檢查：ARKX、TSM 到台北中午仍未更新；過去一年 VOO、IAUM、ARK 系列、TSM 各有 5～9 根收盤錯 0.5% 以上，成交量也偏少），所以美股與 00411A 不再以鉅亨網為主。

### 新聞標題格式（data/news/headlines.json）

依 stock-dashboard `docs/NEWS_ROUTINE.md` 第 3.1 節的約定：

- 頂層：`generated_at_taipei`、`window_hours`（72）、`feeds[]`（`name`、`ok`、`count`，失敗時多一個 `error`）、`errors[]`、`items[]`。
- `items[]`：`id`（Google News 連結 SHA-1 的前 16 碼）、`published_utc`、`published_taipei`、`source`、`tier`、`lang`、`title`、`link`（Google News 轉址連結）、`symbols`、`topics`。
- `url`：原文網址（出版商網址）。Tier 1～2、最近 36 小時（`config.json` 的 `news.url_hours`）的項目，把 Google News 轉址解開取得：先讀文章頁拿簽章，再呼叫 Google 的 batchexecute（一次 10 則）；解過的依 `id` 沿用到下一次；每次最多 600 則、240 秒，整批都解不開（多半是被限速）就停、剩下的留到下次。解不開或不在範圍內是 `null`（不會放 Google 網址）；Fed 的 `link` 本來就是原文，`url` 同 `link`。`link` 照舊保留。解碼狀況在 `stats.url`（`target` 範圍內則數、`ok` 有網址、`cached` 沿用、`new` 本次解開、`failed`、`left` 留到下次、`stopped`、`notes`），meta 的 `news/headlines` 有 `url_ok`／`url_total`。解不開不算進 `errors`，不影響 `status`。
- **中央社官方 RSS**（2026-10-27 加入）：Google News 收中央社收得很少（72 小時內只有 2 則），所以另外讀中央社的分類 RSS （`config.json` 的 `news.cna_feeds`，目前是財經 `https://feeds.feedburner.com/rsscna/finance` 與科技 `…/rsscna/technology`；國際 `intworld`、兩岸 `mainland` 也可用，列在 `cna_feeds_available` 先不開）。每個 feed 固定約 20 則、`pubDate` 帶 +0800 時區，連結就是 cna.com.tw 原文，所以 `url` 直接等於 `link`。`source` 是「中央社」、tier 2 rank A、`queries` 是 `cna_finance`／`cna_technology`；symbols 照標題規則對應（台股三檔 ETF、台積電與兩檔主動 ETF 前十大的台股名稱）。和 Google News 收到的同一篇（來源「中央社 CNA」）合併時以官方 RSS 為準（Google 那則的來源列在 `also`）。分類新聞不受「標題無關每個查詢最多 10 則」的上限影響。`feeds[]` 多 `中央社 RSS：finance` 等項，meta 多 `news/cna`（`status` ok／stale（這次失敗、沿用上一次的標題）／fail、`last` 最新一則的時間、`items`、`feeds_ok`）。依中央社 RSS 使用規範（同意個人、非營利組織之非商業用途；需標示出處「中央通訊社」；RSS 只提供標題、前言與連結，不得擅自引用全文；中央社保留隨時要求停止的權利），這裡只存標題與連結，頂層 `attribution` 標示出處。
- **發布時間可疑**：Google News 重新索引時，會把同一來源的一批舊文標成同一個發布時間（2026-10-26：經濟日報 52 則都是 10/24 17:00，實際是 10/13～10/22）。同一次抓取中（加上上一次輸出留下、還沒改正過的項目，因為同一批舊文下一次可能被標成另一個時間），同一來源有 5 則以上（`news.time_suspect_min`）不同標題的發布時間完全相同，就當成可疑（tier 1 官方不檢查）：沒有原文網址的先解開，再讀原文頁找發布時間（依序 JSON-LD `datePublished`、`article:published_time`、`itemprop=datePublished`、`meta name=date`，最後是網址裡的日期）。找得回來就改正 `published_utc`／`published_taipei`，原本的時間記在 `time_fixed_from`（`published_utc`、`published_taipei`、`by`＝用哪個欄位找回的；其他項目是 `null`），改正後超出 72 小時窗的直接拿掉；找不回來的移出 `items`、放到頂層 `suspect_items`（多 `time_suspect: true`、`suspect_reason`），所以讀 `items` 的程式不會讀到。每次最多 200 則、120 秒（`time_check_max`、`time_check_seconds`），結果依 `id` 存在頂層 `time_check_cache`（保留 14 天，失敗的最多重試 3 次）。統計在 `stats.time_check`（`groups` 哪幾批、`detected`、`fixed`、`changed` 時間真的被改、`fixed_out_of_window`、`removed`、`cached`），meta 的 `news/headlines` 有 `time_suspect_detected`、`time_fixed`、`time_removed`。報紙版只有日期的文章（例如 Taipei Times、聯合的民意論壇標 00:00）也會被偵測，但原文頁確認日期沒錯，時間不變、只多 `time_fixed_from`。
- `tier`：1 官方（Fed、政府與監管機關、交易所、公司 IR 與新聞稿、PR Newswire 等新聞稿通路）、2 主要媒體、3 第一手發言（公司部落格等）、4 其他。依 RSS 的來源網址對照 `config.json` 的 `news.tiers` 判斷。
- `symbols`：只看標題——提到查詢的關鍵字（Nvidia → NVDA、台積電 → 2330 與 TSM 等），或兩檔主動 ETF 前十大的台股名稱（台光電 → 2383）。沒有 symbols 的也保留。
- `topics`：依 `news.topic_rules` 的標題關鍵字（中英文），目前有 fed、tariff、export_controls、china、taiwan、oil、war、cpi、jobs、earnings、antitrust、merger。
- 同標題或字詞重疊 ≥ 80% 的合併成一則，保留等級最高、最早的那則，其他來源列在 `also`。

和約定不同或多出來的地方：

- `window_hours` 是 72（約定範例寫 36）：第二層自己依時間窗篩選。Fed 的 tier 1 項目一律保留 7 天，不受 72 小時限制。
- 多出的欄位：頂層 `generated_at_utc`、`status`（ok／partial）、`tiers`（等級說明）、`stats`（依等級、標的、主題的則數）、`sec_8k`（目前「未啟用」）；`items[]` 的 `source_url`、`rank`（tier 2 再分 A 通訊社與一線財經媒體、B 其他主要財經媒體）、`queries`（找到它的查詢）、`title_queries`（標題提到的查詢）、`title_match`（標題有沒有提到查詢關鍵字或成分股；false 是 Google 依內文找到的，每個查詢最多留 10 則）、`also`。
- tier 3（第一手發言）只能從來源網址判斷（目前是 NVIDIA Blog、NVIDIA Developer 這類），川普 Truth Social、X 不在 RSS 裡，仍要第二層自己查。
- `meta.json` 的 `news/headlines`：`status`、`last`（＝`generated_at_taipei`）、`items`、`by_tier`、`feeds_ok`、`errors`、`url_ok`、`url_total`；`news/fed`：`status`、`last`（最新一則的日期）、`checked_at_taipei`、`items`。meta 只由 news.yml 更新這兩項；fetch.yml 推送時保留它們（`publish.py`）。

### 長週期資料（data/longterm/，回測用）

`longterm.py` 產生，只由 `longterm.yml` 手動觸發。兩份都是無表頭 CSV。**數值都是最新修正值，不是當時公布的初值**，回測會有些微前視偏誤；交易訊號請從「發布日的下一個交易日」起才使用。meta：`longterm/tw_business_cycle`、`longterm/buffett_indicator`（`status`、`rows`、`first`、`last`、最新一期數值、`last_release`、`checked_at_taipei` 等）。

**國發會景氣對策信號** `tw_business_cycle.csv`

| 欄 | 說明 |
|---|---|
| month | 資料月份 YYYY-MM（1984-01 起；1982～1983 有指標但沒有燈號分數） |
| score | 綜合判斷分數 9～45（最新修正值） |
| light | 燈號：紅 45～38、黃紅 37～32、綠 31～23、黃藍 22～17、藍 16～9（由分數換算；與來源的燈號文字、代碼逐月核對，不一致記在 meta 的 `light_mismatch`） |
| release_est | 估計發布日：次月 30 日（2 月用月底），遇週末順延，國定假日沒考慮。2025-10～2026-09 實際發布都在次月 27～30 日（例：2026-02 資料 3/30、2026-04 資料 5/28、2026-07 資料 8/27、2026-08 資料 9/29），所以這是偏保守、幾乎不會早於實際的估計。歷史實際發布日在資料裡查不到 |
| release_actual | 實際發布日（只有這支程式記錄到的月份；2026-08 起）：取查詢系統 JSON 的 `latest_date`（JSON 失敗時用 zip 內檔案日期） |
| score_first | 初值（同上，只有記錄到的月份）：某個月第一次以「最新一期」出現時的分數；之後被回溯修正也保留初值。要每月跑一次才不會漏記 |

- 來源：主要用 data.gov.tw「景氣指標及燈號」（資料集 6099，`https://data.gov.tw/api/v2/rest/dataset/6099` 查下載網址，目前是 ws.ndc.gov.tw 的 zip，約 70 KB，UTF-8 CSV；zip 內檔名是亂碼，用表頭「景氣對策信號綜合分數」找檔）。另外讀國發會景氣指標查詢系統 `https://index.ndc.gov.tw/n/json/data/eco/indicators`（先 GET `/n/zh_tw/data/eco` 拿 cookie 與 csrf-token，再帶 `X-CSRF-TOKEN` POST；SR0005＝分數、SR0030＝燈號代碼 1 藍～5 紅），逐月核對分數，並補 zip 還沒更新的月份（`added_from_api`）。2026-10-28 探測：兩者都到 2026-08（9/29 發布），重疊 512 個月分數完全一致。但查詢系統對 GitHub 機房回 HTTP 403（本機可連，不嘗試繞過），所以在 Actions 上只用 zip（meta 的 `cross_check` 是「只有一個來源」、`warnings` 記 403），`release_actual` 用 zip 內檔案日期（2026-08 那期是 2026-09-29，與實際發布日相同）。
- 更新：月資料，國發會約次月 27～30 日下午發布；每次發布都**回溯修正**歷史資料。新聞報導的初值與現值對照：2025-09 初值 35 → 現值 34、2026-02 初值 40 → 現值 41、2026-04 初值 39 → 現值 40，常見 ±1 分，有時跨過燈號門檻。
- 使用限制：政府資料開放授權條款第 1 版（可免費利用、可商用，需標示出處「國家發展委員會」）。

**巴菲特指標** `buffett_indicator.csv`

| 欄 | 說明 |
|---|---|
| quarter | 季別 YYYYQn（1951Q4 起；更早的股權市值只有年資料，不收） |
| equities_bn | 美國非金融企業股權市值（十億美元，季末；FRED `NCBEILQ027S` 百萬美元 ÷ 1000，來源 Fed Z.1 L.103） |
| gdp_bn | 名目 GDP（十億美元，季調年率；FRED `GDP`，來源 BEA） |
| ratio_pct | equities_bn ÷ gdp_bn × 100 |
| trend_pct | ln(ratio) 對時間的全期間對數線性趨勢值（用到未來資料，只適合看歷史位置） |
| dev_sigma | (ln(ratio) − 趨勢) ÷ 全期間殘差標準差 |
| dev_sigma_expanding | 只用到該季為止的資料估趨勢與標準差（回測用；前 40 季留空） |
| release | 這一季資料的發布日：1996Q2 起是 Fed Z.1 實際首次發布日，更早的用季末＋90 天估計 |
| release_basis | `z1`（實際）或 `est`（估計） |

- 來源：FRED `https://fred.stlouisfed.org/graph/fredgraph.csv?id=NCBEILQ027S` 與 `?id=GDP`（免金鑰的 CSV 下載；FRED 會擋瀏覽器 User-Agent，程式用 `market-data/1.0` 的 UA）。發布日：`https://www.federalreserve.gov/releases/z1/release-dates.htm`（每一列有「2026:Q2」與該次發布資料夾日期，目前這期的連結是 current，用頁尾 Last Update）。Wilshire 5000 已從 FRED 下架，不用。
- 更新與落後：季資料，受 Z.1 限制，約季末後 10 週發布（1996Q2 起 121 季：64～107 天，中位數 71 天；2025Q3 延到 2026-01-09 才發布）。GDP 初估約季末後 30 天就有，所以有時 GDP 會比股權市值多一季（meta 的 `gdp_last`、`equity_last`），比率只算兩者都有的季。2026-10-28 探測：最新 2026Q2（2026-09-11 Z.1），下一次約 2026 年 12 月。
- 修正：Z.1 與 GDP 每次發布都會修正過去數季，這裡是最新值；要當時的值得用 ALFRED（需要 API 金鑰，未做）。
- 使用限制：兩個序列在 FRED 標示為 public domain（citation requested）：需註明「Board of Governors of the Federal Reserve System (US)／U.S. Bureau of Economic Analysis, retrieved from FRED, Federal Reserve Bank of St. Louis」，不得暗示 FRED 或 Fed 背書。

## 測試

```bash
python -m unittest discover -s tests
```

用假資料測多數決、單一來源補核對、24 小時品項的收盤截止與週末列、略過判斷、新聞整理、盤中快照、長週期資料的解析與計算，不連網；`test_publish.py` 用暫存的 git repo 模擬三個 workflow 同時推送。

## 讀取方式

```bash
curl -sSf https://raw.githubusercontent.com/<帳號>/market-data/main/data/meta.json
```

## 注意

TradingView scanner 不是官方公開 API，改版時可能失效；失效時雷達只少了綜合評分。TradingView 偶爾有錯誤的 K 棒（00411A 的高低點曾出錯，ADX、ATR、Pivot 全部失真），所以雷達的指標一律用 repo 的官方日線計算。
技術面雷達只描述結構，不是投資建議，也不改變排程任務的買區判定。

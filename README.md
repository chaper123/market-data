# market-data

每天由 GitHub Actions 抓取行情，給 Claude 的排程任務（台股 ETF 晨報、美股晨報）直接讀取。
排程任務的雲端環境連不到證交所、鉅亨網、台銀與 TradingView，但連得到 GitHub，所以由這裡代抓。

只放公開行情數據，不放帳本、持股或成本。

## 排程

台北時間週一～五 05:13、06:37、07:53、08:41、09:47 各排一次。日線完整（美股、台股最後日期都到台北今天的前一個平日）之後，其餘幾次只更新 ETF 淨值（投信收盤後才公布，晨報前要再抓一次）。

GitHub 的排程常延遲，偶爾整個漏跑，所以排多次。需要立刻更新時，到 Actions → fetch-market-data → Run workflow。

## 檔案

| 路徑 | 內容 | 來源（主 / 核對） |
|---|---|---|
| `data/us/<代號>.csv` | 美股日線，已收盤的交易日 | 鉅亨網 / Yahoo |
| `data/tw/<代號>_norm.csv` | 台股日線，已收盤的交易日 | 上市：證交所 / 鉅亨網；00411A：鉅亨網 / Yahoo |
| `data/tw/quotes.json` | 抓取當下的盤中快照（備援用） | 鉅亨網 |
| `data/tw/nav_history.csv` | ETF 每日淨值與當日收盤折溢價：date,code,nav,close,premium_pct,source（逐日累積） | 證交所基本市況報導網站 mis.twse.com.tw |
| `data/tw/nav_raw.json` | 最新一次抓到的淨值原始資料，含盤中預估淨值（f）與預估折溢價（g） | 同上 |
| `data/fx/usdtwd_mid.csv` | 美元兌台幣收盤（市場中間價），約 2 年。台銀網站擋 GitHub 伺服器，即期賣出由排程任務自己抓，這份是備援 | Yahoo |
| `data/fx/usdtwd_bot_sell.csv` | 台銀美元即期賣出：date,rate，約 13 個月，16:30 前不收當天。格式同上，可直接當排程的 fx_csv（台銀網站擋 GitHub，改由 FinMind 取得，已與台銀網頁 6 個月表逐筆比對一致） | FinMind TaiwanExchangeRate / 與 usdtwd_mid 核對價差 |
| `data/tv/snapshot.json` | TradingView 日線技術面快照 | TradingView scanner |
| `data/radar.json`、`data/radar.md` | 技術面雷達判讀（結構、位置、訊號矛盾、關鍵價位）。指標由 repo 日線自己算，TradingView 只提供綜合評分，且只在數據一致時採用 | `radar.py`、`indicators.py` |
| `data/macro/<名稱>.csv` | 美股指數與總經日線，已收盤的交易日，約 2 年：SPX、IXIC、SOX、VIX、TNX（10 年期殖利率 %）、DXY、GOLD、WTI、TSM（代號對照見 `config.json` 的 `macro`） | Yahoo / 鉅亨網（SPX、IXIC、SOX、DXY、TSM 有核對） |
| `data/sentiment/cnn_fear_greed.json` | CNN 恐懼貪婪目前分數與評等、前收／一週／一月／一年前，以及 7 個分項 | CNN dataviz |
| `data/sentiment/cnn_fear_greed.csv` | 恐懼貪婪每日收盤分數：date,score,rating（逐日累積） | 同上 |
| `data/sentiment/taifex_vix.csv` | 臺指選擇權波動率指數每日收盤：date,vix（逐日累積） | 期交所月檔 |
| `data/valuation/us.json` | 可買標的最新估值：個股 Forward PE、PEG，VOO 本益比；頁面股價與交易日、隱含預估 EPS 與一週前比較（變動超過 ±20% 標記）、Yahoo 下次財報日 | stockanalysis / 頁面股價與 repo 日線核對；財報日 Yahoo |
| `data/valuation/us_history.csv` | 估值每日紀錄：date,symbol,price,fwd_pe,peg,pe（逐日累積，空白＝n/a） | 同上 |
| `data/calendar/calendar.json` | 行事曆：台股開休市（entries 含「開始交易日」等其實有交易的日子，closed 是平日休市日）、FOMC 會議（今年與已公布的明年，sep＝有經濟預測）、可買個股下次財報日；CPI 目前標「未取得」 | 證交所 OpenAPI / rwd 版核對；Fed 官網；財報日同 valuation |
| `data/meta.json` | 每個資料集的狀態、列數、最後日期、雙來源核對結果、錯誤 | — |

CSV 欄位：`date,volume,open,high,low,close`（無表頭），與排程任務的 `monitor.py`、`monitor_us.py` 相同。
`macro/`、`sentiment/` 等 Phase 1 新資料由 `macro.py` 等模組產生，只在完整抓取時更新（只更新淨值的那幾次不動）；某一項失敗時保留舊檔、meta 標 stale，不影響日線。

`meta.json` 的 `status`：`ok` 本次成功；`stale` 本次失敗，檔案是上一次的；`fail` 從未成功。
`cross_check_ok=false` 代表主來源與核對來源的收盤差超過 0.5%。

## 讀取方式

```bash
curl -sSf https://raw.githubusercontent.com/<帳號>/market-data/main/data/meta.json
```

## 注意

TradingView scanner 不是官方公開 API，改版時可能失效；失效時雷達只少了綜合評分。TradingView 偶爾有錯誤的 K 棒（00411A 的高低點曾出錯，ADX、ATR、Pivot 全部失真），所以雷達的指標一律用 repo 的官方日線計算。
技術面雷達只描述結構，不是投資建議，也不改變排程任務的買區判定。

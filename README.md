# market-data

每天由 GitHub Actions 抓取行情，給 Claude 的排程任務（台股 ETF 晨報、美股晨報）直接讀取。
排程任務的雲端環境連不到證交所、鉅亨網、台銀與 TradingView，但連得到 GitHub，所以由這裡代抓。

只放公開行情數據，不放帳本、持股或成本。

## 排程

台北時間週一～五 05:13、06:37、07:53、08:41、09:47 各排一次。當天第一次全部成功後，其餘幾次直接略過。

GitHub 的排程常延遲，偶爾整個漏跑，所以排多次。需要立刻更新時，到 Actions → fetch-market-data → Run workflow。

## 檔案

| 路徑 | 內容 | 來源（主 / 核對） |
|---|---|---|
| `data/us/<代號>.csv` | 美股日線，已收盤的交易日 | 鉅亨網 / Yahoo |
| `data/tw/<代號>_norm.csv` | 台股日線，已收盤的交易日 | 上市：證交所 / 鉅亨網；00411A：鉅亨網 / Yahoo |
| `data/tw/quotes.json` | 抓取當下的盤中快照（備援用） | 鉅亨網 |
| `data/fx/usdtwd_mid.csv` | 美元兌台幣收盤（市場中間價），約 2 年。台銀網站擋 GitHub 伺服器，即期賣出由排程任務自己抓，這份是備援 | Yahoo |
| `data/tv/snapshot.json` | TradingView 日線技術面快照 | TradingView scanner |
| `data/radar.json`、`data/radar.md` | 技術面雷達判讀（結構、位置、訊號矛盾、關鍵價位） | 由 `radar.py` 計算 |
| `data/meta.json` | 每個資料集的狀態、列數、最後日期、雙來源核對結果、錯誤 | — |

CSV 欄位：`date,volume,open,high,low,close`（無表頭），與排程任務的 `monitor.py`、`monitor_us.py` 相同。

`meta.json` 的 `status`：`ok` 本次成功；`stale` 本次失敗，檔案是上一次的；`fail` 從未成功。
`cross_check_ok=false` 代表主來源與核對來源的收盤差超過 0.5%。

## 讀取方式

```bash
curl -sSf https://raw.githubusercontent.com/<帳號>/market-data/main/data/meta.json
```

## 注意

TradingView scanner 不是官方公開 API，改版時可能失效；失效時只有雷達段落受影響，日線與匯率不受影響。
技術面雷達只描述結構，不是投資建議，也不改變排程任務的買區判定。

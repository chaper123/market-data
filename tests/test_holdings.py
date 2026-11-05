"""holdings.py 的解析與輸出測試，不連網（凱基投信頁面用簡化的假 HTML，數字是假的）。

執行：python -m unittest discover -s tests（在 repo 根目錄）
"""
import csv
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fetch  # noqa: E402
import holdings  # noqa: E402


def kgi_page(day="2026/11/05", rows=(("2330", "台積電", "1,000,000", "40.00"), ("2454  ", "聯發科", "200,000", "5.00"))):
    """模仿凱基投信基金頁的結構：持股比重區塊（電腦版、手機版兩份表格）與定審頻率表。中文用 HTML 實體，和真的頁面一樣。"""
    ent = lambda s: "".join(f"&#x{ord(c):X};" if ord(c) > 127 else c for c in s)  # noqa: E731
    cell = '<span class="fund-asset__list-cell">{}</span>'
    li = lambda k, v: f'<li class="fund-asset__list-item">\n {cell.format(ent(k))}\n {cell.format(v)}\n</li>'  # noqa: E731
    body = "".join(f'<tr name="content"><td style="x">{c}</td><td>{ent(n)}</td><td>{s}</td><td>{w}</td></tr>'
                   for c, n, s, w in rows)
    stock = (f'<table class="js-table-a-0"><thead><tr><th>{ent("股票代號")}</th><th>{ent("股票名稱")}</th>'
             f'<th>{ent("股數")}</th><th>{ent("權重(%)")}</th></tr></thead><tbody>{body}</tbody></table>')
    fut = (f'<table class="js-table-a-1"><thead><tr><th>{ent("期貨代號")}</th><th>x</th><th>x</th><th>x</th><th>x</th></tr>'
           f'</thead><tbody><tr name="content"><td>TX </td><td>{ent("臺股期貨")}</td><td>10</td><td>3.00</td>'
           '<td>2026/11</td></tr></tbody></table>')
    review = (f'<table class="js-table-c-1"><thead><tr><th>{ent("定審生效日")}</th></tr></thead><tbody>'
              '<tr name="content"><td>2026/08/19</td><td>50</td><td>3</td><td>3</td><td>50</td></tr></tbody></table>')
    return (f'<div id="FundDetail-06"><div class="fund-asset__title index-title">{ent("持股比重")}</div>\n'
            f'<p class="fund-asset__date">({day})</p><ul>'
            + li("基金淨值資產價值", "TWD$1,000,000,000") + li("基金在外流通單位數", "100,000,000")
            + li("基金每單位淨值", "10.00") + li("股票", "TWD$450,000,000") + li("現金", "TWD$20,000,000")
            + f"</ul>{stock}{fut}{stock}{fut}</div>"
            f'<div id="FundDetail-02">{review}</div>').encode("utf-8")


class ParseKgiTest(unittest.TestCase):
    def test_parse(self):
        p = holdings.parse_kgifund(kgi_page())
        self.assertEqual(p["date"], "2026-11-05")
        self.assertEqual((p["nav"], p["units"], p["net_assets"]), (10.0, 100000000.0, 1000000000.0))
        self.assertEqual(len(p["holdings"]), 2)  # 手機版的重複表格不算
        self.assertEqual(p["holdings"][0], {"code": "2330", "name": "台積電", "shares": 1000000.0, "amount": None,
                                            "weight": 40.0, "currency": "NTD"})
        self.assertEqual(p["holdings"][1]["code"], "2454")  # 去掉代號後的空白
        self.assertEqual(p["futures"][0]["contracts"], 10.0)
        self.assertEqual(p["other_assets"], {"股票": 450000000.0, "現金": 20000000.0})
        self.assertEqual(p["reviews"], [{"effective": "2026-08-19", "before": 50.0, "added": 3.0, "removed": 3.0,
                                         "after": 50.0}])
        self.assertEqual(holdings.cross_check(p)[1], True)

    def test_missing_section_raises(self):
        with self.assertRaises(RuntimeError):
            holdings.parse_kgifund(b"<html>maintenance</html>")

    def test_cross_check_fails_on_mismatch(self):
        p = holdings.parse_kgifund(kgi_page(rows=(("2330", "台積電", "1,000,000", "60.00"),)))
        checks, ok = holdings.cross_check(p)
        self.assertFalse(ok)
        self.assertIn("權重加總 60.00%", checks[1])


class FetchTest(unittest.TestCase):
    def test_writes_out_dir_and_replaces_same_day(self):
        cfg = {"holdings": {"009816": {"source": "kgifund", "fund": "J023"}}}
        with tempfile.TemporaryDirectory() as tmp:
            pages = [kgi_page("2026/11/04"), kgi_page("2026/11/05"), kgi_page("2026/11/05")]
            with mock.patch.object(holdings, "http", side_effect=[(200, x) for x in pages]), \
                    mock.patch.object(holdings.time, "sleep"):
                for _ in pages:
                    meta = {"datasets": {}, "errors": []}
                    holdings.fetch_holdings(cfg, meta, tmp)
            self.assertEqual(meta["errors"], [])
            ds = meta["datasets"]["holdings/009816"]
            self.assertEqual((ds["status"], ds["source"], ds["last"], ds["compare_date"]),
                             ("ok", "凱基投信", "2026-11-05", "2026-11-04"))
            with open(os.path.join(tmp, "holdings", "009816_history.csv"), encoding="utf-8") as f:
                rows = list(csv.reader(f))
            self.assertEqual([r[0] for r in rows], ["2026-11-04"] * 2 + ["2026-11-05"] * 2)  # 同一天重抓不重複
            with open(os.path.join(tmp, "holdings", "009816.json"), encoding="utf-8") as f:
                snap = json.load(f)
            self.assertEqual((snap["source_name"], snap["top10"][0]["code"], len(snap["reviews"])), ("凱基投信", "2330", 1))

    def test_old_config_string_means_ezmoney(self):
        self.assertEqual(holdings.fund_spec("49YTW"), ("ezmoney", "49YTW"))
        self.assertEqual(holdings.fund_spec({"source": "kgifund", "fund": "J023"}), ("kgifund", "J023"))

    def test_not_in_public_fetch(self):
        self.assertNotIn("fetch_holdings", [f.__name__ for f in fetch.PHASE1_STEPS])
        self.assertIn("holdings/00981A", fetch.DISABLED)


if __name__ == "__main__":
    unittest.main()

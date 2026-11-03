"""停用的資料集（fetch.DISABLED）在 meta 的標示測試，不連網。

執行：python -m unittest discover -s tests（在 repo 根目錄）
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common  # noqa: E402
import fetch  # noqa: E402


class DisabledTest(unittest.TestCase):
    def test_marks_disabled_and_keeps_last(self):
        prev = {"intraday/tw": {"status": "ok", "last": "2026-11-03 10:05:17", "source": "mis.twse.com.tw getStockInfo"},
                "tw/nav": {"status": "ok", "latest": {"009816": "2026-10-31"}, "source": "mis.twse.com.tw all_etf"}}
        meta = {"datasets": {}, "errors": []}
        with mock.patch.object(common, "_PREV_META", prev):
            fetch.mark_disabled(meta)
        ds = meta["datasets"]
        self.assertEqual(set(ds), set(fetch.DISABLED))
        self.assertTrue(all(v["status"] == "disabled" and "停用" in v["reason"] for v in ds.values()))
        self.assertEqual(ds["intraday/tw"]["last"], "2026-11-03 10:05:17")
        self.assertEqual(ds["tw/nav"]["latest"], {"009816": "2026-10-31"})

    def test_nav_not_in_full_fetch(self):
        src = open(fetch.__file__, encoding="utf-8").read()
        self.assertNotIn("fetch_nav, fetch_tv", src)  # 完整抓取不再呼叫淨值
        self.assertNotIn("fetch_nav(json.load", src)  # 略過時也不再更新淨值


if __name__ == "__main__":
    unittest.main()

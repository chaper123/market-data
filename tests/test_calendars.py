"""calendars.tw_market（證交所 OpenAPI 開休市日）測試，全部用假資料、不連網。

執行：python -m unittest discover -s tests（在 repo 根目錄）
"""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import calendars  # noqa: E402

API = [{"Date": "1151225", "Name": "行憲紀念日", "Description": "依規定放假1日"},
       {"Date": "1150102", "Name": "開始交易日", "Description": "國曆新年開始交易"}]
NEXT = {"date": "2027-01-01", "name": "中華民國開國紀念日", "description": "", "trading": False}


class CalendarTest(unittest.TestCase):
    def test_keeps_next_year_from_previous_file(self):
        with mock.patch.object(calendars, "get_json", lambda url, **kw: API):
            cal, m = calendars.tw_market(dt.date(2026, 12, 20), {"entries": [NEXT]})
        self.assertEqual([e["date"] for e in cal["entries"]], ["2026-01-02", "2026-12-25", "2027-01-01"])
        self.assertEqual(cal["closed"], ["2026-12-25", "2027-01-01"])  # 開始交易日不算休市
        self.assertEqual(m["next_year"], "2027 年沿用上一次的 1 筆")

    def test_openapi_next_year_wins_and_wrong_year_fails(self):
        api = API + [{"Date": "1160101", "Name": "開國紀念日", "Description": ""}]
        with mock.patch.object(calendars, "get_json", lambda url, **kw: api):
            cal, m = calendars.tw_market(dt.date(2026, 12, 20), {"entries": [NEXT]})
        self.assertEqual([e["name"] for e in cal["entries"] if e["date"] == "2027-01-01"], ["開國紀念日"])
        self.assertNotIn("next_year", m)
        with mock.patch.object(calendars, "get_json", lambda url, **kw: API), self.assertRaises(RuntimeError):
            calendars.tw_market(dt.date(2027, 1, 2), None)


if __name__ == "__main__":
    unittest.main()

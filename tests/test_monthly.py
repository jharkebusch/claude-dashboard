"""Workload by month, and the usual-day line on the daily chart."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

# serve.py opens its database at import time, so it has to be pointed somewhere
# disposable before it is imported — never at the real ~/.claude.
_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("DATA_DIR", _TMP.name)
os.environ.setdefault("CLAUDE_DIR", _TMP.name)

import ingest
import serve


def local(days_ago, hour):
    """A local wall-clock instant, built naive and then localised so a date on
    the far side of a daylight-saving change still lands on the hour asked for."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return (today - timedelta(days=days_ago)).replace(hour=hour).astimezone()


class MonthlySeriesTest(unittest.TestCase):
    def setUp(self):
        serve.DB = ingest.connect(":memory:")

    def tearDown(self):
        serve.DB.close()

    def activity(self, date, messages=100, sessions=5, tool_calls=20):
        serve.DB.execute(
            "INSERT INTO daily_activity VALUES (?,?,?,?)", (date, messages, sessions, tool_calls)
        )

    def series(self, first="2026-02-01", through="2026-04-30"):
        return serve.monthly_series({"first_session_date": first, "last_computed": through})

    def months(self, result):
        return {m["month"]: m for m in result["months"]}

    def test_days_are_summed_into_their_month(self):
        self.activity("2026-02-03", messages=100, sessions=2, tool_calls=10)
        self.activity("2026-02-19", messages=250, sessions=3, tool_calls=40)
        months = self.months(self.series())
        self.assertEqual(months["2026-02"]["messages"], 350)
        self.assertEqual(months["2026-02"]["sessions"], 5)
        self.assertEqual(months["2026-02"]["tool_calls"], 50)
        self.assertEqual(months["2026-02"]["active_days"], 2)

    def test_a_month_with_no_activity_is_still_a_bar(self):
        self.activity("2026-02-03")
        self.activity("2026-04-07")
        result = self.series()
        self.assertEqual([m["month"] for m in result["months"]], ["2026-02", "2026-03", "2026-04"])
        self.assertEqual(self.months(result)["2026-03"]["messages"], 0)
        self.assertEqual(self.months(result)["2026-03"]["active_days"], 0)

    def test_the_first_month_is_partial_when_history_starts_mid_month(self):
        self.activity("2026-02-10")
        self.activity("2026-03-04")
        months = self.months(self.series(first="2026-02-10", through="2026-03-31"))
        self.assertTrue(months["2026-02"]["partial"])
        self.assertFalse(months["2026-03"]["partial"])

    def test_the_first_month_is_whole_when_history_starts_on_the_first(self):
        self.activity("2026-02-01")
        self.activity("2026-03-04")
        months = self.months(self.series(first="2026-02-01", through="2026-03-31"))
        self.assertFalse(months["2026-02"]["partial"])

    def test_the_last_month_is_partial_when_the_cache_stopped_inside_it(self):
        self.activity("2026-02-05")
        self.activity("2026-03-04")
        months = self.months(self.series(first="2026-02-01", through="2026-03-15"))
        self.assertTrue(months["2026-03"]["partial"])
        self.assertFalse(months["2026-02"]["partial"])

    def test_the_last_month_is_whole_when_the_cache_reached_its_end(self):
        self.activity("2026-02-05")
        months = self.months(self.series(first="2026-02-01", through="2026-02-28"))
        self.assertFalse(months["2026-02"]["partial"])

    def test_no_history_yields_no_months(self):
        result = self.series()
        self.assertEqual(result["months"], [])
        self.assertEqual(result["through"], "2026-04-30")


class DailyUsualTest(unittest.TestCase):
    def setUp(self):
        serve.DB = ingest.connect(":memory:")
        self.rows = 0

    def tearDown(self):
        serve.DB.close()

    def add(self, when, tokens):
        self.rows += 1
        serve.DB.execute(
            "INSERT INTO requests (request_id, ts, ts_epoch, model, session_id, project, input)"
            " VALUES (?,?,?,?,?,?,?)",
            (str(self.rows), when.isoformat(), when.timestamp(), "claude-opus-5", "s", "alpha",
             tokens),
        )

    def usual(self):
        return serve.build_snapshot("30d", "all")["daily_usual"]

    def test_the_mean_is_taken_over_days_that_were_worked(self):
        self.add(local(1, 9), 1000)
        self.add(local(5, 9), 3000)
        self.assertEqual(self.usual(), {"tokens": 2000, "days": 2})

    def test_today_is_left_out_of_the_usual_day(self):
        self.add(local(1, 9), 1000)
        self.add(local(2, 9), 1000)
        self.add(local(0, 9), 500_000)
        self.assertEqual(self.usual(), {"tokens": 1000, "days": 2})

    def test_a_single_worked_day_is_reported_so_the_line_can_be_dropped(self):
        self.add(local(1, 9), 1000)
        self.assertEqual(self.usual()["days"], 1)

    def test_no_history_leaves_the_usual_day_empty(self):
        self.add(local(0, 9), 1000)
        self.assertEqual(self.usual(), {"tokens": 0, "days": 0})


if __name__ == "__main__":
    unittest.main()

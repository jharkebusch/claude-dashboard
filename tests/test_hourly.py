"""The by-hour series: today's bars, and the usual day they are measured against."""

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


MIDNIGHT = local(0, 0)


class HourlySeriesTest(unittest.TestCase):
    def setUp(self):
        serve.DB = ingest.connect(":memory:")
        self.rows = 0

    def tearDown(self):
        serve.DB.close()

    def add(self, when, tokens, project="alpha"):
        """One request of `tokens` input tokens. The other token columns stay at
        zero, so the sum the queries take is exactly what was asked for."""
        self.rows += 1
        serve.DB.execute(
            "INSERT INTO requests (request_id, ts, ts_epoch, model, session_id, project, input)"
            " VALUES (?,?,?,?,?,?,?)",
            (str(self.rows), when.isoformat(), when.timestamp(), "claude-opus-5", "s", project,
             tokens),
        )

    def series(self, days=7, project="all"):
        result = serve.hourly_series(project, days, MIDNIGHT)
        return result, {h["hour"]: h for h in result["hours"]}

    def test_mean_is_taken_over_active_days_not_calendar_days(self):
        self.add(local(1, 9), 1000)
        self.add(local(4, 9), 3000)
        result, hours = self.series(days=7)
        self.assertEqual(result["days"], 2)
        self.assertEqual(hours[9]["typical"], 2000)

    def test_today_is_left_out_of_the_baseline(self):
        self.add(local(1, 9), 1000)
        self.add(local(2, 9), 1000)
        self.add(local(0, 9), 500_000)
        result, hours = self.series(days=7)
        self.assertEqual(result["days"], 2)
        self.assertEqual(hours[9]["typical"], 1000)
        self.assertEqual(hours[9]["tokens"], 500_000)

    def test_silent_hours_are_reported_as_zero(self):
        self.add(local(1, 9), 1000)
        self.add(local(2, 9), 1000)
        result, hours = self.series(days=7)
        self.assertEqual(len(result["hours"]), 24)
        self.assertEqual(hours[3]["typical"], 0)
        self.assertEqual(hours[3]["tokens"], 0)

    def test_the_baseline_counts_whole_days_not_a_rolling_window(self):
        # A rolling 24-hour bound would cut yesterday off at whatever time of day
        # this happens to run; the last complete day counts in full.
        self.add(local(1, 2), 1000)
        self.add(local(1, 23), 4000)
        result, hours = self.series(days=1)
        self.assertEqual(result["days"], 1)
        self.assertEqual(hours[2]["typical"], 1000)
        self.assertEqual(hours[23]["typical"], 4000)

    def test_days_outside_the_range_do_not_count(self):
        self.add(local(30, 9), 1000)
        self.add(local(1, 9), 3000)
        result, hours = self.series(days=7)
        self.assertEqual(result["days"], 1)
        self.assertEqual(hours[9]["typical"], 3000)

    def test_the_all_range_reaches_back_without_a_bound(self):
        self.add(local(300, 9), 1000)
        self.add(local(1, 9), 3000)
        result, hours = self.series(days=None)
        self.assertEqual(result["days"], 2)
        self.assertEqual(hours[9]["typical"], 2000)

    def test_project_filter_scopes_the_baseline(self):
        for day in (1, 2):
            self.add(local(day, 9), 1000, project="alpha")
            self.add(local(day, 9), 9000, project="beta")
        _, hours = self.series(days=7, project="alpha")
        self.assertEqual(hours[9]["typical"], 1000)

    def test_request_counts_are_averaged_too(self):
        self.add(local(1, 9), 1000)
        self.add(local(1, 9), 1000)
        self.add(local(2, 9), 1000)
        _, hours = self.series(days=7)
        self.assertEqual(hours[9]["typical_requests"], 1.5)

    def test_no_history_leaves_the_baseline_empty(self):
        self.add(local(0, 9), 1000)
        result, hours = self.series(days=7)
        self.assertEqual(result["days"], 0)
        self.assertEqual(hours[9]["typical"], 0)
        self.assertEqual(hours[9]["tokens"], 1000)


if __name__ == "__main__":
    unittest.main()

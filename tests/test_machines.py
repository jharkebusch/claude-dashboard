"""Several machines feeding one database: ingest, migration, and the merge."""

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

# serve.py opens its database at import time, so it has to be pointed somewhere
# disposable before it is imported — never at the real ~/.claude.
_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("DATA_DIR", _TMP.name)
os.environ.setdefault("CLAUDE_DIR", _TMP.name)

import ingest
import serve


def local(days_ago, hour=12):
    """A local wall-clock instant, so the dates the queries compute in localtime
    are the dates the test meant."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return (today - timedelta(days=days_ago)).replace(hour=hour).astimezone()


def day(days_ago):
    return local(days_ago).date().isoformat()


def transcript(directory, session, request_id, tokens=100):
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "assistant",
        "requestId": request_id,
        "sessionId": session,
        "timestamp": local(1).isoformat(),
        "cwd": "/home/justin/Projects/alpha",
        "message": {"model": "claude-opus-5", "usage": {"input_tokens": tokens}},
    }
    (directory / f"{session}.jsonl").write_text(json.dumps(record) + "\n")


class HostDirsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_projects_directory_means_one_machines_own_claude_dir(self):
        (self.root / "projects").mkdir()
        self.assertEqual(
            ingest.host_dirs(self.root, "fedora"), [("fedora", self.root)]
        )

    def test_otherwise_every_subdirectory_is_a_machine_named_after_it(self):
        for name in ("laptop", "fedora"):
            (self.root / name / "projects").mkdir(parents=True)
        self.assertEqual(
            [name for name, _ in ingest.host_dirs(self.root, "ignored")],
            ["fedora", "laptop"],
        )

    def test_a_subdirectory_without_transcripts_is_not_a_machine(self):
        (self.root / "laptop" / "projects").mkdir(parents=True)
        (self.root / "db").mkdir()
        self.assertEqual([name for name, _ in ingest.host_dirs(self.root, "x")], ["laptop"])

    def test_a_missing_root_is_no_machines(self):
        self.assertEqual(ingest.host_dirs(self.root / "gone", "fedora"), [])


class IngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = ingest.connect(":memory:", "fedora")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def rows(self):
        return {
            (r["request_id"], r["host"], r["input"])
            for r in self.db.execute("SELECT request_id, host, input FROM requests")
        }

    def test_every_request_records_the_machine_it_came_from(self):
        transcript(self.root / "fedora" / "projects" / "alpha", "s1", "req-1", tokens=100)
        transcript(self.root / "laptop" / "projects" / "alpha", "s2", "req-2", tokens=200)
        ingest.ingest(self.db, self.root)
        self.assertEqual(self.rows(), {("req-1", "fedora", 100), ("req-2", "laptop", 200)})

    def test_the_same_request_arriving_twice_is_counted_once(self):
        # A transcript synced under two names, or a directory copied between
        # machines. Request ids are unique per API call, so the second is dropped.
        transcript(self.root / "fedora" / "projects" / "alpha", "s1", "req-1", tokens=100)
        transcript(self.root / "laptop" / "projects" / "alpha", "s1", "req-1", tokens=100)
        ingest.ingest(self.db, self.root)
        total = self.db.execute("SELECT SUM(input) AS t, COUNT(*) AS n FROM requests").fetchone()
        self.assertEqual((total["n"], total["t"]), (1, 100))

    def test_a_single_claude_dir_is_stamped_with_the_local_name(self):
        transcript(self.root / "projects" / "alpha", "s1", "req-1")
        ingest.ingest(self.db, self.root, "fedora")
        self.assertEqual(self.rows(), {("req-1", "fedora", 100)})

    def test_each_machines_stats_cache_is_kept_apart(self):
        for name, tokens in (("fedora", 10), ("laptop", 20)):
            directory = self.root / name
            (directory / "projects").mkdir(parents=True)
            (directory / "stats-cache.json").write_text(json.dumps({
                "lastComputedDate": "2026-08-31",
                "firstSessionDate": "2026-01-05",
                "totalSessions": 3,
                "totalMessages": 30,
                "modelUsage": {"claude-opus-5": {"inputTokens": tokens}},
                "dailyActivity": [{"date": "2026-08-30", "messageCount": tokens}],
            }))
        ingest.import_lifetime(self.db, self.root)
        self.assertEqual(
            {(r["host"], r["input"]) for r in self.db.execute("SELECT host, input FROM lifetime")},
            {("fedora", 10), ("laptop", 20)},
        )
        # Same day from two machines: two rows, not one overwriting the other.
        self.assertEqual(
            self.db.execute("SELECT SUM(messages) AS t FROM daily_activity").fetchone()["t"], 30
        )

    def test_reimporting_one_machine_leaves_the_others_alone(self):
        for name in ("fedora", "laptop"):
            directory = self.root / name
            (directory / "projects").mkdir(parents=True)
            (directory / "stats-cache.json").write_text(json.dumps({
                "modelUsage": {"claude-opus-5": {"inputTokens": 10}},
                "dailyActivity": [{"date": "2026-08-30", "messageCount": 10}],
            }))
        ingest.import_lifetime(self.db, self.root)
        (self.root / "fedora" / "stats-cache.json").write_text(json.dumps({
            "modelUsage": {"claude-opus-5": {"inputTokens": 99}},
            "dailyActivity": [{"date": "2026-08-30", "messageCount": 99}],
        }))
        ingest.import_lifetime(self.db, self.root)
        self.assertEqual(
            {(r["host"], r["input"]) for r in self.db.execute("SELECT host, input FROM lifetime")},
            {("fedora", 99), ("laptop", 10)},
        )


LEGACY_SCHEMA = """
CREATE TABLE requests (
    request_id TEXT PRIMARY KEY, ts TEXT NOT NULL, ts_epoch REAL NOT NULL,
    model TEXT NOT NULL, session_id TEXT, project TEXT, cwd TEXT, branch TEXT,
    sidechain INTEGER NOT NULL DEFAULT 0, input INTEGER NOT NULL DEFAULT 0,
    output INTEGER NOT NULL DEFAULT 0, cache_read INTEGER NOT NULL DEFAULT 0,
    cache_w5m INTEGER NOT NULL DEFAULT 0, cache_w1h INTEGER NOT NULL DEFAULT 0,
    web_search INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE lifetime (
    model TEXT PRIMARY KEY, input INTEGER NOT NULL DEFAULT 0,
    output INTEGER NOT NULL DEFAULT 0, cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE daily_activity (
    date TEXT PRIMARY KEY, messages INTEGER NOT NULL DEFAULT 0,
    sessions INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


class MigrationTest(unittest.TestCase):
    """A database written before machines existed. Claude Code has pruned the
    transcripts behind its older rows, so it is stamped rather than rebuilt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "usage.db")
        old = sqlite3.connect(self.path)
        old.executescript(LEGACY_SCHEMA)
        old.execute(
            "INSERT INTO requests (request_id, ts, ts_epoch, model, input)"
            " VALUES ('req-1', '2026-08-01T10:00:00Z', 1, 'claude-opus-5', 100)"
        )
        old.execute("INSERT INTO lifetime VALUES ('claude-opus-5', 500, 1, 2, 3)")
        old.execute("INSERT INTO daily_activity VALUES ('2026-08-30', 40, 2, 9)")
        old.executemany(
            "INSERT INTO meta VALUES (?,?)",
            [("last_computed", "2026-08-31"), ("total_sessions", "7"), ("limits", "{}")],
        )
        old.commit()
        old.close()
        self.db = ingest.connect(self.path, "fedora")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_existing_requests_are_kept_and_stamped(self):
        row = self.db.execute("SELECT host, input FROM requests").fetchone()
        self.assertEqual((row["host"], row["input"]), ("fedora", 100))

    def test_the_lifetime_and_activity_rows_survive_the_rebuild(self):
        lifetime = self.db.execute("SELECT host, model, input FROM lifetime").fetchone()
        self.assertEqual((lifetime["host"], lifetime["model"], lifetime["input"]),
                         ("fedora", "claude-opus-5", 500))
        activity = self.db.execute("SELECT host, date, messages FROM daily_activity").fetchone()
        self.assertEqual((activity["host"], activity["date"], activity["messages"]),
                         ("fedora", "2026-08-30", 40))

    def test_per_machine_scalars_move_out_of_meta(self):
        moved = {r["key"]: r["value"] for r in self.db.execute(
            "SELECT key, value FROM host_meta WHERE host = 'fedora'")}
        self.assertEqual(moved["last_computed"], "2026-08-31")
        self.assertEqual(moved["total_sessions"], "7")
        self.assertEqual(self.db.execute("SELECT COUNT(*) AS n FROM meta").fetchone()["n"], 1)

    def test_the_account_wide_limits_payload_stays_in_meta(self):
        row = self.db.execute("SELECT value FROM meta WHERE key = 'limits'").fetchone()
        self.assertEqual(row["value"], "{}")

    def test_migrating_twice_changes_nothing(self):
        self.db.close()
        self.db = ingest.connect(self.path, "laptop")
        hosts = {r["host"] for r in self.db.execute("SELECT DISTINCT host FROM requests")}
        self.assertEqual(hosts, {"fedora"})


class MergeTest(unittest.TestCase):
    def setUp(self):
        serve.DB = ingest.connect(":memory:", "fedora")

    def tearDown(self):
        serve.DB.close()

    def scalars(self, host, **values):
        serve.DB.executemany(
            "INSERT INTO host_meta VALUES (?,?,?)",
            [(host, key, str(value)) for key, value in values.items()],
        )

    def request(self, request_id, host, days_ago, tokens):
        when = local(days_ago)
        serve.DB.execute(
            "INSERT INTO requests (request_id, host, ts, ts_epoch, model, session_id, input)"
            " VALUES (?,?,?,?,?,?,?)",
            (request_id, host, when.isoformat(), when.timestamp(), "claude-opus-5", "s", tokens),
        )

    def test_each_machines_transcripts_are_topped_up_from_its_own_cutoff(self):
        # The caches stop on different days. One cutoff for both would count the
        # overlap twice or drop it, depending which machine's date won.
        serve.DB.execute("INSERT INTO lifetime VALUES ('fedora', 'claude-opus-5', 1000, 0, 0, 0)")
        serve.DB.execute("INSERT INTO lifetime VALUES ('laptop', 'claude-opus-5', 2000, 0, 0, 0)")
        self.scalars("fedora", last_computed=day(3))
        self.scalars("laptop", last_computed=day(10))
        self.request("a-after", "fedora", 2, 10)      # after fedora's cutoff
        self.request("a-before", "fedora", 5, 999)    # already inside fedora's cache
        self.request("b-after", "laptop", 5, 20)      # after laptop's cutoff

        combined = serve.lifetime_models(serve.host_scalars())
        self.assertEqual(combined["claude-opus-5"]["input"], 3030)

    def test_a_machine_with_no_stats_cache_contributes_all_its_transcripts(self):
        self.request("only", "laptop", 5, 50)
        combined = serve.lifetime_models(serve.host_scalars())
        self.assertEqual(combined["claude-opus-5"]["input"], 50)

    def test_sessions_and_messages_sum_across_machines(self):
        self.scalars("fedora", total_sessions=10, total_messages=100)
        self.scalars("laptop", total_sessions=5, total_messages=50)
        merged = serve.merged_meta(serve.host_scalars())
        self.assertEqual(merged["total_sessions"], 15)
        self.assertEqual(merged["total_messages"], 150)

    def test_coverage_is_bounded_by_the_narrowest_machine(self):
        # History reaches back as far as the earliest machine, but is only
        # complete up to the earliest cache stop — past that a month is missing
        # whatever the machine that stopped first would have added.
        self.scalars("fedora", first_session_date="2026-01-05", last_computed="2026-08-31")
        self.scalars("laptop", first_session_date="2025-11-20", last_computed="2026-07-15")
        merged = serve.merged_meta(serve.host_scalars())
        self.assertEqual(merged["first_session_date"], "2025-11-20")
        self.assertEqual(merged["last_computed"], "2026-07-15")

    def test_no_machines_leaves_the_bounds_empty(self):
        merged = serve.merged_meta({})
        self.assertEqual(merged["first_session_date"], "")
        self.assertEqual(merged["last_computed"], "")
        self.assertEqual(merged["total_sessions"], 0)

    def test_the_machine_filter_scopes_the_snapshot(self):
        self.request("a", "fedora", 1, 100)
        self.request("b", "laptop", 1, 900)
        snapshot = serve.build_snapshot("30d", "all", "fedora")
        self.assertEqual(snapshot["range_stats"]["tokens"], 100)
        self.assertEqual([h["host"] for h in snapshot["hosts"]], ["fedora"])

    def test_unfiltered_the_snapshot_lists_every_machine(self):
        self.request("a", "fedora", 1, 100)
        self.request("b", "laptop", 1, 900)
        snapshot = serve.build_snapshot("30d", "all", "all")
        self.assertEqual(snapshot["host_options"], ["laptop", "fedora"])
        self.assertEqual({h["host"]: h["tokens"] for h in snapshot["hosts"]},
                         {"fedora": 100, "laptop": 900})


class CredentialsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved = (serve.CONFIG["claude_dir"], serve.CONFIG.get("limits_host"))
        serve.CONFIG["claude_dir"] = str(self.root)
        serve.CONFIG["limits_host"] = ""

    def tearDown(self):
        serve.CONFIG["claude_dir"], serve.CONFIG["limits_host"] = self.saved
        self.tmp.cleanup()

    def machine(self, name, mtime):
        directory = self.root / name
        (directory / "projects").mkdir(parents=True)
        path = directory / ".credentials.json"
        path.write_text("{}")
        os.utime(path, (mtime, mtime))
        return path

    def test_the_freshest_token_wins(self):
        # Claude Code rotates the token in place, so the machine that wrote most
        # recently is the one whose token has not expired.
        self.machine("laptop", 1_000_000)
        newest = self.machine("fedora", 2_000_000)
        self.assertEqual(serve.credentials_path(), str(newest))

    def test_limits_host_pins_a_machine(self):
        pinned = self.machine("laptop", 1_000_000)
        self.machine("fedora", 2_000_000)
        serve.CONFIG["limits_host"] = "laptop"
        self.assertEqual(serve.credentials_path(), str(pinned))

    def test_with_nothing_synced_the_error_names_a_real_path(self):
        (self.root / "laptop" / "projects").mkdir(parents=True)
        self.assertEqual(serve.credentials_path(), str(self.root / ".credentials.json"))


if __name__ == "__main__":
    unittest.main()

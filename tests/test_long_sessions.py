"""Long sessions: which sessions count, what is read from them, and their live status."""

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# serve.py opens its database at import time, so it has to be pointed somewhere
# disposable before it is imported — never at the real ~/.claude.
_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("DATA_DIR", _TMP.name)
os.environ.setdefault("CLAUDE_DIR", _TMP.name)

import ingest
import serve

UNI = "/home/justin/Projects/uni/.claude/worktrees/viewer"
KICKOFF = (
    "Lies den Handoff /home/justin/Projects/uni/.claude/handoffs/2026-11-04-round3.md"
    " vollständig, dann die dort unter Einstiegspunkte genannten Dateien, und führe die"
    " Arbeit weiter."
)


def iso(minutes_ago):
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return when.isoformat().replace("+00:00", "Z")


def request(sid, request_id, minutes_ago, context, sidechain=False):
    return {
        "type": "assistant", "requestId": request_id, "sessionId": sid,
        "timestamp": iso(minutes_ago), "cwd": UNI, "gitBranch": "worker-locks",
        "isSidechain": sidechain,
        "message": {"model": "claude-opus-5-5", "usage": {
            "input_tokens": 10, "cache_read_input_tokens": context - 10, "output_tokens": 5,
        }},
    }


def compaction(sid, uuid, minutes_ago, before, after, sidechain=False):
    return {
        "type": "system", "subtype": "compact_boundary", "uuid": uuid, "sessionId": sid,
        "timestamp": iso(minutes_ago), "isSidechain": sidechain,
        "compactMetadata": {"trigger": "manual", "preTokens": before, "postTokens": after},
    }


def prompt(sid, text, minutes_ago=60):
    return {
        "type": "user", "sessionId": sid, "timestamp": iso(minutes_ago), "entrypoint": "cli",
        "message": {"role": "user", "content": text},
    }


def tool_output(sid, text):
    """A user record carrying a tool result, the way a grep's output lands."""
    return {
        "type": "user", "sessionId": sid, "timestamp": iso(30), "entrypoint": "cli",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": text},
        ]},
    }


class LongSessionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved = serve.CONFIG["claude_dir"]
        serve.CONFIG["claude_dir"] = str(self.root)
        serve.DB = ingest.connect(":memory:", "wsl")

    def tearDown(self):
        serve.DB.close()
        serve.CONFIG["claude_dir"] = self.saved
        self.tmp.cleanup()

    def transcript(self, sid, records, machine="wsl", subagent=None):
        directory = self.root / machine / "projects" / "uni"
        path = directory / sid / "subagents" / f"{subagent}.jsonl" if subagent else directory / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            for record in records:
                # Compact, as Claude Code writes them: ingest's cheap substring
                # filters are written against that form.
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")

    def agents(self, entries, machine="wsl", age=0):
        path = self.root / machine / "agents.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries))
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))

    def ingest(self):
        ingest.ingest(serve.DB, self.root)

    def listed(self):
        return {s["id"]: s for s in serve.build_snapshot("30d", "all", "all")["long_sessions"]}

    def compacted_background_session(self):
        self.transcript("bg-1", [
            {**prompt("bg-1", KICKOFF, 300), "sessionKind": "bg"},
            {"type": "custom-title", "customTitle": "handoff-2026-11-04-round3", "sessionId": "bg-1"},
            request("bg-1", "r1", 200, 975_817),
            compaction("bg-1", "c1", 10, 979_939, 18_401),
        ])

    def test_a_short_interactive_session_is_not_listed(self):
        self.transcript("plain", [prompt("plain", "fix the button"), request("plain", "p1", 5, 40_000)])
        self.ingest()
        self.assertEqual(self.listed(), {})

    def test_background_handoff_and_compacted_sessions_are_listed(self):
        self.transcript("bg", [{**prompt("bg", "work", 50), "sessionKind": "bg"},
                               request("bg", "b1", 40, 1000)])
        self.transcript("handoff", [prompt("handoff", KICKOFF), request("handoff", "h1", 40, 1000)])
        self.transcript("compacted", [request("compacted", "c1", 40, 1000),
                                      compaction("compacted", "u1", 30, 900_000, 20_000)])
        self.ingest()
        self.assertEqual(set(self.listed()), {"bg", "handoff", "compacted"})

    def test_a_handoff_quoted_in_tool_output_is_no_handoff(self):
        # A session that searched transcripts for the kickoff text has it in its
        # own transcript, inside tool results — that does not make it one.
        self.transcript("searcher", [prompt("searcher", "ps"), tool_output("searcher", KICKOFF),
                                     request("searcher", "s1", 5, 1000)])
        self.ingest()
        self.assertEqual(self.listed(), {})

    def test_the_handoff_file_and_name_are_read_from_the_transcript(self):
        self.compacted_background_session()
        self.ingest()
        session = self.listed()["bg-1"]
        self.assertEqual(
            (session["title"], session["handoff"], session["background"]),
            ("handoff-2026-11-04-round3", "~/Projects/uni/.claude/handoffs/2026-11-04-round3.md", True),
        )

    def test_a_given_name_wins_over_the_generated_title_everywhere(self):
        self.compacted_background_session()
        self.transcript("bg-1", [{"type": "ai-title", "aiTitle": "Review fixes", "sessionId": "bg-1"}])
        self.ingest()
        snapshot = serve.build_snapshot("30d", "all", "all")
        titles = {s["title"] for key in ("long_sessions", "live_sessions", "sessions")
                  for s in snapshot[key]}
        self.assertEqual(titles, {"handoff-2026-11-04-round3"})

    def test_after_a_compaction_the_context_is_what_it_was_compacted_to(self):
        self.compacted_background_session()
        self.ingest()
        session = self.listed()["bg-1"]
        self.assertEqual(
            (session["context"], session["compactions"], session["last_compaction"]["before"]),
            (18_401, 1, 979_939),
        )

    def test_a_request_after_the_compaction_is_the_context_again(self):
        self.compacted_background_session()
        self.transcript("bg-1", [request("bg-1", "r2", 5, 25_000)])
        self.ingest()
        self.assertEqual(self.listed()["bg-1"]["context"], 25_000)

    def test_a_subagents_requests_and_compactions_are_not_the_sessions_context(self):
        self.compacted_background_session()
        self.transcript("bg-1", [request("bg-1", "sub-r", 2, 150_000, sidechain=True),
                                 compaction("bg-1", "sub-c", 1, 160_000, 9_000, sidechain=True)],
                        subagent="agent-a1")
        self.ingest()
        session = self.listed()["bg-1"]
        self.assertEqual((session["context"], session["compactions"]), (18_401, 1))

    def test_reading_every_transcript_again_counts_nothing_twice(self):
        # The upgrade re-reads every transcript once to pick up session records.
        self.compacted_background_session()
        self.transcript("bg-1", [{"type": "pr-link", "sessionId": "bg-1", "timestamp": iso(20),
                                  "prUrl": "https://github.com/jharkebusch/uni/pull/206",
                                  "prRepository": "jharkebusch/uni", "prNumber": 206}])
        self.ingest()
        serve.DB.execute("UPDATE files SET offset = 0, size = -1")
        self.ingest()
        counts = [serve.DB.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("requests", "compactions", "session_prs")]
        self.assertEqual(counts, [1, 1, 1])

    def test_a_database_from_before_this_feature_reads_every_transcript_again(self):
        path = self.root / "old.db"
        db = ingest.connect(str(path), "wsl")
        db.execute("INSERT INTO files VALUES ('/x.jsonl', 500, 500, 1.0)")
        db.execute("DROP TABLE session_info")
        db.commit()
        db.close()
        db = ingest.connect(str(path), "wsl")
        self.assertEqual(tuple(db.execute("SELECT offset, size FROM files").fetchone()), (0, -1))
        db.close()

    def test_only_https_links_are_kept(self):
        self.compacted_background_session()
        self.transcript("bg-1", [
            {"type": "system", "subtype": "bridge_status", "sessionId": "bg-1",
             "timestamp": iso(9), "url": "javascript:alert(1)"},
            {"type": "pr-link", "sessionId": "bg-1", "timestamp": iso(8),
             "prUrl": "javascript:alert(1)", "prRepository": "x/y", "prNumber": 1},
        ])
        self.ingest()
        session = self.listed()["bg-1"]
        self.assertEqual((session["remote_url"], session["prs"]), (None, []))

    def test_a_running_background_session_is_attached_to(self):
        self.compacted_background_session()
        self.ingest()
        self.agents([{"id": "bg-1abcd", "sessionId": "bg-1", "kind": "background",
                      "status": "busy", "state": "blocked"}])
        session = self.listed()["bg-1"]
        self.assertEqual((session["status"], session["command"]), ("blocked", "claude attach bg-1abcd"))

    def test_a_session_its_machine_no_longer_lists_has_ended(self):
        self.compacted_background_session()
        self.ingest()
        self.agents([])
        session = self.listed()["bg-1"]
        self.assertEqual(
            (session["status"], session["command"]),
            ("ended", "cd ~/Projects/uni/.claude/worktrees/viewer && claude --resume bg-1"),
        )

    def test_a_stale_session_list_says_nothing_about_running(self):
        self.compacted_background_session()
        self.ingest()
        self.agents([{"id": "bg-1abcd", "sessionId": "bg-1", "kind": "background",
                      "status": "busy"}], age=serve.AGENTS_STALE + 60)
        self.assertEqual(self.listed()["bg-1"]["status"], "unknown")

    def test_the_limit_never_cuts_an_open_session(self):
        # More recently active long sessions than the card shows, and one open
        # session that has been idle for days.
        for n in range(serve.LONG_SESSIONS_LIMIT + 2):
            self.transcript(f"recent-{n}", [request(f"recent-{n}", f"q{n}", 10 + n, 1000),
                                            compaction(f"recent-{n}", f"cq{n}", 5, 900_000, 20_000)])
        self.compacted_background_session()
        self.ingest()
        self.agents([{"id": "bg-1abcd", "sessionId": "bg-1", "kind": "background", "status": "idle"}])
        listed = self.listed()
        self.assertEqual((len(listed), "bg-1" in listed), (serve.LONG_SESSIONS_LIMIT, True))

    def test_an_open_session_is_listed_whatever_the_range(self):
        self.transcript("old", [request("old", "o1", 60 * 24 * 40, 1000),
                                compaction("old", "u1", 60 * 24 * 40, 900_000, 20_000)])
        self.ingest()
        self.agents([{"id": "old", "sessionId": "old", "kind": "background", "status": "idle"}])
        self.assertEqual(list(self.listed()), ["old"])


if __name__ == "__main__":
    unittest.main()

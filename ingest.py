"""Incremental ingest of Claude Code transcripts into SQLite.

Transcripts live in ~/.claude/projects/<slug>/<session-id>.jsonl and are appended
to while a session runs, so every file is read from its last byte offset instead
of being re-parsed. Assistant records repeat the same `usage` object once per
content block, so rows are keyed on the request id.

Several machines can feed one database. Each one syncs its own ~/.claude into a
directory named after it, and every row records which machine it came from.
Request ids are unique per API request, so the same transcript arriving twice
costs nothing but the read.
"""

import json
import re
import socket
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id  TEXT PRIMARY KEY,
    host        TEXT    NOT NULL DEFAULT '',
    ts          TEXT    NOT NULL,
    ts_epoch    REAL    NOT NULL,
    model       TEXT    NOT NULL,
    session_id  TEXT,
    project     TEXT,
    cwd         TEXT,
    branch      TEXT,
    sidechain   INTEGER NOT NULL DEFAULT 0,
    input       INTEGER NOT NULL DEFAULT 0,
    output      INTEGER NOT NULL DEFAULT 0,
    cache_read  INTEGER NOT NULL DEFAULT 0,
    cache_w5m   INTEGER NOT NULL DEFAULT 0,
    cache_w1h   INTEGER NOT NULL DEFAULT 0,
    web_search  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_requests_ts      ON requests(ts_epoch);
CREATE INDEX IF NOT EXISTS idx_requests_model   ON requests(model);
CREATE INDEX IF NOT EXISTS idx_requests_session ON requests(session_id);

CREATE TABLE IF NOT EXISTS files (
    path   TEXT PRIMARY KEY,
    offset INTEGER NOT NULL,
    size   INTEGER NOT NULL,
    mtime  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS titles (
    session_id TEXT PRIMARY KEY,
    title      TEXT NOT NULL
);

-- How a session was started. User records carry `entrypoint`: "cli" for a
-- session someone is typing in, "sdk-py" and friends for one spawned
-- programmatically — plugin hooks like the security reviewer open a fresh
-- session per run, and those should not be presented as windows you have open.
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    entrypoint TEXT
);

-- What a long-running session is, beyond its requests: the name it was started
-- under, `kind` "bg" when it runs detached (`claude --bg`), the handoff file it
-- was started from, its Remote Control link, and the latest of Claude Code's own
-- away summaries. Main-thread records only — a subagent's transcript carries its
-- parent's session id.
CREATE TABLE IF NOT EXISTS session_info (
    session_id TEXT PRIMARY KEY,
    name       TEXT,
    kind       TEXT,
    handoff    TEXT,
    remote_url TEXT,
    summary    TEXT,
    summary_at REAL
);

-- One row per compaction, keyed by its boundary record so reading a transcript
-- again adds nothing.
CREATE TABLE IF NOT EXISTS compactions (
    uuid        TEXT PRIMARY KEY,
    session_id  TEXT    NOT NULL,
    ts_epoch    REAL    NOT NULL,
    trigger     TEXT,
    pre_tokens  INTEGER,
    post_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS idx_compactions_session ON compactions(session_id);

CREATE TABLE IF NOT EXISTS session_prs (
    session_id TEXT NOT NULL,
    url        TEXT NOT NULL,
    repo       TEXT,
    number     INTEGER,
    ts_epoch   REAL,
    PRIMARY KEY (session_id, url)
);

-- Lifetime per-model totals from stats-cache.json. Claude Code prunes
-- transcripts after about a month, so this is the only source for the months
-- before the oldest surviving one. It is cumulative through each machine's
-- host_meta.last_computed; that machine's transcripts dated after that day are
-- added on top, which is why the date is stored per machine rather than assumed.
CREATE TABLE IF NOT EXISTS lifetime (
    host        TEXT    NOT NULL,
    model       TEXT    NOT NULL,
    input       INTEGER NOT NULL DEFAULT 0,
    output      INTEGER NOT NULL DEFAULT 0,
    cache_read  INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (host, model)
);

-- Per-day activity counts from stats-cache.json, and the only source that
-- reaches back past the transcript window — it starts at the first session ever,
-- where the transcripts hold about a month. Deliberately never mixed with the
-- requests table: these count different events, and measured against transcript
-- request counts on the same days the ratio swings from 0.5x to 14x.
CREATE TABLE IF NOT EXISTS daily_activity (
    host       TEXT    NOT NULL,
    date       TEXT    NOT NULL,
    messages   INTEGER NOT NULL DEFAULT 0,
    sessions   INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (host, date)
);

-- Scalars that belong to one machine's stats cache: where its history starts,
-- how far its cache has computed, its lifetime session and message counts.
CREATE TABLE IF NOT EXISTS host_meta (
    host  TEXT NOT NULL,
    key   TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (host, key)
);

-- Whole-database state that is not per machine — the last good plan-limits
-- payload, which is per account.
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

HOST_META_KEYS = ("last_computed", "first_session_date", "total_sessions", "total_messages")

REQUEST_COLUMNS = (
    "request_id, host, ts, ts_epoch, model, session_id, project, cwd, branch,"
    " sidechain, input, output, cache_read, cache_w5m, cache_w1h, web_search"
)


def local_hostname():
    return socket.gethostname()


def connect(db_path, local_host=None):
    db = sqlite3.connect(db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    had_session_info = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_info'"
    ).fetchone()
    db.executescript(SCHEMA)
    _migrate(db, local_host or local_hostname())
    if not had_session_info:
        # Every transcript read so far was read for its requests only. Read them
        # all once more so their session records are picked up; each write ingest
        # makes is an insert-or-ignore or an upsert, so nothing is counted twice.
        db.execute("UPDATE files SET offset = 0, size = -1")
    # After the migration, never in SCHEMA: on a database written before
    # machines existed the column does not exist yet when SCHEMA runs.
    db.execute("CREATE INDEX IF NOT EXISTS idx_requests_host ON requests(host)")
    db.commit()
    return db


def _columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _migrate(db, local_host):
    """Bring a single-machine database up to the per-machine schema.

    Everything already in it was recorded on this machine, so it is stamped with
    the local name rather than dropped: Claude Code has long since pruned the
    transcripts behind the older rows, and this database is the only copy.
    """
    if "host" not in _columns(db, "requests"):
        db.execute("ALTER TABLE requests ADD COLUMN host TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE requests SET host = ?", (local_host,))

    for table, columns in (
        ("lifetime", "model, input, output, cache_read, cache_write"),
        ("daily_activity", "date, messages, sessions, tool_calls"),
    ):
        if "host" in _columns(db, table):
            continue
        db.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
        db.executescript(SCHEMA)
        db.execute(
            f"INSERT INTO {table} (host, {columns}) SELECT ?, {columns} FROM {table}_legacy",
            (local_host,),
        )
        db.execute(f"DROP TABLE {table}_legacy")

    marks = ",".join("?" * len(HOST_META_KEYS))
    legacy = db.execute(
        f"SELECT key, value FROM meta WHERE key IN ({marks})", HOST_META_KEYS
    ).fetchall()
    if legacy:
        db.executemany(
            "INSERT OR IGNORE INTO host_meta VALUES (?,?,?)",
            [(local_host, row["key"], row["value"]) for row in legacy],
        )
        db.execute(f"DELETE FROM meta WHERE key IN ({marks})", HOST_META_KEYS)


def host_dirs(claude_dir, local_host):
    """Every machine's ~/.claude under `claude_dir`, as (machine, directory).

    A `projects/` directory sitting directly inside means this is one machine's
    own ~/.claude — the host run, and the container before any sync existed.
    Otherwise each subdirectory is one machine, named after the directory its
    sync writes into.
    """
    root = Path(claude_dir)
    if (root / "projects").is_dir():
        return [(local_host, root)]
    if not root.is_dir():
        return []
    return sorted(
        (child.name, child) for child in root.iterdir() if (child / "projects").is_dir()
    )


def epoch(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def normalize_model(model):
    """Strip the date suffix so claude-haiku-4-5-20251001 prices like claude-haiku-4-5."""
    parts = model.rsplit("-", 1)
    if len(parts) == 2 and len(parts[1]) == 8 and parts[1].isdigit():
        return parts[0]
    return model


def _request_row(rec, host):
    msg = rec.get("message") or {}
    usage = msg.get("usage") or {}
    model = msg.get("model")
    if not usage or not model or model.startswith("<"):
        return None
    request_id = rec.get("requestId") or msg.get("id") or rec.get("uuid")
    ts = rec.get("timestamp")
    if not request_id or not ts:
        return None

    creation = usage.get("cache_creation") or {}
    w5m = creation.get("ephemeral_5m_input_tokens") or 0
    w1h = creation.get("ephemeral_1h_input_tokens") or 0
    if not w5m and not w1h:
        w5m = usage.get("cache_creation_input_tokens") or 0

    cwd = rec.get("cwd") or ""
    server_tools = usage.get("server_tool_use") or {}

    return (
        request_id,
        host,
        ts,
        epoch(ts),
        normalize_model(model),
        rec.get("sessionId"),
        Path(cwd).name if cwd else "",
        cwd,
        rec.get("gitBranch") or "",
        1 if rec.get("isSidechain") else 0,
        usage.get("input_tokens") or 0,
        usage.get("output_tokens") or 0,
        usage.get("cache_read_input_tokens") or 0,
        w5m,
        w1h,
        (server_tools.get("web_search_requests") or 0)
        + (server_tools.get("web_fetch_requests") or 0),
    )


# The kickoff prompt the handoff skill starts its background session with
# (skills/handoff/scripts/start-session.sh). Matched at the very start of a typed
# prompt only, so a transcript that merely quotes it in tool output is no handoff.
HANDOFF_PROMPT = re.compile(r"Lies den Handoff (\S+\.md) ")

# Substrings that make a line worth parsing for session records. Only a filter:
# the parsed record is checked properly, since tool output can contain any text.
SESSION_MARKERS = (
    '"custom-title"', '"compact_boundary"', '"away_summary"', '"bridge_status"',
    '"pr-link"', "Lies den Handoff ",
)


def _https(url):
    """Links taken from transcripts end up as hrefs on the page; only https passes."""
    return url if isinstance(url, str) and url.startswith("https://") else None


def _prompt_text(rec):
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        content = next(
            (b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"),
            None,
        )
    return content if isinstance(content, str) else ""


def _session_record(rec, info, compactions, prs):
    """Fold one record into the pending session rows. Returns whether it was one."""
    sid = rec.get("sessionId")
    if not sid or rec.get("isSidechain"):
        return False
    kind, subtype, ts = rec.get("type"), rec.get("subtype"), rec.get("timestamp")
    if kind == "custom-title" and rec.get("customTitle"):
        info.setdefault(sid, {})["name"] = rec["customTitle"]
    elif kind == "system" and subtype == "compact_boundary" and rec.get("uuid") and ts:
        meta = rec.get("compactMetadata") or {}
        compactions.append(
            (rec["uuid"], sid, epoch(ts), meta.get("trigger"), meta.get("preTokens"),
             meta.get("postTokens"))
        )
    elif kind == "system" and subtype == "away_summary" and rec.get("content") and ts:
        info.setdefault(sid, {}).update(summary=rec["content"], summary_at=epoch(ts))
    elif kind == "system" and subtype == "bridge_status" and _https(rec.get("url")):
        info.setdefault(sid, {})["remote_url"] = rec["url"]
    elif kind == "pr-link" and _https(rec.get("prUrl")):
        prs.append(
            (sid, rec["prUrl"], rec.get("prRepository"), rec.get("prNumber"),
             epoch(ts) if ts else None)
        )
    elif kind == "user" and not rec.get("isMeta") and (
        match := HANDOFF_PROMPT.match(_prompt_text(rec))
    ):
        info.setdefault(sid, {}).setdefault("handoff", match[1])
    else:
        return False
    return True


def _read_new_lines(path, offset):
    """Return (complete lines, new offset). A half-written trailing line is left
    for the next pass so a session being appended to never yields a torn record."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
    if not chunk:
        return [], offset
    cut = chunk.rfind(b"\n")
    if cut == -1:
        return [], offset
    complete = chunk[: cut + 1]
    return complete.decode("utf-8", "replace").splitlines(), offset + len(complete)


def ingest(db, claude_dir, local_host=None):
    """Read every machine's new transcript bytes.

    Returns how many records it took in — requests plus session records — so the
    caller knows the page has something new to show even when no request did.
    """
    seen = {r["path"]: r for r in db.execute("SELECT * FROM files")}
    known_sessions = {r["session_id"] for r in db.execute("SELECT session_id FROM sessions")}
    known_bg = {
        r["session_id"] for r in db.execute("SELECT session_id FROM session_info WHERE kind = 'bg'")
    }
    rows, titles, file_state, origins = [], {}, [], {}
    info, compactions, prs, session_records = {}, [], [], 0

    for host, directory in host_dirs(claude_dir, local_host or local_hostname()):
        for path in (directory / "projects").rglob("*.jsonl"):
            key = str(path)
            try:
                stat = path.stat()
            except OSError:
                continue

            prev = seen.get(key)
            offset = prev["offset"] if prev else 0
            if prev and stat.st_size == prev["size"] and stat.st_mtime == prev["mtime"]:
                continue
            if stat.st_size < offset:  # truncated or replaced — start over
                offset = 0

            try:
                lines, new_offset = _read_new_lines(path, offset)
            except OSError:
                continue

            # The file is named after its session, so the entrypoint only has to
            # be looked for until it is known — no parsing of user records after
            # that.
            want_origin = path.stem not in known_sessions and path.stem not in origins
            # Every record of a background session says so; one is enough.
            want_kind = path.stem not in known_bg and "kind" not in info.get(path.stem, {})

            for line in lines:
                if want_origin and '"entrypoint"' in line:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        rec = {}
                    if rec.get("sessionId") and rec.get("entrypoint"):
                        origins[rec["sessionId"]] = rec["entrypoint"]
                        want_origin = False
                if want_kind and '"sessionKind":"bg"' in line:
                    want_kind = False
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        rec = {}
                    if rec.get("sessionId") and not rec.get("isSidechain"):
                        info.setdefault(rec["sessionId"], {})["kind"] = "bg"
                        session_records += 1
                if any(marker in line for marker in SESSION_MARKERS):
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if _session_record(rec, info, compactions, prs):
                        session_records += 1
                        continue
                if '"assistant"' not in line and '"ai-title"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                kind = rec.get("type")
                if kind == "ai-title":
                    if rec.get("sessionId") and rec.get("aiTitle"):
                        titles[rec["sessionId"]] = rec["aiTitle"]
                elif kind == "assistant":
                    row = _request_row(rec, host)
                    if row:
                        rows.append(row)

            file_state.append((key, new_offset, stat.st_size, stat.st_mtime))

    if rows:
        db.executemany(
            f"INSERT OR IGNORE INTO requests ({REQUEST_COLUMNS})"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    if titles:
        db.executemany(
            "INSERT INTO titles VALUES (?,?) ON CONFLICT(session_id) DO UPDATE SET title=excluded.title",
            list(titles.items()),
        )
    if origins:
        db.executemany("INSERT OR IGNORE INTO sessions VALUES (?,?)", list(origins.items()))
    if info:
        # A later record replaces an earlier one, except the handoff: a session is
        # started from one handoff, and that is the first it was told to read.
        db.executemany(
            "INSERT INTO session_info VALUES (?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET"
            " name = COALESCE(excluded.name, name), kind = COALESCE(excluded.kind, kind),"
            " handoff = COALESCE(handoff, excluded.handoff),"
            " remote_url = COALESCE(excluded.remote_url, remote_url),"
            " summary = COALESCE(excluded.summary, summary),"
            " summary_at = COALESCE(excluded.summary_at, summary_at)",
            [
                (sid, f.get("name"), f.get("kind"), f.get("handoff"), f.get("remote_url"),
                 f.get("summary"), f.get("summary_at"))
                for sid, f in info.items()
            ],
        )
    if compactions:
        db.executemany("INSERT OR IGNORE INTO compactions VALUES (?,?,?,?,?,?)", compactions)
    if prs:
        db.executemany("INSERT OR IGNORE INTO session_prs VALUES (?,?,?,?,?)", prs)
    if file_state:
        db.executemany(
            "INSERT INTO files VALUES (?,?,?,?) ON CONFLICT(path) DO UPDATE SET"
            " offset=excluded.offset, size=excluded.size, mtime=excluded.mtime",
            file_state,
        )
    db.commit()
    return len(rows) + session_records


def _import_host_lifetime(db, host, directory):
    """Load one machine's cumulative per-model totals from its stats-cache.json.

    `modelUsage` carries the same four token classes the transcripts do, so the
    two sources add up in the same unit. (`dailyModelTokens` in the same file
    counts only input+output and is deliberately unused — mixing it with
    transcript totals would compare different things.)
    """
    cache = directory / "stats-cache.json"
    if not cache.is_file():
        return 0
    try:
        data = json.loads(cache.read_text())
    except (OSError, ValueError):
        return 0

    rows = [
        (
            host,
            normalize_model(model),
            usage.get("inputTokens") or 0,
            usage.get("outputTokens") or 0,
            usage.get("cacheReadInputTokens") or 0,
            usage.get("cacheCreationInputTokens") or 0,
        )
        for model, usage in (data.get("modelUsage") or {}).items()
    ]
    if not rows:
        return 0

    db.execute("DELETE FROM lifetime WHERE host = ?", (host,))
    db.executemany("INSERT INTO lifetime VALUES (?,?,?,?,?,?)", rows)
    db.executemany(
        "INSERT INTO host_meta VALUES (?,?,?) ON CONFLICT(host, key) DO UPDATE SET"
        " value=excluded.value",
        [
            (host, "last_computed", data.get("lastComputedDate") or ""),
            (host, "first_session_date", (data.get("firstSessionDate") or "")[:10]),
            (host, "total_sessions", str(data.get("totalSessions") or 0)),
            (host, "total_messages", str(data.get("totalMessages") or 0)),
        ],
    )

    # The cache is authoritative for this machine's days, so its rows are
    # replaced wholesale rather than merged — it recomputes days that were
    # already written. Only this machine's rows: the others are still current.
    activity = [
        (host, day["date"], day.get("messageCount") or 0, day.get("sessionCount") or 0,
         day.get("toolCallCount") or 0)
        for day in (data.get("dailyActivity") or [])
        if day.get("date")
    ]
    db.execute("DELETE FROM daily_activity WHERE host = ?", (host,))
    if activity:
        db.executemany("INSERT INTO daily_activity VALUES (?,?,?,?,?)", activity)
    return len(rows)


def import_lifetime(db, claude_dir, local_host=None):
    """Load every machine's stats cache. Returns the number of model rows."""
    imported = sum(
        _import_host_lifetime(db, host, directory)
        for host, directory in host_dirs(claude_dir, local_host or local_hostname())
    )
    db.commit()
    return imported


if __name__ == "__main__":
    import sys
    import time

    claude_dir = sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".claude")
    data_dir = Path(__file__).parent / "data"
    data_dir.mkdir(exist_ok=True)
    db = connect(str(data_dir / "usage.db"))
    start = time.time()
    new = ingest(db, claude_dir)
    models = import_lifetime(db, claude_dir)
    total = db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    machines = db.execute("SELECT COUNT(DISTINCT host) FROM requests").fetchone()[0]
    print(
        f"{new} new records ({total} requests from {machines} machines),"
        f" {models} lifetime models, {time.time() - start:.2f}s"
    )

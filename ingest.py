"""Incremental ingest of Claude Code transcripts into SQLite.

Transcripts live in ~/.claude/projects/<slug>/<session-id>.jsonl and are appended
to while a session runs, so every file is read from its last byte offset instead
of being re-parsed. Assistant records repeat the same `usage` object once per
content block, so rows are keyed on the request id.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id  TEXT PRIMARY KEY,
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

-- Lifetime per-model totals from stats-cache.json. Claude Code prunes
-- transcripts after about a month, so this is the only source for the months
-- before the oldest surviving one. It is cumulative through meta.last_computed;
-- transcripts dated after that day are added on top, which is why that date is
-- stored rather than assumed.
CREATE TABLE IF NOT EXISTS lifetime (
    model       TEXT PRIMARY KEY,
    input       INTEGER NOT NULL DEFAULT 0,
    output      INTEGER NOT NULL DEFAULT 0,
    cache_read  INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(db_path):
    db = sqlite3.connect(db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    db.commit()
    return db


def epoch(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def normalize_model(model):
    """Strip the date suffix so claude-haiku-4-5-20251001 prices like claude-haiku-4-5."""
    parts = model.rsplit("-", 1)
    if len(parts) == 2 and len(parts[1]) == 8 and parts[1].isdigit():
        return parts[0]
    return model


def _request_row(rec):
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


def ingest(db, claude_dir):
    """Read every transcript's new bytes. Returns the number of new requests."""
    root = Path(claude_dir) / "projects"
    if not root.is_dir():
        return 0

    seen = {r["path"]: r for r in db.execute("SELECT * FROM files")}
    known_sessions = {r["session_id"] for r in db.execute("SELECT session_id FROM sessions")}
    rows, titles, file_state, origins = [], {}, [], {}

    for path in root.rglob("*.jsonl"):
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

        # The file is named after its session, so the entrypoint only has to be
        # looked for until it is known — no parsing of user records after that.
        want_origin = path.stem not in known_sessions and path.stem not in origins

        for line in lines:
            if want_origin and '"entrypoint"' in line:
                try:
                    rec = json.loads(line)
                except ValueError:
                    rec = {}
                if rec.get("sessionId") and rec.get("entrypoint"):
                    origins[rec["sessionId"]] = rec["entrypoint"]
                    want_origin = False
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
                row = _request_row(rec)
                if row:
                    rows.append(row)

        file_state.append((key, new_offset, stat.st_size, stat.st_mtime))

    if rows:
        db.executemany(
            "INSERT OR IGNORE INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
    if titles:
        db.executemany(
            "INSERT INTO titles VALUES (?,?) ON CONFLICT(session_id) DO UPDATE SET title=excluded.title",
            list(titles.items()),
        )
    if origins:
        db.executemany("INSERT OR IGNORE INTO sessions VALUES (?,?)", list(origins.items()))
    if file_state:
        db.executemany(
            "INSERT INTO files VALUES (?,?,?,?) ON CONFLICT(path) DO UPDATE SET"
            " offset=excluded.offset, size=excluded.size, mtime=excluded.mtime",
            file_state,
        )
    db.commit()
    return len(rows)


def import_lifetime(db, claude_dir):
    """Load cumulative per-model totals from stats-cache.json.

    `modelUsage` carries the same four token classes the transcripts do, so the
    two sources add up in the same unit. (`dailyModelTokens` in the same file
    counts only input+output and is deliberately unused — mixing it with
    transcript totals would compare different things.)
    """
    cache = Path(claude_dir) / "stats-cache.json"
    if not cache.is_file():
        return 0
    try:
        data = json.loads(cache.read_text())
    except (OSError, ValueError):
        return 0

    rows = [
        (
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

    db.execute("DELETE FROM lifetime")
    db.executemany("INSERT INTO lifetime VALUES (?,?,?,?,?)", rows)
    db.executemany(
        "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [
            ("last_computed", data.get("lastComputedDate") or ""),
            ("first_session_date", (data.get("firstSessionDate") or "")[:10]),
            ("total_sessions", str(data.get("totalSessions") or 0)),
            ("total_messages", str(data.get("totalMessages") or 0)),
        ],
    )
    db.commit()
    return len(rows)


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
    print(f"{new} new requests ({total} total), {models} lifetime models, {time.time() - start:.2f}s")

"""Local dashboard for Claude Code token usage.

Serves a single HTML page plus a JSON API over the SQLite database that
ingest.py keeps in step with ~/.claude/projects. Standard library only.

    python3 serve.py            # http://127.0.0.1:7581
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import ingest
import limits as limits_api

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / "config.json").read_text())
PRICING = json.loads((BASE / "pricing.json").read_text())

# Environment overrides, so the same config.json works on the host and in a
# container where the paths and bind address differ.
CONFIG["claude_dir"] = os.environ.get("CLAUDE_DIR", CONFIG["claude_dir"])
CONFIG["host"] = os.environ.get("DASHBOARD_HOST", CONFIG["host"])
CONFIG["port"] = int(os.environ.get("DASHBOARD_PORT", CONFIG["port"]))
DATA = Path(os.environ.get("DATA_DIR", str(BASE / "data")))

RANGES = {"24h": 1, "7d": 7, "30d": 30, "90d": 90, "all": None}
OTHER_COLOR = "#8a8a86"
SESSION_WINDOW = timedelta(hours=5)
WEEK_WINDOW = timedelta(days=7)
LIMITS_MIN_INTERVAL = 120       # floor for the account endpoint, in seconds
LIMITS_MAX_INTERVAL = 3600      # ceiling the backoff climbs to after failures
MANUAL_COOLDOWN = 15            # shortest gap between manual refreshes
CREDENTIALS = str(Path(CONFIG["claude_dir"]) / ".credentials.json")

_db_lock = threading.Lock()
_state_lock = threading.Lock()
_limits_lock = threading.Lock()
_state = {
    "version": 0,          # bumps whenever the data behind a snapshot changes
    "published": 0,        # the version clients are told about, coalesced
    "published_at": 0.0,
    "limits": {"ok": False, "error": "not fetched yet"},
    "next_limits": 0.0,    # when the account endpoint may be called again
    "limits_interval": 0.0,
    "last_limits_attempt": 0.0,
}

DATA.mkdir(parents=True, exist_ok=True)
DB = ingest.connect(str(DATA / "usage.db"))


def _restore_limits():
    """Bring back the last good limits payload after a restart.

    The endpoint rate-limits, so a restart can easily land in a window where the
    first call fails. Reset times do not move between polls, so showing the
    stored numbers with their age beats showing nothing.
    """
    try:
        row = DB.execute("SELECT value FROM meta WHERE key = 'limits'").fetchone()
        payload = json.loads(row["value"]) if row else None
    except (sqlite3.Error, ValueError, TypeError):
        return
    if isinstance(payload, dict) and payload.get("ok"):
        _state["limits"] = payload


_restore_limits()

TOKEN_SUM = (
    "SUM(input) AS input, SUM(output) AS output, SUM(cache_read) AS cache_read,"
    " SUM(cache_w5m) AS w5m, SUM(cache_w1h) AS w1h,"
    " SUM(input + output + cache_read + cache_w5m + cache_w1h) AS tokens,"
    " COUNT(*) AS requests"
)


# --- helpers ---------------------------------------------------------------


def query(sql, params=()):
    with _db_lock:
        return DB.execute(sql, params).fetchall()


def cost_of(model, input_, output, cache_read, w5m, w1h):
    rates = PRICING["models"].get(model) or PRICING["default"]
    mult = PRICING["cache_multipliers"]
    return (
        input_ * rates["input"]
        + output * rates["output"]
        + cache_read * rates["input"] * mult["read"]
        + w5m * rates["input"] * mult["write_5m"]
        + w1h * rates["input"] * mult["write_1h"]
    ) / 1_000_000


def row_cost(row):
    return cost_of(
        row["model"], row["input"], row["output"], row["cache_read"], row["w5m"], row["w1h"]
    )


def model_label(model):
    name = model.replace("claude-", "")
    parts = name.split("-")
    family = parts[0].capitalize()
    version = ".".join(parts[1:]) if len(parts) > 1 else ""
    return f"{family} {version}".strip()


def window_totals(start_epoch, end_epoch=None):
    clause = "ts_epoch >= ?"
    params = [start_epoch]
    if end_epoch is not None:
        clause += " AND ts_epoch < ?"
        params.append(end_epoch)
    rows = query(f"SELECT model, {TOKEN_SUM} FROM requests WHERE {clause} GROUP BY model", params)
    tokens = sum(r["tokens"] or 0 for r in rows)
    requests = sum(r["requests"] or 0 for r in rows)
    return {"tokens": tokens, "requests": requests, "cost": sum(row_cost(r) for r in rows)}


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_model_list(totals_by_model):
    """Shape {model: {input, output, cache_read, cache_write, requests}} into the
    rows the page renders, largest first."""
    colors = CONFIG.get("series_colors", {})
    grand_total = sum(
        v["input"] + v["output"] + v["cache_read"] + v["cache_write"]
        for v in totals_by_model.values()
    )
    rows = []
    for model, v in totals_by_model.items():
        tokens = v["input"] + v["output"] + v["cache_read"] + v["cache_write"]
        rows.append(
            {
                "model": model,
                "label": model_label(model),
                "color": colors.get(model, OTHER_COLOR),
                "mapped": model in colors,
                "tokens": tokens,
                "input": v["input"],
                "output": v["output"],
                "cache_read": v["cache_read"],
                "cache_write": v["cache_write"],
                "requests": v.get("requests", 0),
                "cost": cost_of(
                    model, v["input"], v["output"], v["cache_read"], v["cache_write"], 0
                ),
                "share": tokens / grand_total if grand_total else 0,
            }
        )
    return sorted(rows, key=lambda r: r["tokens"], reverse=True)


def lifetime_models(meta):
    """Stats-cache totals through its last computed day, plus every transcript
    request dated after it."""
    combined = {
        r["model"]: {
            "input": r["input"],
            "output": r["output"],
            "cache_read": r["cache_read"],
            "cache_write": r["cache_write"],
            "requests": 0,
        }
        for r in query("SELECT * FROM lifetime")
    }
    for r in query(
        f"SELECT model, {TOKEN_SUM} FROM requests"
        " WHERE date(ts_epoch, 'unixepoch', 'localtime') > ? GROUP BY model",
        (meta.get("last_computed") or "1970-01-01",),
    ):
        slot = combined.setdefault(
            r["model"],
            {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "requests": 0},
        )
        slot["input"] += r["input"] or 0
        slot["output"] += r["output"] or 0
        slot["cache_read"] += r["cache_read"] or 0
        slot["cache_write"] += (r["w5m"] or 0) + (r["w1h"] or 0)
        slot["requests"] += r["requests"] or 0
    return combined


# --- snapshot --------------------------------------------------------------


def build_snapshot(range_key, project):
    now = datetime.now(timezone.utc)
    with _state_lock:
        limits = dict(_state["limits"])
        version = _state["version"]

    days = RANGES.get(range_key, 30)
    range_start = None if days is None else (now - timedelta(days=days)).timestamp()

    where = ["1=1"]
    params = []
    if range_start is not None:
        where.append("ts_epoch >= ?")
        params.append(range_start)
    if project and project != "all":
        where.append("project = ?")
        params.append(project)
    scope = " AND ".join(where)

    # Per-model totals for the selected range.
    models = build_model_list(
        {
            r["model"]: {
                "input": r["input"] or 0,
                "output": r["output"] or 0,
                "cache_read": r["cache_read"] or 0,
                "cache_write": (r["w5m"] or 0) + (r["w1h"] or 0),
                "requests": r["requests"] or 0,
            }
            for r in query(
                f"SELECT model, {TOKEN_SUM} FROM requests WHERE {scope} GROUP BY model", params
            )
        }
    )

    totals_row = query(f"SELECT {TOKEN_SUM} FROM requests WHERE {scope}", params)[0]
    sessions_in_range = query(
        f"SELECT COUNT(DISTINCT session_id) AS n FROM requests WHERE {scope}", params
    )[0]["n"]
    sidechain = query(
        f"SELECT SUM(input + output + cache_read + cache_w5m + cache_w1h) AS tokens"
        f" FROM requests WHERE {scope} AND sidechain = 1",
        params,
    )[0]["tokens"] or 0

    cache_read_total = totals_row["cache_read"] or 0
    fresh_input = (totals_row["input"] or 0) + (totals_row["w5m"] or 0) + (totals_row["w1h"] or 0)
    range_stats = {
        "tokens": totals_row["tokens"] or 0,
        "input": totals_row["input"] or 0,
        "output": totals_row["output"] or 0,
        "cache_read": cache_read_total,
        "cache_write": (totals_row["w5m"] or 0) + (totals_row["w1h"] or 0),
        "requests": totals_row["requests"] or 0,
        "sessions": sessions_in_range or 0,
        "cost": sum(m["cost"] for m in models),
        "sidechain_tokens": sidechain,
        "cache_hit_rate": cache_read_total / (cache_read_total + fresh_input)
        if (cache_read_total + fresh_input)
        else 0,
    }

    # Lifetime figures come from the stats cache plus the transcripts that
    # postdate it; the transcript window alone is barely a month.
    meta = {r["key"]: r["value"] for r in query("SELECT key, value FROM meta")}
    lifetime = lifetime_models(meta)
    lifetime_list = build_model_list(lifetime)
    transcript_first = query("SELECT MIN(ts_epoch) AS e FROM requests")[0]["e"]
    totals = {
        "tokens": sum(m["tokens"] for m in lifetime_list),
        "cost": sum(m["cost"] for m in lifetime_list),
        "requests": query("SELECT COUNT(*) AS n FROM requests")[0]["n"] or 0,
        "sessions": int(meta.get("total_sessions") or 0)
        or (query("SELECT COUNT(DISTINCT session_id) AS n FROM requests")[0]["n"] or 0),
        "messages": int(meta.get("total_messages") or 0),
        "first_day": meta.get("first_session_date") or "",
        "lifetime_through": meta.get("last_computed") or "",
        "transcript_first_day": datetime.fromtimestamp(transcript_first).strftime("%Y-%m-%d")
        if transcript_first
        else "",
    }

    # "All" reports the lifetime picture; the shorter ranges are transcript-only,
    # where every request is individually accounted for.
    if range_key == "all" and (not project or project == "all"):
        models = lifetime_list
        range_stats.update(
            {
                "tokens": totals["tokens"],
                "input": sum(m["input"] for m in models),
                "output": sum(m["output"] for m in models),
                "cache_read": sum(m["cache_read"] for m in models),
                "cache_write": sum(m["cache_write"] for m in models),
                "cost": totals["cost"],
                "sessions": totals["sessions"],
                "requests_partial": True,
            }
        )
        cached = range_stats["cache_read"]
        fresh = range_stats["input"] + range_stats["cache_write"]
        range_stats["cache_hit_rate"] = cached / (cached + fresh) if (cached + fresh) else 0

    # Rolling windows. When the account endpoint answers, its reset times define
    # the exact window the percentages refer to, so the token counts line up with
    # the gauges instead of approximating them.
    session_reset = parse_iso((limits.get("five_hour") or {}).get("resets_at"))
    week_reset = parse_iso((limits.get("seven_day") or {}).get("resets_at"))
    session_start = (session_reset - SESSION_WINDOW) if session_reset else (now - SESSION_WINDOW)
    week_start = (week_reset - WEEK_WINDOW) if week_reset else (now - WEEK_WINDOW)
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)

    windows = {
        "session": {
            **window_totals(session_start.timestamp()),
            "start": session_start.isoformat(),
            "anchored": session_reset is not None,
        },
        "week": {
            **window_totals(week_start.timestamp()),
            "start": week_start.isoformat(),
            "anchored": week_reset is not None,
        },
        "today": {**window_totals(midnight.timestamp()), "start": midnight.isoformat()},
    }

    # Daily series — transcripts only, so every day is measured the same way.
    daily_map = {}
    for r in query(
        f"SELECT date(ts_epoch, 'unixepoch', 'localtime') AS day, model,"
        f" SUM(input + output + cache_read + cache_w5m + cache_w1h) AS tokens"
        f" FROM requests WHERE {scope} GROUP BY day, model ORDER BY day",
        params,
    ):
        day = daily_map.setdefault(r["day"], {"date": r["day"], "by_model": {}})
        day["by_model"][r["model"]] = day["by_model"].get(r["model"], 0) + (r["tokens"] or 0)

    # Fill the quiet days so the x-axis measures elapsed time rather than
    # activity — a gap in usage should look like a gap.
    if daily_map:
        first = datetime.strptime(min(daily_map), "%Y-%m-%d").date()
        if range_start is not None:
            first = max(first, datetime.fromtimestamp(range_start).date())
        last = datetime.now().date()
        cursor = first
        while cursor <= last:
            daily_map.setdefault(cursor.isoformat(), {"date": cursor.isoformat(), "by_model": {}})
            cursor += timedelta(days=1)

    daily = sorted(daily_map.values(), key=lambda d: d["date"])
    for day in daily:
        day["total"] = sum(day["by_model"].values())

    hourly_rows = query(
        f"SELECT CAST(strftime('%H', ts_epoch, 'unixepoch', 'localtime') AS INTEGER) AS hour,"
        f" SUM(input + output + cache_read + cache_w5m + cache_w1h) AS tokens,"
        f" COUNT(*) AS requests FROM requests"
        f" WHERE ts_epoch >= ? {'AND project = ?' if project and project != 'all' else ''}"
        f" GROUP BY hour",
        (midnight.timestamp(), project) if project and project != "all" else (midnight.timestamp(),),
    )
    by_hour = {r["hour"]: r for r in hourly_rows}
    hourly = [
        {
            "hour": h,
            "tokens": (by_hour[h]["tokens"] or 0) if h in by_hour else 0,
            "requests": (by_hour[h]["requests"] or 0) if h in by_hour else 0,
        }
        for h in range(24)
    ]

    project_cost = {}
    for r in query(
        f"SELECT project, model, {TOKEN_SUM} FROM requests WHERE {scope} GROUP BY project, model",
        params,
    ):
        project_cost[r["project"]] = project_cost.get(r["project"], 0) + row_cost(r)
    projects = [
        {
            "project": r["project"] or "(none)",
            "tokens": r["tokens"] or 0,
            "requests": r["requests"] or 0,
            "sessions": r["sessions"] or 0,
            "cost": project_cost.get(r["project"], 0.0),
        }
        for r in query(
            f"SELECT project, COUNT(DISTINCT session_id) AS sessions, {TOKEN_SUM}"
            f" FROM requests WHERE {scope} GROUP BY project ORDER BY tokens DESC LIMIT 15",
            params,
        )
    ]

    return {
        "version": version,
        "generated_at": now.isoformat(),
        "range": range_key,
        "project": project or "all",
        "project_options": [
            r["project"]
            for r in query(
                "SELECT project, SUM(input + output + cache_read + cache_w5m + cache_w1h) AS t"
                " FROM requests WHERE project != '' GROUP BY project ORDER BY t DESC"
            )
        ],
        "limits": limits,
        "totals": totals,
        "range_stats": range_stats,
        "windows": windows,
        "models": models,
        "daily": daily,
        "hourly": hourly,
        "projects": projects,
        "sessions": recent_sessions(scope, params),
        "current_session": current_session(),
        "cost_enabled": bool(CONFIG.get("show_cost", True)),
        "other_color": OTHER_COLOR,
    }


def session_costs(session_ids):
    if not session_ids:
        return {}
    marks = ",".join("?" * len(session_ids))
    rows = query(
        f"SELECT session_id, model, {TOKEN_SUM} FROM requests"
        f" WHERE session_id IN ({marks}) GROUP BY session_id, model",
        session_ids,
    )
    costs = {}
    for r in rows:
        costs[r["session_id"]] = costs.get(r["session_id"], 0) + row_cost(r)
    return costs


def _session_rows(sql, params):
    rows = query(sql, params)
    costs = session_costs([r["session_id"] for r in rows if r["session_id"]])
    return [
        {
            "id": r["session_id"],
            "title": r["title"] or "",
            "project": r["project"] or "",
            "branch": r["branch"] or "",
            "models": sorted({model_label(m) for m in (r["models"] or "").split(",") if m}),
            "turns": r["requests"] or 0,
            "tokens": r["tokens"] or 0,
            "cost": costs.get(r["session_id"], 0.0),
            "start": datetime.fromtimestamp(r["start"], timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(r["end"], timezone.utc).isoformat(),
        }
        for r in rows
    ]


SESSION_SELECT = (
    "SELECT r.session_id AS session_id, MIN(ts_epoch) AS start, MAX(ts_epoch) AS end,"
    " MAX(project) AS project, MAX(branch) AS branch,"
    " GROUP_CONCAT(DISTINCT model) AS models, t.title AS title,"
    f" {TOKEN_SUM}"
    " FROM requests r LEFT JOIN titles t ON t.session_id = r.session_id"
)


def recent_sessions(scope, params):
    return _session_rows(
        f"{SESSION_SELECT} WHERE {scope} GROUP BY r.session_id ORDER BY end DESC LIMIT 20", params
    )


def current_session():
    latest = query("SELECT session_id FROM requests ORDER BY ts_epoch DESC LIMIT 1")
    if not latest or not latest[0]["session_id"]:
        return None
    rows = _session_rows(
        f"{SESSION_SELECT} WHERE r.session_id = ? GROUP BY r.session_id", (latest[0]["session_id"],)
    )
    if not rows:
        return None
    session = rows[0]
    detail = query(
        f"SELECT {TOKEN_SUM} FROM requests WHERE session_id = ?", (latest[0]["session_id"],)
    )[0]
    session.update(
        {
            "input": detail["input"] or 0,
            "output": detail["output"] or 0,
            "cache_read": detail["cache_read"] or 0,
            "cache_write": (detail["w5m"] or 0) + (detail["w1h"] or 0),
            "active": (time.time() - datetime.fromisoformat(session["end"]).timestamp()) < 300,
        }
    )
    return session


# --- background refresh ----------------------------------------------------


def base_limits_interval():
    return max(LIMITS_MIN_INTERVAL, int(CONFIG.get("limits_refresh_seconds", 300)))


def refresh_limits(manual=False):
    """Call the account endpoint, fold the result into state, reschedule.

    Shared by the background loop and the manual refresh button, so both take the
    same backoff and persistence path. A manual call is allowed during a backoff
    — the point of the button is to ask again now — but is rate-limited itself so
    the endpoint cannot be hammered from the page.
    """
    with _limits_lock:
        with _state_lock:
            waited = time.time() - _state["last_limits_attempt"]
            if manual and waited < MANUAL_COOLDOWN:
                return {"ok": False, "cooldown": max(1, round(MANUAL_COOLDOWN - waited))}
            _state["last_limits_attempt"] = time.time()

        result = limits_api.fetch(CREDENTIALS)
        persist = None
        with _state_lock:
            previous = _state["limits"]
            interval = _state["limits_interval"] or base_limits_interval()
            if result.get("ok"):
                interval = base_limits_interval()
                current = persist = {**result, "fetched_at": time.time()}
                _state["next_limits"] = time.time() + interval
            else:
                # A failed poll keeps the last good numbers on screen, marked
                # stale, rather than blanking the gauges over a transient 429.
                current = (
                    {**previous, "stale_error": result["error"]}
                    if previous.get("ok")
                    else result
                )
                if manual:
                    # Pressing the button must not stretch the automatic
                    # schedule; only honour an explicit Retry-After.
                    if result.get("retry_after"):
                        _state["next_limits"] = max(
                            _state["next_limits"], time.time() + result["retry_after"]
                        )
                else:
                    interval = min(
                        LIMITS_MAX_INTERVAL, result.get("retry_after") or interval * 2
                    )
                    _state["next_limits"] = time.time() + interval
                    print(f"limits: {result['error']} — next attempt in {interval}s")
            _state["limits_interval"] = interval
            if current != previous:
                _state["limits"] = current
                _state["version"] += 1

        if persist:
            try:
                with _db_lock:
                    DB.execute(
                        "INSERT INTO meta VALUES ('limits', ?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (json.dumps(persist),),
                    )
                    DB.commit()
            except (sqlite3.Error, TypeError):
                pass
        return result


def refresher():
    """Watch the transcripts continuously; poll the account endpoint sparingly.

    Transcript polling is local file I/O and costs nothing, so it stays on a
    few-second loop. Clients are notified at most once per `notify_seconds`,
    because an active Claude session appends every few seconds and a redraw per
    append reads as the page reloading itself.
    """
    poll = max(1, int(CONFIG.get("poll_seconds", 3)))
    notify_every = max(1, int(CONFIG.get("notify_seconds", 10)))

    with _db_lock:
        ingest.import_lifetime(DB, CONFIG["claude_dir"])
        ingest.ingest(DB, CONFIG["claude_dir"])

    while True:
        try:
            with _db_lock:
                if ingest.ingest(DB, CONFIG["claude_dir"]):
                    with _state_lock:
                        _state["version"] += 1
        except sqlite3.Error as exc:
            print(f"ingest error: {exc}")

        with _state_lock:
            due = time.time() >= _state["next_limits"]
        if due:
            refresh_limits()

        # Coalesce: publish the newest version, never more often than the
        # interval, and never drop the last change.
        with _state_lock:
            pending = _state["version"] != _state["published"]
            if pending and time.time() - _state["published_at"] >= notify_every:
                _state["published"] = _state["version"]
                _state["published_at"] = time.time()
        time.sleep(poll)


# --- http ------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        params = parse_qs(url.query)

        if url.path in ("/", "/index.html"):
            page = (BASE / "index.html").read_bytes()
            return self._send(200, page, "text/html; charset=utf-8")

        if url.path == "/api/snapshot":
            range_key = params.get("range", [CONFIG.get("default_range", "30d")])[0]
            if range_key not in RANGES:
                range_key = "30d"
            project = params.get("project", ["all"])[0]
            payload = json.dumps(build_snapshot(range_key, project)).encode()
            return self._send(200, payload, "application/json")

        if url.path == "/api/stream":
            return self.stream()

        return self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if urlparse(self.path).path != "/api/refresh":
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        result = refresh_limits(manual=True)
        with _state_lock:
            limits = dict(_state["limits"])
            next_in = max(0, round(_state["next_limits"] - time.time()))
        payload = json.dumps(
            {
                "ok": bool(result.get("ok")),
                "cooldown": result.get("cooldown"),
                "error": result.get("error"),
                "limits": limits,
                "next_automatic_in": next_in,
            }
        ).encode()
        return self._send(200, payload, "application/json")

    def stream(self):
        # No Content-Length, so the body is framed by the connection closing —
        # EventSource reads until EOF and reconnects on its own.
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        sent = -1
        beat = time.time()
        try:
            while True:
                with _state_lock:
                    version = _state["published"]
                if version != sent:
                    sent = version
                    self.wfile.write(f"event: update\ndata: {version}\n\n".encode())
                    self.wfile.flush()
                elif time.time() - beat > 15:
                    beat = time.time()
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            return


def main():
    threading.Thread(target=refresher, daemon=True).start()
    server = ThreadingHTTPServer((CONFIG["host"], int(CONFIG["port"])), Handler)
    server.daemon_threads = True
    print(f"claude-dashboard → http://{CONFIG['host']}:{CONFIG['port']}  (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()

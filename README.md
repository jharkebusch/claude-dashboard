# claude-dashboard

A local, live dashboard for Claude Code token usage: total tokens, the current
5-hour and weekly plan windows, the running session, and a per-model / per-project
/ per-session breakdown. Python standard library and one HTML file — no
dependencies, no build step, nothing leaves the machine except the call that reads
your own plan limits.

> **Unsupported by design.** Everything here reads Claude Code's own local files
> and one endpoint the CLI uses for `/usage`, none of which is a published API.
> It can change or stop working without notice. Nothing is written to Claude
> Code's state — the transcripts are only ever read.

```bash
python3 serve.py       # http://127.0.0.1:7581
```

The page updates itself: the server watches the transcript files and pushes an
event when new requests land, so a session in another terminal shows up within
seconds. Those pushes are coalesced (`notify_seconds`) and applied without
dimming the page, because an active session appends constantly and a redraw per
append looks like the page reloading itself. Only a deliberate action — changing
the range or project, or pressing **Refresh** — dims while it loads.

The **Refresh** button next to *Plan limits* asks the account endpoint again
straight away, on top of the 5-minute schedule. It is debounced (15 s) and a
failed press never stretches the automatic backoff; a successful one resets it.

## Docker

```bash
docker compose up -d             # http://127.0.0.1:7581
docker compose logs -f           # follow
docker compose down              # stop
```

The container mounts `~/.claude` **read-only** and keeps its database in a named
volume (`dashboard-data`), so it never writes to Claude Code's state. It runs as
uid 1000 — that is what makes the 0600 credentials file readable. If your user is
not uid 1000, pass your own:

```bash
DASHBOARD_UID=$(id -u) DASHBOARD_GID=$(id -g) docker compose up -d
```

The published port is bound to `127.0.0.1`, same as the host run.

Every day and hour boundary on the page is computed in the **container's**
timezone, and the base image is UTC — which would draw the by-hour chart shifted
from your wall clock and start "today" at the wrong moment. `compose.yaml` sets
`TZ` to `Europe/Berlin` for that reason; export `TZ` to override it.

Both ways of running it use the same `config.json`; the container overrides only
the paths and bind address through `CLAUDE_DIR`, `DATA_DIR`, `DASHBOARD_HOST` and
`DASHBOARD_PORT`. Only one of them can hold port 7581 at a time.

Add `restart: unless-stopped` to `compose.yaml` if you want it to come back with
the Docker daemon; by default it stays on-demand.

## Where the numbers come from

| Figure | Source |
|---|---|
| Tokens, requests, sessions, projects | `~/.claude/projects/**/*.jsonl` — every assistant turn records its exact `usage` |
| All-time totals | `~/.claude/stats-cache.json` (`modelUsage`) plus every transcript dated after its `lastComputedDate` |
| Plan percentages and reset times | `GET /api/oauth/usage` on your Claude account — the same endpoint `/usage` uses |
| API-equivalent cost | `pricing.json` × the token split, computed locally |

**Current sessions** lists every Claude Code window you have open — several
commonly run at once, one per project — with a live dot for anything that wrote
in the last 5 minutes and an idle marker up to 15. Sessions Claude Code starts
programmatically are excluded from that list and counted alongside it instead:
plugin hooks such as the security reviewer open a fresh session per run, and a
handful of those would otherwise crowd out the windows you actually opened. They
are still real spend, so they stay in every total and appear in the sessions
table marked `auto`. The signal is the `entrypoint` each transcript records —
`cli` for a session you type in, `sdk-py` for one a plugin started.

**By hour** puts today's bars against the day you usually have, on one shared
scale. The baseline is a mean over *active* days — days you used Claude at all —
so a week away does not quietly flatten it toward zero, and it is a mean over the
whole day count rather than per hour, which is what lets the hours you are
normally asleep read as quiet instead of being averaged away. It follows the
range and project filters like everything else.

Two days are deliberately left out of it. **Today**, because at 10:00 it
contributes nothing to the evening hours and would drag the baseline below the
day it is being compared against. And **partial days at the far end**: the `24h`
and `7d` ranges are rolling timestamps, so bounding the baseline by one would cut
the oldest day off mid-morning and understate those hours — every day counted is
a whole one. Under two complete days there is no habit to speak of, so the line
is dropped and the note says so; at `24h` that is always the case.

Three details worth knowing:

- **One API response is written as several assistant records**, one per content
  block, each carrying an identical copy of `usage`. Rows are keyed on the request
  id for that reason — summing the records naively roughly triples every figure.
- **Transcripts are pruned after about a month.** That is why all-time figures lean
  on the stats cache; the daily chart, sessions and projects only cover the window
  the transcripts still hold (shown in the footer).
- **`dailyModelTokens` in the stats cache counts only input + output**, not cache
  reads and writes, so it is deliberately unused — mixing it with transcript
  totals would compare different things.

## Cost

Your Max subscription bills none of this. The cost column is what the same traffic
would list at on the Claude API, computed from `pricing.json` (USD per million
tokens, with the documented cache multipliers: reads ×0.1, 5-minute writes ×1.25,
1-hour writes ×2). Edit that file when prices change, or set `show_cost` to `false`
in `config.json` to drop cost from the page entirely.

## Credentials

`limits.py` reads the OAuth access token from `~/.claude/.credentials.json` on each
poll and sends it only to `api.anthropic.com`. It is never cached, logged, or
written anywhere. Claude Code rotates that token itself — the file is re-read every
time rather than held — and if it ever goes stale the page says so and keeps
showing the locally measured token counts, which do not depend on it.

That endpoint rate-limits on a quota window it does not disclose — a 429 comes
back with `Retry-After: 0`, which is no guidance at all — and the quota is shared
with anything else on the account that reads it, Claude Code's own `/usage`
included. So it is polled slowly (see `limits_refresh_seconds`)
and independently of the transcript watcher, which is local file I/O and stays on
its few-second loop. A failed call doubles the interval up to an hour, honouring
`Retry-After` when the endpoint sends one, and resets to the configured interval on
the next success. Meanwhile the last good percentages stay on screen labelled with
their age rather than disappearing, since reset times do not move between polls —
and that last good read is stored in the database, so a restart during a
rate-limited window still shows them instead of an empty panel. The stored read
also carries its own timestamp into the schedule, so restarting the server
repeatedly — rebuilding the image, say — does not spend a call each time.

If the percentages are unavailable anyway, the panel falls back to the same two
windows measured from your transcripts, which never depended on the endpoint, and
the note says when the next attempt is due. Backoff tops out at 15 minutes, so
recovery is picked up on its own; **Refresh** forces the question immediately.

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `host`, `port` | Bind address. `127.0.0.1` keeps it off the network. |
| `claude_dir` | Where Claude Code keeps its state. `~` is expanded, and `CLAUDE_DIR` overrides it. |
| `poll_seconds` | How often transcripts are checked (a full pass over ~650 files is a few ms) |
| `notify_seconds` | Shortest gap between pushes to the page. An active session appends every few seconds; redrawing on each one reads as the page reloading itself, so updates are coalesced. |
| `limits_refresh_seconds` | How often the account endpoint is called. Default 300, floored at 120 — it rate-limits, and the reset times it returns only move once every few hours. |
| `show_cost` | Show or hide every cost figure |
| `default_range` | Range selected on load |
| `series_colors` | Fixed model → colour map for the stacked chart |

### About `series_colors`

Those four hexes are the one four-colour subset of the palette that passes every
colour-blindness and contrast check on the dark surface at all pair distances, so
the stacked chart stays readable. Models not listed are drawn as a neutral "Other"
and still appear individually in the table below the chart. If you swap a colour,
re-validate rather than picking by eye.

## Files

| File | Role |
|---|---|
| `ingest.py` | Transcript → SQLite. Tracks a byte offset per file, so each pass reads only what was appended, and keys rows on the request id (usage is repeated once per content block). |
| `limits.py` | Reads the account usage endpoint |
| `serve.py` | Aggregation queries, JSON API, SSE, static page |
| `index.html` | The whole front end |
| `tests/` | Standard-library `unittest`, no dependencies |
| `data/usage.db` | Derived cache. Safe to delete — a rebuild takes about a second. |

Run `python3 ingest.py` on its own to rebuild the database without starting the
server.

## Tests

```bash
python3 -m unittest discover
```

Nothing is mocked: the tests build a real SQLite database in memory and hand it
to the same query functions the server uses. They point `DATA_DIR` and
`CLAUDE_DIR` at a temporary directory before importing `serve`, which opens its
database at import time — your real `~/.claude` is never touched.

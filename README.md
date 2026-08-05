# claude-dashboard

A local, live dashboard for Claude Code token usage: total tokens, the current
5-hour and weekly plan windows, the running session, and a per-model / per-project
/ per-session breakdown. Python standard library and one HTML file — no
dependencies, no build step, nothing leaves the machine except the call that reads
your own plan limits.

```bash
python3 serve.py       # http://127.0.0.1:7581
```

The page updates itself: the server watches the transcript files and pushes an
event the moment a new request lands, so a session in another terminal shows up
within a few seconds.

## Docker

```bash
docker compose up -d --build     # http://127.0.0.1:7581
docker compose logs -f           # follow
docker compose down              # stop
```

The container mounts `~/.claude` **read-only** and keeps its database in a named
volume (`dashboard-data`), so it never writes to Claude Code's state. It runs as
uid 1000 — that is what makes the 0600 credentials file readable, so if your user
is not uid 1000, change `user:` in `compose.yaml` to match `id -u`. The published
port is bound to `127.0.0.1`, same as the host run.

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

Two details worth knowing:

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

That endpoint rate-limits, so it is polled slowly (see `limits_refresh_seconds`)
and independently of the transcript watcher, which is local file I/O and stays on
its few-second loop. A failed call doubles the interval up to an hour, honouring
`Retry-After` when the endpoint sends one, and resets to the configured interval on
the next success. Meanwhile the last good percentages stay on screen labelled with
their age rather than disappearing, since reset times do not move between polls —
and that last good read is stored in the database, so a restart during a
rate-limited window still shows them instead of an empty panel.

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `host`, `port` | Bind address. `127.0.0.1` keeps it off the network. |
| `claude_dir` | Where Claude Code keeps its state |
| `poll_seconds` | How often transcripts are checked (a full pass over ~650 files is a few ms) |
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
| `data/usage.db` | Derived cache. Safe to delete — a rebuild takes about a second. |

Run `python3 ingest.py` on its own to rebuild the database without starting the
server.

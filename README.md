# claude-dashboard

A local, live dashboard for Claude Code token usage: total tokens, the current
5-hour and weekly plan windows, the running session, and a per-model / per-project
/ per-session breakdown. Python standard library and one HTML file — no
dependencies, no build step, nothing leaves the machine except the call that reads
your own plan limits. Several machines can feed one dashboard — see below.

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

## Several machines

One dashboard can hold every machine you use Claude Code on. Each machine syncs
its own `~/.claude` into a directory named after it, and the server reads all of
them:

```
/srv/data/claude-dashboard/hosts/
    fedora/
        projects/
        stats-cache.json
    laptop/
        projects/
        stats-cache.json
```

Point `CLAUDE_DIR` at that root instead of a single `~/.claude` and nothing else
changes: the directory names become the machine names, and a machine that appears
later is picked up on the next pass. A directory with `projects/` directly inside
it is still read as one machine's own state, so the single-machine setup keeps
working exactly as it did.

Nothing has to be reconciled. Rows are keyed on the request id Claude Code
records per API call, which is unique across machines, so the same transcript
arriving twice costs a read and nothing else.

### Pushing from each machine

`sync/claude-dashboard-sync` is rsync with the right filters, and a systemd timer
to run it:

```bash
install -m 755 sync/claude-dashboard-sync ~/.local/bin/
install -m 644 sync/claude-dashboard-sync.{service,timer} ~/.config/systemd/user/
systemctl --user enable --now claude-dashboard-sync.timer
loginctl enable-linger "$USER"      # so it also runs when you are not logged in
```

Settings go in `~/.config/claude-dashboard-sync.env`:

| Variable | Default | Meaning |
|---|---|---|
| `DASHBOARD_TARGET` | `contabo` | ssh destination of the machine running the dashboard |
| `DASHBOARD_REMOTE_ROOT` | `/srv/data/claude-dashboard/hosts` | the directory of machines on it |
| `DASHBOARD_MACHINE` | `hostname -s` | this machine's name, and its directory |
| `DASHBOARD_SYNC_CREDENTIALS` | `0` | also copy `.credentials.json` — see below |

It never passes `--delete`. Claude Code prunes transcripts after about a month
and the dashboard's database is the only place those months survive, so deleting
the server's copy in step with the local one would throw that history away on
every run. Transcripts are append-only, so after the first run each pass moves
very little.

The timer's interval is the freshness of everything live on the page — the
running-session dots go stale by up to that long — and nothing else depends on
it. A minute is a reasonable default.

### Plan limits across machines

The percentages come from one account endpoint, so one token answers for every
machine, but the token has to reach the server. Set `DASHBOARD_SYNC_CREDENTIALS=1`
on at least one machine and its `.credentials.json` is synced with the
transcripts. That is a real widening — an OAuth token for your Claude account
then lives on the dashboard host — and it is worth doing only if you trust that
host as much as the machine that owns the token. Without it everything else still
works: the limits panel falls back to the same two windows measured from the
transcripts.

Whichever synced credentials file was written most recently is the one read.
Claude Code rotates the token in place, so the machine you used last carries the
copy least likely to have expired; `limits_host` pins a specific machine instead.

### What the merge is exact about, and what it is not

Tokens, requests, sessions, projects and every chart drawn from transcripts are
exact — each request is counted once, on the machine that made it.

The all-time and by-month figures lean on each machine's `stats-cache.json`, and
those stop on different days. Each machine's cache is topped up with that
machine's own transcripts from that machine's own `lastComputedDate`, so nothing
is counted twice — but a month drawn past the *earliest* of those dates is
missing whatever the machine that stopped first would have added. The by-month
note carries that earliest date for exactly this reason.

### Filtering

Once more than one machine is present, a **Machine** dropdown appears beside the
project filter and scopes the page the same way, a **By machine** card breaks the
selected range down, and each session carries the machine it ran on. With a
single machine none of that is drawn.

### Upgrading an existing database

A database written before this existed has no machine on its rows. It is migrated
in place on the next start: the column is added and every existing row is stamped
with `DASHBOARD_LOCAL_HOST`, rather than the database being rebuilt — Claude Code
has long since pruned the transcripts behind the older rows, and that database is
the only copy of them. Set that variable to the name the machine will sync under
**before** the first start, or its history reads as a machine of its own.

## Where the numbers come from

| Figure | Source |
|---|---|
| Tokens, requests, sessions, projects | `~/.claude/projects/**/*.jsonl` — every assistant turn records its exact `usage` |
| All-time totals | `~/.claude/stats-cache.json` (`modelUsage`) plus every transcript dated after its `lastComputedDate` |
| Plan percentages and reset times | `GET /api/oauth/usage` on your Claude account — the same endpoint `/usage` uses |
| API-equivalent cost | `pricing.json` × the token split, computed locally |
| Workload by month | `~/.claude/stats-cache.json` (`dailyActivity`) — sessions, messages and tool calls per day |

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
range, project and machine filters like everything else.

Two days are deliberately left out of it. **Today**, because at 10:00 it
contributes nothing to the evening hours and would drag the baseline below the
day it is being compared against. And **partial days at the far end**: the `24h`
and `7d` ranges are rolling timestamps, so bounding the baseline by one would cut
the oldest day off mid-morning and understate those hours — every day counted is
a whole one. Under two complete days there is no habit to speak of, so the line
is dropped and the note says so; at `24h` that is always the case.

**Tokens per day** carries the same usual-day level as a flat line, on the same
rule — active days, today excluded — so each bar reads as above or below normal
at a glance.

**Workload by month** is the one card that is not about tokens, and it is
measured in sessions and messages for a reason. The transcripts reach back about
a month; only the stats cache goes back to your first session. Its per-day token
figures (`dailyModelTokens`) count input and output alone, so a month there is
tens of millions against the billions the rest of the page reports — the same
unit trap described below. Its *activity* counts have no such problem, so that is
what the months are drawn in.

Those counts are never mixed with the transcripts either. Measured against
transcript request counts on the same days, `messageCount` runs anywhere from
0.5× to 14× — the two count different events. So the card stops where the cache
stops: `lastComputedDate`, which trails the present by days, which is why the
current month is usually absent and the note carries the cutoff. Months cut short
at either end — the first, which starts at your first session, and the last,
where the cache stopped — are drawn faint and marked `partial`, so a short bar is
never read as a quiet month. The card ignores the range and project filters: the
stats cache has no project dimension, and a 30-day range would empty it.

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

`limits.py` reads the OAuth access token from `.credentials.json` on each
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
| `claude_dir` | Where Claude Code keeps its state, or a directory of one such directory per machine. `~` is expanded, and `CLAUDE_DIR` overrides it. |
| `local_host` | Name for the rows read from a single `~/.claude`; empty means the system hostname. `DASHBOARD_LOCAL_HOST` overrides it. Unused when a directory of machines is mounted. |
| `limits_host` | Machine whose credentials file the plan-limits call reads; empty means the most recently written one. `DASHBOARD_LIMITS_HOST` overrides it. |
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
| `sync/` | rsync script and systemd timer that push one machine's state to the dashboard |
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

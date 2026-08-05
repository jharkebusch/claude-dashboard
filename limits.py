"""Read the plan's real limit utilisation from the Claude account endpoint.

`/usage` in the CLI reads GET /api/oauth/usage; the same call works with the
OAuth access token Claude Code stores in ~/.claude/.credentials.json. The file is
re-read on every call because Claude Code rotates the token in place — the token
is never cached here, never logged, and never sent anywhere but api.anthropic.com.
"""

import json
import urllib.error
import urllib.request

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TIMEOUT = 15


def _read_token(credentials_path):
    with open(credentials_path) as fh:
        oauth = (json.load(fh) or {}).get("claudeAiOauth") or {}
    return oauth.get("accessToken"), oauth.get("subscriptionType")


def _gauge(payload, key):
    block = payload.get(key)
    if not isinstance(block, dict):
        return None
    utilization = block.get("utilization")
    if utilization is None:
        return None
    return {"utilization": float(utilization), "resets_at": block.get("resets_at")}


def fetch(credentials_path):
    """Return the current plan limits, or an error the page can explain."""
    try:
        token, plan = _read_token(credentials_path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"cannot read credentials: {exc}"}
    if not token:
        return {"ok": False, "error": "no OAuth token in credentials file"}

    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": "claude-dashboard/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {
                "ok": False,
                "error": "token rejected (401) — run any Claude Code session to refresh it",
            }
        return {"ok": False, "error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, ValueError, OSError) as exc:
        return {"ok": False, "error": f"request failed: {exc}"}

    scoped = [
        {
            "kind": entry.get("kind"),
            "label": ((entry.get("scope") or {}).get("model") or {}).get("display_name"),
            "utilization": float(entry.get("percent") or 0),
            "resets_at": entry.get("resets_at"),
        }
        for entry in (payload.get("limits") or [])
        if entry.get("kind") == "weekly_scoped" and (entry.get("scope") or {}).get("model")
    ]

    extra = payload.get("extra_usage") or {}
    return {
        "ok": True,
        "plan": plan,
        "five_hour": _gauge(payload, "five_hour"),
        "seven_day": _gauge(payload, "seven_day"),
        "seven_day_opus": _gauge(payload, "seven_day_opus"),
        "scoped": scoped,
        "extra_usage": {
            "enabled": bool(extra.get("is_enabled")),
            "utilization": float(extra.get("utilization") or 0),
            "currency": extra.get("currency"),
        }
        if extra
        else None,
    }

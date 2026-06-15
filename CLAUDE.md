# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
# Activate the venv first
source .venv/bin/activate

# Run locally (listens on 0.0.0.0:5000 by default)
python app.py

# Override host/port
FLASK_HOST=127.0.0.1 FLASK_PORT=8080 python app.py
```

Environment variables:
- `FLASK_SECRET_KEY` — session signing key (default: `cef-test-tool-dev-session-key`)
- `ADMIN_ACCESS_KEY` — admin login password (default: `amagi123`)
- `FLASK_HOST` / `FLASK_PORT` — bind address (defaults: `0.0.0.0`, `5000`)

The app is also deployable to Vercel via `vercel.json` (Python runtime, all routes → `app.py`).

## Architecture

Single-file Flask app (`app.py`) with no database. All state lives in the in-process `app_state` dict (a deep copy of `DEFAULT_OVERLAY`), protected by `_state_lock` (a `threading.RLock`).

**State flow:**
1. Admin authenticates at `/admin` (session-cookie via `hmac.compare_digest`)
2. Admin POSTs changes via `/api/overlay` or `/api/hls` → `deep_merge()` patches `app_state`
3. `notify_sse_subscribers()` pushes the new JSON snapshot to all connected clients via queues in `_sse_clients`
4. Browser pages subscribe to `/api/stream` (SSE) and re-render on each event

**Key concepts:**
- `deep_merge(target, patch)` — recursive merge that skips `None` values and deep-copies nested dicts
- `state_snapshot()` — always returns a deep copy (never a reference) to avoid data races
- SSE clients each get a `Queue(maxsize=8)`; full queues silently drop messages
- `@require_admin` decorator redirects HTML requests to `/admin?next=…` and returns 401 JSON for `/api/` requests

**Routes and their purpose** are documented in `ROUTE_DOCS` dict in `app.py:135`. The `/` home page renders this as a live index.

**Console log dual mode** (`mode: "dual_info_error"`) — the `/console-log` page reads settings from the SSE stream and runs two independent timers: a primary INFO flood at `primary_interval_ms` cadence, and a secondary burst of `secondary_levels` messages every `secondary_cycle_ms`.

**Templates** (`templates/`) are standalone Jinja2 HTML files using Tailwind (CDN) for admin pages and plain CSS for overlay/test pages. They pull live state from `/api/stream` (SSE) on load — no page refresh needed.

**Static assets**: `static/output2.mp4` (background video), `static/sounds/` (audio clips).

## Adding a new route

1. Add the Flask route handler in `app.py`
2. Add an entry to `ROUTE_DOCS` (key = endpoint function name) — required or the home page shows a warning
3. Add an entry to `ROUTE_EXAMPLE_KWARGS` if the route has a GET example link

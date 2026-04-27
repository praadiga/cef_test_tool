import copy
import functools
import hmac
import json
import os
import queue
import threading
import time
from flask import Flask, render_template, request, abort, jsonify, Response, stream_with_context, redirect, url_for, session

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "cef-test-tool-dev-session-key")

_state_lock = threading.RLock()
_sse_clients = []
ADMIN_ACCESS_KEY = os.environ.get("ADMIN_ACCESS_KEY", "amagi123")

DEFAULT_OVERLAY = {
    "main_status": 200,
    "team": "INDIA",
    "score": "184/4",
    "overs": "18.4",
    "target": "210",
    "series_title": "ICC Men's T20 World Cup - Final",
    "viewer_count": "4.2Cr",
    "prob_ind_pct": 68,
    "prob_aus_pct": 32,
    "batter_striker": {
        "name": "V. Kohli*",
        "runs": "74",
        "balls": "48",
        "extras_line": "4s: 6  6s: 3  SR: 154.2",
    },
    "batter_non": {
        "name": "H. Pandya",
        "runs": "28",
        "balls": "14",
    },
    "bowler": {
        "name": "M. Starc",
        "figures": "1/34",
        "overs_fig": "3.4",
    },
    "breaking": {
        "enabled": True,
        "items": [
            "Virat Kohli becomes the first player to reach 4000 T20I runs.",
            "WEATHER UPDATE: Cloudy skies over Barbados, but no rain expected for the next 2 hours.",
            "MATCH FACT: India's highest ever T20 World Cup total against Australia was 188/5 in 2007.",
            "FAN POLL: 72% of fans believe India will defend this total successfully today.",
        ],
    },
    "recent_balls": ["1", "4", "wd", "6", "1", "0", "4"],
    "sound": {"seq": 0, "clip": None},
    # Controls /video_hls: toggled from admin via POST /api/hls, pushed on /api/stream (SSE)
    "hls": {
        "enabled": True,
        "url": "https://devstreaming-cdn.apple.com/videos/streaming/examples/img_bipbop_adv_example_fmp4/master.m3u8",
        "seq": 0,
    },
}

app_state = copy.deepcopy(DEFAULT_OVERLAY)


def deep_merge(target: dict, patch: dict) -> None:
    for key, value in patch.items():
        if value is None:
            continue
        if (
            key in target
            and isinstance(target[key], dict)
            and isinstance(value, dict)
        ):
            deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def state_snapshot() -> dict:
    with _state_lock:
        return copy.deepcopy(app_state)


def state_json() -> str:
    return json.dumps(state_snapshot())


def notify_sse_subscribers() -> None:
    payload = state_json()
    with _state_lock:
        clients = list(_sse_clients)
    for q in clients:
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass


def is_admin_authenticated() -> bool:
    return session.get("admin_authenticated") is True


def require_admin(view_func):
    @functools.wraps(view_func)
    def wrapper(*args, **kwargs):
        if is_admin_authenticated():
            return view_func(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "admin key required"}), 401
        return redirect(url_for("admin", next=request.full_path if request.query_string else request.path))

    return wrapper


def safe_local_next(next_url):
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return url_for("admin")


# Shown on /. Update when you add a route (key = view function / endpoint name).
ROUTE_DOCS = {
    "static": "Serves files under the static/ folder (CSS, JS, output2.mp4, sounds, etc.).",
    "home": "This index: all routes, methods, and short descriptions.",
    "return_error": "Renders an error page with a given HTTP status (e.g. 404, 500) for CEF error handling tests.",
    "return_delay": "Waits N seconds, then returns graphics HTML. Used to test slow / delayed HTTP responses.",
    "lBand": "Template page: lBand.html (layout / band UI test).",
    "main_graphics": "Default “graphics” overlay; HTTP status is controlled from /admin (200 vs 4xx/5xx) via overlay state.",
    "get_overlay": "JSON snapshot of the full app state (overlay, sound, hls, etc.) used by the admin and clients.",
    "post_overlay": "Admin-only: merges JSON into app state, bumps SSE, and returns the new snapshot (“Apply to overlay”).",
    "get_hls": "JSON for current HLS settings (enabled, url, seq) for /video_hls, /video_hls2, and /admin.",
    "post_hls": "Admin-only: set HLS enabled URL and/or URL; increases seq; notifies all /api/stream subscribers.",
    "hls_client_error": "Browser POSTs a plain-text error (e.g. hls.js failure) so the server log can mirror the client error.",
    "post_sound": "Admin-only: queues a sound clip id; /main overlay consumes it over SSE to play a cue.",
    "redirect_route": "HTTP redirect to a fixed external Amplify URL (redirect behavior test).",
    "sse_stream": "Server-Sent Events stream: sends full JSON state on connect and on every update (pings to keep connections alive).",
    "admin": "Admin UI: enter the admin key, then edit overlay, play sounds, and control /main / HLS settings.",
    "admin_logout": "Clears the current admin session and returns to the admin key prompt.",
    "display": "Full-page background MP4 from /static/output2.mp4 (like a simple “live” dashboard).",
    "display_hls": "HLS (M3U8) full-page player; start/stop and URL from /admin; updates live over the same SSE stream.",
    "display_hls2": "Same look as /video, but when HLS is enabled in admin, plays the configured M3U8; else falls back to output2.mp4. Reports client errors to the server on failure.",
    "cheer_sound": "Plays a bundled cheer MP3 shortly after load (sounds/ test).",
    "stress_client": "Heavy in-browser CSS/JS (canvas, workers) to stress CEF (CPU/memory/compositing).",
    "audio": "302 redirect to an external SoundCloud page (link / media test).",
    "fail_midway": "Streams partial HTML, waits, then aborts the body (mid-stream failure; optional ?delay= and ?http_status=).",
}

# For GET (or any) “open this” links on the home page; key = endpoint.
ROUTE_EXAMPLE_KWARGS = {
    "static": {"filename": "output2.mp4"},
    "return_error": {"code": 404},
    "return_delay": {"seconds": 2},
    "main_graphics": {},
    "get_overlay": {},
    "get_hls": {},
    "lBand": {},
    "admin": {},
    "admin_logout": {},
    "display": {},
    "display_hls": {},
    "display_hls2": {},
    "cheer_sound": {},
    "stress_client": {},
    "redirect_route": {},
    "audio": {},
    "fail_midway": {},
    "sse_stream": {},
    "home": {},
}

# 1. Error Test Endpoint
@app.route("/error/<int:code>")
def return_error(code):
    return render_template("error.html", code=code), code


# 2. Delay Test Endpoint
@app.route("/delay/<int:seconds>")
def return_delay(seconds):
    time.sleep(seconds)
    return render_template("graphics.html", seconds=seconds)

@app.route("/lBand")
def lBand():
    return render_template("lBand.html")

# 3. Controlled Main Endpoint
@app.route("/main")
def main_graphics():
    status = app_state["main_status"]
    if status == 200:
        return render_template("graphics.html")
    else:
        abort(status)


@app.route("/api/overlay", methods=["GET"])
def get_overlay():
    return jsonify(state_snapshot())


@app.route("/api/overlay", methods=["POST"])
@require_admin
def post_overlay():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400
    with _state_lock:
        deep_merge(app_state, data)
    notify_sse_subscribers()
    return jsonify(state_snapshot())


def _hls_log(message: str, payload=None) -> None:
    if payload is not None:
        print("[hls api] {} {}".format(message, json.dumps(payload, sort_keys=True)), flush=True)
    else:
        print("[hls api] {}".format(message), flush=True)


@app.route("/api/hls", methods=["GET"])
def get_hls():
    """Current HLS play/stop state and master URL (for /video_hls and admin)."""
    with _state_lock:
        h = copy.deepcopy(app_state.get("hls", {}))
    return jsonify(h)


@app.route("/api/hls", methods=["POST"])
@require_admin
def post_hls():
    """Set `enabled` and/or `url`; bumps `seq` and notifies /api/stream subscribers."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400
    with _state_lock:
        h = app_state.setdefault(
            "hls",
            {
                "enabled": True,
                "url": (
                    "https://devstreaming-cdn.apple.com/videos/streaming/examples/"
                    "img_bipbop_adv_example_fmp4/master.m3u8"
                ),
                "seq": 0,
            },
        )
        if "enabled" in data:
            h["enabled"] = bool(data["enabled"])
        if "url" in data and data["url"] is not None:
            u = data["url"] if isinstance(data["url"], str) else str(data["url"])
            u = u.strip()
            if u:
                h["url"] = u
        h["seq"] = int(h.get("seq", 0)) + 1
        out = copy.deepcopy(h)
    _hls_log(
        "POST /api/hls state updated; notifying SSE (seq={})".format(out.get("seq")),
        out,
    )
    notify_sse_subscribers()
    return jsonify(out)


@app.route("/api/hls/client-error", methods=["POST"])
def hls_client_error():
    """Log the exact error string reported by the browser (e.g. /video_hls2 hls.js / network)."""
    data = request.get_json(silent=True) or {}
    msg = data.get("message", "")
    if not isinstance(msg, str):
        msg = str(msg)
    msg = msg.strip()[:8000]
    if not msg:
        return jsonify({"error": "message required"}), 400
    # One line, verbatim client text — no “stopped” or other paraphrase.
    print("[hls client error] {}".format(msg), flush=True)
    return jsonify({"ok": True})


@app.route("/api/sound", methods=["POST"])
@require_admin
def post_sound():
    """Bump sound seq and push over SSE so /main (graphics) can play a cue."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    clip = data.get("clip") or data.get("id")
    if clip is not None and not isinstance(clip, str):
        return jsonify({"error": "clip must be a string"}), 400
    if not clip:
        clip = "default"
    with _state_lock:
        s = app_state.setdefault("sound", {"seq": 0, "clip": None})
        s["seq"] = int(s.get("seq", 0)) + 1
        s["clip"] = clip
        sound_out = copy.deepcopy(app_state["sound"])
    notify_sse_subscribers()
    return jsonify({"ok": True, "sound": sound_out})


# redirect to https://main.d1qoagnu7ropxn.amplifyapp.com/
@app.route("/redirect")
def redirect_route():
    return redirect("https://main.d1qoagnu7ropxn.amplifyapp.com/")

@app.route("/api/stream")
def sse_stream():
    def generate():
        q = queue.Queue(maxsize=8)
        with _state_lock:
            _sse_clients.append(q)
            first = state_json()
        try:
            yield "data: {}\n\n".format(first)
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield "data: {}\n\n".format(msg)
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            with _state_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# Admin — key prompt first; client fills fields via GET /api/overlay after login.
@app.route("/admin", methods=["GET", "POST"])
def admin():
    error = None
    if request.method == "POST":
        submitted_key = request.form.get("secret_key", "")
        if hmac.compare_digest(submitted_key, ADMIN_ACCESS_KEY):
            session["admin_authenticated"] = True
            return redirect(safe_local_next(request.form.get("next")))
        error = "Invalid secret key."
    if not is_admin_authenticated():
        next_url = safe_local_next(request.args.get("next"))
        return render_template("admin_login.html", error=error, next_url=next_url)
    return render_template("admin.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_authenticated", None)
    return redirect(url_for("admin"))

@app.route("/video")
def display():
    return render_template(
        "video.html",
        video_url="/static/output2.mp4",
        text="Live Streaming Dashboard",
    )


@app.route("/video_hls")
def display_hls():
    """Full-page HLS playback (M3U8). Control start/stop from /admin (POST /api/hls); updates via SSE."""
    with _state_lock:
        h = copy.deepcopy(app_state.get("hls", {}))
    return render_template(
        "video_hls.html",
        hls_url=h.get("url", ""),
        hls_enabled=bool(h.get("enabled", True)),
        hls_seq=int(h.get("seq", 0)),
    )


@app.route("/video_hls2")
def display_hls2():
    """Like /video, but HLS from admin when enabled; /static/output2.mp4 when disabled. Same static UI as video.html."""
    with _state_lock:
        h = copy.deepcopy(app_state.get("hls", {}))
    return render_template("video_hls2.html", video_url="/static/output2.mp4", text="Live Streaming Dashboard", hls_boot=h)


@app.route("/cheer")
def cheer_sound():
    """Play static/sounds/cheer.mp3 in the browser 1s after full page load."""
    return render_template(
        "cheer.html",
        cheer_url=url_for("static", filename="sounds/cheer.mp3"),
    )


@app.route("/stress")
def stress_client():
    """Heavy client-side HTML/CSS/JS to stress CEF: compositing, memory, and CPU in the browser."""
    return render_template("stress.html")


@app.route("/audio")
def audio():
    return redirect("https://soundcloud.com/platform/sama")

@app.route("/fail-midway")
def fail_midway():
    """Partial HTML first, then stream abort after a delay (CEF/network test).

    Important: HTTP sends **one** status line with the **first** byte. You cannot
    get **200** first and then **500** later on the same response. This handler
    sends **500** together with the first chunk (partial HTML in the body), then
    waits ``delay`` seconds (default **5**), then aborts — so the client already
    has HTTP **500** from the start; after the wait the connection fails mid-body.

    Optional: ``?delay=5`` (seconds, ``>= 0``). Use ``?http_status=200`` if your
    client only renders “successful” responses but you still want the same
    mid-stream failure (then the status is 200 for the whole response, not 500).
    """
    try:
        delay_sec = float(request.args.get("delay", "5"))
    except (TypeError, ValueError):
        delay_sec = 5.0
    delay_sec = max(0.0, delay_sec)

    use_200 = request.args.get("http_status", "").strip() in ("200", "ok")

    first_chunk = render_template("fail_midway_partial.html")

    def generate():
        yield first_chunk
        time.sleep(delay_sec)
        raise RuntimeError("fail_midway: intentional stream abort")

    return Response(
        stream_with_context(generate()),
        mimetype="text/html; charset=utf-8",
        status=200 if use_200 else 500,
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


def _route_index_rows() -> list:
    """Each rule with methods, Werkzeug path, one-line help, and optional GET open link."""
    rows = []
    for rule in sorted(
        app.url_map.iter_rules(),
        key=lambda r: (r.rule, r.endpoint or "", tuple(sorted((r.methods or set()) - {"HEAD", "OPTIONS"}))),
    ):
        ep = rule.endpoint
        m = sorted(
            x
            for x in (rule.methods or set())
            if x not in ("HEAD", "OPTIONS")
        )
        desc = ROUTE_DOCS.get(
            ep,
            "— (add an entry in ROUTE_DOCS in app.py for endpoint “%s”.)" % (ep,),
        )
        ex_url = None
        ex_kwargs = ROUTE_EXAMPLE_KWARGS.get(ep)
        with app.test_request_context():
            if "GET" in m:
                if ex_kwargs is not None:
                    try:
                        ex_url = url_for(ep, **ex_kwargs)
                    except Exception:
                        ex_url = None
                if ex_url is None:
                    try:
                        ex_url = url_for(ep)
                    except Exception:
                        ex_url = None
        row = {
            "endpoint": ep,
            "rule": str(rule),
            "methods": m,
            "doc": desc,
            "example_url": ex_url,
        }
        if ep == "fail_midway" and ex_url:
            row["example_url_qs"] = ex_url + ("&" if "?" in ex_url else "?") + "delay=2"
        else:
            row["example_url_qs"] = None
        rows.append(row)
    return rows


# home route: index of all routes with descriptions (ROUTE_DOCS)
@app.route("/")
def home():
    return render_template("home.html", route_rows=_route_index_rows())

if __name__ == "__main__":
    # 0.0.0.0 = listen on all interfaces so other machines on the LAN can connect.
    # If still unreachable: allow the port in the OS firewall (e.g. ufw allow 5000/tcp)
    # and on cloud VMs open the security group / inbound rule for this port.
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "5000"))
    print(
        "Flask: http://{}:{}/  (from another device use this machine's LAN IP, e.g. http://192.168.x.x:{}/ )".format(
            host, port, port
        ),
        flush=True,
    )
    app.run(host=host, port=port, debug=True, threaded=True)

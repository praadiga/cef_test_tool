import copy
import json
import queue
import threading
import time
from flask import Flask, render_template, request, abort, jsonify, Response, stream_with_context

app = Flask(__name__)

_state_lock = threading.RLock()
_sse_clients: list = []

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


# 1. Error Test Endpoint
@app.route("/error/<int:code>")
def return_error(code):
    return render_template("error.html", code=code), code


# 2. Delay Test Endpoint
@app.route("/delay/<int:seconds>")
def return_delay(seconds):
    time.sleep(seconds)
    return f"<h1>Response delivered after {seconds} seconds delay</h1>"


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
def post_overlay():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400
    with _state_lock:
        deep_merge(app_state, data)
    notify_sse_subscribers()
    return jsonify(state_snapshot())


@app.route("/api/stream")
def sse_stream():
    def generate():
        q: queue.Queue = queue.Queue(maxsize=8)
        with _state_lock:
            _sse_clients.append(q)
            first = state_json()
        try:
            yield f"data: {first}\n\n"
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield f"data: {msg}\n\n"
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


# Admin — load shell; client fills fields via GET /api/overlay
@app.route("/admin", methods=["GET"])
def admin():
    return render_template("admin.html")


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)

"""
Flask application — serves the canvas editor UI and the control API.

Endpoints:
  GET  /               — main editor page
  GET  /api/canvas     — current canvas as base64 PNG
  GET  /api/drawing    — user drawing layer {x_y: color_id}
  POST /api/drawing    — apply pixel updates
  POST /api/drawing/clear
  GET  /api/status     — worker status + stats
  POST /api/control    — {action: start|stop|pause|resume}
  GET  /api/palette    — 32-colour palette list

Run:  python app.py
"""
import atexit
import base64
import logging
import queue
import sys

from flask import Flask, Response, jsonify, render_template, request

from canvas_manager import CanvasManager
from sip_worker import SIPWorker
import config

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    stream=sys.stdout,
)
logging.getLogger('sip_worker').setLevel(logging.DEBUG)
logging.getLogger('audio_detector').setLevel(logging.DEBUG)
logging.getLogger('werkzeug').setLevel(logging.WARNING)   # silence per-request GET noise
log = logging.getLogger(__name__)

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

canvas_mgr = CanvasManager()
worker = SIPWorker()

atexit.register(worker.shutdown)


# ── routes ────────────────────────────────────────────────────────────────────

@app.get('/')
def index():
    return render_template('index.html', palette=config.PALETTE)


@app.get('/api/canvas')
def api_canvas():
    return jsonify(canvas_mgr.get_canvas_b64())


@app.get('/api/drawing')
def api_get_drawing():
    return jsonify(canvas_mgr.get_drawing())


@app.post('/api/drawing')
def api_update_drawing():
    data = request.get_json(silent=True) or {}
    canvas_mgr.update_drawing(data)
    return jsonify({'ok': True})


@app.post('/api/drawing/clear')
def api_clear_drawing():
    canvas_mgr.clear_drawing()
    return jsonify({'ok': True})


@app.get('/api/status')
def api_status():
    return jsonify(worker.get_status())


@app.post('/api/control')
def api_control():
    body = request.get_json(silent=True) or {}
    action = body.get('action', '')

    if action == 'start':
        skip_matching = body.get('skip_matching', True)
        queue = canvas_mgr.build_queue(skip_matching=skip_matching)
        if not queue:
            return jsonify({'ok': False, 'msg': 'Drawing layer is empty'})
        worker.start(queue)
        return jsonify({'ok': True, 'queued': len(queue)})

    elif action == 'stop':
        worker.stop()
        return jsonify({'ok': True})

    elif action == 'pause':
        worker.pause()
        return jsonify({'ok': True})

    elif action == 'resume':
        worker.resume()
        return jsonify({'ok': True})

    return jsonify({'ok': False, 'msg': f'Unknown action: {action}'})


@app.get('/api/audio/stream')
def api_audio_stream():
    """SSE endpoint — streams PCM16 audio chunks as base64 while a call is active."""
    q = worker.subscribe_audio()

    def generate():
        try:
            while True:
                try:
                    chunk = q.get(timeout=1.0)
                    b64 = base64.b64encode(chunk).decode()
                    yield f'data: {b64}\n\n'
                except queue.Empty:
                    yield ': keepalive\n\n'
        finally:
            worker.unsubscribe_audio(q)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.get('/api/palette')
def api_palette():
    return jsonify([
        {'id': pid, 'name': name, 'hex': hx}
        for name, hx, pid in config.PALETTE
    ])


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    log.info("Starting pixelebbe bot on http://0.0.0.0:5001")
    app.run(host='0.0.0.0', port=5001, debug=False, use_reloader=False)

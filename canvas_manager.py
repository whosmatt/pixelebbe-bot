"""
Manages the canvas background (fetched from pixeleb.be) and the user's drawing layer.
"""
import base64
import io
import json
import logging
import os
import threading
import time

import numpy as np
import requests
from PIL import Image

import config

log = logging.getLogger(__name__)

# Pre-build palette lookup: hex string → (r, g, b) and id→(r,g,b)
PALETTE_RGB = {}   # id → (r, g, b)
PALETTE_HEX = {}   # '#rrggbb' → id
for name, hex_str, pid in config.PALETTE:
    r = int(hex_str[1:3], 16)
    g = int(hex_str[3:5], 16)
    b = int(hex_str[5:7], 16)
    PALETTE_RGB[pid] = (r, g, b)
    PALETTE_HEX[hex_str.lower()] = pid


def nearest_palette_id(r: int, g: int, b: int) -> int:
    """Return the palette color ID closest (Euclidean RGB) to the given colour."""
    best_id, best_dist = 1, float('inf')
    for pid, (pr, pg, pb) in PALETTE_RGB.items():
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_dist:
            best_dist, best_id = d, pid
    return best_id


class CanvasManager:
    def __init__(self):
        self._lock = threading.Lock()
        # drawing layer: {x_y: color_id}  — only explicitly drawn pixels
        self._drawing: dict[str, int] = {}
        # current canvas pixels (fetched): {x_y: color_id}
        self._canvas_pixels: dict[str, int] = {}
        # raw PNG bytes of the last fetched canvas
        self._canvas_png: bytes | None = None
        self._canvas_last_fetch: float = 0.0
        self._load_drawing()

    # ------------------------------------------------------------------ #
    # Drawing layer persistence                                            #
    # ------------------------------------------------------------------ #

    def _load_drawing(self):
        if os.path.exists(config.DRAWING_FILE):
            try:
                with open(config.DRAWING_FILE) as f:
                    self._drawing = json.load(f)
                log.info("Loaded drawing layer: %d pixels", len(self._drawing))
            except Exception as e:
                log.warning("Could not load drawing: %s", e)

    def _save_drawing(self):
        try:
            with open(config.DRAWING_FILE, 'w') as f:
                json.dump(self._drawing, f)
        except Exception as e:
            log.warning("Could not save drawing: %s", e)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def get_drawing(self) -> dict:
        with self._lock:
            return dict(self._drawing)

    def update_drawing(self, updates: dict):
        """
        Apply a batch of pixel updates from the frontend.
        updates: {x_y: color_id, ...}  — use 0 to erase a pixel.
        """
        with self._lock:
            for key, cid in updates.items():
                if cid == 0:
                    self._drawing.pop(key, None)
                else:
                    self._drawing[key] = int(cid)
            self._save_drawing()

    def clear_drawing(self):
        with self._lock:
            self._drawing.clear()
            self._save_drawing()

    def get_canvas_png(self) -> bytes | None:
        """Return the cached raw PNG bytes (refresh if stale)."""
        now = time.time()
        with self._lock:
            stale = now - self._canvas_last_fetch > config.CANVAS_REFRESH_S
            cached = self._canvas_png
        if stale or cached is None:
            self._refresh_canvas()
        with self._lock:
            return self._canvas_png

    def get_canvas_b64(self) -> dict:
        png = self.get_canvas_png()
        if png is None:
            return {'ok': False, 'data': None}
        return {'ok': True, 'data': base64.b64encode(png).decode()}

    def build_queue(self, skip_matching: bool = True) -> list[tuple[int, int, int]]:
        """
        Return list of (x, y, color_id) for all drawn pixels.
        If skip_matching=True, skip pixels that already match the current canvas.
        Ordering: top-to-bottom, left-to-right.
        """
        with self._lock:
            drawing = dict(self._drawing)
            canvas = dict(self._canvas_pixels)

        queue = []
        for key, cid in sorted(drawing.items()):
            if skip_matching and canvas.get(key) == cid:
                continue
            try:
                x_str, y_str = key.split('_')
                x, y = int(x_str), int(y_str)
            except ValueError:
                continue
            queue.append((x, y, cid))

        queue.sort(key=lambda t: (t[1], t[0]))
        log.info("Queue built: %d pixels (skip_matching=%s)", len(queue), skip_matching)
        return queue

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _refresh_canvas(self):
        try:
            resp = requests.get(config.CANVAS_URL, timeout=10)
            resp.raise_for_status()
            png_bytes = resp.content
            img = Image.open(io.BytesIO(png_bytes)).convert('RGB')
            arr = np.array(img)
            pixels = {}
            for y in range(min(config.CANVAS_HEIGHT, arr.shape[0])):
                for x in range(min(config.CANVAS_WIDTH, arr.shape[1])):
                    r, g, b = int(arr[y, x, 0]), int(arr[y, x, 1]), int(arr[y, x, 2])
                    cid = nearest_palette_id(r, g, b)
                    pixels[f'{x}_{y}'] = cid
            with self._lock:
                self._canvas_png = png_bytes
                self._canvas_pixels = pixels
                self._canvas_last_fetch = time.time()
            log.debug("Canvas refreshed")
        except Exception as e:
            log.error("Canvas refresh failed: %s", e)

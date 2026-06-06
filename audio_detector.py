"""
Detects the end of the "Willkommen bei Pixelebbe…" announcement so DTMF can be sent.

Strategy (two layers):
  1. Whisper keyword detection (faster-whisper, if installed) — accurate.
  2. Energy-based state machine fallback — no extra deps, works when Whisper
     is unavailable or the audio is too degraded for transcription.

Audio format expected: raw 16-bit signed PCM at 8 000 Hz (mono).
pyVoIP decodes PCMU → linear PCM before handing audio to read_audio().
"""
import logging
import threading
import time
from collections import deque

import numpy as np

import config

log = logging.getLogger(__name__)

# ── optional Whisper ──────────────────────────────────────────────────────────
try:
    from faster_whisper import WhisperModel
    _whisper_model: 'WhisperModel | None' = None
    _whisper_lock = threading.Lock()

    def _get_whisper():
        global _whisper_model
        with _whisper_lock:
            if _whisper_model is None:
                log.info("Loading Whisper model '%s' …", config.WHISPER_MODEL)
                _whisper_model = WhisperModel(
                    config.WHISPER_MODEL,
                    device='cpu',
                    compute_type='int8',
                )
                log.info("Whisper ready")
        return _whisper_model

    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False
    log.warning("faster-whisper not installed; using energy-only detection")


# ── helpers ───────────────────────────────────────────────────────────────────

SAMPLE_RATE = config.DTMF_SAMPLE_RATE   # 8 000 Hz
FRAME_SAMPLES = 160                      # 20 ms per frame


def _rms(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if len(samples) else 0.0


def _upsample_2x(pcm16: np.ndarray) -> np.ndarray:
    """Simple linear interpolation from 8 kHz to 16 kHz."""
    out = np.empty(len(pcm16) * 2, dtype=np.float32)
    out[0::2] = pcm16
    out[1::2] = pcm16
    return out / 32768.0


def _transcribe_buffer(buf_pcm16: np.ndarray) -> str:
    """Run Whisper on a PCM16 buffer and return lower-cased transcript."""
    model = _get_whisper()
    audio_f32 = _upsample_2x(buf_pcm16)  # Whisper needs 16 kHz float32
    segments, _ = model.transcribe(
        audio_f32,
        language='de',
        beam_size=1,
        best_of=1,
        temperature=0.0,
        vad_filter=False,
    )
    return ' '.join(s.text for s in segments).lower()


# ── detector ──────────────────────────────────────────────────────────────────

class AnnouncementDetector:
    """
    Feed 20-ms PCM16 frames via process_frame().
    .triggered becomes True (and process_frame() returns True) once the
    announcement end is detected — that is the moment to start sending DTMF.
    """

    # energy state-machine thresholds
    # G.711 A-law on a phone line typically decodes to RMS ~200-3000 for speech.
    # Set conservatively low so any signal registers; tune up if false triggers.
    SILENCE_RMS = 150          # RMS below this is "silence"
    MIN_AUDIO_FRAMES = 20      # 400 ms — minimum "real" audio segment
    MIN_SILENCE_FRAMES = 18    # 360 ms — minimum silence to end a segment
    MUSIC_MIN_FRAMES = 150     # 3 s — audio segment this long is classified as music

    # whisper parameters
    WHISPER_CHUNK_S = 4        # seconds of audio to transcribe at once
    WHISPER_STEP_S = 2         # how often to re-transcribe (sliding window)

    def __init__(self):
        self.triggered = False
        self._frame_count = 0
        self._start_time = time.time()

        # energy state machine
        self._consec_silence = 0
        self._consec_audio = 0
        self._seg_frames = 0    # frames in the current audio segment
        self._in_audio = False
        self._music_seen = False  # True once we've seen a long audio segment

        # whisper sliding buffer: stores PCM16 samples for last N seconds
        _cap = int(self.WHISPER_CHUNK_S * SAMPLE_RATE)
        self._audio_buf: deque[np.int16] = deque(maxlen=_cap)
        self._last_whisper_t: float = 0.0
        self._keyword_hit: bool = False

    # ------------------------------------------------------------------ #

    def process_frame(self, pcm_bytes: bytes) -> bool:
        """
        Call with each 20-ms chunk of raw PCM16 bytes from pyVoIP.
        Returns True when DTMF should be sent.
        """
        if self.triggered:
            return True

        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        self._frame_count += 1
        elapsed = time.time() - self._start_time

        # Log RMS every 2 s so you can verify audio is flowing and tune threshold
        rms_now = _rms(samples)
        if self._frame_count % 100 == 0:
            _state = 'music' if self._music_seen else ('audio' if self._in_audio else 'silence')
            log.info("audio RMS=%.0f  elapsed=%.1fs  energy_state=%s",
                     rms_now, elapsed, _state)

        # feed whisper buffer
        self._audio_buf.extend(samples.tolist())

        # --- Whisper layer (async-ish: runs every WHISPER_STEP_S seconds) ---
        if WHISPER_AVAILABLE and elapsed > 2.0:
            now = time.time()
            if now - self._last_whisper_t >= self.WHISPER_STEP_S:
                self._last_whisper_t = now
                buf = np.array(list(self._audio_buf), dtype=np.int16)
                try:
                    text = _transcribe_buffer(buf)
                    log.debug("Whisper: %r", text)
                    if any(kw in text for kw in config.WHISPER_TRIGGER_KEYWORDS):
                        self._keyword_hit = True
                        log.info("Whisper keyword hit in: %r", text)
                except Exception as e:
                    log.debug("Whisper error: %s", e)

        # If Whisper already found the keyword, wait for silence to confirm end
        if self._keyword_hit:
            rms = _rms(samples)
            if rms < self.SILENCE_RMS:
                self._consec_silence += 1
                if self._consec_silence >= self.MIN_SILENCE_FRAMES:
                    log.info("Announcement end detected via Whisper+silence")
                    self._fire()
                    return True
            else:
                self._consec_silence = 0
            return False

        # --- Energy-only state machine fallback ---
        rms = _rms(samples)
        is_audio = rms > self.SILENCE_RMS

        if is_audio:
            self._consec_silence = 0
            self._consec_audio += 1
            if not self._in_audio:
                self._in_audio = True
                self._seg_frames = 0
            self._seg_frames += 1
        else:
            self._consec_audio = 0
            self._consec_silence += 1

            if self._in_audio and self._consec_silence >= self.MIN_SILENCE_FRAMES:
                seg = self._seg_frames
                self._in_audio = False
                self._seg_frames = 0

                if seg >= self.MIN_AUDIO_FRAMES:
                    if seg >= self.MUSIC_MIN_FRAMES:
                        # Long segment = music; note it and keep waiting
                        self._music_seen = True
                        log.debug("Energy: music segment (%.1fs)", seg * 0.02)
                    else:
                        # Short segment after [optionally] music = announcement
                        log.info(
                            "Announcement end via energy (seg=%.1fs, music_seen=%s)",
                            seg * 0.02, self._music_seen,
                        )
                        self._fire()
                        return True

        # Hard timeout — just try DTMF after ANNOUNCEMENT_TIMEOUT_S
        if elapsed >= config.ANNOUNCEMENT_TIMEOUT_S:
            log.warning("Announcement detection timed out, sending DTMF anyway")
            self._fire()
            return True

        return False

    def _fire(self):
        self.triggered = True

    def elapsed_s(self) -> float:
        return time.time() - self._start_time

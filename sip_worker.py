"""
SIP call worker — drives a baresip subprocess via its ctrl_tcp interface.

Baresip handles all SIP signalling (including digest auth) natively.
DTMF is sent via RFC 2833 telephone-event RTP — no audio tone generation.
Received call audio is read from a FIFO (/tmp/bs_rx) for announcement detection.

Architecture:
  BaresipBridge  – subprocess lifecycle + ctrl_tcp socket → events/commands
  SIPWorker      – call queue management, pixel loop, audio monitoring
"""
import json
import logging
import os
import queue
import socket
import subprocess
import threading
import time
from typing import Optional

import numpy as np

import config
from audio_detector import AnnouncementDetector, FRAME_SAMPLES

log = logging.getLogger(__name__)

CTRL_PORT  = 4444
RECORD_WAV = '/tmp/call_rx.wav'    # baresip 'record' writes here per-call
WAV_HEADER = 44                    # bytes to skip (standard PCM WAV header)
FRAME_BYTES = FRAME_SAMPLES * 2   # 16-bit PCM = 320 bytes = 20 ms


# ── baresip process + ctrl_tcp ────────────────────────────────────────────────

class BaresipBridge:
    """
    Manages the baresip subprocess and its ctrl_tcp control channel.

    Wire protocol (baresip 1.0.0):  netstring-wrapped JSON in both directions.
      Send:  len:{"command":"dial","params":"sip:..."},
      Recv:  len:{"event":true,"type":"CALL_ESTABLISHED",...},
             len:{"response":true,"ok":true,"data":"..."},

    Registration detection: baresip 1.0.0 does NOT emit ctrl_tcp events for
    REGISTER — we detect it by watching baresip's stdout for "200 OK".
    """

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._event_q: queue.Queue = queue.Queue()
        self._registered = threading.Event()
        self._lock = threading.Lock()
        self._active_call_id: Optional[str] = None
        self._call_state: str = 'idle'
        # VU meter levels (dB, -100 = silence) from vumeter.so via ctrl_tcp
        self.vu_queue: queue.Queue = queue.Queue(maxsize=200)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self, sip_user: str, sip_pass: str, sip_server: str):
        """Write accounts file, create audio FIFO, launch baresip."""
        accounts_path = '/root/.baresip/accounts'
        os.makedirs(os.path.dirname(accounts_path), exist_ok=True)
        with open(accounts_path, 'w') as f:
            f.write(
                f'<sip:{sip_user}@{sip_server};transport=tcp>'
                f';auth_pass={sip_pass};\n'
            )
        log.info("baresip account written: %s@%s (TCP)", sip_user, sip_server)

        # No FIFO needed — audio captured via baresip 'record' command per call

        # libre resolves home dir via LOGNAME/USER (not HOME) in Docker
        import os as _os
        env = dict(_os.environ)
        env.update({'USER': 'root', 'LOGNAME': 'root', 'HOME': '/root'})

        self._proc = subprocess.Popen(
            ['/usr/bin/baresip', '-v'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
        )
        log.info("baresip started (pid %d)", self._proc.pid)

        threading.Thread(target=self._log_baresip, daemon=True).start()
        self._connect_ctrl()

        log.info("Waiting for SIP registration …")
        if not self._registered.wait(timeout=60):
            raise RuntimeError("baresip did not register within 60 s")
        log.info("SIP registered")

    def stop(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    # ── netstring helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _encode_ns(obj: dict) -> bytes:
        msg = json.dumps(obj)
        return f'{len(msg)}:{msg},'.encode()

    @staticmethod
    def _parse_ns(buf: bytes) -> tuple[list[str], bytes]:
        """Extract all complete netstring payloads; return (msgs, remainder)."""
        msgs = []
        while buf:
            colon = buf.find(b':')
            if colon < 0:
                break
            try:
                length = int(buf[:colon])
            except ValueError:
                buf = buf[1:]   # skip bad byte and retry
                continue
            end = colon + 1 + length
            if len(buf) < end + 1:
                break           # incomplete
            if buf[end:end + 1] != b',':
                buf = buf[1:]   # malformed, skip
                continue
            msgs.append(buf[colon + 1:end].decode('utf-8', errors='replace'))
            buf = buf[end + 1:]
        return msgs, buf

    # ── ctrl_tcp ──────────────────────────────────────────────────────────────

    def _connect_ctrl(self):
        for _ in range(20):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect(('127.0.0.1', CTRL_PORT))
                self._sock = s
                log.info("ctrl_tcp connected")
                threading.Thread(target=self._recv_events, daemon=True).start()
                return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.5)
        raise RuntimeError("Could not connect to baresip ctrl_tcp after 10 s")

    def _recv_events(self):
        raw = b''
        try:
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                raw += chunk
                msgs, raw = self._parse_ns(raw)
                for msg in msgs:
                    self._handle_event(msg)
        except Exception as e:
            log.debug("ctrl_tcp recv ended: %s", e)

    def _handle_event(self, msg: str):
        try:
            ev = json.loads(msg)
        except json.JSONDecodeError:
            log.debug("ctrl_tcp raw: %s", msg[:120])
            return

        ev_type = ev.get('type', '')
        ev_class = ev.get('class', '')

        if ev.get('response'):
            ok = ev.get('ok', False)
            log.debug("ctrl_tcp response: ok=%s %s", ok, ev.get('data', '')[:80])
            return

        log.debug("baresip event %s/%s", ev_class, ev_type)

        if ev_class == 'call':
            call_id = ev.get('id', '')
            if ev_type == 'CALL_ESTABLISHED':
                with self._lock:
                    self._active_call_id = call_id
                    self._call_state = 'answered'
                log.info("Call established id=%s", call_id)
            elif ev_type in ('CALL_CLOSED', 'CALL_HANGUP'):
                param = ev.get('param', '')
                with self._lock:
                    self._call_state = 'ended'
                    self._active_call_id = None
                log.info("Call closed: %s", param)
            elif ev_type == 'CALL_RINGING':
                with self._lock:
                    self._call_state = 'ringing'

        elif ev_type in ('VU_METER', 'VU_RX_REPORT'):
            # 'param' holds the dB value in baresip 1.0.0's vumeter
            try:
                vu_recv = float(ev.get('vu_recv',
                                ev.get('rx', ev.get('param', -100))))
                log.debug("VU RX: %.1f dB", vu_recv)
                try:
                    self.vu_queue.put_nowait(vu_recv)
                except queue.Full:
                    pass
            except (ValueError, TypeError):
                pass
            return

        self._event_q.put(ev)

    def send(self, command: str, params: str = ''):
        """Send a JSON netstring command to baresip."""
        obj = {'command': command}
        if params:
            obj['params'] = params
        if self._sock:
            try:
                self._sock.sendall(self._encode_ns(obj))
                log.debug("→ baresip: %s %s", command, params)
            except Exception as e:
                log.warning("ctrl_tcp send failed: %s", e)

    def get_call_state(self) -> str:
        with self._lock:
            return self._call_state

    def reset_call_state(self):
        with self._lock:
            self._call_state = 'idle'
            self._active_call_id = None
        while True:
            try:
                self._event_q.get_nowait()
            except queue.Empty:
                break

    # ── stdout forwarder (also detects registration) ──────────────────────────

    def _log_baresip(self):
        for line in self._proc.stdout:
            line = line.rstrip()
            log.info("baresip| %s", line)
            # baresip 1.0.0 does not emit ctrl_tcp events for REGISTER;
            # detect it from the stdout log line instead.
            if '200 OK' in line and '@' in line and 'binding' in line:
                log.info("Registration detected via stdout")
                self._registered.set()


# ── SIP worker ────────────────────────────────────────────────────────────────

class SIPWorker:

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()

        self._queue: list[tuple[int, int, int]] = []
        self._history: list[dict] = []

        self._status = {
            'running': False, 'paused': False,
            'current_pixel': None, 'call_status': 'idle',
            'pixels_done': 0, 'pixels_failed': 0, 'pixels_pending': 0,
            'call_count': 0, 'last_error': None, 'started_at': None,
        }

        # Audio monitoring subscribers
        self._audio_subscribers: list[queue.Queue] = []
        self._audio_sub_lock = threading.Lock()

        self._bridge: Optional[BaresipBridge] = None
        self._bridge_lock = threading.Lock()

        # Per-call WAV reader — started/stopped in _call_pixel, not at init
        self._wav_stop = threading.Event()

    # ── public control ────────────────────────────────────────────────────────

    def start(self, pixel_queue: list[tuple[int, int, int]]):
        with self._lock:
            if self._status['running']:
                return
            self._queue = list(pixel_queue)
            self._status.update({
                'running': True, 'paused': False,
                'pixels_done': 0, 'pixels_failed': 0,
                'pixels_pending': len(pixel_queue),
                'call_count': 0, 'last_error': None,
                'started_at': time.time(),
            })
            self._stop_event.clear()
            self._pause_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._pause_event.clear()
        with self._lock:
            self._status['running'] = False

    def pause(self):
        self._pause_event.set()
        with self._lock:
            self._status['paused'] = True

    def resume(self):
        self._pause_event.clear()
        with self._lock:
            self._status['paused'] = False

    def get_status(self) -> dict:
        with self._lock:
            s = dict(self._status)
        s['history'] = list(self._history[-20:])
        s['queue_preview'] = [
            {'x': x, 'y': y, 'color_id': c} for x, y, c in self._queue[:30]
        ]
        if s.get('started_at'):
            elapsed = time.time() - s['started_at']
            done = s['pixels_done'] + s['pixels_failed']
            s['elapsed_s'] = round(elapsed)
            s['rate_per_hour'] = round(done / elapsed * 3600, 1) if elapsed > 5 and done else 0
        return s

    # ── audio monitoring ──────────────────────────────────────────────────────

    def subscribe_audio(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=150)
        with self._audio_sub_lock:
            self._audio_subscribers.append(q)
        return q

    def unsubscribe_audio(self, q: queue.Queue):
        with self._audio_sub_lock:
            try:
                self._audio_subscribers.remove(q)
            except ValueError:
                pass

    def _broadcast_audio(self, pcm: bytes):
        with self._audio_sub_lock:
            dead = []
            for q in self._audio_subscribers:
                try:
                    q.put_nowait(pcm)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._audio_subscribers.remove(q)

    def _audio_wav_reader(self, stop_event: threading.Event):
        """
        Tails RECORD_WAV while a call is active, broadcasting 20 ms PCM16 frames.

        baresip's 'record' command writes a standard WAV file (44-byte header,
        then raw PCM16 @ 8 kHz mono).  We skip the header and stream the rest.
        aufile.so in Debian's baresip 1.0.0 has no auplay, so FIFO-based capture
        is impossible; the record command is the only supported capture path.
        """
        log.debug("WAV reader started, waiting for %s", RECORD_WAV)
        # Wait up to 5 s for baresip to create the file
        deadline = time.time() + 5
        while not os.path.exists(RECORD_WAV) and time.time() < deadline:
            if stop_event.is_set():
                return
            time.sleep(0.05)

        if not os.path.exists(RECORD_WAV):
            log.warning("WAV file never appeared at %s", RECORD_WAV)
            return

        frames_rx = 0
        try:
            with open(RECORD_WAV, 'rb') as f:
                # Skip WAV header (may not be fully written yet; wait if needed)
                header = b''
                while len(header) < WAV_HEADER and not stop_event.is_set():
                    chunk = f.read(WAV_HEADER - len(header))
                    if chunk:
                        header += chunk
                    else:
                        time.sleep(0.02)

                log.debug("WAV header consumed (%d bytes), streaming audio", len(header))
                buf = b''
                while not stop_event.is_set():
                    data = f.read(4096)
                    if data:
                        buf += data
                        while len(buf) >= FRAME_BYTES:
                            self._broadcast_audio(buf[:FRAME_BYTES])
                            buf = buf[FRAME_BYTES:]
                            frames_rx += 1
                    else:
                        time.sleep(0.02)   # no new data yet, poll
        except Exception as e:
            log.debug("WAV reader error: %s", e)

        log.debug("WAV reader finished: %d frames broadcast", frames_rx)
        # Clean up for next call
        try:
            os.remove(RECORD_WAV)
        except OSError:
            pass

    # ── worker loop ───────────────────────────────────────────────────────────

    def _run(self):
        try:
            self._ensure_bridge()
        except Exception as e:
            log.error("baresip startup failed: %s", e)
            with self._lock:
                self._status.update({'running': False, 'last_error': str(e)})
            return

        while not self._stop_event.is_set():
            while self._pause_event.is_set() and not self._stop_event.is_set():
                time.sleep(0.5)

            with self._lock:
                if not self._queue:
                    self._status['running'] = False
                    break
                x, y, cid = self._queue[0]
                self._status['current_pixel'] = {'x': x, 'y': y, 'color_id': cid}
                self._status['call_status'] = 'dialing'

            t0 = time.time()
            success, reason = self._call_pixel(x, y, cid)
            duration = round(time.time() - t0, 1)

            result = {
                'x': x, 'y': y, 'color_id': cid,
                'success': success, 'reason': reason,
                'duration_s': duration, 'ts': time.time(),
            }
            with self._lock:
                self._queue.pop(0)
                if success:
                    self._status['pixels_done'] += 1
                else:
                    self._status['pixels_failed'] += 1
                self._status['pixels_pending'] = len(self._queue)
                self._status['call_count'] += 1
                self._status['current_pixel'] = None
                self._status['call_status'] = 'idle'
                if not success:
                    self._status['last_error'] = f'({x},{y}): {reason}'
            self._history.append(result)
            if len(self._history) > 50:
                self._history = self._history[-50:]

            log.info("Pixel (%d,%d) c%d → %s [%s] %.1fs",
                     x, y, cid, 'OK' if success else 'FAIL', reason, duration)

            if self._queue:
                time.sleep(config.INTER_CALL_DELAY)

        with self._lock:
            self._status.update({'running': False, 'call_status': 'idle', 'current_pixel': None})

    # ── single pixel call ─────────────────────────────────────────────────────

    def _call_pixel(self, x: int, y: int, color_id: int) -> tuple[bool, str]:
        bridge = self._bridge
        dtmf_seq = f'#{x}#{y}#{color_id}*'
        log.info("Dialing %s for DTMF %s", config.HOTLINE_NUMBER, dtmf_seq)

        bridge.reset_call_state()

        # Dial
        bridge.send('dial', f'sip:{config.HOTLINE_NUMBER}@{config.SIP_SERVER}')
        with self._lock:
            self._status['call_status'] = 'ringing'

        # Wait for answer
        deadline = time.time() + config.CALL_TIMEOUT_S
        while time.time() < deadline:
            if self._stop_event.is_set():
                bridge.send('hangup')
                return False, 'stopped'
            state = bridge.get_call_state()
            if state == 'answered':
                break
            if state == 'ended':
                return False, 'call_ended_before_answer'
            time.sleep(0.1)
        else:
            bridge.send('hangup')
            return False, 'ringing_timeout'

        with self._lock:
            self._status['call_status'] = 'in_queue'

        # Drain stale VU readings from previous calls
        while True:
            try:
                bridge.vu_queue.get_nowait()
            except queue.Empty:
                break

        detector = AnnouncementDetector()
        audio_q = self.subscribe_audio()
        dtmf_sent = False

        try:
            t_answer = time.time()
            while time.time() - t_answer < config.CALL_TIMEOUT_S:
                if self._stop_event.is_set():
                    break
                if bridge.get_call_state() == 'ended':
                    break

                # Get next VU level; events arrive ~500 ms apart in baresip 1.0.0
                try:
                    vu_db = bridge.vu_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                # Convert VU dB → amplitude for synthetic PCM16 frames.
                # The detector uses energy (RMS), not actual waveform content.
                linear = min(1.0, 10.0 ** (vu_db / 20.0)) if vu_db > -80 else 0.0
                amplitude = int(linear * 32767)
                pcm = np.full(FRAME_SAMPLES, amplitude, dtype=np.int16).tobytes()

                # VU updates arrive ~500 ms apart; the detector expects 20 ms
                # frames.  Repeat each reading 25× so state-machine timing
                # (400 ms min audio, 360 ms min silence) stays proportional.
                triggered = False
                for _ in range(25):
                    self._broadcast_audio(pcm)
                    if detector.process_frame(pcm):
                        triggered = True
                        break

                if triggered:
                    with self._lock:
                        self._status['call_status'] = 'sending_dtmf'
                    log.info("Sending DTMF: %s  (triggered at VU %.1f dB)", dtmf_seq, vu_db)
                    for ch in dtmf_seq:
                        bridge.send('dtmf', ch)
                        time.sleep((config.DTMF_TONE_MS + config.DTMF_GAP_MS) / 1000)

                    with self._lock:
                        self._status['call_status'] = 'confirming'

                    t_dtmf = time.time()
                    while time.time() - t_dtmf < 5:
                        if bridge.get_call_state() == 'ended':
                            break
                        time.sleep(0.1)
                    dtmf_sent = True
                    break
        finally:
            self.unsubscribe_audio(audio_q)

        if bridge.get_call_state() != 'ended':
            bridge.send('hangup')
        time.sleep(0.5)

        return dtmf_sent, 'ok' if dtmf_sent else 'no_announcement_detected'

    # ── bridge lifecycle ──────────────────────────────────────────────────────

    def _ensure_bridge(self):
        with self._bridge_lock:
            if self._bridge is not None:
                return
            b = BaresipBridge()
            b.start(config.SIP_USER, config.SIP_PASS, config.SIP_SERVER)
            self._bridge = b

    def shutdown(self):
        self.stop()
        with self._bridge_lock:
            if self._bridge:
                self._bridge.stop()
                self._bridge = None

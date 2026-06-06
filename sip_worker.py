"""
SIP call worker — pure-Python SIP/RTP via opensip.

Architecture:
  SIPWorker  – thread-safe public API (start/stop/pause/resume)
               Runs a dedicated asyncio event loop in a background thread.
               Per-pixel: register once → invite → on_pcm callback feeds
               AnnouncementDetector → send_dtmf → hangup → repeat.

No subprocess.  No netstrings.  No VU-meter approximation.
Real decoded PCM16 frames arrive via call.on_pcm().
"""
import asyncio
import logging
import queue
import threading
import time
from typing import Optional

import config
from audio_detector import AnnouncementDetector, FRAME_SAMPLES

log = logging.getLogger(__name__)

FRAME_BYTES = FRAME_SAMPLES * 2  # 160 samples × 2 bytes = 320 bytes per 20 ms


class SIPWorker:

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

        self._queue: list[tuple[int, int, int]] = []
        self._history: list[dict] = []

        self._status = {
            'running': False, 'paused': False,
            'current_pixel': None, 'call_status': 'idle',
            'pixels_done': 0, 'pixels_failed': 0, 'pixels_pending': 0,
            'call_count': 0, 'last_error': None, 'started_at': None,
        }

        self._audio_subscribers: list[queue.Queue] = []
        self._audio_sub_lock = threading.Lock()

        # Thread-safe flags — set by Flask thread, checked by async loop
        self._stop_flag = threading.Event()
        self._pause_flag = threading.Event()

        self._loop: Optional[asyncio.AbstractEventLoop] = None

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
            self._stop_flag.clear()
            self._pause_flag.clear()
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_flag.set()
        with self._lock:
            self._status['running'] = False

    def pause(self):
        self._pause_flag.set()
        with self._lock:
            self._status['paused'] = True

    def resume(self):
        self._pause_flag.clear()
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
            for sub in self._audio_subscribers:
                try:
                    sub.put_nowait(pcm)
                except queue.Full:
                    dead.append(sub)
            for sub in dead:
                self._audio_subscribers.remove(sub)

    def shutdown(self):
        self.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)

    # ── asyncio worker ────────────────────────────────────────────────────────

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._async_run())
        except Exception as e:
            log.error("Worker loop crashed: %s", e, exc_info=True)
        finally:
            loop.close()
            self._loop = None
            with self._lock:
                self._status.update({
                    'running': False,
                    'call_status': 'idle',
                    'current_pixel': None,
                })

    async def _async_run(self):
        from opensip import UserAgent, Account

        ua = UserAgent(
            local_addr=("0.0.0.0", config.SIP_LOCAL_PORT),
            rtp_port_range=(config.RTP_PORT_LOW, config.RTP_PORT_HIGH),
        )
        await ua.start()

        acc = Account(
            username=config.SIP_USER,
            domain=config.SIP_SERVER,
            password=config.SIP_PASS,
            server=(config.SIP_SERVER, config.SIP_PORT),
        )

        log.info("Registering %s@%s …", config.SIP_USER, config.SIP_SERVER)
        try:
            await ua.register(acc)
            log.info("SIP registered OK")
        except Exception as e:
            log.error("SIP registration failed: %s", e)
            with self._lock:
                self._status.update({'running': False, 'last_error': str(e)})
            await ua.stop()
            return

        try:
            while not self._stop_flag.is_set():
                while self._pause_flag.is_set() and not self._stop_flag.is_set():
                    await asyncio.sleep(0.5)

                with self._lock:
                    if not self._queue:
                        self._status['running'] = False
                        break
                    x, y, cid = self._queue[0]
                    self._status['current_pixel'] = {'x': x, 'y': y, 'color_id': cid}
                    self._status['call_status'] = 'dialing'

                t0 = time.time()
                success, reason = await self._call_pixel(ua, acc, x, y, cid)
                duration = round(time.time() - t0, 1)

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

                self._history.append({
                    'x': x, 'y': y, 'color_id': cid,
                    'success': success, 'reason': reason,
                    'duration_s': duration, 'ts': time.time(),
                })
                if len(self._history) > 50:
                    self._history = self._history[-50:]

                log.info("Pixel (%d,%d) c%d → %s [%s] %.1fs",
                         x, y, cid, 'OK' if success else 'FAIL', reason, duration)

                if self._queue and not self._stop_flag.is_set():
                    await asyncio.sleep(config.INTER_CALL_DELAY)
        finally:
            await ua.stop()
            with self._lock:
                self._status.update({
                    'running': False,
                    'call_status': 'idle',
                    'current_pixel': None,
                })

    async def _call_pixel(self, ua, acc, x: int, y: int, color_id: int) -> tuple[bool, str]:
        dtmf_seq = f'#{x}#{y}#{color_id}*'
        target = f"sip:{config.HOTLINE_NUMBER}@{config.SIP_SERVER}"
        log.info("Dialing %s for DTMF %s", target, dtmf_seq)

        try:
            call = await ua.invite(acc, target)
        except Exception as e:
            log.warning("INVITE failed: %s", e)
            return False, f'invite_failed: {e}'

        with self._lock:
            self._status['call_status'] = 'ringing'

        try:
            await call.wait_answered(timeout=float(config.CALL_TIMEOUT_S))
        except Exception as e:
            log.warning("Call not answered (%d,%d): %s", x, y, e)
            try:
                await call.hangup()
            except Exception:
                pass
            return False, f'not_answered: {e}'

        log.info("Call answered (%d,%d)", x, y)
        with self._lock:
            self._status['call_status'] = 'in_queue'

        detector = AnnouncementDetector()
        triggered = asyncio.Event()

        def on_audio(pcm_bytes: bytes):
            self._broadcast_audio(pcm_bytes)
            if not triggered.is_set() and detector.process_frame(pcm_bytes):
                triggered.set()

        call.on_pcm(on_audio)

        try:
            await asyncio.wait_for(triggered.wait(), timeout=float(config.CALL_TIMEOUT_S))
        except asyncio.TimeoutError:
            log.warning("Announcement detection timed out for (%d,%d); sending DTMF anyway", x, y)

        # Keep on_audio registered so audio monitoring continues through DTMF and hangup.
        # process_frame() is a no-op once triggered, so only _broadcast_audio runs.

        with self._lock:
            self._status['call_status'] = 'sending_dtmf'

        log.info("Sending DTMF: %s  (%.1fs after answer)", dtmf_seq, detector.elapsed_s())
        try:
            await call.send_dtmf(
                dtmf_seq,
                duration_ms=config.DTMF_TONE_MS,
                gap_ms=config.DTMF_GAP_MS,
            )
        except Exception as e:
            log.warning("DTMF send failed: %s", e)
            try:
                await call.hangup()
            except Exception:
                pass
            return False, f'dtmf_failed: {e}'

        with self._lock:
            self._status['call_status'] = 'confirming'

        await asyncio.sleep(2.0)

        try:
            await call.hangup()
        except Exception as e:
            log.debug("hangup error: %s", e)

        return True, 'ok'

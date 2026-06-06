"""
Standalone SIP diagnostic — run this directly to test registration and one call
without starting the full Flask app.

  python test_sip.py

Writes full SIP trace to sip_debug.log.  Watch stdout for high-level results.
"""
import logging
import socket
import time

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)

# Route pyVoIP at DEBUG → file
fh = logging.FileHandler('sip_debug.log', encoding='utf-8')
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
logging.getLogger('pyVoIP').addHandler(fh)
logging.getLogger('pyVoIP').setLevel(logging.DEBUG)

log = logging.getLogger('test_sip')

import config

# Detect outbound IP
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.connect((config.SIP_SERVER, config.SIP_PORT))
local_ip = s.getsockname()[0]
s.close()
log.info("Local IP → SIP server: %s", local_ip)

try:
    from pyVoIP.VoIP import VoIPPhone, CallState
except ImportError:
    log.error("pyVoIP not installed. Run: pip install pyVoIP==1.6.8")
    raise SystemExit(1)

calls_seen = []

def on_call(call):
    log.info("Incoming call ignored, hanging up")
    try:
        call.hangup()
    except Exception:
        pass

log.info("Registering %s@%s:%d …", config.SIP_USER, config.SIP_SERVER, config.SIP_PORT)
phone = VoIPPhone(
    server=config.SIP_SERVER,
    port=config.SIP_PORT,
    username=config.SIP_USER,
    password=config.SIP_PASS,
    myIP=local_ip,
    callCallback=on_call,
    sipPort=config.SIP_LOCAL_PORT,
    rtpPortLow=config.RTP_PORT_LOW,
    rtpPortHigh=config.RTP_PORT_HIGH,
)
phone.start()
log.info("Registration sent — waiting 3 s …")
time.sleep(3)

log.info("Dialing %s …", config.HOTLINE_NUMBER)
call = phone.call(config.HOTLINE_NUMBER)

deadline = time.time() + 30
while time.time() < deadline:
    try:
        state = call.state
    except Exception as e:
        log.error("state error: %s", e)
        break

    log.info("Call state: %s", state)

    if state == CallState.ANSWERED:
        log.info("ANSWERED — listening for 5 s then hanging up")
        time.sleep(5)
        call.hangup()
        log.info("Hung up cleanly")
        break
    elif state == CallState.ENDED:
        log.error("Call ENDED before answer — check sip_debug.log for the SIP response")
        break

    time.sleep(1)
else:
    log.warning("Timeout waiting for answer")
    try:
        call.hangup()
    except Exception:
        pass

phone.stop()
log.info("Done — full SIP trace in sip_debug.log")

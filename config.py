import os

def _e(key, default):
    return os.environ.get(key, default)

SIP_SERVER       = _e('SIP_SERVER',   'sip.micropoc.de')
SIP_PORT         = int(_e('SIP_PORT', '5060'))
SIP_USER         = _e('SIP_USER',     '9379')
SIP_PASS         = _e('SIP_PASS',     '89783690294e')
SIP_LOCAL_PORT   = 5080
RTP_PORT_LOW     = 10000
RTP_PORT_HIGH    = 20000
HOTLINE_NUMBER   = _e('HOTLINE_NUMBER', '7321')

SIP_DEBUG        = True
SIP_DEBUG_LOG    = 'sip_debug.log'

CANVAS_URL       = 'https://pixeleb.be/at/gpn24/view.png'
CANVAS_WIDTH     = 160
CANVAS_HEIGHT    = 120
CANVAS_REFRESH_S = 10

INTER_CALL_DELAY = 5
CALL_TIMEOUT_S   = 120

DTMF_TONE_MS     = 120
DTMF_GAP_MS      = 80
DTMF_SAMPLE_RATE = 8000

ANNOUNCEMENT_TIMEOUT_S  = 90
WHISPER_MODEL           = 'tiny'
WHISPER_TRIGGER_KEYWORDS = ['willkommen']
DRAWING_FILE     = 'drawing.json'

PALETTE = [
    ('K1', '#000000', 1),  ('K2', '#222222', 2),  ('K3', '#444444', 3),  ('K4', '#666666', 4),
    ('W1', '#888888', 5),  ('W2', '#aaaaaa', 6),  ('W3', '#cccccc', 7),  ('W4', '#ffffff', 8),
    ('R1', '#880000', 9),  ('R2', '#aa2222', 10), ('R3', '#cc4444', 11), ('R4', '#ff6666', 12),
    ('G1', '#008800', 13), ('G2', '#22aa22', 14), ('G3', '#44cc44', 15), ('G4', '#66ff66', 16),
    ('B1', '#000088', 17), ('B2', '#2222aa', 18), ('B3', '#4444cc', 19), ('B4', '#6666ff', 20),
    ('C1', '#008888', 21), ('C2', '#22aaaa', 22), ('C3', '#44cccc', 23), ('C4', '#66ffff', 24),
    ('M1', '#880088', 25), ('M2', '#aa22aa', 26), ('M3', '#cc44cc', 27), ('M4', '#ff66ff', 28),
    ('Y1', '#888800', 29), ('Y2', '#aaaa22', 30), ('Y3', '#cccc44', 31), ('Y4', '#ffff66', 32),
]

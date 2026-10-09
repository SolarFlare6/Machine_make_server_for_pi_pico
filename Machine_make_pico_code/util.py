"""
util.py

Small MicroPython helpers that replace the CPython standard-library pieces
the reference (Raspberry Pi / desktop) Device Server relies on but which
do not exist on the Pico W:

    logging          -> log(), log_exc()
    secrets / uuid   -> random_hex(), random_code(), uuid4()
    hmac             -> hmac_sha256(), const_time_equal()
    os.path / json   -> file_exists(), ensure_dir(), load_json(), save_json()
    time.time()*1000 -> now_ms() (epoch-corrected), uptime_s()
"""

import sys
import os
import time
import binascii
import json

try:
    import hashlib
except ImportError:  # very old firmware
    import uhashlib as hashlib  # type: ignore

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
_log_level = 20


def set_log_level(name):
    global _log_level
    _log_level = _LEVELS.get(str(name).lower(), 20)


def log(tag, msg, level="info"):
    if _LEVELS.get(level, 20) < _log_level:
        return
    print("[%8.1f] %-7s %s: %s" % (uptime_s(), level.upper(), tag, msg))


def log_exc(tag, msg, exc):
    log(tag, "%s: %s" % (msg, exc), "error")
    try:
        sys.print_exception(exc)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

# Some MicroPython builds use a 2000-01-01 epoch; DCP timestamps are Unix ms.
try:
    _EPOCH_OFFSET = 946684800 if time.gmtime(0)[0] == 2000 else 0
except Exception:
    _EPOCH_OFFSET = 0

_boot_ticks = time.ticks_ms()
_last_ticks = _boot_ticks
_uptime_ms = 0


def now_s():
    return time.time() + _EPOCH_OFFSET


def now_ms():
    try:
        return time.time_ns() // 1000000 + _EPOCH_OFFSET * 1000
    except Exception:
        return now_s() * 1000


def uptime_s():
    """Monotonic uptime that survives the ~12 day ticks_ms() wrap, as long
    as it is called at least once every few days (the telemetry loop does)."""
    global _last_ticks, _uptime_ms
    t = time.ticks_ms()
    _uptime_ms += time.ticks_diff(t, _last_ticks)
    _last_ticks = t
    return _uptime_ms / 1000.0


# --------------------------------------------------------------------------
# Randomness / identifiers
# --------------------------------------------------------------------------

def random_bytes(n):
    return os.urandom(n)


def random_hex(nbytes):
    return binascii.hexlify(os.urandom(nbytes)).decode()


def random_code(digits=6):
    """Uniform-enough numeric code, e.g. 6-digit pairing PIN."""
    lo = 10 ** (digits - 1)
    span = 9 * lo
    return str(lo + int.from_bytes(os.urandom(4), "big") % span)


def uuid4():
    h = random_hex(16)
    return "%s-%s-4%s-%s-%s" % (h[0:8], h[8:12], h[13:16], h[16:20], h[20:32])


def unique_id_hex():
    try:
        import machine
        return binascii.hexlify(machine.unique_id()).decode()
    except Exception:
        return random_hex(8)


# --------------------------------------------------------------------------
# Crypto helpers (MicroPython has hashlib.sha256 but no hmac module)
# --------------------------------------------------------------------------

def hmac_sha256(key, msg):
    """RFC 2104 HMAC-SHA256. key/msg are bytes. Returns raw digest bytes."""
    block = 64
    if len(key) > block:
        key = hashlib.sha256(key).digest()
    k = bytearray(block)
    k[:len(key)] = key
    ipad = bytearray(block)
    opad = bytearray(block)
    for i in range(block):
        ipad[i] = k[i] ^ 0x36
        opad[i] = k[i] ^ 0x5C
    inner = hashlib.sha256(bytes(ipad) + msg).digest()
    return hashlib.sha256(bytes(opad) + inner).digest()


def hmac_sha256_hex(key, msg):
    return binascii.hexlify(hmac_sha256(key, msg)).decode()


def const_time_equal(a, b):
    """Constant-time comparison of two str/bytes values."""
    if isinstance(a, str):
        a = a.encode()
    if isinstance(b, str):
        b = b.encode()
    if len(a) != len(b):
        return False
    r = 0
    for i in range(len(a)):
        r |= a[i] ^ b[i]
    return r == 0


def sha1(data):
    """SHA-1 digest (needed only for the WebSocket handshake). Uses the
    firmware implementation when available, else a pure-Python fallback."""
    try:
        return hashlib.sha1(data).digest()
    except Exception:
        return _sha1_py(data)


def _rol(x, n):
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


def _sha1_py(data):
    import struct
    h0, h1, h2, h3, h4 = 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0
    ml = len(data) * 8
    data = bytes(data) + b"\x80"
    data += b"\x00" * ((56 - len(data) % 64) % 64)
    data += struct.pack(">Q", ml)
    for off in range(0, len(data), 64):
        w = list(struct.unpack(">16I", data[off:off + 64]))
        for i in range(16, 80):
            w.append(_rol(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1))
        a, b, c, d, e = h0, h1, h2, h3, h4
        for i in range(80):
            if i < 20:
                f = (b & c) | ((~b) & d)
                k = 0x5A827999
            elif i < 40:
                f = b ^ c ^ d
                k = 0x6ED9EBA1
            elif i < 60:
                f = (b & c) | (b & d) | (c & d)
                k = 0x8F1BBCDC
            else:
                f = b ^ c ^ d
                k = 0xCA62C1D6
            t = (_rol(a, 5) + (f & 0xFFFFFFFF) + e + k + w[i]) & 0xFFFFFFFF
            e, d, c, b, a = d, c, _rol(b, 30), a, t
        h0 = (h0 + a) & 0xFFFFFFFF
        h1 = (h1 + b) & 0xFFFFFFFF
        h2 = (h2 + c) & 0xFFFFFFFF
        h3 = (h3 + d) & 0xFFFFFFFF
        h4 = (h4 + e) & 0xFFFFFFFF
    return struct.pack(">5I", h0, h1, h2, h3, h4)


def b64encode(data):
    return binascii.b2a_base64(data).strip()


# --------------------------------------------------------------------------
# Filesystem / JSON persistence
# --------------------------------------------------------------------------

def file_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def ensure_dir(path):
    if not path:
        return
    cur = "/" if path.startswith("/") else ""
    for p in path.split("/"):
        if not p:
            continue
        cur = (cur + p) if cur in ("", "/") else (cur + "/" + p)
        if not file_exists(cur):
            try:
                os.mkdir(cur)
            except OSError:
                pass


def dirname(path):
    i = path.rfind("/")
    return path[:i] if i > 0 else ""


def load_json(path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        if file_exists(path):
            log("util", "could not parse %s (%s); using defaults" % (path, exc), "warning")
        return default


def save_json(path, obj):
    """Atomic-ish write: write tmp file then rename, so a power loss while
    writing never leaves a half-written trust store / identity file."""
    ensure_dir(dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    try:
        os.remove(path)
    except OSError:
        pass
    os.rename(tmp, path)


def deep_merge(base, override):
    """Returns a new dict: base recursively updated with override."""
    out = {}
    for k in base:
        out[k] = base[k]
    for k in override:
        v = override[k]
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def is_awaitable(obj):
    """MicroPython coroutines are generator objects."""
    return hasattr(obj, "send") and hasattr(obj, "throw")

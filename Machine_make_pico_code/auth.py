"""
auth.py

Authentication and trusted-device storage (Pico W port of the reference
auth.py). Same wire-compatible scheme:

  - Pairing (pairing.py) establishes a per-client shared secret and hands
    it to TrustStore.add_trusted().
  - Authentication is a nonce challenge-response over that secret:
        response = hex(HMAC-SHA256(bytes.fromhex(secret), nonce.encode()))
    so the existing mobile app / example_client work unchanged.
  - IP address, BLE MAC, hostname and device name are never used as
    authentication evidence.
"""

import binascii
import time
import util

TRUSTED_FILE = "/config/trusted_devices.json"


class TrustStore:
    """Persists trusted clients: client_id -> {secret, name, permission}."""

    def __init__(self, trusted_file=TRUSTED_FILE):
        self._trusted_file = trusted_file
        self._trusted = util.load_json(trusted_file, {}) or {}

    def _persist(self):
        util.save_json(self._trusted_file, self._trusted)

    def is_trusted(self, client_id):
        return client_id in self._trusted

    def add_trusted(self, client_id, name, permission="CONTROL"):
        secret = util.random_hex(32)
        self._trusted[client_id] = {"secret": secret, "name": name, "permission": permission}
        self._persist()
        return secret

    def revoke(self, client_id):
        existed = self._trusted.pop(client_id, None) is not None
        self._persist()
        return existed

    def set_permission(self, client_id, permission):
        entry = self._trusted.get(client_id)
        if entry is None:
            return False
        entry["permission"] = permission
        self._persist()
        return True

    def permission_for(self, client_id):
        entry = self._trusted.get(client_id)
        return entry["permission"] if entry else None

    def secret_for(self, client_id):
        entry = self._trusted.get(client_id)
        return entry["secret"] if entry else None

    def items(self):
        return self._trusted.items()

    def count(self):
        return len(self._trusted)


class Authenticator:
    """Runs the challenge-response handshake. A fresh nonce is created per
    attempt and is single-use, to prevent replay."""

    NONCE_TTL_MS = 30000

    def __init__(self, trust_store):
        self._trust_store = trust_store
        self._pending = {}  # client_id -> (nonce, issued_ticks)

    def create_challenge(self, client_id):
        if not self._trust_store.is_trusted(client_id):
            return None
        nonce = util.random_hex(16)
        self._pending[client_id] = (nonce, time.ticks_ms())
        return nonce

    def verify_response(self, client_id, response_hex):
        pending = self._pending.pop(client_id, None)
        if pending is None:
            return False
        nonce, issued = pending
        if time.ticks_diff(time.ticks_ms(), issued) > self.NONCE_TTL_MS:
            return False
        secret = self._trust_store.secret_for(client_id)
        if secret is None or not isinstance(response_hex, str):
            return False
        try:
            key = binascii.unhexlify(secret)
        except Exception:
            return False
        expected = util.hmac_sha256_hex(key, nonce.encode())
        return util.const_time_equal(expected, response_hex.lower())

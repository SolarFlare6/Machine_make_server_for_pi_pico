"""
pairing.py

PairingManager (Pico W port of the reference pairing.py).

Pairing establishes trust; it is distinct from authentication. Supported
methods (same DCP `request_pairing` / `pair` command as the reference):

  - "qr":   client presents a one-time pairing code. The Pico has no screen
            and no QR library, so `generate_qr_pairing()` prints the exact
            same JSON payload the reference encodes into its QR image on the
            serial console, prefixed with "QR_PAYLOAD:". Render it on your
            PC with host_tools/make_qr.py. Optionally a permanent
            `pairing.static_code` can be configured for headless pairing.
  - "wifi" / "bluetooth": interactive approval. Instead of a blocking
            input() prompt, a pending request is approved by either
              * typing  y / n  in the serial console, or
              * pressing the BOOTSEL button on the Pico W (approve),
            while the onboard LED blinks rapidly. Requests expire after
            `pairing.request_ttl_s` seconds (default 60).
"""

import asyncio
import json
import time
import util

BLE_DCP_SERVICE_UUID = "dcf00001-0000-1000-8000-00805f9b34fb"


class PairingRequest:
    def __init__(self, request_id, client_id, client_name, method):
        self.request_id = request_id
        self.client_id = client_id
        self.client_name = client_name
        self.method = method
        self.created = time.ticks_ms()
        self.decided = None
        self.decided_via = None
        self.event = asyncio.Event()


class PairingManager:
    def __init__(self, trust_store, identity=None, settings=None, host_provider=None, port=8765):
        self._trust_store = trust_store
        self._identity = identity
        self._settings = settings or {}
        self._host_provider = host_provider  # callable -> current IP string
        self._port = port
        self._requests = {}       # request_id -> PairingRequest (pending only)
        self._active_qr = {}      # code -> {client_name, expires_ticks, claimed_by}
        self._request_ttl_ms = int(self._settings.get("request_ttl_s", 60) * 1000)

    def set_identity(self, identity):
        self._identity = identity

    # -- QR pairing --------------------------------------------------------

    def _host(self):
        try:
            return self._host_provider() if self._host_provider else "0.0.0.0"
        except Exception:
            return "0.0.0.0"

    def _purge_qr(self):
        now = time.ticks_ms()
        for code in list(self._active_qr.keys()):
            if time.ticks_diff(self._active_qr[code]["expires"], now) <= 0:
                self._active_qr.pop(code, None)

    def qr_payload(self, code):
        host = self._host()
        device_id = self._identity.device_id if self._identity else "unknown"
        name = self._identity.name if self._identity else "Machine Make Device"
        profile = self._identity.profile if self._identity else "generic"
        # Identical field set to the reference server (Flutter PairingManager.parseQrPayload).
        return {
            "protocol": "DCP",
            "version": "1.0",
            "device_id": device_id,
            "name": name,
            "device_name": name,
            "profile": profile,
            "pairing": "qr",
            "action": "pair",
            "pairing_code": code,
            "host": host,
            "address": host,
            "ip": host,
            "port": self._port,
            "transport": "wifi",
            "ble_service_uuid": BLE_DCP_SERVICE_UUID,
        }

    def generate_qr_pairing(self, client_name="Mobile App", pairing_code=None, ttl_seconds=None,
                            display_terminal=True):
        self._purge_qr()
        ttl = int(ttl_seconds or self._settings.get("qr_ttl_s", 300))
        code = str(pairing_code).strip() if pairing_code else util.random_code(6)
        self._active_qr[code] = {
            "client_name": client_name,
            "expires": time.ticks_add(time.ticks_ms(), ttl * 1000),
            "claimed_by": None,
        }
        payload = self.qr_payload(code)
        payload_json = json.dumps(payload)

        if display_terminal:
            try:
                line = "=" * 50
                print("\n" + line)
                print("         MACHINE MAKE - DCP QR PAIRING")
                print(line)
                print("Device:       %s (%s)" % (payload["name"], payload["device_id"]))
                print("Profile:      %s" % payload["profile"])
                print("Pairing Code: %s" % code)
                print("Target:       %s:%s" % (payload["host"], self._port))
                print("BLE Service:  %s" % BLE_DCP_SERVICE_UUID)
                print("Expires In:   %d seconds" % ttl)
                print("-" * 50)
                print("Render the QR on your PC:  python host_tools/make_qr.py --port <COMx>")
                print("or paste the line below into  python host_tools/make_qr.py --json '<payload>'")
                print("QR_PAYLOAD:" + payload_json)
                print(line + "\n")
            except Exception:
                pass

        return {"approved": True, "pairing_code": code, "payload": payload,
                "payload_json": payload_json, "expires_in_s": ttl}

    async def verify_qr_pairing(self, client_id, client_name, pairing_code):
        """Idempotent: an already-trusted client gets its existing secret back
        (handles fast repeated camera scans)."""
        if self._trust_store.is_trusted(client_id):
            util.log("pairing", "client '%s' already trusted; returning existing credentials" % client_id)
            return {"approved": True, "secret": self._trust_store.secret_for(client_id),
                    "client_id": client_id, "device_id": self._device_id()}

        self._purge_qr()
        code = str(pairing_code).strip()
        static_code = self._settings.get("static_code")
        entry = self._active_qr.get(code)

        if entry is None and not (static_code and util.const_time_equal(str(static_code), code)):
            util.log("pairing", "QR pairing attempt with invalid/expired code", "warning")
            return {"approved": False, "reason": "invalid_pairing_code"}

        secret = self._trust_store.add_trusted(client_id, client_name, permission="CONTROL")
        util.log("pairing", "QR pairing successful for '%s' (%s)" % (client_id, client_name))
        if entry is not None:
            entry["claimed_by"] = client_id
            # keep a 60s grace period for camera retries
            grace = time.ticks_add(time.ticks_ms(), 60000)
            if time.ticks_diff(entry["expires"], grace) > 0:
                entry["expires"] = grace
        return {"approved": True, "secret": secret, "client_id": client_id, "device_id": self._device_id()}

    # -- Interactive (wifi / bluetooth) pairing ------------------------------

    def has_pending(self):
        self._expire_requests()
        return len(self._requests) > 0

    def pending_requests(self):
        self._expire_requests()
        return list(self._requests.values())

    def resolve_oldest(self, approved, via="console"):
        """Approves / denies the oldest pending request. Returns it or None."""
        pending = self.pending_requests()
        if not pending:
            return None
        pending.sort(key=lambda r: r.created)
        req = pending[0]
        req.decided = bool(approved)
        req.decided_via = via
        self._requests.pop(req.request_id, None)
        req.event.set()
        return req

    def _expire_requests(self):
        now = time.ticks_ms()
        for rid in list(self._requests.keys()):
            req = self._requests[rid]
            if time.ticks_diff(now, req.created) > self._request_ttl_ms:
                req.decided = False
                req.decided_via = "expired"
                self._requests.pop(rid, None)
                req.event.set()

    async def request_pairing(self, client_id, client_name, method):
        req = PairingRequest(util.random_code(6), client_id, client_name, method)
        self._requests[req.request_id] = req

        try:
            line = "=" * 32
            print("\n" + line)
            print("       DCP PAIRING REQUEST")
            print(line)
            print("Device: %s" % client_name)
            print("Method: %s" % method)
            print("Request ID: %s\n" % req.request_id)
            print("Allow this device?")
            print("  [Y] Yes  -> type 'y' + Enter%s" %
                  (" or press BOOTSEL" if self._settings.get("bootsel_confirm", True) else ""))
            print("  [N] No   -> type 'n' + Enter")
            print("Expires in %d s" % (self._request_ttl_ms // 1000))
            print(line + "\n")
        except Exception:
            pass

        try:
            await asyncio.wait_for(req.event.wait(), self._request_ttl_ms / 1000 + 1)
        except asyncio.TimeoutError:
            req.decided = False
            req.decided_via = "expired"
        self._requests.pop(req.request_id, None)

        if not req.decided:
            util.log("pairing", "pairing request %s denied (%s)" % (req.request_id, req.decided_via))
            return {"approved": False, "reason": "denied_or_expired"}

        secret = self._trust_store.add_trusted(client_id, client_name, permission="CONTROL")
        util.log("pairing", "pairing request %s approved via %s" % (req.request_id, req.decided_via))
        return {"approved": True, "secret": secret, "client_id": client_id, "device_id": self._device_id()}

    def _device_id(self):
        return self._identity.device_id if self._identity else None

"""
identity.py

Device Identity (Pico W port of the reference identity.py).

The device_id is the permanent logical identity of the device and must
remain stable across restarts. On the Pico W it defaults to one derived
from the RP2040/RP2350 flash unique ID, so it even survives a filesystem
wipe. Transport addresses (IP, BLE MAC) are never used as identity.
"""

import os
import util

IDENTITY_FILE = "/config/device_identity.json"
FIRMWARE_VERSION = "2.1.0-pico"


def _board_info():
    try:
        u = os.uname()
        return {"board": u.machine, "micropython": u.release, "sysname": u.sysname}
    except Exception:
        return {"board": "Raspberry Pi Pico W", "micropython": "?", "sysname": "rp2"}


class DeviceIdentity:
    def __init__(self, name, profile, device_type="device",
                 identity_file=IDENTITY_FILE, device_id=None):
        self._identity_file = identity_file
        self.name = name
        self.profile = profile
        self.device_type = device_type
        self.device_id = device_id
        self.server_key = None  # hex-encoded long-term secret
        self._load_or_create()

    def _load_or_create(self):
        data = util.load_json(self._identity_file, None)
        if data:
            stored_id = data.get("device_id")
            self.server_key = data.get("server_key") or util.random_hex(32)
            if self.device_id and self.device_id != stored_id:
                self._persist()  # custom device_id provided -> persist update
            else:
                self.device_id = stored_id
                if not data.get("server_key"):
                    self._persist()
        else:
            if not self.device_id:
                self.device_id = self._generate_device_id()
            self.server_key = util.random_hex(32)
            self._persist()

    def _generate_device_id(self):
        # e.g. "pico-w-e6614c31" -- stable: derived from the chip unique ID.
        return "pico-w-%s" % util.unique_id_hex()[-8:]

    def _persist(self):
        util.save_json(self._identity_file, {"device_id": self.device_id, "server_key": self.server_key})

    def device_info(self):
        """get_device_info payload (DCP spec section 12 + Needle AI metadata)."""
        info = {
            "device_id": self.device_id,
            "name": self.name,
            "type": self.device_type,
            "profile": self.profile,
            "platform": "raspberry_pi_pico_w",
            "firmware_version": FIRMWARE_VERSION,
            "supported_versions": ["1.0", "1.1"],
            "ai": {
                "needle": {
                    "supported": True,
                    "execution": ["device"],
                    "preferred": "device",
                }
            },
        }
        info.update(_board_info())
        return info

    def advertisement_info(self):
        """Minimal public info -- never includes server_key or other secrets."""
        return {
            "device_id": self.device_id,
            "name": self.name,
            "profile": self.profile,
            "protocol": "DCP",
            "protocol_version": "1.0",
        }

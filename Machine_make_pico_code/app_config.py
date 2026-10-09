"""
app_config.py

Configuration Manager: Physical Configuration (Pico W port of the
reference device_server/config.py).

The Pico W hardware is fixed and known, so instead of an auto-detect /
setup-wizard step, this module carries a complete built-in default
configuration. /config/device_config.json is optional and only needs to
contain the keys you want to override (it is deep-merged over DEFAULTS).

Wi-Fi credentials live in a separate file, /config/wifi.json, so the
device config can be shared / committed without leaking secrets.
"""

import util

CONFIG_FILE = "/config/device_config.json"
WIFI_FILE = "/config/wifi.json"

DEFAULTS = {
    "identity": {
        "name": "MachineMake Pico W",
        "profile": "microcontroller",
        "type": "device",
        # "device_id": "pico-w-01",   # optional; default derives from the chip's unique ID
    },
    "configuration": {
        # GPIO pins reserved for wiring you add later (never touched by
        # gpio_write / pwm_set / servo tools), e.g. [4, 5].
        "reserved_pins": [],
        # Friendly labels shown in the gpio capability, e.g. {"15": "Relay"}.
        "pin_labels": {},
        "i2c": {
            "scan_on_boot": True,
            # Both RP2040/RP2350 hardware I2C controllers on common pins.
            # Change to match your wiring if you attach I2C devices.
            "buses": [
                {"id": 0, "sda": 4, "scl": 5, "freq": 100000},
                {"id": 1, "sda": 6, "scl": 7, "freq": 100000},
            ],
            # When devices are found on a bus, reserve its SDA/SCL pins so
            # GPIO tools cannot accidentally reconfigure them.
            "reserve_pins_when_devices_found": True,
        },
        "servo": {"min_pulse_us": 500, "max_pulse_us": 2500, "freq_hz": 50},
    },
    "safety_limits": {
        "pwm_min_freq_hz": 10,
        "pwm_max_freq_hz": 100000,
        "max_sleep_s": 86400,
    },
    "connection_loss_policy": {
        "interactive_control": "safe_stop",   # safe_stop | none
        "autonomous_tasks": "continue",
    },
    "transports": {
        "wifi": {"enabled": True, "port": 8765, "max_clients": 3, "max_message_bytes": 16384},
        "ble": {"enabled": True, "mtu": 512, "max_connections": 1, "adv_interval_ms": 250},
    },
    "pairing": {
        "qr_ttl_s": 300,
        "request_ttl_s": 60,
        # Print a QR pairing payload on boot when nothing is paired yet.
        "auto_qr_on_boot": True,
        # Optional permanent pairing code (e.g. printed on a sticker) for
        # headless pairing without a serial terminal. null = disabled.
        "static_code": None,
        # Allow pressing the BOOTSEL button to approve a pending pairing request.
        "bootsel_confirm": True,
    },
    "security": {
        # True = same behaviour as the reference server / mobile app:
        # 'auth', 'execute' and 'request_control' implicitly authenticate a
        # session with CONTROL permission. Set False to REQUIRE pairing +
        # HMAC challenge-response before any tool can run.
        "implicit_auth": True,
    },
    "telemetry": {"interval_s": 2.0},
    "system": {
        "log_level": "info",
        "ntp_sync": True,
        "auto_restart_on_crash": True,
        "crash_restart_delay_s": 10,
        "console": True,
    },
}

WIFI_DEFAULTS = {
    "networks": [],              # [{"ssid": "...", "password": "..."}]
    "country": "DE",
    "hostname": "machinemake-pico",
    "connect_timeout_s": 15,
    "power_save": False,
    "ap_fallback": True,
    "ap_ssid": "MachineMake-Pico",
    "ap_password": "machinemake",  # >= 8 chars
}


class ConfigurationManager:
    def __init__(self, config_file=CONFIG_FILE, wifi_file=WIFI_FILE):
        self._config_file = config_file
        self._wifi_file = wifi_file
        self._overrides = {}
        self._config = {}
        self._wifi = {}
        self.reload()

    def reload(self):
        self._overrides = util.load_json(self._config_file, {}) or {}
        self._config = util.deep_merge(DEFAULTS, self._overrides)
        self._wifi = util.deep_merge(WIFI_DEFAULTS, util.load_json(self._wifi_file, {}) or {})

    def save(self):
        """Persists only the user overrides, not the full default tree."""
        util.save_json(self._config_file, self._overrides)

    # -- sections ----------------------------------------------------------

    @property
    def identity(self):
        return self._config.get("identity", {})

    @property
    def physical_configuration(self):
        return self._config.get("configuration", {})

    @property
    def safety_limits(self):
        return self._config.get("safety_limits", {})

    @property
    def connection_loss_policy(self):
        return self._config.get("connection_loss_policy", {})

    @property
    def transports(self):
        return self._config.get("transports", {})

    @property
    def pairing(self):
        return self._config.get("pairing", {})

    @property
    def security(self):
        return self._config.get("security", {})

    @property
    def telemetry(self):
        return self._config.get("telemetry", {})

    @property
    def system(self):
        return self._config.get("system", {})

    @property
    def wifi(self):
        return self._wifi

    def update_configuration(self, patch):
        """Handler for the `update_configuration` DCP command (ADMIN only,
        enforced by the DCP handler). Takes effect after reboot for most keys."""
        cfg = self._overrides.setdefault("configuration", {})
        for k in patch:
            cfg[k] = patch[k]
        self._config = util.deep_merge(DEFAULTS, self._overrides)
        self.save()

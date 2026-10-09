"""
hardware_mapper.py

Hardware Mapper for Raspberry Pi Pico W.
Per the user request:
"note there is no need to have full hardware scanning on the pi pico w because
the hardware is known, scanning the i2c bus if possible for connected devices."

This module produces a standardized hardware map matching DCP-HW-1.0 schema,
scans configured I2C buses using machine.I2C, and provides bus rescan tools.
"""

import machine
import os
import util

_KNOWN_I2C_HINTS = {
    0x40: ["PCA9685"],
    0x68: ["MPU6050", "DS3231"],
    0x69: ["MPU6050 (AD0 high)"],
    0x76: ["BMP280", "BME280"],
    0x77: ["BMP280", "BME280"],
    0x3C: ["SSD1306 OLED"],
    0x3D: ["SSD1306 OLED"],
    0x27: ["PCF8574 LCD backpack"],
}


class HardwareMapper:
    def __init__(self, physical_config=None):
        self._cfg = physical_config or {}
        self._last_map = None
        self._active_buses = {}  # bus_id -> machine.I2C

    @property
    def last_map(self):
        return self._last_map

    def get_i2c_bus(self, bus_id):
        return self._active_buses.get(bus_id)

    def scan_i2c(self):
        """Scans the configured I2C buses on the Pico W."""
        i2c_cfg = self._cfg.get("i2c", {})
        bus_configs = i2c_cfg.get("buses", [
            {"id": 0, "sda": 4, "scl": 5, "freq": 100000},
            {"id": 1, "sda": 6, "scl": 7, "freq": 100000},
        ])

        results = []
        for b_cfg in bus_configs:
            bid = b_cfg.get("id", 0)
            sda_pin = b_cfg.get("sda", 4)
            scl_pin = b_cfg.get("scl", 5)
            freq = b_cfg.get("freq", 100000)

            bus_res = {
                "bus": bid,
                "sda": sda_pin,
                "scl": scl_pin,
                "freq": freq,
                "devices": [],
                "error": None,
            }

            try:
                # Initialize machine.I2C
                bus = machine.I2C(
                    bid,
                    sda=machine.Pin(sda_pin),
                    scl=machine.Pin(scl_pin),
                    freq=freq
                )
                self._active_buses[bid] = bus

                # Scan responds with list of integer 7-bit addresses
                addrs = bus.scan()
                for a in addrs:
                    hints = _KNOWN_I2C_HINTS.get(a, [])
                    bus_res["devices"].append({
                        "address": "0x{:02x}".format(a),
                        "candidates": hints,
                    })
                util.log("hw_mapper", "I2C bus %d (SDA=%d, SCL=%d): found %d device(s)"
                         % (bid, sda_pin, scl_pin, len(addrs)))
            except Exception as exc:
                util.log("hw_mapper", "I2C bus %d error: %s" % (bid, exc), "warning")
                bus_res["error"] = str(exc)

            results.append(bus_res)
        return results

    def rescan_i2c(self):
        """Forces rescan of I2C and updates last_map."""
        i2c_scan = self.scan_i2c()
        if self._last_map is not None:
            self._last_map["devices"]["i2c"] = i2c_scan
        return i2c_scan

    def scan(self):
        """Constructs full standardized hardware map."""
        try:
            uname = os.uname()
            board = uname.machine
            sysname = uname.sysname
            release = uname.release
        except Exception:
            board = "Raspberry Pi Pico W"
            sysname = "rp2"
            release = "?"

        chip = "RP2350" if "RP2350" in board else "RP2040"

        # I2C scanning
        i2c_cfg = self._cfg.get("i2c", {})
        if i2c_cfg.get("scan_on_boot", True):
            i2c_devices = self.scan_i2c()
        else:
            i2c_devices = []

        hw_map = {
            "schema": "DCP-HW-1.0",
            "device": {
                "platform": "raspberry_pi_pico_w",
                "architecture": chip.lower(),
                "os": "micropython",
                "board": board,
                "sysname": sysname,
                "release": release,
            },
            "system": {
                "cpu": {
                    "cores": 2,
                    "model": chip,
                    "freq_mhz": machine.freq() // 1000000,
                },
                "memory_kb": {
                    "total": 264 if chip == "RP2040" else 520,
                },
                "storage": [{
                    "mount": "/",
                    "device": "internal_flash",
                    "fstype": "littlefs",
                }],
            },
            "interfaces": {
                "gpio": True,
                "i2c": True,
                "spi": True,
                "uart": True,
                "usb": True,
                "wifi": True,
                "bluetooth": True,
            },
            "devices": {
                "i2c": i2c_devices,
                "usb": [{"type": "cdc_acm_serial", "name": "USB REPL"}],
                "cameras": [],
                "audio": {"input": [], "output": []},
                "onboard": [
                    {"name": "status_led", "type": "led", "pin": "WL_GPIO0 (LED)"},
                    {"name": "temp_sensor", "type": "temperature", "pin": "ADC_CORE_TEMP"},
                    {"name": "vbus_sense", "type": "power_sense", "pin": "WL_GPIO2"},
                    {"name": "bootsel_button", "type": "button", "pin": "BOOTSEL"},
                ],
            },
            "detector_errors": {},
        }

        self._last_map = hw_map
        return hw_map

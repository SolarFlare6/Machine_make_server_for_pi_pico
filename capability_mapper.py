"""
capability_mapper.py

Capability Mapper (Pico W port).
Maps hardware map and physical configuration to DCP capabilities
(matching MachineMake specs, Section 2).
"""


class CapabilityMapper:
    def __init__(self, pico_controller=None):
        self._controller = pico_controller

    def build(self, hardware_map, physical_config):
        capabilities = []
        interfaces = hardware_map.get("interfaces", {})

        # 1. Onboard LED / Lighting
        capabilities.append({
            "id": "lighting",
            "type": "led",
            "name": "Onboard Status LED",
            "description": "RP2040/RP2350 CYW43 onboard status LED with animation patterns",
            "params": {"pin": "LED", "patterns": ["blink_oke", "blink_warning", "flash_alert", "running"]},
            "enabled": True,
        })
        capabilities.append("led")

        # 2. GPIO Header
        if interfaces.get("gpio", True):
            allowed_pins = self._controller.allowed_pins if self._controller else list(range(0, 23)) + [26, 27, 28]
            capabilities.append({
                "id": "gpio",
                "type": "gpio",
                "name": "Raspberry Pi Pico W GPIO Header",
                "description": "3.3V digital I/O header with PWM and ADC support",
                "params": {
                    "pins": allowed_pins,
                    "adc_pins": [26, 27, 28],
                },
                "enabled": True,
            })
            capabilities.append("gpio")

        # 3. PWM / Servo
        capabilities.append({
            "id": "pwm",
            "type": "pwm",
            "name": "Hardware PWM Controllers",
            "description": "8-slice RP2040/RP2350 hardware PWM generator and servo driver",
            "params": {
                "freq_min": 10,
                "freq_max": 100000,
            },
            "enabled": True,
        })
        capabilities.append("servo")

        # 4. ADC
        capabilities.append({
            "id": "adc",
            "type": "adc",
            "name": "12-bit Analog-to-Digital Converter",
            "description": "3 external ADC channels (GP26-28) + internal temperature sensor",
            "params": {"channels": [26, 27, 28], "internal_temp": True},
            "enabled": True,
        })

        # 5. I2C Bus
        if interfaces.get("i2c", True):
            capabilities.append({
                "id": "i2c",
                "type": "i2c",
                "name": "RP2040 Hardware I2C Controller",
                "description": "Dual I2C controller bus scanner and peripheral interface",
                "params": {"buses": [0, 1]},
                "enabled": True,
            })
            capabilities.append("i2c")

        # 6. Telemetry & Power
        capabilities.append({
            "id": "telemetry",
            "type": "telemetry",
            "name": "Microcontroller Telemetry",
            "description": "CPU frequency, core temperature, RAM usage, flash storage, and VBUS power",
            "params": {},
            "enabled": True,
        })

        capabilities.append({
            "id": "power",
            "type": "power",
            "name": "System Power Management",
            "description": "Software reset, deep sleep, and bootloader trigger",
            "params": {"actions": ["reboot", "shutdown", "sleep", "bootloader"]},
            "enabled": True,
        })

        # Standard keyword aliases
        capabilities += [
            "storage", "network", "needle_ai", "ai",
            "emergency_stop", "config", "reboot", "shutdown"
        ]

        # Deduplicate
        unique = []
        seen = set()
        for item in capabilities:
            key = item.get("id", item.get("type", "")) if isinstance(item, dict) else str(item)
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

"""
pico_controller.py

Hardware controller for a bare Raspberry Pi Pico W / Pico 2 W
(the Pico counterpart of the reference robot_controller.py).

Onboard hardware driven here:
  - user LED            Pin("LED")        (wired to the CYW43 radio, WL_GPIO0)
  - VBUS sense          Pin("WL_GPIO2")   (1 = powered from USB)
  - core temperature    ADC channel 4 (RP2040) / ADC.CORE_TEMP
  - BOOTSEL button      rp2.bootsel_button()
  - GPIO / PWM / ADC on the header pins (GP0-22, GP26-28)
  - hardware I2C controllers 0 and 1 (via HardwareMapper's bus config)

The CYW43 radio pins GP23, GP24, GP25 and GP29 are never exposed.
"""

import asyncio
import gc
import os
import re
import time
import machine
from machine import Pin, PWM, ADC

import util

try:
    import rp2
except ImportError:  # running on a non-rp2 port
    rp2 = None

USER_GPIO = list(range(0, 23)) + [26, 27, 28]
ADC_PINS = (26, 27, 28)
RADIO_PINS = {"23": "WL_ON (CYW43 power)", "24": "WL_D (CYW43 SPI data/IRQ)",
              "25": "WL_CS (CYW43 SPI CS)", "29": "WL_CLK (CYW43 SPI clk) / VSYS ADC3"}

# LED patterns: list of (led_on, duration_ms); "loop" patterns repeat until stopped.
_PATTERNS = {
    "blink_oke": ([(1, 100), (0, 100), (1, 100), (0, 300)], False),
    "blink_warning": ([(1, 400), (0, 300)] * 3, False),
    "flash_alert": ([(1, 50), (0, 50)] * 10, False),
    "running": ([(1, 80), (0, 120), (1, 80), (0, 720)], True),
    "pairing": ([(1, 60), (0, 60)], True),
    "boot": ([(1, 30), (0, 70)] * 5, False),
    "error": ([(1, 600), (0, 200), (1, 100), (0, 200)], True),
}


class PicoController:
    def __init__(self, physical_config, safety_limits=None):
        self._cfg = physical_config or {}
        self._limits = safety_limits or {}
        self.board = self._board_name()
        self.chip = "RP2350" if "RP2350" in self.board else "RP2040"

        reserved = set()
        for p in self._cfg.get("reserved_pins", []) or []:
            try:
                reserved.add(int(p))
            except (TypeError, ValueError):
                pass
        self._reserved = reserved

        try:
            self._led = Pin("LED", Pin.OUT)
        except Exception:
            self._led = None
        self.led_state = False
        self._set_led_hw(False)
        self._anim_task = None
        self._anim_name = None

        try:
            self._vbus = Pin("WL_GPIO2", Pin.IN)
        except Exception:
            self._vbus = None

        try:
            self._temp_adc = ADC(getattr(ADC, "CORE_TEMP", 4))
        except Exception:
            self._temp_adc = None

        self._outputs = {}      # pin -> bool (pins we drive as digital outputs)
        self._pwms = {}         # pin -> {"pwm": PWM, "freq": int, "duty": float}
        self._servos = {}       # pin -> angle
        self._adcs = {}
        self.dynamic_config = {}
        self.hardware_mapper = None  # set by main (for i2c tools / needle)
        self.wifi = None             # set by main (for stats)
        self._shutdown_hooks = []

    # ------------------------------------------------------------------
    # Board / pin helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _board_name():
        try:
            return os.uname().machine
        except Exception:
            return "Raspberry Pi Pico W"

    @property
    def allowed_pins(self):
        return [p for p in USER_GPIO if p not in self._reserved]

    def reserve_pins(self, pins, reason="reserved"):
        for p in pins:
            self._reserved.add(int(p))
        util.log("controller", "reserved pins %s (%s)" % (sorted(pins), reason))

    @property
    def reserved_pins(self):
        return sorted(self._reserved)

    def _check_pin(self, pin):
        if pin is None:
            raise ValueError("pin is required")
        try:
            pin = int(pin)
        except (TypeError, ValueError):
            raise ValueError("pin must be an integer")
        if str(pin) in RADIO_PINS:
            raise ValueError("GP%d is used by the CYW43 wireless chip" % pin)
        if pin not in USER_GPIO:
            raise ValueError("GP%d is not a user GPIO pin" % pin)
        if pin in self._reserved:
            raise ValueError("GP%d is reserved" % pin)
        return pin

    @staticmethod
    def pick_pin(kw):
        for key in ("pin", "channel", "index", "gpio"):
            if kw.get(key) is not None:
                return kw.get(key)
        return None

    def on_shutdown(self, fn):
        self._shutdown_hooks.append(fn)

    # ------------------------------------------------------------------
    # Onboard LED + patterns
    # ------------------------------------------------------------------

    def _set_led_hw(self, on):
        if self._led is not None:
            try:
                self._led.value(1 if on else 0)
            except Exception:
                pass

    def set_led(self, on=True, **kw):
        self.stop_animation(restore=False)
        self.led_state = bool(on)
        self._set_led_hw(self.led_state)
        return {"status": "success", "led_state": self.led_state}

    def toggle_led(self, **kw):
        return self.set_led(not self.led_state)

    def get_led(self, **kw):
        return {"led_state": self.led_state, "animation": self._anim_name}

    def start_pattern(self, name):
        if name not in _PATTERNS:
            raise ValueError("unknown pattern '%s'" % name)
        if self._anim_name == name and self._anim_task is not None:
            return
        self.stop_animation(restore=False)
        self._anim_name = name
        self._anim_task = asyncio.create_task(self._run_pattern(name))

    def stop_animation(self, restore=True, **kw):
        if self._anim_task is not None:
            try:
                self._anim_task.cancel()
            except Exception:
                pass
        self._anim_task = None
        self._anim_name = None
        if restore:
            self._set_led_hw(self.led_state)
        return {"status": "stopped", "led_state": self.led_state}

    @property
    def animation(self):
        return self._anim_name

    async def _run_pattern(self, name):
        steps, loop = _PATTERNS[name]
        try:
            while True:
                for on, ms in steps:
                    self._set_led_hw(on)
                    await asyncio.sleep_ms(ms)
                if not loop:
                    break
        except asyncio.CancelledError:
            return
        self._anim_task = None
        self._anim_name = None
        self._set_led_hw(self.led_state)

    def blink_oke(self, **kw):
        self.start_pattern("blink_oke")
        return {"status": "ok", "animation": "blink_oke"}

    def blink_warning(self, **kw):
        self.start_pattern("blink_warning")
        return {"status": "warning", "animation": "blink_warning"}

    def flash_alert(self, **kw):
        self.start_pattern("flash_alert")
        return {"status": "alert", "animation": "flash_alert"}

    def animate_running_process(self, **kw):
        self.start_pattern("running")
        return {"status": "running", "animation": "running_process"}

    async def blink_led(self, times=5, interval_ms=250, progress_cb=None, **kw):
        """Long-running tool: blinks `times` times, reporting progress."""
        times = max(1, min(100, int(times)))
        interval_ms = max(20, min(5000, int(interval_ms)))
        self.stop_animation(restore=False)
        for i in range(times):
            self._set_led_hw(True)
            await asyncio.sleep_ms(interval_ms)
            self._set_led_hw(False)
            await asyncio.sleep_ms(interval_ms)
            if progress_cb:
                progress_cb((i + 1) / times)
        self._set_led_hw(self.led_state)
        return {"status": "completed", "times": times, "interval_ms": interval_ms}

    # ------------------------------------------------------------------
    # Sensors / power
    # ------------------------------------------------------------------

    def read_temperature_c(self):
        if self._temp_adc is None:
            try:
                self._temp_adc = ADC(getattr(ADC, "CORE_TEMP", 4))
            except Exception:
                return None
        try:
            raw = 0
            for _ in range(8):
                raw += self._temp_adc.read_u16()
            volts = (raw / 8) * 3.3 / 65535
            return round(27 - (volts - 0.706) / 0.001721, 2)
        except Exception:
            return None

    def get_temperature(self, **kw):
        c = self.read_temperature_c()
        f = round(c * 9.0 / 5.0 + 32.0, 2) if c is not None else None
        return {
            "temperature_c": c,
            "temperature_f": f,
            "sensor": "%s internal" % self.chip,
            "status": "success",
        }

    def usb_powered(self):
        if self._vbus is None:
            return None
        try:
            return bool(self._vbus.value())
        except Exception:
            return None

    def get_battery(self, **kw):
        usb = self.usb_powered()
        return {
            "available": False,
            "power_source": "usb" if usb else ("vsys_external" if usb is not None else "unknown"),
            "usb_powered": usb,
            "charging": False,
            "battery": None,
            "voltage": None,
            "percentage": None,
            "level": None,
            "note": "Bare Pico W has no battery gauge; VSYS ADC shares GP29 with the radio.",
        }

    def bootsel_pressed(self):
        if rp2 is None or not hasattr(rp2, "bootsel_button"):
            return False
        try:
            return bool(rp2.bootsel_button())
        except Exception:
            return False

    def get_bootsel_button(self, **kw):
        return {"pressed": self.bootsel_pressed()}

    def memory_stats(self):
        gc.collect()
        free = gc.mem_free()
        alloc = gc.mem_alloc()
        total = free + alloc
        return {"free_kb": round(free / 1024, 1), "used_kb": round(alloc / 1024, 1),
                "total_kb": round(total / 1024, 1),
                "percent": round(alloc * 100 / total, 1) if total else 0.0}

    def storage_stats(self):
        try:
            st = os.statvfs("/")
            total = st[0] * st[2]
            free = st[0] * st[3]
            return {"mount": "/", "device": "internal_flash", "fstype": "littlefs",
                    "total_kb": total // 1024, "free_kb": free // 1024,
                    "used_kb": (total - free) // 1024,
                    "total_mb": round(total / 1048576, 2), "free_mb": round(free / 1048576, 2),
                    "used_mb": round((total - free) / 1048576, 2),
                    "percent": round((total - free) * 100 / total, 1) if total else 0.0}
        except Exception as exc:
            return {"mount": "/", "error": str(exc)}

    def get_storage(self, **kw):
        return {"storage": [self.storage_stats()]}

    def get_system_stats(self, **kw):
        res = {
            "uptime_s": round(util.uptime_s(), 1),
            "cpu_freq_mhz": machine.freq() // 1000000,
            "memory": self.memory_stats(),
            "temperature_c": self.read_temperature_c(),
            "usb_powered": self.usb_powered(),
            "led_state": self.led_state,
            "active_pwm_pins": sorted(self._pwms.keys()),
            "output_pins": sorted(self._outputs.keys()),
        }
        if self.wifi is not None:
            res["wifi"] = self.wifi.status()
        return res

    def get_hardware_info(self, **kw):
        try:
            u = os.uname()
            release, version = u.release, u.version
        except Exception:
            release, version = "?", "?"
        mem = self.memory_stats()
        res = {
            "os": "micropython",
            "release": release,
            "version": version,
            "architecture": self.chip.lower(),
            "board": self.board,
            "hostname": self.wifi.hostname if self.wifi is not None else None,
            "unique_id": util.unique_id_hex(),
            "cpu": {"cores": 2, "model": self.chip + (" (Cortex-M33)" if self.chip == "RP2350" else " (Cortex-M0+)"),
                    "freq_mhz": machine.freq() // 1000000},
            "memory": {"total_kb": mem["total_kb"], "available_kb": mem["free_kb"],
                       "used_kb": mem["used_kb"], "percent": mem["percent"]},
            "storage": self.storage_stats(),
            "battery": self.get_battery(),
            "temperature_c": self.read_temperature_c(),
            "reset_cause": self._reset_cause(),
        }
        return res

    @staticmethod
    def _reset_cause():
        try:
            c = machine.reset_cause()
            names = {getattr(machine, "PWRON_RESET", -1): "power_on",
                     getattr(machine, "WDT_RESET", -2): "watchdog",
                     getattr(machine, "HARD_RESET", -3): "hard_reset",
                     getattr(machine, "SOFT_RESET", -4): "soft_reset",
                     getattr(machine, "DEEPSLEEP_RESET", -5): "deepsleep"}
            return names.get(c, str(c))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # GPIO / PWM / servo / ADC
    # ------------------------------------------------------------------

    def _release_pwm(self, pin):
        entry = self._pwms.pop(pin, None)
        self._servos.pop(pin, None)
        if entry is not None:
            try:
                entry["pwm"].deinit()
            except Exception:
                pass

    def gpio_write(self, pin=None, state=False, **kw):
        pin = self._check_pin(pin if pin is not None else self.pick_pin(kw))
        self._release_pwm(pin)
        state = bool(state)
        Pin(pin, Pin.OUT, value=1 if state else 0)
        self._outputs[pin] = state
        return {"pin": pin, "state": state, "status": "success"}

    def gpio_read(self, pin=None, pull=None, **kw):
        pin = pin if pin is not None else self.pick_pin(kw)
        try:
            pin = int(pin)
        except (TypeError, ValueError):
            raise ValueError("pin must be an integer")
        if pin not in USER_GPIO:
            raise ValueError("GP%d is not a user GPIO pin" % pin)
        if pin in self._outputs:
            # Don't reconfigure a pin we're driving; report the driven value.
            return {"pin": pin, "state": bool(Pin(pin).value()), "mode": "output"}
        if pin in self._pwms:
            return {"pin": pin, "state": None, "mode": "pwm", "duty": self._pwms[pin]["duty"]}
        if pin in self._reserved:
            return {"pin": pin, "state": bool(Pin(pin).value()), "mode": "reserved"}
        pulls = {"up": Pin.PULL_UP, "down": Pin.PULL_DOWN}
        p = Pin(pin, Pin.IN, pulls.get(str(pull).lower()) if pull else None)
        return {"pin": pin, "state": bool(p.value()), "mode": "input", "pull": pull}

    def pwm_set(self, value=0.0, freq=None, **kw):
        pin = self._check_pin(self.pick_pin(kw))
        duty = max(0.0, min(1.0, float(value)))
        f = int(freq) if freq is not None else (self._pwms.get(pin, {}).get("freq") or 1000)
        lo = int(self._limits.get("pwm_min_freq_hz", 10))
        hi = int(self._limits.get("pwm_max_freq_hz", 100000))
        if f < lo or f > hi:
            raise ValueError("freq must be between %d and %d Hz" % (lo, hi))
        self._outputs.pop(pin, None)
        self._servos.pop(pin, None)
        entry = self._pwms.get(pin)
        if entry is None:
            entry = {"pwm": PWM(Pin(pin))}
            self._pwms[pin] = entry
        entry["pwm"].freq(f)
        entry["pwm"].duty_u16(int(duty * 65535))
        entry["freq"] = f
        entry["duty"] = duty
        return {"channel": pin, "pin": pin, "value": duty, "freq": f, "status": "success"}

    def pwm_stop(self, **kw):
        pin = self._check_pin(self.pick_pin(kw))
        self._release_pwm(pin)
        Pin(pin, Pin.IN)
        return {"pin": pin, "status": "stopped"}

    def set_servo_pulse(self, pulse_us=1500, **kw):
        pin = self._check_pin(self.pick_pin(kw))
        scfg = self._cfg.get("servo", {})
        lo = int(scfg.get("min_pulse_us", 500))
        hi = int(scfg.get("max_pulse_us", 2500))
        f = int(scfg.get("freq_hz", 50))
        p_us = max(lo, min(hi, int(pulse_us)))
        self._outputs.pop(pin, None)
        entry = self._pwms.get(pin)
        if entry is None:
            entry = {"pwm": PWM(Pin(pin))}
            self._pwms[pin] = entry
        entry["pwm"].freq(f)
        try:
            entry["pwm"].duty_ns(p_us * 1000)
        except (AttributeError, Exception):
            entry["pwm"].duty_u16(int((p_us / 20000.0) * 65535))
        entry["freq"] = f
        entry["duty"] = p_us * f / 1000000
        angle = round((p_us - lo) * 180.0 / (hi - lo), 1)
        self._servos[pin] = angle
        return {"pin": pin, "index": pin, "pulse_us": p_us, "angle": angle, "status": "success"}

    def set_servo_angle(self, angle=90, **kw):
        pin = self._check_pin(self.pick_pin(kw))
        scfg = self._cfg.get("servo", {})
        lo = int(scfg.get("min_pulse_us", 500))
        hi = int(scfg.get("max_pulse_us", 2500))
        a = max(0.0, min(180.0, float(angle)))
        res = self.set_servo_pulse(pulse_us=int(lo + (hi - lo) * a / 180.0), pin=pin)
        res["angle"] = a
        self._servos[res["pin"]] = a
        return res

    def get_servo_angles(self, **kw):
        return {"servos": self.get_servo_angles_dict()}

    def get_servo_angles_dict(self):
        return dict((str(p), a) for p, a in self._servos.items())

    def cleanup_servos(self, **kw):
        pins = list(self._servos.keys())
        for p in pins:
            self._release_pwm(p)
            Pin(p, Pin.IN)
        return {"status": "servos_released", "released": True, "pins": pins}

    def adc_read(self, pin=None, samples=4, **kw):
        pin = pin if pin is not None else self.pick_pin(kw)
        try:
            pin = int(pin)
        except (TypeError, ValueError):
            raise ValueError("pin must be an integer")
        if pin not in ADC_PINS:
            raise ValueError("ADC is only available on GP26, GP27, GP28")
        adc = self._adcs.get(pin)
        if adc is None:
            adc = ADC(Pin(pin))
            self._adcs[pin] = adc
        n = max(1, min(64, int(samples)))
        raw = 0
        for _ in range(n):
            raw += adc.read_u16()
        raw = raw // n
        return {"pin": pin, "channel": ADC_PINS.index(pin), "raw_u16": raw,
                "voltage": round(raw * 3.3 / 65535, 4), "fraction": round(raw / 65535, 4)}

    # ------------------------------------------------------------------
    # Safety
    # ------------------------------------------------------------------

    def safe_stop(self, **kw):
        """Connection-loss policy: stop all PWM/servo outputs and LED patterns."""
        for p in list(self._pwms.keys()):
            self._release_pwm(p)
            try:
                Pin(p, Pin.IN)
            except Exception:
                pass
        self.stop_animation()
        return {"stopped": True, "pwm_released": True}

    def emergency_stop(self, **kw):
        """Stop everything: PWM/servos released, driven outputs set LOW, LED off."""
        self.safe_stop()
        for p in list(self._outputs.keys()):
            try:
                Pin(p, Pin.OUT, value=0)
                self._outputs[p] = False
            except Exception:
                pass
        self.set_led(False)
        return {"stopped": True, "outputs_low": sorted(self._outputs.keys()),
                "message": "Emergency stop triggered"}

    # ------------------------------------------------------------------
    # Power
    # ------------------------------------------------------------------

    def _run_shutdown_hooks(self):
        for fn in self._shutdown_hooks:
            try:
                fn()
            except Exception:
                pass
        try:
            self.safe_stop()
            self._set_led_hw(False)
        except Exception:
            pass

    def _schedule(self, delay_s, action):
        async def _later():
            await asyncio.sleep(max(0.2, delay_s))
            self._run_shutdown_hooks()
            action()
        asyncio.create_task(_later())

    def reboot(self, delay=2, simulate=None, **kw):
        if simulate:
            return {"status": "simulated", "action": "reboot"}
        self._schedule(float(delay), machine.reset)
        return {"status": "rebooting", "action": "reboot", "delay": delay}

    def shutdown(self, delay=2, simulate=None, **kw):
        """No power switch on a Pico: indefinite deep sleep is the closest
        equivalent. Wake it with a reset / power cycle."""
        if simulate:
            return {"status": "simulated", "action": "shutdown"}
        self._schedule(float(delay), machine.deepsleep)
        return {"status": "shutting_down", "action": "shutdown", "delay": delay,
                "note": "Pico enters indefinite deep sleep; power-cycle or reset to wake."}

    def sleep(self, duration_s=60, delay=2, simulate=None, **kw):
        """Deep sleep for duration_s then reboot (Wi-Fi/BLE drop while asleep)."""
        max_s = int(self._limits.get("max_sleep_s", 86400))
        d = max(1, min(max_s, int(duration_s)))
        if simulate:
            return {"status": "simulated", "action": "sleep", "duration_s": d}
        self._schedule(float(delay), lambda: machine.deepsleep(d * 1000))
        return {"status": "sleeping", "action": "sleep", "duration_s": d, "delay": delay,
                "note": "Device reboots after waking."}

    def bootloader(self, delay=1, **kw):
        """Reboot into the USB mass-storage bootloader for a firmware update."""
        self._schedule(float(delay), machine.bootloader)
        return {"status": "entering_bootloader", "delay": delay}

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def set_config(self, param="", value=None, **kw):
        self.dynamic_config[param] = value
        return {"updated": True, "param": param, "value": value}

    def telemetry(self):
        mem = self.memory_stats()
        usb = self.usb_powered()
        data = {
            "cpu": 0.0,
            "ram": mem["percent"],
            "temp": self.read_temperature_c(),
            "gpu": None,
            "power_source": "usb" if usb else "vsys_external",
            "battery": None,
            "voltage": None,
            "imu": None,
            "servos": self.get_servo_angles_dict(),
            "led": self.led_state,
            "mem_free_kb": mem["free_kb"],
            "uptime_s": round(util.uptime_s(), 1),
        }
        if self.wifi is not None:
            data["wifi_rssi"] = self.wifi.rssi()
        return data

    async def needle_prompt(self, prompt="", **kw):
        """Tiny on-device keyword interpreter mapped onto the Pico's tools."""
        text = (prompt or "").lower().strip()
        util.log("needle", "prompt: %s" % text)

        m = re.search(r"(gpio|pin|gp) ?([0-9]+)", text)
        if m and ("on" in text.split() or "high" in text or "off" in text.split() or "low" in text):
            pin = int(m.group(2))
            state = ("on" in text.split()) or ("high" in text)
            return {"status": "executed", "action": "gpio_write", "result": self.gpio_write(pin=pin, state=state)}
        if m and ("read" in text or "state" in text):
            return {"status": "executed", "action": "gpio_read", "result": self.gpio_read(pin=int(m.group(2)))}
        if "stop" in text or "halt" in text or "emergency" in text:
            return {"status": "executed", "action": "emergency_stop", "result": self.emergency_stop()}
        if "alert" in text or "alarm" in text:
            return {"status": "executed", "action": "flash_alert", "result": self.flash_alert()}
        if "warn" in text:
            return {"status": "executed", "action": "blink_warning", "result": self.blink_warning()}
        if "blink" in text or "flash" in text:
            return {"status": "executed", "action": "blink_oke", "result": self.blink_oke()}
        if "led" in text or "light" in text:
            on = not ("off" in text or "kill" in text)
            self.set_led(on)
            return {"status": "executed", "action": "set_led", "message": "LED turned %s" % ("on" if on else "off")}
        if "temp" in text or "hot" in text:
            t = self.read_temperature_c()
            return {"status": "executed", "action": "get_temperature", "message": "Core temperature is %s C" % t,
                    "temperature_c": t}
        if "scan" in text or "i2c" in text:
            if self.hardware_mapper is not None:
                buses = self.hardware_mapper.rescan_i2c()
                found = sum(len(b.get("devices", [])) for b in buses)
                return {"status": "executed", "action": "i2c_scan", "message": "Found %d I2C device(s)" % found,
                        "buses": buses}
        if "memory" in text or "ram" in text:
            return {"status": "executed", "action": "get_system_stats", "result": self.memory_stats()}
        if "power" in text or "battery" in text or "usb" in text:
            return {"status": "executed", "action": "get_battery", "result": self.get_battery()}
        if "reboot" in text or "restart" in text:
            return {"status": "executed", "action": "reboot", "result": self.reboot()}
        if any(k in text for k in ("hardware", "spec", "info", "what are you", "status")):
            mem = self.memory_stats()
            msg = "%s, %d MHz, %.0f KB RAM free, %s C" % (self.board, machine.freq() // 1000000,
                                                         mem["free_kb"], self.read_temperature_c())
            return {"status": "executed", "action": "hardware_info", "message": msg,
                    "hardware_map": self.hardware_mapper.last_map if self.hardware_mapper else None}
        return {"status": "unrecognized", "message": "Needle AI could not interpret prompt: '%s'" % prompt}

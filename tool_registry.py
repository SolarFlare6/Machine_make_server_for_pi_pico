"""
tool_registry.py

Tool Registry (Pico W port).
Defines and registers all tools exposed over DCP to the MachineMake client.
Optimized for MicroPython RAM efficiency so large schema JSON payloads
do not trigger heap fragmentation or MemoryError during 'tools' handshake.
"""


class ToolDefinition:
    def __init__(self, name, description, arguments, permission, handler,
                 is_long_running=False, safety_constraints=None, exposed=True):
        self.name = name
        self.description = description
        self.arguments = arguments
        self.permission = permission
        self.handler = handler
        self.is_long_running = is_long_running
        self.safety_constraints = safety_constraints or {}
        self.exposed = exposed

    def to_dcp(self):
        return {
            "name": self.name,
            "description": self.description,
            "arguments": self.arguments,
            "permission": self.permission,
        }


class ToolRegistry:
    def __init__(self):
        self._tools = {}
        self._cached_list = None

    def register(self, tool):
        self._tools[tool.name] = tool
        self._cached_list = None

    def get(self, name):
        return self._tools.get(name)

    def all_tools(self):
        return dict(self._tools)

    def to_dcp_list(self):
        if self._cached_list is None:
            self._cached_list = [tool.to_dcp() for tool in self._tools.values() if tool.exposed]
        return self._cached_list

    def build_default_tools(self, capabilities, pico_controller, safety_limits=None, hardware_mapper=None):
        limits = safety_limits or {}
        ctrl = pico_controller

        # --- 1. LED & Basic Indicators (Exposed) ---
        self.register(ToolDefinition(
            name="set_led",
            description="Turn onboard LED on/off",
            arguments={"on": {"type": "bool"}},
            permission="CONTROL",
            handler=lambda on=True, **kw: ctrl.set_led(on=on),
        ))

        self.register(ToolDefinition(
            name="toggle_led",
            description="Toggle onboard LED",
            arguments={},
            permission="CONTROL",
            handler=lambda **kw: ctrl.toggle_led(),
        ))

        self.register(ToolDefinition(
            name="get_led",
            description="Get LED state and pattern",
            arguments={},
            permission="READ_ONLY",
            handler=lambda **kw: ctrl.get_led(),
        ))

        self.register(ToolDefinition(
            name="blink_oke",
            description="Double flash confirmation",
            arguments={},
            permission="CONTROL",
            handler=lambda **kw: ctrl.blink_oke(),
        ))

        # Secondary LED patterns (callable by name, omitted from DCP list to save RAM)
        self.register(ToolDefinition(
            name="blink_warning", description="Pulsing warning alert",
            arguments={}, permission="CONTROL", handler=lambda **kw: ctrl.blink_warning(),
            exposed=False
        ))
        self.register(ToolDefinition(
            name="flash_alert", description="Rapid blinking alert",
            arguments={}, permission="CONTROL", handler=lambda **kw: ctrl.flash_alert(),
            exposed=False
        ))
        self.register(ToolDefinition(
            name="animate_running_process", description="Heart-beat pulse sequence",
            arguments={}, permission="CONTROL", handler=lambda **kw: ctrl.animate_running_process(),
            exposed=False
        ))
        self.register(ToolDefinition(
            name="blink_led", description="Blink LED N times",
            arguments={"times": {"type": "int"}, "interval_ms": {"type": "int"}},
            permission="CONTROL",
            handler=lambda progress_cb=None, times=5, interval_ms=250, **kw: ctrl.blink_led(
                times=times, interval_ms=interval_ms, progress_cb=progress_cb, **kw
            ),
            is_long_running=True, exposed=False
        ))

        # --- 2. GPIO & Digital I/O (Exposed) ---
        self.register(ToolDefinition(
            name="gpio_write",
            description="Set digital state (HIGH/LOW)",
            arguments={"pin": {"type": "int"}, "state": {"type": "any"}},
            permission="CONTROL",
            handler=lambda pin=0, state=False, **kw: ctrl.gpio_write(pin=pin, state=state, **kw),
        ))

        self.register(ToolDefinition(
            name="gpio_read",
            description="Read digital state",
            arguments={"pin": {"type": "int"}, "pull": {"type": "str"}},
            permission="READ_ONLY",
            handler=lambda pin=0, pull=None, **kw: ctrl.gpio_read(pin=pin, pull=pull, **kw),
        ))

        # --- 3. PWM & Servo Control (Exposed) ---
        self.register(ToolDefinition(
            name="pwm_set",
            description="Set PWM duty cycle and freq",
            arguments={"pin": {"type": "int"}, "value": {"type": "float"}, "freq": {"type": "int"}},
            permission="CONTROL",
            handler=lambda value=0.0, freq=None, **kw: ctrl.pwm_set(value=value, freq=freq, **kw),
        ))

        self.register(ToolDefinition(
            name="pwm_stop",
            description="Stop PWM output on GPIO pin",
            arguments={"pin": {"type": "int"}},
            permission="CONTROL",
            handler=lambda **kw: ctrl.pwm_stop(**kw),
        ))

        self.register(ToolDefinition(
            name="set_servo_angle",
            description="Set servo angle (0-180 deg)",
            arguments={"pin": {"type": "int"}, "angle": {"type": "float"}},
            permission="CONTROL",
            handler=lambda angle=90, **kw: ctrl.set_servo_angle(angle=angle, **kw),
        ))

        self.register(ToolDefinition(
            name="get_servo_angles",
            description="Get servo angles map",
            arguments={},
            permission="READ_ONLY",
            handler=lambda **kw: ctrl.get_servo_angles(),
        ))

        # Auxiliary servo tools
        self.register(ToolDefinition(
            name="set_servo_pulse", description="Set servo pulse width",
            arguments={"pin": {"type": "int"}, "pulse_us": {"type": "int"}},
            permission="CONTROL",
            handler=lambda pulse_us=1500, **kw: ctrl.set_servo_pulse(pulse_us=pulse_us, **kw),
            exposed=False
        ))
        self.register(ToolDefinition(
            name="cleanup_servos", description="De-energize servos",
            arguments={}, permission="CONTROL", handler=lambda **kw: ctrl.cleanup_servos(),
            exposed=False
        ))

        # --- 4. ADC & Sensors (Exposed) ---
        self.register(ToolDefinition(
            name="adc_read",
            description="Read analog voltage (GP26-28)",
            arguments={"pin": {"type": "int"}, "samples": {"type": "int"}},
            permission="READ_ONLY",
            handler=lambda pin=26, samples=4, **kw: ctrl.adc_read(pin=pin, samples=samples, **kw),
        ))

        self.register(ToolDefinition(
            name="get_temperature",
            description="Read internal core temp",
            arguments={},
            permission="READ_ONLY",
            handler=lambda **kw: ctrl.get_temperature(),
        ))

        self.register(ToolDefinition(
            name="get_bootsel_button",
            description="Read onboard BOOTSEL button",
            arguments={},
            permission="READ_ONLY",
            handler=lambda **kw: ctrl.get_bootsel_button(),
        ))

        # --- 5. Safety & System (Exposed) ---
        self.register(ToolDefinition(
            name="emergency_stop",
            description="Emergency stop: halt all outputs",
            arguments={},
            permission="CONTROL",
            handler=lambda **kw: ctrl.emergency_stop(),
        ))

        self.register(ToolDefinition(
            name="safe_stop",
            description="Safe stop: release PWM/servos",
            arguments={},
            permission="CONTROL",
            handler=lambda **kw: ctrl.safe_stop(),
        ))

        self.register(ToolDefinition(
            name="reboot",
            description="Reboot microcontroller",
            arguments={"delay": {"type": "int"}},
            permission="CONTROL",
            handler=lambda delay=2, simulate=None, **kw: ctrl.reboot(delay=delay, simulate=simulate),
        ))

        self.register(ToolDefinition(
            name="get_battery",
            description="Read power source and VBUS status",
            arguments={},
            permission="READ_ONLY",
            handler=lambda **kw: ctrl.get_battery(),
        ))

        self.register(ToolDefinition(
            name="get_hardware_info",
            description="Get hardware specs and details",
            arguments={},
            permission="READ_ONLY",
            handler=lambda **kw: ctrl.get_hardware_info(),
        ))

        # Auxiliary system tools (registered for execute_tool, omitted from broadcast list)
        self.register(ToolDefinition(
            name="shutdown", description="Pico deep sleep",
            arguments={"delay": {"type": "int"}}, permission="CONTROL",
            handler=lambda delay=2, simulate=None, **kw: ctrl.shutdown(delay=delay, simulate=simulate),
            exposed=False
        ))
        self.register(ToolDefinition(
            name="sleep", description="Timed deep sleep",
            arguments={"duration_s": {"type": "int"}}, permission="CONTROL",
            handler=lambda duration_s=60, delay=2, simulate=None, **kw: ctrl.sleep(
                duration_s=duration_s, delay=delay, simulate=simulate
            ),
            exposed=False
        ))
        self.register(ToolDefinition(
            name="bootloader", description="Reboot into USB flash bootloader",
            arguments={"delay": {"type": "int"}}, permission="ADMIN",
            handler=lambda delay=1, **kw: ctrl.bootloader(delay=delay),
            exposed=False
        ))
        self.register(ToolDefinition(
            name="get_storage", description="Get flash filesystem stats",
            arguments={}, permission="READ_ONLY", handler=lambda **kw: ctrl.get_storage(),
            exposed=False
        ))
        self.register(ToolDefinition(
            name="get_system_stats", description="Get RAM and uptime stats",
            arguments={}, permission="READ_ONLY", handler=lambda **kw: ctrl.get_system_stats(),
            exposed=False
        ))
        self.register(ToolDefinition(
            name="set_config", description="Set runtime configuration param",
            arguments={"param": {"type": "str"}, "value": {"type": "any"}}, permission="CONTROL",
            handler=lambda param="", value=None, **kw: ctrl.set_config(param=param, value=value),
            exposed=False
        ))
        self.register(ToolDefinition(
            name="needle_prompt", description="Natural language prompt handler",
            arguments={"prompt": {"type": "str"}}, permission="CONTROL",
            handler=lambda prompt="", **kw: ctrl.needle_prompt(prompt=prompt),
            exposed=False
        ))

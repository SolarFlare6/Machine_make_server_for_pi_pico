"""
safety_manager.py

Safety Manager (Pico W port of the reference safety_manager.py).

Answers "Is this physically allowed, right now?":
  - argument range / enum validation against each tool's safety_constraints
    (on the Pico this is also what blocks the CYW43 radio pins GP23-25/29
    and any reserved pins from being driven by GPIO / PWM tools)
  - control ownership for CONTROL tools
  - no stacking of long-running tasks
"""


class SafetyViolation(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class SafetyManager:
    def __init__(self):
        self._busy = False

    def mark_busy(self, busy):
        self._busy = busy

    @property
    def busy(self):
        return self._busy

    def validate(self, tool, parameters, has_control_ownership):
        if tool.permission == "CONTROL" and not has_control_ownership:
            raise SafetyViolation("Session does not hold control ownership.")
        if tool.is_long_running and self._busy:
            raise SafetyViolation("Device is busy with another task.")
        self._validate_constraints(tool, parameters)

    def _validate_constraints(self, tool, parameters):
        for arg_name, rule in tool.safety_constraints.items():
            if arg_name not in parameters:
                continue
            value = parameters[arg_name]
            if "enum" in rule and value not in rule["enum"]:
                raise SafetyViolation("'%s' must be one of %s, got %s." % (arg_name, rule["enum"], value))
            try:
                if "min" in rule and value < rule["min"]:
                    raise SafetyViolation("'%s' below minimum allowed value %s." % (arg_name, rule["min"]))
                if "max" in rule and value > rule["max"]:
                    raise SafetyViolation("'%s' exceeds configured limit %s." % (arg_name, rule["max"]))
            except TypeError:
                raise SafetyViolation("'%s' has an invalid type." % arg_name)

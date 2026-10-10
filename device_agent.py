"""
device_agent.py

Device Agent: the authoritative execution layer (Pico W port of the
reference device_agent.py). Every protected command flows through:

    Authentication -> Permission check -> Argument validation
        -> Safety validation -> Execution
"""

from safety_manager import SafetyViolation
import util

_PERMISSION_RANK = {"READ_ONLY": 0, "CONTROL": 1, "ADMIN": 2}

_TYPE_MAP = {
    "string": str, "str": str,
    "number": (int, float), "int": (int, float), "integer": (int, float), "float": (int, float),
    "boolean": bool, "bool": bool,
    "list": (list, tuple), "array": (list, tuple),
    "object": dict, "dict": dict,
}


class DCPError(Exception):
    """Carries a DCP standard error code + message."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class DeviceAgent:
    def __init__(self, tool_registry, safety_manager, task_manager):
        self._tools = tool_registry
        self._safety = safety_manager
        self._tasks = task_manager

    async def execute_tool(self, session, tool_name, parameters):
        # 1. Authentication
        if not session.authenticated:
            raise DCPError("UNAUTHORIZED", "Session is not authenticated.")

        tool = self._tools.get(tool_name)
        if tool is None:
            raise DCPError("NOT_FOUND", "Unknown tool '%s'." % tool_name)

        # 2. Permission
        self._check_permission(session, tool)

        # 3. Argument validation
        if not isinstance(parameters, dict):
            raise DCPError("INVALID_ARGUMENT", "parameters must be an object.")
        self._validate_arguments(tool, parameters)

        # 4. Safety validation
        try:
            self._safety.validate(tool, parameters, session.has_control_ownership)
        except SafetyViolation as exc:
            raise DCPError("SAFETY_VIOLATION", exc.message)

        # 5. Execution
        if tool.is_long_running:
            handler = tool.handler
            params = parameters
            task_id = self._tasks.start(
                lambda progress_cb: handler(progress_cb=progress_cb, **params),
                tool_name=tool_name,
                parameters=parameters,
            )
            return {"task_id": task_id}

        try:
            result = tool.handler(**parameters)
            if util.is_awaitable(result):
                result = await result
        except DCPError:
            raise
        except ValueError as exc:
            raise DCPError("INVALID_ARGUMENT", str(exc))
        except Exception as exc:
            util.log_exc("device_agent", "tool '%s' failed" % tool_name, exc)
            raise DCPError("INTERNAL_ERROR", str(exc))
        return {"result": result}

    def _check_permission(self, session, tool):
        if session.permission is None:
            raise DCPError("UNAUTHORIZED", "Session has no assigned permission.")
        if _PERMISSION_RANK.get(session.permission, -1) < _PERMISSION_RANK.get(tool.permission, 99):
            raise DCPError("FORBIDDEN", "Tool '%s' requires %s permission." % (tool.name, tool.permission))

    def _validate_arguments(self, tool, parameters):
        for arg_name, schema in tool.arguments.items():
            if arg_name not in parameters:
                if schema.get("required", True):
                    raise DCPError("MISSING_ARGUMENT", "Missing required argument '%s'." % arg_name)
                continue
            schema_type = schema.get("type")
            if schema_type and schema_type != "any":
                expected = _TYPE_MAP.get(schema_type)
                if expected and not isinstance(parameters[arg_name], expected):
                    raise DCPError("INVALID_ARGUMENT",
                                   "Argument '%s' must be of type %s." % (arg_name, schema_type))

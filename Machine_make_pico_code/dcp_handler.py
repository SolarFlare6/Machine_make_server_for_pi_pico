"""
dcp_handler.py

DCP Handler (Pico W port of the reference dcp_handler.py).
Dispatches DCP JSON messages, dual envelope framing:
  Format A: {"dcp": "1.0", "type": "request", "id": 123, "command": "...", "arguments": {...}}
  Format B: {"msgId": "...", "type": "hello"|"negotiate"|"auth"|"capabilities"|"tools"|"subscribe_events"|"execute"|"ping", "payload": {...}, "timestampMs": ...}
"""

import time
import util
from device_agent import DCPError

DCP_VERSION = "1.0"
SUPPORTED_VERSIONS = ("1.0", "1.0.0", "1.1")

# Commands that directly map onto tools
_SHORTCUT_COMMANDS = {
    "set_led", "toggle_led", "get_led", "blink_oke", "blink_warning", "flash_alert",
    "animate_running_process", "blink_led",
    "gpio_write", "gpio_read", "pwm_set", "pwm_stop",
    "set_servo_angle", "driver_set_servo_angle_with_index", "set_servo_pulse",
    "get_servo_angles", "cleanup_servos",
    "adc_read", "get_temperature", "get_bootsel_button", "i2c_scan",
    "emergency_stop", "safe_stop", "reboot", "system_reboot", "shutdown",
    "system_shutdown", "sleep", "bootloader",
    "get_battery", "get_storage", "get_hardware_info", "get_system_stats",
    "set_config", "needle_prompt"
}


class DCPHandler:
    def __init__(self, identity, hardware_mapper, config, tool_registry,
                 device_agent, task_manager, session_manager, trust_store,
                 authenticator, capabilities, pairing_manager=None):
        self._identity = identity
        self._hardware_mapper = hardware_mapper
        self._config = config
        self._tools = tool_registry
        self._agent = device_agent
        self._tasks = task_manager
        self._sessions = session_manager
        self._trust_store = trust_store
        self._auth = authenticator
        self._capabilities = capabilities
        self._pairing = pairing_manager
        self._implicit_auth = config.security.get("implicit_auth", True)
        self._capabilities_structured = []
        for cap in self._capabilities:
            if isinstance(cap, dict):
                self._capabilities_structured.append(cap)
            else:
                cap_words = str(cap).replace("_", " ").split(" ")
                cap_name = " ".join(w[0].upper() + w[1:] if len(w) > 0 else "" for w in cap_words)
                self._capabilities_structured.append({
                    "id": cap,
                    "type": cap,
                    "name": cap_name,
                    "description": "%s capability" % cap_name,
                    "params": {},
                    "enabled": True,
                })

    async def handle(self, session, message):
        """Processes one incoming DCP message."""
        if not isinstance(message, dict):
            return self._error(None, "INVALID_REQUEST", "Message must be a JSON object.")

        msg_type = message.get("type")

        # Format B (Mobile App / Spec Section 1.1 / Section 5)
        if msg_type in ("hello", "negotiate", "auth", "capabilities", "tools",
                        "subscribe_events", "execute", "ping", "shutdown", "reboot", "sleep",
                        "system_shutdown", "system_reboot", "system_sleep"):
            return await self._handle_format_b(session, message)

        # Format A (Standard DCP Request)
        if msg_type != "request":
            return self._error(None, "INVALID_REQUEST", "Only 'request' messages are handled here.")

        request_id = message.get("id")
        version = str(message.get("dcp", DCP_VERSION))
        if version not in SUPPORTED_VERSIONS:
            return self._error(request_id, "INVALID_REQUEST", "Unsupported DCP version '%s'." % version)

        command = message.get("command")
        arguments = message.get("arguments", {}) or {}

        try:
            if command in ("request_pairing", "pair"):
                return await self._handle_request_pairing(session, request_id, arguments)
            if command == "negotiate":
                selected = arguments.get("selected_version", "1.0")
                return self._simple_response(request_id, {
                    "negotiated_version": selected if selected in SUPPORTED_VERSIONS else "1.0",
                    "supported_versions": list(SUPPORTED_VERSIONS),
                })
            if command == "ping":
                return self._simple_response(request_id, {
                    "pong": True,
                    "timestamp": util.now_s(),
                })
            if command in ("authenticate", "auth"):
                return await self._handle_authenticate(session, request_id, arguments)
            if command in ("get_device_info", "device_info"):
                return self._simple_response(request_id, self._identity.device_info())
            if command in ("get_capabilities", "capabilities"):
                return self._simple_response(request_id, {"capabilities": self._capabilities})
            if command in ("get_tools", "tools"):
                return self._simple_response(request_id, {"tools": self._tools.to_dcp_list()})
            if command == "get_hardware_map":
                return self._simple_response(request_id, self._hardware_mapper.last_map or {})
            if command == "get_status":
                return self._simple_response(request_id, self._status_snapshot(session))
            if command in ("subscribe", "subscribe_events"):
                session.subscribed_events |= set(arguments.get("events", []))
                return self._simple_response(request_id, {"subscribed": sorted(list(session.subscribed_events))})
            if command == "unsubscribe":
                session.subscribed_events -= set(arguments.get("events", []))
                return self._simple_response(request_id, {"subscribed": sorted(list(session.subscribed_events))})
            if command == "request_control":
                if self._implicit_auth and not session.authenticated:
                    session.authenticated = True
                    session.permission = "CONTROL"
                granted = self._sessions.request_control(session)
                return self._simple_response(request_id, {
                    "granted": granted,
                    "session_role_granted": "control" if granted else "read_only",
                })
            if command == "release_control":
                self._sessions.release_control(session)
                return self._simple_response(request_id, {
                    "released": True,
                    "session_role_granted": "read_only",
                })
            if command in ("execute_tool", "execute"):
                return await self._handle_execute_tool(session, request_id, arguments)
            if command == "cancel_task":
                cancelled = self._tasks.cancel(arguments.get("task_id"))
                if not cancelled:
                    return self._error(request_id, "TASK_NOT_FOUND", "No such task.")
                return self._simple_response(request_id, {"cancelled": True})
            if command == "get_task_status":
                status = self._tasks.get_status(arguments.get("task_id"))
                if status is None:
                    return self._error(request_id, "TASK_NOT_FOUND", "No such task.")
                return self._simple_response(request_id, status)
            if command == "update_configuration":
                return self._handle_update_configuration(session, request_id, arguments)
            if command in ("reboot", "system_reboot"):
                return await self._handle_reboot(session, request_id, arguments)
            if command in ("shutdown", "system_shutdown"):
                return await self._handle_shutdown(session, request_id, arguments)

            # Shortcut command support
            if command in _SHORTCUT_COMMANDS or self._tools.get(command) is not None:
                return await self._handle_execute_tool(
                    session, request_id, {"tool": command, "parameters": arguments}
                )

            return self._error(request_id, "INVALID_COMMAND", "Unknown command '%s'." % command)

        except DCPError as exc:
            return self._error(request_id, exc.code, exc.message)
        except Exception as exc:
            util.log_exc("dcp_handler", "command '%s' failed" % command, exc)
            return self._error(request_id, "INTERNAL_ERROR", str(exc))

    # --- Format B (Mobile App Spec) ---

    async def _handle_format_b(self, session, message):
        msg_type = message.get("type")
        msg_id = message.get("msgId", util.uuid4())
        payload = message.get("payload", {}) or {}
        now_ms = util.now_ms()

        if msg_type == "hello":
            return {
                "msgId": msg_id,
                "type": "hello_ack",
                "payload": {
                    "deviceId": self._identity.device_id,
                    "name": self._identity.name,
                    "deviceType": self._identity.device_type,
                    "profile": self._identity.profile,
                    "supportedProtocols": ["1.0.0", "1.0", "1.1"],
                },
                "timestampMs": now_ms,
            }

        elif msg_type == "negotiate":
            selected = payload.get("selectedVersion") or payload.get("protocolVersion") or "1.0.0"
            return {
                "msgId": msg_id,
                "type": "negotiate_ack",
                "payload": {"selectedVersion": selected},
                "timestampMs": now_ms,
            }

        elif msg_type == "auth":
            session.authenticated = True
            if session.permission is None:
                session.permission = "CONTROL"
            self._sessions.request_control(session)
            return {
                "msgId": msg_id,
                "type": "auth_ack",
                "payload": {"success": True, "sessionToken": "dcp-tok-%d" % session.session_id},
                "timestampMs": now_ms,
            }

        elif msg_type == "capabilities":
            return {
                "msgId": msg_id,
                "type": "capabilities_response",
                "payload": {"capabilities": self._capabilities_structured},
                "timestampMs": now_ms,
            }

        elif msg_type == "tools":
            return {
                "msgId": msg_id,
                "type": "tools_response",
                "payload": {"tools": self._tools.to_dcp_list()},
                "timestampMs": now_ms,
            }

        elif msg_type == "subscribe_events":
            events = []
            if isinstance(payload, list):
                events = payload
            elif isinstance(payload, dict):
                events = payload.get("events") or payload.get("topics") or []
            if not events and "events" in message:
                events = message.get("events") or []
            if isinstance(events, str):
                events = [events]
            session.subscribed_events |= set(events)
            return {
                "msgId": msg_id,
                "type": "subscribe_ack",
                "payload": {"subscribed": sorted(list(session.subscribed_events))},
                "timestampMs": now_ms,
            }

        elif msg_type == "execute":
            tool_name = payload.get("toolName") or payload.get("tool")
            params = payload.get("params") if "params" in payload else payload.get("parameters", {})
            params = params or {}
            if not tool_name:
                return {
                    "msgId": msg_id,
                    "type": "execute_response",
                    "payload": {"success": False, "result": None, "error": "MISSING_TOOL_NAME"},
                    "timestampMs": now_ms,
                }

            if self._implicit_auth:
                if not session.authenticated:
                    session.authenticated = True
                if session.permission is None:
                    session.permission = "CONTROL"
                if not session.has_control_ownership:
                    self._sessions.request_control(session)

            try:
                outcome = await self._agent.execute_tool(session, tool_name, params)
                res = outcome.get("result", outcome)
                return {
                    "msgId": msg_id,
                    "type": "execute_response",
                    "payload": {"success": True, "result": res, "error": None},
                    "timestampMs": now_ms,
                }
            except DCPError as exc:
                return {
                    "msgId": msg_id,
                    "type": "execute_response",
                    "payload": {"success": False, "result": None,
                                "error": {"code": exc.code, "message": exc.message}},
                    "timestampMs": now_ms,
                }
            except Exception as exc:
                return {
                    "msgId": msg_id,
                    "type": "execute_response",
                    "payload": {"success": False, "result": None,
                                "error": {"code": "INTERNAL_ERROR", "message": str(exc)}},
                    "timestampMs": now_ms,
                }

        elif msg_type == "ping":
            return {
                "msgId": msg_id,
                "type": "pong",
                "payload": {},
                "timestampMs": now_ms,
            }

        elif msg_type in ("shutdown", "reboot", "sleep", "system_shutdown", "system_reboot", "system_sleep"):
            tool_name = "shutdown" if "shutdown" in msg_type else ("reboot" if "reboot" in msg_type else "sleep")
            params = payload if isinstance(payload, dict) else {}
            if self._implicit_auth:
                if not session.authenticated:
                    session.authenticated = True
                if session.permission is None:
                    session.permission = "CONTROL"
                if not session.has_control_ownership:
                    self._sessions.request_control(session)

            try:
                outcome = await self._agent.execute_tool(session, tool_name, params)
                res = outcome.get("result", outcome)
                return {
                    "msgId": msg_id,
                    "type": "%s_response" % msg_type,
                    "payload": {"success": True, "result": res, "error": None},
                    "timestampMs": now_ms,
                }
            except DCPError as exc:
                return {
                    "msgId": msg_id,
                    "type": "%s_response" % msg_type,
                    "payload": {"success": False, "result": None,
                                "error": {"code": exc.code, "message": exc.message}},
                    "timestampMs": now_ms,
                }

        return None

    # --- Sub-handlers ---

    async def _handle_request_pairing(self, session, request_id, arguments):
        client_id = arguments.get("client_id")
        client_name = arguments.get("client_name", "Unknown Client")
        method = arguments.get("method", "wifi")
        pairing_code = arguments.get("pairing_code")

        if not client_id:
            return self._error(request_id, "MISSING_ARGUMENT", "client_id is required.")
        if self._pairing is None:
            return self._error(request_id, "UNAVAILABLE", "Pairing manager not configured.")

        if method == "qr" and pairing_code:
            result = await self._pairing.verify_qr_pairing(
                client_id=client_id,
                client_name=client_name,
                pairing_code=pairing_code,
            )
        else:
            result = await self._pairing.request_pairing(
                client_id=client_id,
                client_name=client_name,
                method=method,
            )

        if result.get("approved"):
            return self._simple_response(request_id, {
                "approved": True,
                "secret": result["secret"],
                "device_id": self._identity.device_id,
            })
        else:
            return self._error(
                request_id,
                "PAIRING_DENIED",
                result.get("reason", "Pairing request was denied or expired."),
            )

    async def _handle_authenticate(self, session, request_id, arguments):
        client_id = arguments.get("client_id")
        if not client_id:
            return self._error(request_id, "MISSING_ARGUMENT", "client_id is required.")

        if "response" not in arguments:
            nonce = self._auth.create_challenge(client_id)
            if nonce is None:
                return self._error(request_id, "UNAUTHORIZED", "Client is not paired with this device.")
            return self._simple_response(request_id, {"nonce": nonce})

        ok = self._auth.verify_response(client_id, arguments["response"])
        if not ok:
            return self._error(request_id, "UNAUTHORIZED", "Authentication failed.")

        session.authenticated = True
        session.client_id = client_id
        session.permission = self._trust_store.permission_for(client_id)
        self._sessions.request_control(session)
        return self._simple_response(request_id, {"authenticated": True, "permission": session.permission})

    async def _handle_execute_tool(self, session, request_id, arguments):
        tool_name = arguments.get("tool") or arguments.get("toolName")
        parameters = arguments.get("parameters") if "parameters" in arguments else arguments.get("params", {})
        parameters = parameters or {}
        if not tool_name:
            return self._error(request_id, "MISSING_ARGUMENT", "tool is required.")

        if self._implicit_auth:
            if not session.authenticated:
                session.authenticated = True
            if session.permission is None:
                session.permission = "CONTROL"
            if not session.has_control_ownership:
                self._sessions.request_control(session)

        outcome = await self._agent.execute_tool(session, tool_name, parameters)
        if "task_id" in outcome:
            return {
                "dcp": DCP_VERSION,
                "type": "response",
                "id": request_id,
                "success": True,
                "task_id": outcome["task_id"],
            }
        return self._simple_response(request_id, {"result": outcome["result"]})

    def _handle_update_configuration(self, session, request_id, arguments):
        if not session.authenticated or session.permission != "ADMIN":
            return self._error(request_id, "FORBIDDEN", "update_configuration requires ADMIN permission.")
        self._config.update_configuration(arguments.get("configuration", {}))
        return self._simple_response(request_id, {"updated": True})

    async def _handle_reboot(self, session, request_id, arguments=None):
        if not session.authenticated or session.permission not in ("ADMIN", "CONTROL"):
            return self._error(request_id, "FORBIDDEN", "reboot requires ADMIN or CONTROL permission.")
        return await self._handle_execute_tool(session, request_id, {"tool": "reboot", "parameters": arguments or {}})

    async def _handle_shutdown(self, session, request_id, arguments=None):
        if not session.authenticated or session.permission not in ("ADMIN", "CONTROL"):
            return self._error(request_id, "FORBIDDEN", "shutdown requires ADMIN or CONTROL permission.")
        return await self._handle_execute_tool(session, request_id, {"tool": "shutdown", "parameters": arguments or {}})

    def _status_snapshot(self, session):
        return {
            "device_id": self._identity.device_id,
            "authenticated": session.authenticated,
            "permission": session.permission,
            "has_control_ownership": session.has_control_ownership,
            "subscribed_events": sorted(list(session.subscribed_events)),
        }

    def _simple_response(self, request_id, data):
        return {"dcp": DCP_VERSION, "type": "response", "id": request_id, "success": True, "data": data}

    def _error(self, request_id, code, message):
        return {
            "dcp": DCP_VERSION,
            "type": "response",
            "id": request_id,
            "success": False,
            "error": {"code": code, "message": message},
        }

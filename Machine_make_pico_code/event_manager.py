"""
event_manager.py

Event Manager (Pico W port of the reference event_manager.py).

Broadcasts asynchronous DCP events to every session subscribed to that
event name, using the same dual envelope (standard DCP + mobile-app
Section 4 schema) as the reference server.
"""

import util

DCP_VERSION = "1.0"
_TASK_EVENTS = ("task_update", "task_progress", "task_started", "task_completed", "task_failed")
_TASK_ALIASES = ("task_update", "task_progress", "task_completed", "task_started")


class EventManager:
    def __init__(self, session_manager, identity=None):
        self._sessions = session_manager
        self._identity = identity

    def set_identity(self, identity):
        self._identity = identity

    async def emit(self, event_name, data=None, task_id=None):
        payload_data = dict(data or {})
        if task_id is not None and "task_id" not in payload_data:
            payload_data["task_id"] = task_id

        targets = {}
        for s in self._sessions.sessions_subscribed_to(event_name):
            targets[s.session_id] = s
        if event_name in _TASK_EVENTS:
            for alt in _TASK_ALIASES:
                for s in self._sessions.sessions_subscribed_to(alt):
                    targets[s.session_id] = s
        if not targets:
            return

        device_id = self._identity.device_id if self._identity else ""
        message = {
            "dcp": DCP_VERSION,
            "type": "event",
            "event": event_name,
            "event_type": event_name,
            "device_id": device_id,
            "data": payload_data,
            "msgId": util.uuid4(),
            "payload": {
                "eventType": event_name,
                "deviceId": device_id,
                "data": payload_data,
            },
            "timestampMs": util.now_ms(),
        }
        if task_id is not None:
            message["task_id"] = task_id

        for session in targets.values():
            try:
                await session.send(message)
            except Exception as exc:
                util.log("event_manager", "failed to deliver %s to session %s: %s"
                         % (event_name, session.session_id, exc), "warning")

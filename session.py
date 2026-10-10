"""
session.py

Session Manager (Pico W port of the reference session.py).

A Session exists per connected client (one per WebSocket / BLE
connection) and tracks authentication state, permission level, event
subscriptions and whether it currently holds CONTROL ownership. Only one
session may hold CONTROL ownership at a time.
"""

_next_id = 1


class Session:
    def __init__(self, session_id, send_fn, transport="wifi"):
        self.session_id = session_id
        self.send_fn = send_fn  # async callable(dict) -> None
        self.transport = transport
        self.client_id = None
        self.authenticated = False
        self.permission = None  # READ_ONLY | CONTROL | ADMIN
        self.subscribed_events = set()
        self.has_control_ownership = False

    async def send(self, message):
        await self.send_fn(message)


class SessionManager:
    def __init__(self):
        self._sessions = {}
        self._control_owner_id = None
        self._removed_callbacks = []

    def on_session_removed(self, callback):
        """callback(session, had_control) -- used for the connection-loss
        safety policy (protocol spec section 44)."""
        self._removed_callbacks.append(callback)

    def create_session(self, send_fn, transport="wifi"):
        global _next_id
        session = Session(_next_id, send_fn, transport)
        _next_id += 1
        self._sessions[session.session_id] = session
        return session

    def remove_session(self, session):
        self._sessions.pop(session.session_id, None)
        had_control = self._control_owner_id == session.session_id
        if had_control:
            self._control_owner_id = None
        session.has_control_ownership = False
        for cb in self._removed_callbacks:
            try:
                cb(session, had_control)
            except Exception:
                pass

    def request_control(self, session):
        if self._control_owner_id in (None, session.session_id):
            self._control_owner_id = session.session_id
            session.has_control_ownership = True
            return True
        return False

    def release_control(self, session):
        if self._control_owner_id == session.session_id:
            self._control_owner_id = None
        session.has_control_ownership = False

    def sessions_subscribed_to(self, event_name):
        return [s for s in self._sessions.values() if event_name in s.subscribed_events]

    def all_sessions(self):
        return list(self._sessions.values())

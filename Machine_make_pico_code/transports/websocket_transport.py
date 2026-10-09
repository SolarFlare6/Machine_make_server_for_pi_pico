"""
transports/websocket_transport.py

Wi-Fi DCP Server: Lightweight WebSocket Server over TCP for MicroPython (Pico W).
Implements RFC 6455 framing and HTTP handshake using asyncio.
Accepts incoming connections, creates DCP Sessions, parses JSON requests,
and forwards them to DCPHandler.
"""

import asyncio
import gc
import json
import socket
import struct
import util

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketConnection:
    def __init__(self, reader, writer, max_msg_size=16384):
        self.reader = reader
        self.writer = writer
        self.max_msg_size = max_msg_size
        self.closed = False
        self._write_lock = asyncio.Lock()

    async def send_frame(self, opcode, payload):
        if self.closed:
            return
        if isinstance(payload, str):
            gc.collect()
            try:
                payload = payload.encode("utf-8")
            except MemoryError:
                gc.collect()
                payload = payload.encode("utf-8")

        length = len(payload)
        header = bytearray()
        header.append(0x80 | (opcode & 0x0F))  # FIN bit + opcode

        # Server to client frames must NOT be masked per RFC 6455
        if length <= 125:
            header.append(length)
        elif length <= 65535:
            header.append(126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(127)
            header.extend(struct.pack(">Q", length))

        async with self._write_lock:
            try:
                self.writer.write(header)
                if payload:
                    self.writer.write(payload)
                await self.writer.drain()
            except Exception:
                self.closed = True

    async def send_text(self, text):
        await self.send_frame(OP_TEXT, text)

    async def send_json(self, data):
        """
        Sends JSON payload. For small payloads, uses fast-path serialization.
        For large payloads (> 1.5 KB), streams chunks directly to the socket
        without allocating large contiguous buffers on RP2040 heap.
        """
        if self.closed:
            return

        gc.collect()

        # Fast path for small payloads (< 1500 chars)
        try:
            dumped = json.dumps(data)
            if len(dumped) < 1500:
                payload = dumped.encode("utf-8")
                dumped = None
                await self.send_frame(OP_TEXT, payload)
                payload = None
                gc.collect()
                return
            dumped = None
        except MemoryError:
            gc.collect()
        except Exception:
            pass

        # Streaming path for large payloads (avoids MemoryError)
        try:
            import uio as io
        except ImportError:
            import io
        StreamBase = getattr(io, "IOBase", object)

        class _Counter(StreamBase):
            def __init__(self):
                self.count = 0
            def write(self, s):
                n = len(s) if isinstance(s, (bytes, bytearray)) else len(s.encode("utf-8"))
                self.count += n
                return n

        counter = _Counter()
        json.dump(data, counter)
        length = counter.count
        del counter

        header = bytearray([0x80 | (OP_TEXT & 0x0F)])
        if length <= 125:
            header.append(length)
        elif length <= 65535:
            header.append(126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(127)
            header.extend(struct.pack(">Q", length))

        class _ChunkWriter(StreamBase):
            def __init__(self, writer, chunk_size=256):
                self.writer = writer
                self.chunk_size = chunk_size
                self.buf = bytearray()
            def write(self, s):
                b = s if isinstance(s, (bytes, bytearray)) else s.encode("utf-8")
                self.buf.extend(b)
                if len(self.buf) >= self.chunk_size:
                    self.writer.write(self.buf)
                    self.buf = bytearray()
                return len(s)
            def flush(self):
                if self.buf:
                    self.writer.write(self.buf)
                    self.buf = bytearray()

        async with self._write_lock:
            try:
                self.writer.write(header)
                chunk_w = _ChunkWriter(self.writer, chunk_size=256)
                json.dump(data, chunk_w)
                chunk_w.flush()
                del chunk_w
                await self.writer.drain()
            except Exception:
                self.closed = True

        gc.collect()

    async def read_frame(self):
        """Reads one RFC 6455 WebSocket frame from client."""
        try:
            head = await self.reader.readexactly(2)
        except Exception:
            return None, None

        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        has_mask = bool(head[1] & 0x80)
        length = head[1] & 0x7F

        if length == 126:
            ext = await self.reader.readexactly(2)
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = await self.reader.readexactly(8)
            length = struct.unpack(">Q", ext)[0]

        if length > self.max_msg_size:
            util.log("ws", "frame too large (%d bytes)" % length, "warning")
            await self.close(1009)
            return None, None

        mask = None
        if has_mask:
            mask = await self.reader.readexactly(4)

        payload = b""
        if length > 0:
            payload = await self.reader.readexactly(length)

        if mask and payload:
            unmasked = bytearray(payload)
            for i in range(len(unmasked)):
                unmasked[i] ^= mask[i % 4]
            payload = bytes(unmasked)

        return opcode, payload

    async def close(self, code=1000):
        if not self.closed:
            self.closed = True
            try:
                await self.send_frame(OP_CLOSE, struct.pack(">H", code))
            except Exception:
                pass
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass


class WiFiDCPServer:
    def __init__(self, dcp_handler, session_manager, host="0.0.0.0", port=8765, max_clients=3):
        self._handler = dcp_handler
        self._sessions = session_manager
        self._host = host
        self._port = port
        self._max_clients = max_clients
        self._server = None
        self._client_count = 0

    async def start(self):
        self._server = await asyncio.start_server(self._handle_client, self._host, self._port)
        util.log("ws_transport", "Wi-Fi DCP server listening on ws://%s:%s" % (self._host, self._port))

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        util.log("ws_transport", "Wi-Fi DCP server stopped")

    async def _handle_client(self, reader, writer):
        if self._client_count >= self._max_clients:
            util.log("ws_transport", "Connection rejected: max clients reached", "warning")
            writer.close()
            return

        self._client_count += 1
        ws = None
        session = None

        try:
            # 1. HTTP Handshake
            ws_key = await self._do_handshake(reader, writer)
            if not ws_key:
                writer.close()
                return

            ws = WebSocketConnection(reader, writer)

            # 2. Setup Session with DCP
            async def send_fn(message):
                await ws.send_json(message)

            session = self._sessions.create_session(send_fn, transport="wifi")
            util.log("ws_transport", "Client connected: session %d" % session.session_id)

            # 3. Message loop
            msg_buf = bytearray()
            while not ws.closed:
                opcode, payload = await ws.read_frame()
                if opcode is None:
                    break

                if opcode == OP_CLOSE:
                    await ws.close()
                    break
                elif opcode == OP_PING:
                    await ws.send_frame(OP_PONG, payload)
                    continue
                elif opcode == OP_PONG:
                    continue
                elif opcode == OP_TEXT:
                    msg_buf = bytearray(payload)
                elif opcode == OP_CONT:
                    msg_buf.extend(payload)
                else:
                    continue

                try:
                    message_str = msg_buf.decode("utf-8")
                    message = json.loads(message_str)
                except Exception:
                    await send_fn({
                        "dcp": "1.0", "type": "response", "id": None, "success": False,
                        "error": {"code": "INVALID_REQUEST", "message": "Malformed JSON."},
                    })
                    msg_buf = bytearray()
                    continue

                msg_buf = bytearray()
                message_str = None
                response = await self._handler.handle(session, message)
                message = None
                if response is not None:
                    await send_fn(response)
                    response = None
                gc.collect()

        except Exception as exc:
            util.log_exc("ws_transport", "Error in client connection", exc)
        finally:
            if session is not None:
                util.log("ws_transport", "Client disconnected: session %d" % session.session_id)
                self._sessions.remove_session(session)
            if ws is not None:
                await ws.close()
            else:
                try:
                    writer.close()
                except Exception:
                    pass
            self._client_count = max(0, self._client_count - 1)

    async def _do_handshake(self, reader, writer):
        """Reads HTTP GET request, verifies WebSocket headers, and writes 101 Switching Protocols response."""
        try:
            request_line = await reader.readline()
            if not request_line or b"GET" not in request_line:
                return None

            ws_key = None
            client_subprotocol = None
            while True:
                line = await reader.readline()
                if not line or line == b"\r\n" or line == b"\n":
                    break
                line_lower = line.lower()
                if line_lower.startswith(b"sec-websocket-key:"):
                    parts = line.split(b":", 1)
                    if len(parts) == 2:
                        ws_key = parts[1].strip()
                elif line_lower.startswith(b"sec-websocket-protocol:"):
                    parts = line.split(b":", 1)
                    if len(parts) == 2:
                        client_subprotocol = parts[1].strip()

            if not ws_key:
                return None

            # Calculate Sec-WebSocket-Accept
            accept_raw = util.sha1(ws_key + WS_GUID)
            accept_key = util.b64encode(accept_raw).decode("ascii")

            # RFC 6455: Only include Sec-WebSocket-Protocol if client requested it
            proto_header = b""
            if client_subprotocol and b"dcp" in client_subprotocol.lower():
                proto_header = b"Sec-WebSocket-Protocol: dcp\r\n"

            response = (
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept_key.encode("ascii") + b"\r\n"
                + proto_header +
                b"\r\n"
            )
            writer.write(response)
            await writer.drain()
            return ws_key
        except Exception:
            return None

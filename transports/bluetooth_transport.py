"""
transports/bluetooth_transport.py

Bluetooth (BLE) DCP Server for MicroPython (Raspberry Pi Pico W / aioble).
Implements the DCP BLE GATT peripheral server with chunk fragmentation & reassembly.

Exposes:
  Service UUID:         dcf00001-0000-1000-8000-00805f9b34fb
  TX Characteristic:    dcf00002-0000-1000-8000-00805f9b34fb (Write: App -> Pico W)
  RX Characteristic:    dcf00003-0000-1000-8000-00805f9b34fb (Notify: Pico W -> App)

Uses the same chunk framing header (>HBB: total_len, chunk_idx, total_chunks)
as the reference implementation.
"""

import asyncio
import json
import struct
import util

try:
    import bluetooth
    import aioble
    HAVE_BLE = True
except ImportError:
    HAVE_BLE = False

SERVICE_UUID = bluetooth.UUID("dcf00001-0000-1000-8000-00805f9b34fb") if HAVE_BLE else None
CHAR_TX_UUID = bluetooth.UUID("dcf00002-0000-1000-8000-00805f9b34fb") if HAVE_BLE else None
CHAR_RX_UUID = bluetooth.UUID("dcf00003-0000-1000-8000-00805f9b34fb") if HAVE_BLE else None

HEADER_FORMAT = ">HBB"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
DEFAULT_MAX_PAYLOAD_CHUNK = 240  # Conservative MTU chunk size for BLE


def fragment_message(data_bytes, max_payload_size=DEFAULT_MAX_PAYLOAD_CHUNK):
    total_len = len(data_bytes)
    total_chunks = (total_len + max_payload_size - 1) // max_payload_size
    if total_chunks == 0:
        total_chunks = 1

    chunks = []
    for idx in range(total_chunks):
        chunk_data = data_bytes[idx * max_payload_size:(idx + 1) * max_payload_size]
        header = struct.pack(HEADER_FORMAT, total_len, idx, total_chunks)
        chunks.append(header + chunk_data)
    return chunks


def reassemble_chunk(buffer, chunk_bytes):
    if len(chunk_bytes) < HEADER_SIZE:
        return False, None

    total_len, chunk_idx, total_chunks = struct.unpack(HEADER_FORMAT, chunk_bytes[:HEADER_SIZE])
    payload = chunk_bytes[HEADER_SIZE:]

    if buffer.get("total_len") != total_len or buffer.get("total_chunks") != total_chunks:
        buffer["total_len"] = total_len
        buffer["total_chunks"] = total_chunks
        buffer["chunks"] = {}

    buffer["chunks"][chunk_idx] = payload

    if len(buffer["chunks"]) == total_chunks:
        full_data = bytearray()
        for idx in range(total_chunks):
            if idx not in buffer["chunks"]:
                return False, None
            full_data.extend(buffer["chunks"][idx])
        buffer.clear()
        if len(full_data) == total_len:
            return True, bytes(full_data)

    return False, None


class BluetoothDCPServer:
    def __init__(self, dcp_handler, session_manager, device_name="MachineMake Pico W", adv_interval_ms=250):
        self._handler = dcp_handler
        self._sessions = session_manager
        self._device_name = device_name
        self._adv_interval_ms = adv_interval_ms
        self._running = False
        self._task = None

        self._service = None
        self._tx_char = None
        self._rx_char = None
        self._session = None
        self._rx_buffer = {}

    async def start(self):
        if not HAVE_BLE:
            util.log("ble_transport", "aioble/bluetooth module not available; BLE server skipped", "warning")
            return

        try:
            # 1. Register GATT Service & Characteristics
            self._service = aioble.Service(SERVICE_UUID)
            self._tx_char = aioble.Characteristic(
                self._service, CHAR_TX_UUID, write=True, capture=True
            )
            self._rx_char = aioble.Characteristic(
                self._service, CHAR_RX_UUID, read=True, notify=True
            )
            aioble.register_services(self._service)

            self._running = True
            self._task = asyncio.create_task(self._server_loop())
            util.log("ble_transport", "BLE DCP GATT server registered (service: %s)" % SERVICE_UUID)
        except Exception as exc:
            util.log_exc("ble_transport", "Failed to start BLE server", exc)

    async def stop(self):
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._session is not None:
            self._sessions.remove_session(self._session)
            self._session = None
        util.log("ble_transport", "BLE DCP server stopped")

    async def _server_loop(self):
        while self._running:
            try:
                util.log("ble_transport", "Advertising BLE service as '%s'..." % self._device_name)
                connection = await aioble.advertise(
                    self._adv_interval_ms * 1000,
                    name=self._device_name,
                    services=[SERVICE_UUID],
                )
                util.log("ble_transport", "BLE client connected from %s" % connection.device)

                # Initialize Session
                async def send_fn(message):
                    await self._send_ble(connection, message)

                self._session = self._sessions.create_session(send_fn, transport="ble")
                self._rx_buffer.clear()

                # Read write requests until disconnect
                while connection.is_connected():
                    try:
                        conn, data = await self._tx_char.written(timeout_ms=1000)
                        if data:
                            await self._handle_incoming_chunk(data)
                    except asyncio.TimeoutError:
                        continue

            except asyncio.CancelledError:
                break
            except Exception as exc:
                util.log_exc("ble_transport", "BLE connection error", exc)
                await asyncio.sleep(1)
            finally:
                if self._session is not None:
                    util.log("ble_transport", "BLE client disconnected: session %d" % self._session.session_id)
                    self._sessions.remove_session(self._session)
                    self._session = None

    async def _handle_incoming_chunk(self, chunk_bytes):
        complete, payload = reassemble_chunk(self._rx_buffer, chunk_bytes)
        if not complete or payload is None:
            return

        try:
            message_str = payload.decode("utf-8")
            message = json.loads(message_str)
        except Exception:
            if self._session:
                await self._session.send({
                    "dcp": "1.0", "type": "response", "id": None, "success": False,
                    "error": {"code": "INVALID_REQUEST", "message": "Malformed JSON over BLE."},
                })
            return

        if self._session:
            response = await self._handler.handle(self._session, message)
            if response is not None:
                await self._session.send(response)

    async def _send_ble(self, connection, message):
        try:
            raw_bytes = json.dumps(message).encode("utf-8")
            chunks = fragment_message(raw_bytes)
            for chunk in chunks:
                if not connection.is_connected():
                    break
                await self._rx_char.notify(connection, chunk)
                await asyncio.sleep_ms(15)  # Yield for BLE flow control
        except Exception as exc:
            util.log_exc("ble_transport", "Failed to send BLE notification", exc)

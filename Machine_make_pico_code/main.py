"""
main.py

Main entry point for MachineMake Device Server on Raspberry Pi Pico W / Pico 2 W.
Orchestrates:
  - Configuration & Identity initialization
  - Hardware mapping (with I2C bus scan)
  - Tool registration & security agent pipeline
  - Dual transports: Wi-Fi WebSocket (port 8765) + BLE GATT (aioble)
  - Interactive pairing & telemetry broadcasting loop
  - Non-blocking USB serial console & BOOTSEL button pairing approval
"""

import asyncio
import gc
import json
import machine
import socket
import sys
import time

import util
from auth import Authenticator, TrustStore
from capability_mapper import CapabilityMapper
from app_config import ConfigurationManager
from dcp_handler import DCPHandler
from device_agent import DeviceAgent
from event_manager import EventManager
from hardware_mapper import HardwareMapper
from identity import DeviceIdentity
from pairing import PairingManager
from pico_controller import PicoController
from safety_manager import SafetyManager
from session import SessionManager
from task_manager import TaskManager
from tool_registry import ToolRegistry
from transports.websocket_transport import WiFiDCPServer
from wifi_manager import WiFiManager

# Optional BLE transport import
try:
    from transports.bluetooth_transport import BluetoothDCPServer
    HAVE_BLE = True
except Exception:
    HAVE_BLE = False


async def udp_beacon_loop(identity, wifi_mgr, port=8765, beacon_port=8766, interval_s=2.5):
    """
    Broadcasts UDP beacons and responds to discovery queries on beacon_port (8766).
    Enables automatic zero-configuration discovery by the MachineMake Flutter app.
    """
    s = None
    while True:
        try:
            if not wifi_mgr.is_connected or wifi_mgr.ip_address in ("0.0.0.0", "", None):
                await asyncio.sleep(2.0)
                continue

            if s is None:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                except Exception:
                    pass
                try:
                    s.setsockopt(socket.SOL_SOCKET, 1, 1)
                except Exception:
                    pass
                s.setblocking(False)
                try:
                    s.bind(("0.0.0.0", beacon_port))
                except Exception as bind_err:
                    util.log("discovery", "UDP beacon bind notice: %s" % bind_err, "debug")

            payload = json.dumps({
                "dcp": "1.0",
                "device_id": identity.device_id,
                "name": identity.name,
                "profile": identity.profile,
                "ip": wifi_mgr.ip_address,
                "port": port,
                "type": "pico",
            })
            payload_bytes = payload.encode("utf-8")

            # Listen for discovery queries and reply directly to querier
            for _ in range(5):
                try:
                    data, addr = s.recvfrom(512)
                    if data:
                        try:
                            s.sendto(payload_bytes, addr)
                        except Exception:
                            pass
                except (OSError, Exception):
                    pass
                await asyncio.sleep(0.1)

            # Global subnet broadcast
            try:
                s.sendto(payload_bytes, ("255.255.255.255", beacon_port))
            except Exception:
                pass

            # Local subnet broadcast
            try:
                parts = wifi_mgr.ip_address.split(".")
                if len(parts) == 4:
                    subnet_bcast = "%s.%s.%s.255" % (parts[0], parts[1], parts[2])
                    s.sendto(payload_bytes, (subnet_bcast, beacon_port))
            except Exception:
                pass

            await asyncio.sleep(interval_s)

        except asyncio.CancelledError:
            if s:
                try:
                    s.close()
                except Exception:
                    pass
            break
        except Exception as exc:
            util.log("discovery", "UDP beacon loop warning: %s" % exc, "debug")
            if s:
                try:
                    s.close()
                except Exception:
                    pass
                s = None
            await asyncio.sleep(3.0)


async def telemetry_loop(event_manager, session_manager, pico_controller, interval_s=2.0):
    """Emits periodic telemetry events to subscribed sessions."""
    while True:
        try:
            await asyncio.sleep(interval_s)
            if not session_manager.sessions_subscribed_to("telemetry"):
                continue

            telemetry_data = pico_controller.telemetry()
            await event_manager.emit("telemetry", telemetry_data)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            util.log("telemetry", "error in telemetry loop: %s" % exc, "debug")


async def bootsel_monitor_loop(pairing_manager, pico_controller):
    """Monitors the physical BOOTSEL button to approve pending pairing requests."""
    pressed_count = 0
    while True:
        try:
            await asyncio.sleep_ms(100)
            if not pairing_manager.has_pending():
                continue

            # Signal pending pairing request with fast flash pattern
            if pico_controller.animation != "pairing":
                pico_controller.start_pattern("pairing")

            if pico_controller.bootsel_pressed():
                pressed_count += 1
                if pressed_count >= 3:  # Debounce: held for ~300ms
                    pico_controller.stop_animation()
                    pairing_manager.resolve_oldest(approved=True, via="bootsel")
                    pico_controller.blink_oke()
                    pressed_count = 0
            else:
                pressed_count = 0

            # If no more pending requests, restore pattern
            if not pairing_manager.has_pending() and pico_controller.animation == "pairing":
                pico_controller.stop_animation()

        except asyncio.CancelledError:
            break
        except Exception:
            pass


async def console_loop(pairing_manager, trust_store, session_manager, identity, pico_controller):
    """Non-blocking USB REPL console reader for pairing approval and diagnostic commands."""
    try:
        import select
        poll = select.poll()
        poll.register(sys.stdin, select.POLLIN)
    except Exception:
        poll = None

    banner = (
        "\n" + "=" * 60 + "\n"
        "  MACHINE MAKE PICO W DEVICE SERVER CONSOLE\n"
        "  Commands:\n"
        "    qr [name]    - Display QR pairing code in terminal\n"
        "    y / n        - Approve or deny pending pairing request\n"
        "    devices      - List paired clients & active sessions\n"
        "    status       - Show device status and runtime info\n"
        "    scan         - Rescan I2C buses\n"
        "    reboot       - Reboot microcontroller\n"
        "    help         - Show available commands\n"
        + "=" * 60 + "\n"
    )
    print(banner)

    line_buf = ""
    while True:
        try:
            if poll is not None:
                events = poll.poll(0)
                if events:
                    ch = sys.stdin.read(1)
                    if ch in ("\r", "\n"):
                        cmd_line = line_buf.strip()
                        line_buf = ""
                        if cmd_line:
                            await _process_console_command(
                                cmd_line, pairing_manager, trust_store,
                                session_manager, identity, pico_controller
                            )
                    elif ch:
                        line_buf += ch
            await asyncio.sleep_ms(100)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep_ms(200)


async def _process_console_command(cmd_line, pairing_manager, trust_store,
                                   session_manager, identity, pico_controller):
    parts = cmd_line.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("y", "yes"):
        req = pairing_manager.resolve_oldest(approved=True, via="console")
        if req:
            print("[server] Approved pairing for '%s'" % req.client_name)
        else:
            print("[server] No pending pairing requests.")
    elif cmd in ("n", "no"):
        req = pairing_manager.resolve_oldest(approved=False, via="console")
        if req:
            print("[server] Denied pairing for '%s'" % req.client_name)
        else:
            print("[server] No pending pairing requests.")
    elif cmd == "qr":
        client_name = arg or "Mobile App"
        pairing_manager.generate_qr_pairing(client_name=client_name)
    elif cmd in ("devices", "paired"):
        print("\n--- Paired Devices (TrustStore) ---")
        for cid, data in trust_store.items():
            print("  - %s: %s (permission: %s)" % (cid, data.get("name"), data.get("permission")))
        if trust_store.count() == 0:
            print("  No devices paired yet. Run 'qr' to pair a mobile app.")
        print("--- Active Sessions: %d ---" % len(session_manager.all_sessions()))
        for s in session_manager.all_sessions():
            print("  - Session %d: client=%s auth=%s control=%s (via %s)"
                  % (s.session_id, s.client_id, s.authenticated, s.has_control_ownership, s.transport))
        print()
    elif cmd == "status":
        print("\nDevice ID:   %s" % identity.device_id)
        print("Name:        %s" % identity.name)
        print("Profile:     %s" % identity.profile)
        stats = pico_controller.get_system_stats()
        print("Uptime:      %.1f s" % stats["uptime_s"])
        print("RAM:         %.1f KB free" % stats["memory"]["free_kb"])
        print("Core Temp:   %s C" % stats["temperature_c"])
        print("Active WS:   %d session(s)\n" % len(session_manager.all_sessions()))
    elif cmd == "scan":
        print("\nScanning I2C buses...")
        if pico_controller.hardware_mapper:
            buses = pico_controller.hardware_mapper.rescan_i2c()
            for b in buses:
                devs = [d["address"] for d in b.get("devices", [])]
                print("  Bus %d (SDA=%d, SCL=%d): %s" % (b["bus"], b["sda"], b["scl"], devs or "No devices"))
        print()
    elif cmd == "reboot":
        print("[server] Rebooting...")
        pico_controller.reboot(delay=1)
    elif cmd == "help":
        print("\nCommands: qr, y, n, devices, status, scan, reboot, help\n")
    else:
        print("[server] Unknown command '%s'. Type 'help' for options." % cmd)


async def main():
    util.log("main", "Starting MachineMake Device Server on Raspberry Pi Pico W...")

    # 1. Load Configuration
    config = ConfigurationManager()
    util.set_log_level(config.system.get("log_level", "info"))

    # 2. Hardware Controller
    pico_controller = PicoController(config.physical_configuration, config.safety_limits)
    pico_controller.blink_oke()

    # 3. Hardware Mapper & I2C bus scan
    hardware_mapper = HardwareMapper(config.physical_configuration)
    hardware_map = hardware_mapper.scan()
    pico_controller.hardware_mapper = hardware_mapper

    # Reserve SDA/SCL pins if I2C devices were found
    if config.physical_configuration.get("i2c", {}).get("reserve_pins_when_devices_found", True):
        for b in hardware_map.get("devices", {}).get("i2c", []):
            if b.get("devices"):
                pico_controller.reserve_pins([b["sda"], b["scl"]], "I2C bus %d" % b["bus"])

    # 4. Identity
    identity_cfg = config.identity
    identity = DeviceIdentity(
        name=identity_cfg.get("name", "MachineMake Pico W"),
        profile=identity_cfg.get("profile", "microcontroller"),
        device_type=identity_cfg.get("type", "device"),
        device_id=identity_cfg.get("device_id"),
    )
    util.log("main", "Device Identity: %s (profile: %s, name: '%s')"
             % (identity.device_id, identity.profile, identity.name))

    # 5. Capabilities & Tool Registry
    capability_mapper = CapabilityMapper(pico_controller)
    capabilities = capability_mapper.build(hardware_map, config.physical_configuration)

    tool_registry = ToolRegistry()
    tool_registry.build_default_tools(
        capabilities, pico_controller, config.safety_limits, hardware_mapper=hardware_mapper
    )
    util.log("main", "Registered %d tools: %s"
             % (len(tool_registry.all_tools()), list(tool_registry.all_tools().keys())))

    # 6. Trust Store, Authenticator, Session & Event Managers
    trust_store = TrustStore()
    authenticator = Authenticator(trust_store)
    session_manager = SessionManager()
    safety_manager = SafetyManager()
    event_manager = EventManager(session_manager, identity=identity)
    task_manager = TaskManager(event_manager, safety_manager)
    device_agent = DeviceAgent(tool_registry, safety_manager, task_manager)

    # Connection-loss policy hook
    loss_policy = config.connection_loss_policy.get("interactive_control", "safe_stop")
    if loss_policy == "safe_stop":
        def _on_session_dropped(session, had_control):
            if had_control:
                util.log("main", "Control session disconnected; executing safe_stop", "warning")
                pico_controller.safe_stop()
        session_manager.on_session_removed(_on_session_dropped)

    # 7. Wi-Fi Manager
    wifi_mgr = WiFiManager(config.wifi)
    pico_controller.wifi = wifi_mgr

    transports_cfg = config.transports
    wifi_cfg = transports_cfg.get("wifi", {})

    if wifi_cfg.get("enabled", True):
        await wifi_mgr.start()
        if config.system.get("ntp_sync", True):
            asyncio.create_task(wifi_mgr.sync_ntp())

    port = wifi_cfg.get("port", 8765)

    # 8. Pairing Manager
    pairing_manager = PairingManager(
        trust_store,
        identity=identity,
        settings=config.pairing,
        host_provider=lambda: wifi_mgr.ip_address,
        port=port,
    )

    # 9. DCP Handler
    dcp_handler = DCPHandler(
        identity=identity,
        hardware_mapper=hardware_mapper,
        config=config,
        tool_registry=tool_registry,
        device_agent=device_agent,
        task_manager=task_manager,
        session_manager=session_manager,
        trust_store=trust_store,
        authenticator=authenticator,
        capabilities=capabilities,
        pairing_manager=pairing_manager,
    )

    # 10. Start Wi-Fi WebSocket DCP Server
    if wifi_cfg.get("enabled", True):
        wifi_server = WiFiDCPServer(
            dcp_handler, session_manager,
            host="0.0.0.0", port=port,
            max_clients=wifi_cfg.get("max_clients", 3)
        )
        await wifi_server.start()
        beacon_port = wifi_cfg.get("beacon_port", 8766)
        asyncio.create_task(udp_beacon_loop(identity, wifi_mgr, port=port, beacon_port=beacon_port))

    # 11. Start Bluetooth BLE GATT DCP Server
    ble_cfg = transports_cfg.get("ble", {})
    if HAVE_BLE and ble_cfg.get("enabled", True):
        ble_server = BluetoothDCPServer(
            dcp_handler, session_manager,
            device_name=identity.name,
            adv_interval_ms=ble_cfg.get("adv_interval_ms", 250)
        )
        await ble_server.start()
    else:
        ble_server = None

    # Print pairing invitation if no trusted devices exist
    if trust_store.count() == 0 and config.pairing.get("auto_qr_on_boot", True):
        pairing_manager.generate_qr_pairing()

    # 12. Background Tasks: Telemetry loop, BOOTSEL button monitor, Console
    telemetry_interval = config.telemetry.get("interval_s", 2.0)
    asyncio.create_task(telemetry_loop(event_manager, session_manager, pico_controller, telemetry_interval))

    if config.pairing.get("bootsel_confirm", True):
        asyncio.create_task(bootsel_monitor_loop(pairing_manager, pico_controller))

    if config.system.get("console", True):
        asyncio.create_task(console_loop(
            pairing_manager, trust_store, session_manager, identity, pico_controller
        ))

    util.log("main", "MachineMake Device Server running. Waiting for DCP clients...")

    # Keep main event loop running
    while True:
        await asyncio.sleep(30)
        gc.collect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[main] Server halted.")
    except Exception as exc:
        print("\n[main] Fatal server error:")
        sys.print_exception(exc)

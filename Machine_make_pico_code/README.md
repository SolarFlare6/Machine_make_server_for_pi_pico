# MachineMake Device Server for Raspberry Pi Pico W

This is the full **MicroPython** implementation of the **Device Server** for the **Raspberry Pi Pico W** (and Pico 2 W), completely protocol-compatible with the [MachineMake](file:///c:/Users/damja/Desktop/PIDuino/raspberry%20pi/final_robot_project/app/hardware_layer_for_machine_make/device_server) reference server and the mobile application.

---

## 📁 Project Structure

```
Machine_make_pico_code/
├── main.py                     # Entry point (event loops, task runner, console)
├── pico_controller.py          # Hardware controller (LED, GPIO, PWM, ADC, temp, power)
├── hardware_mapper.py          # Hardware detection with machine.I2C scanning
├── capability_mapper.py        # Translates hardware/config into DCP capabilities
├── tool_registry.py            # DCP tools definitions & dispatchers
├── dcp_handler.py              # Dual-envelope DCP protocol dispatcher (Formats A & B)
├── device_agent.py             # Security pipeline: Auth -> Perm -> Args -> Safety -> Run
├── safety_manager.py           # Range/constraint checks & control ownership validation
├── session.py                  # Client sessions & single CONTROL owner management
├── auth.py                     # TrustStore & HMAC-SHA256 challenge-response
├── pairing.py                  # QR & interactive pairing (console + BOOTSEL button)
├── event_manager.py            # DCP event broadcasting (telemetry, task updates)
├── task_manager.py             # Asynchronous long-running task manager
├── wifi_manager.py             # Wi-Fi Station connection, AP fallback, & NTP sync
├── util.py                     # MicroPython crypto, time, uuid, and file helpers
├── config.py                   # Configuration manager
├── config/
│   ├── device_config.json      # Device settings, pins, and limits
│   └── wifi.json               # Wi-Fi credentials (SSID & password)
├── transports/
│   ├── websocket_transport.py  # Asyncio WebSocket server on port 8765
│   └── bluetooth_transport.py  # BLE GATT peripheral server (aioble)
└── host_tools/
    └── make_qr.py              # Host PC tool to render pairing QR codes on screen
```

---

## ⚙️ Configuration

### 1. Wi-Fi Setup
Edit `config/wifi.json`:
```json
{
  "networks": [
    {
      "ssid": "YOUR_WIFI_SSID",
      "password": "YOUR_WIFI_PASSWORD"
    }
  ],
  "country": "DE",
  "hostname": "machinemake-pico",
  "ap_fallback": true,
  "ap_ssid": "MachineMake-Pico",
  "ap_password": "machinemake"
}
```
If your configured Wi-Fi network is not reachable, the Pico W automatically activates its Access Point (`MachineMake-Pico`) so you can still connect.

---

## 🚀 How to Run on Pico W

1. **Flash MicroPython**: Install the official Pico W MicroPython firmware (`RPI_PICO_W-*.uf2`).
2. **Install aioble (for Bluetooth support)**:
   Via Thonny / mpremote:
   ```bash
   mpremote mip install aioble
   ```
3. **Upload Files**: Copy all files from `Machine_make_pico_code/` onto the root of the Pico W filesystem.
4. **Boot**: Run `main.py` (or rename to `main.py` so it starts automatically on power-on).

---

## 📱 Pairing with MachineMake Mobile App

### Method 1: QR Code
1. On boot, the Pico W outputs a `QR_PAYLOAD:{...}` line over the USB serial console.
2. Run the helper on your PC to view the QR code in your terminal:
   ```bash
   python host_tools/make_qr.py --port COMx
   ```
3. Scan it using the MachineMake mobile app.

### Method 2: Interactive Pairing
When the mobile app requests Wi-Fi or Bluetooth pairing:
- **Via Console**: Type `y` and press Enter in the serial REPL.
- **Via Physical Button**: Press and hold the **BOOTSEL** button on the Pico W for ~0.5 seconds while the onboard LED is flashing.

---

## 🔌 Hardware Features Supported

- **Onboard LED**: Animations (`blink_oke`, `blink_warning`, `flash_alert`, `animate_running_process`).
- **I2C Scanner**: Scans hardware I2C controllers (default: GP4/GP5 and GP6/GP7) and hints detected ICs (PCA9685, MPU6050, SSD1306, BMP280, etc.).
- **GPIO**: Digital read/write across all user header pins (GP0-GP22, GP26-GP28). Radio pins GP23-25/GP29 are protected.
- **PWM / Servo**: High-resolution hardware PWM (10 Hz – 100 kHz) and microsecond servo angle control.
- **ADC**: Analog reads on GP26, GP27, GP28, plus internal chip temperature.
- **Power**: System reset, deep sleep, and bootloader triggers.

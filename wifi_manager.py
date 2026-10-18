"""
wifi_manager.py

Wi-Fi Connection Manager for Raspberry Pi Pico W (CYW43 driver).
Handles:
  - Station connection with auto-reconnect and multiple network fallback.
  - Access Point fallback mode if station connection fails.
  - NTP time synchronization.
  - Status and IP telemetry queries.
"""

import asyncio
import network
import socket
import struct
import time
import util


class WiFiManager:
    def __init__(self, wifi_config):
        self._cfg = wifi_config or {}
        self._wlan_sta = network.WLAN(network.STA_IF)
        self._wlan_ap = network.WLAN(network.AP_IF)
        self._connected = False
        self._ip = "0.0.0.0"
        self._is_ap = False
        self.hostname = self._cfg.get("hostname", "machinemake-pico")

    @property
    def is_connected(self):
        return self._connected or self._is_ap

    @property
    def ip_address(self):
        return self._ip

    def rssi(self):
        if self._connected and self._wlan_sta.isconnected():
            try:
                return self._wlan_sta.status("rssi")
            except Exception:
                return None
        return None

    def status(self):
        return {
            "connected": self.is_connected,
            "mode": "ap" if self._is_ap else ("sta" if self._connected else "disconnected"),
            "ip": self._ip,
            "rssi": self.rssi(),
            "hostname": self.hostname,
        }

    async def start(self):
        """Attempts connection to configured networks; falls back to AP mode if configured."""
        try:
            import rp2
            country = self._cfg.get("country", "DE")
            rp2.country(country)
        except Exception:
            pass

        self._wlan_sta.active(True)

        try:
            self._wlan_sta.config(hostname=self.hostname)
        except Exception:
            pass

        # 1. Try Station networks
        networks = self._cfg.get("networks", [])
        timeout_s = self._cfg.get("connect_timeout_s", 15)

        for net in networks:
            ssid = net.get("ssid")
            password = net.get("password")
            if not ssid:
                continue

            util.log("wifi", "Connecting to Wi-Fi SSID '%s'..." % ssid)
            try:
                self._wlan_sta.connect(ssid, password)
            except Exception as conn_err:
                util.log("wifi", "Connect call error: %s" % conn_err, "warning")

            start_t = time.ticks_ms()
            while not self._wlan_sta.isconnected():
                if time.ticks_diff(time.ticks_ms(), start_t) > timeout_s * 1000:
                    break
                await asyncio.sleep_ms(300)

            if self._wlan_sta.isconnected():
                self._connected = True
                self._ip = self._wlan_sta.ifconfig()[0]
                util.log("wifi", "Connected to '%s'! IP: %s" % (ssid, self._ip))
                return True
            else:
                util.log("wifi", "Failed to connect to '%s'" % ssid, "warning")

        # 2. Check AP Fallback
        if self._cfg.get("ap_fallback", True):
            return await self.start_ap()

        return False

    async def start_ap(self):
        """Starts an Access Point network for setup or standalone operation."""
        ap_ssid = self._cfg.get("ap_ssid", "MachineMake-Pico")
        ap_pass = self._cfg.get("ap_password", "machinemake")

        util.log("wifi", "Starting Access Point SSID '%s'..." % ap_ssid)
        try:
            self._wlan_sta.active(False)
            await asyncio.sleep_ms(200)

            self._wlan_ap.active(True)
            self._wlan_ap.config(essid=ap_ssid, password=ap_pass, authmode=network.AUTH_WPA2_PSK)

            for _ in range(25):  # up to 5 seconds
                if self._wlan_ap.active():
                    break
                await asyncio.sleep_ms(200)

            self._is_ap = True
            self._ip = self._wlan_ap.ifconfig()[0]
            util.log("wifi", "Access Point active. Connect to '%s' (password: '%s'). IP: %s"
                     % (ap_ssid, ap_pass, self._ip))
            return True
        except Exception as exc:
            util.log("wifi", "Failed to start Access Point: %s" % exc, "error")
            return False

    async def sync_ntp(self):
        """Simple SNTP client to sync clock over Wi-Fi."""
        if not self._connected:
            return
        NTP_DELTA = 2208988800  # 1970-01-01 00:00:00 vs 1900-01-01
        NTP_HOST = "pool.ntp.org"

        try:
            addr = socket.getaddrinfo(NTP_HOST, 123)[0][-1]
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(3.0)
            msg = b"\x1b" + 47 * b"\0"
            s.sendto(msg, addr)
            msg, _ = s.recvfrom(48)
            s.close()

            val = struct.unpack("!I", msg[40:44])[0]
            t = val - NTP_DELTA
            tm = time.gmtime(t)
            # machine.RTC().datetime((year, month, day, weekday, hours, minutes, seconds, subseconds))
            import machine
            machine.RTC().datetime((tm[0], tm[1], tm[2], tm[6] + 1, tm[3], tm[4], tm[5], 0))
            util.log("wifi", "NTP time synchronized: %04d-%02d-%02d %02d:%02d:%02d UTC"
                     % (tm[0], tm[1], tm[2], tm[3], tm[4], tm[5]))
        except Exception as exc:
            util.log("wifi", "NTP sync failed: %s" % exc, "warning")

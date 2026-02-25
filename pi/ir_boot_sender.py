#!/usr/bin/env python3
"""
ir_boot_sender.py — Boot-time IR code sender for TVWIZ IR Controller
---------------------------------------------------------------------
This script is meant to run automatically when the Raspberry Pi boots
(via systemd). It:

  1. Reads boot_config.json (created/edited by ir_recorder.py)
  2. Connects to the ESP32 over USB serial
  3. Pings the ESP32 to verify connectivity
  4. For every code that has "send_on_boot": true, it learns/sends the
     code (if a raw_data or value/type is stored) or logs a warning.
  5. Exits cleanly so systemd can track completion.

Boot config format (boot_config.json):
{
  "tv1_power": {
    "type": "NEC",
    "value": "0x20DF10EF",
    "bits": 32,
    "send_on_boot": true,
    "description": "Main hall TV — power on",
    "delay_before_ms": 0
  },
  "tv2_power": {
    "type": "RAW",
    "freq": 38000,
    "data": [9024, 4512, 564, 1692, 564],
    "send_on_boot": true,
    "description": "Breakout room TV — power on",
    "delay_before_ms": 500
  }
}

Setup:
  See README.md for instructions on installing this as a systemd service.
"""

import serial
import json
import time
import argparse
import os
import sys
import logging

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BOOT_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "boot_config.json")
DEFAULT_PORT = "/dev/ttyUSB0"
DEFAULT_BAUD = 115200
PING_RETRIES = 5
PING_RETRY_DELAY = 2  # seconds between ping attempts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ir_boot_sender")


# ---------------------------------------------------------------------------
# Serial helpers
# ---------------------------------------------------------------------------

def open_serial(port: str, baud: int) -> serial.Serial:
    """Open the serial port, retrying if the device is not yet available."""
    for attempt in range(1, PING_RETRIES + 1):
        try:
            ser = serial.Serial(port, baud, timeout=5)
            time.sleep(1.5)  # allow ESP32 to finish boot/reset
            return ser
        except serial.SerialException as exc:
            log.warning(f"Attempt {attempt}/{PING_RETRIES}: cannot open {port} — {exc}")
            time.sleep(PING_RETRY_DELAY)
    log.error(f"Could not open serial port {port} after {PING_RETRIES} attempts.")
    sys.exit(1)


def send_cmd(ser: serial.Serial, cmd: dict) -> dict:
    """Send a JSON command and return the first JSON response line."""
    line = json.dumps(cmd) + "\n"
    ser.write(line.encode())
    raw = ser.readline()
    if not raw:
        return {"ok": False, "err": "no_response"}
    try:
        return json.loads(raw.decode().strip())
    except json.JSONDecodeError as exc:
        return {"ok": False, "err": f"json_parse: {exc}"}


def ping_esp32(ser: serial.Serial) -> bool:
    """Ping the ESP32, retrying a few times."""
    for attempt in range(1, PING_RETRIES + 1):
        resp = send_cmd(ser, {"cmd": "ping"})
        if resp.get("ok") and resp.get("msg") == "pong":
            return True
        log.warning(f"Ping attempt {attempt}/{PING_RETRIES} failed: {resp}")
        time.sleep(PING_RETRY_DELAY)
    return False


# ---------------------------------------------------------------------------
# Code loading
# ---------------------------------------------------------------------------

def load_boot_config(path: str) -> dict:
    if not os.path.exists(path):
        log.error(f"boot_config.json not found at {path}. Run ir_recorder.py first.")
        sys.exit(1)
    with open(path) as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as exc:
            log.error(f"Invalid JSON in {path}: {exc}")
            sys.exit(1)


# ---------------------------------------------------------------------------
# Sending logic
# ---------------------------------------------------------------------------

def load_code_into_esp32(ser: serial.Serial, name: str, entry: dict) -> bool:
    """
    Push a code into ESP32 RAM so it can be sent.

    For decoded protocols (NEC, SONY, …) we use a hypothetical 'define'
    command — extend the ESP32 firmware with this command if needed.
    For RAW codes we use 'define_raw'.

    If neither value nor data is present the boot config was saved without
    full signal data (see ir_recorder.py notes) and we skip the code.
    """
    code_type = entry.get("type", "").upper()

    if code_type == "RAW":
        freq = entry.get("freq", 38000)
        data = entry.get("data")
        if not data:
            log.warning(f"  [{name}] RAW code has no 'data' field — skipping.")
            return False
        cmd = {"cmd": "define_raw", "name": name, "freq": freq, "data": data}

    elif code_type in ("NEC", "SONY", "RC5", "RC6", "SAMSUNG", "LG", "SHARP",
                       "PANASONIC", "JVC", "WHYNTER", "AIWA_RC_T501", "SANYO",
                       "MITSUBISHI", "DENON", "LEGO_PF", "BOSEWAVE", "MAGIQUEST"):
        value = entry.get("value")
        bits = entry.get("bits", 32)
        if value is None:
            log.warning(f"  [{name}] {code_type} code has no 'value' field — skipping.")
            return False
        cmd = {"cmd": "define", "name": name, "type": code_type,
               "value": value, "bits": bits}

    else:
        log.warning(f"  [{name}] Unknown type '{code_type}' — skipping.")
        return False

    resp = send_cmd(ser, cmd)
    if resp.get("ok"):
        log.info(f"  [{name}] Loaded into ESP32 RAM ✓")
        return True
    else:
        log.warning(f"  [{name}] Failed to load: {resp.get('err', 'unknown')}")
        return False


def send_code(ser: serial.Serial, name: str, repeats: int = 0) -> bool:
    resp = send_cmd(ser, {"cmd": "send", "name": name, "repeats": repeats})
    if resp.get("ok"):
        log.info(f"  [{name}] Sent ✓")
        return True
    else:
        log.warning(f"  [{name}] Send failed: {resp.get('err', 'unknown')}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="TVWIZ IR Boot Sender")
    parser.add_argument("--port", default=DEFAULT_PORT,
                        help="Serial port of the ESP32 (default: /dev/ttyUSB0)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--config", default=BOOT_CONFIG_FILE,
                        help="Path to boot_config.json")
    args = parser.parse_args()

    log.info("TVWIZ IR Boot Sender starting…")
    log.info(f"Config: {args.config}")
    log.info(f"Port:   {args.port} @ {args.baud} baud")

    # 1. Load config
    config = load_boot_config(args.config)
    codes_to_send = {k: v for k, v in config.items() if v.get("send_on_boot")}

    if not codes_to_send:
        log.info("No codes marked with 'send_on_boot': true — nothing to do.")
        sys.exit(0)

    log.info(f"Codes to send: {list(codes_to_send.keys())}")

    # 2. Open serial
    ser = open_serial(args.port, args.baud)

    # 3. Ping check
    log.info("Pinging ESP32…")
    if not ping_esp32(ser):
        log.error("ESP32 not responding. Aborting.")
        ser.close()
        sys.exit(1)
    log.info("ESP32 connected ✓")

    # 4. Load and send each code in order
    sent = 0
    failed = 0
    for name, entry in codes_to_send.items():
        desc = entry.get("description", "")
        label = f"{name}" + (f" ({desc})" if desc else "")
        log.info(f"Processing: {label}")

        delay_ms = entry.get("delay_before_ms", 0)
        if delay_ms > 0:
            log.info(f"  Waiting {delay_ms} ms before sending…")
            time.sleep(delay_ms / 1000)

        # Load the code into ESP32 RAM first
        if load_code_into_esp32(ser, name, entry):
            if send_code(ser, name):
                sent += 1
            else:
                failed += 1
        else:
            failed += 1

    ser.close()

    log.info(f"Done — sent: {sent}, failed: {failed}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

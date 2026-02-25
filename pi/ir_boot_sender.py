#!/usr/bin/env python3
"""
ir_boot_sender.py — Boot-time IR code sender for TVWIZ IR Controller
---------------------------------------------------------------------
Designed to run as a systemd oneshot service at Raspberry Pi startup.

Flow:
  1. Load boot_config.json (written by ir_recorder.py)
  2. Open serial to ESP32, retry until device appears
  3. Ping ESP32
  4. For each code with "send_on_boot": true:
       a. Push it into ESP32 RAM with define / define_raw
       b. Send it
  5. Exit 0 on success, 1 if any send failed

boot_config.json is self-contained: it carries all signal data so no
remote control is needed at boot time.

Usage (manual test):
    python3 ir_boot_sender.py [--port /dev/ttyUSB0] [--config boot_config.json]

See README.md for systemd installation instructions.
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
MAX_RETRIES = 5
RETRY_DELAY = 2   # seconds between retries

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
    """Open serial, retrying until the device appears (ESP32 may be slow to enumerate)."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            ser = serial.Serial(port, baud, timeout=5)
            time.sleep(1.5)           # let ESP32 finish boot
            ser.reset_input_buffer()  # discard boot message
            return ser
        except serial.SerialException as exc:
            log.warning(f"Attempt {attempt}/{MAX_RETRIES}: {exc}")
            time.sleep(RETRY_DELAY)
    log.error(f"Could not open {port} after {MAX_RETRIES} attempts.")
    sys.exit(1)


def send_cmd(ser: serial.Serial, cmd: dict) -> dict:
    ser.write((json.dumps(cmd) + "\n").encode())
    raw = ser.readline()
    if not raw:
        return {"ok": False, "err": "no_response"}
    try:
        return json.loads(raw.decode().strip())
    except json.JSONDecodeError as exc:
        return {"ok": False, "err": f"json_parse: {exc}"}


def ping_esp32(ser: serial.Serial) -> bool:
    for attempt in range(1, MAX_RETRIES + 1):
        resp = send_cmd(ser, {"cmd": "ping"})
        if resp.get("ok") and resp.get("msg") == "pong":
            return True
        log.warning(f"Ping {attempt}/{MAX_RETRIES} failed: {resp}")
        time.sleep(RETRY_DELAY)
    return False


# ---------------------------------------------------------------------------
# Config loader
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
# Code push + send
# ---------------------------------------------------------------------------

def push_code(ser: serial.Serial, name: str, entry: dict) -> bool:
    """
    Push a code into ESP32 RAM using define or define_raw.
    Returns True on success.
    """
    code_type = entry.get("type", "").upper()

    if code_type == "RAW":
        freq = entry.get("freq", 38000)
        data = entry.get("data")
        if not data:
            log.warning(f"  [{name}] No 'data' field in boot_config — skipping.")
            return False
        cmd = {"cmd": "define_raw", "name": name, "freq": freq, "data": data}

    else:
        # Decoded protocol (NEC, SONY, SAMSUNG, etc.)
        value = entry.get("value")
        bits  = entry.get("bits", 32)
        if value is None:
            log.warning(f"  [{name}] No 'value' field in boot_config — skipping.")
            return False
        cmd = {"cmd": "define", "name": name, "type": code_type,
               "value": str(value), "bits": bits}

    resp = send_cmd(ser, cmd)
    if resp.get("ok"):
        log.info(f"  [{name}] Loaded into ESP32 RAM ✓")
        return True
    else:
        log.warning(f"  [{name}] define failed: {resp.get('err', 'unknown')}")
        return False


def fire_code(ser: serial.Serial, name: str) -> bool:
    resp = send_cmd(ser, {"cmd": "send", "name": name, "repeats": 0})
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
    parser.add_argument("--port",   default=DEFAULT_PORT)
    parser.add_argument("--baud",   type=int, default=DEFAULT_BAUD)
    parser.add_argument("--config", default=BOOT_CONFIG_FILE)
    args = parser.parse_args()

    log.info("TVWIZ IR Boot Sender starting…")
    log.info(f"Config : {args.config}")
    log.info(f"Port   : {args.port} @ {args.baud} baud")

    # 1. Load config
    config = load_boot_config(args.config)
    to_send = {k: v for k, v in config.items() if v.get("send_on_boot")}

    if not to_send:
        log.info("No codes marked 'send_on_boot': true — nothing to do.")
        sys.exit(0)

    log.info(f"Will send: {list(to_send.keys())}")

    # 2. Open serial
    ser = open_serial(args.port, args.baud)

    # 3. Ping
    log.info("Pinging ESP32…")
    if not ping_esp32(ser):
        log.error("ESP32 not responding. Aborting.")
        ser.close()
        sys.exit(1)
    log.info("ESP32 connected ✓")

    # 4. Push + send each code
    sent = failed = 0
    for name, entry in to_send.items():
        desc = entry.get("description", "")
        log.info(f"Processing: {name}" + (f" ({desc})" if desc else ""))

        delay_ms = entry.get("delay_before_ms", 0)
        if delay_ms > 0:
            log.info(f"  Waiting {delay_ms} ms…")
            time.sleep(delay_ms / 1000)

        if push_code(ser, name, entry) and fire_code(ser, name):
            sent += 1
        else:
            failed += 1

    ser.close()
    log.info(f"Done — sent: {sent}, failed: {failed}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

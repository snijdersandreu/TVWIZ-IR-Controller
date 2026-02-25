#!/usr/bin/env python3
"""
ir_recorder.py — Interactive IR code recorder for TVWIZ IR Controller
----------------------------------------------------------------------
Run this script on a Raspberry Pi to learn and test IR codes via the
ESP32 IR blaster connected over USB serial.

Learned codes are saved to codes.json in the same directory.

Usage:
    python3 ir_recorder.py [--port /dev/ttyUSB0] [--baud 115200]

Menu options:
    l  - Learn a new IR code (give it a name/descriptor)
    t  - Test/send a learned code
    s  - Show all learned codes
    e  - Erase a code
    w  - Write/save codes to codes.json
    q  - Quit
"""

import serial
import json
import time
import argparse
import os
import sys

CODES_FILE = os.path.join(os.path.dirname(__file__), "codes.json")
DEFAULT_PORT = "/dev/ttyUSB0"
DEFAULT_BAUD = 115200
LEARN_TIMEOUT_MS = 15000  # 15 seconds to point the remote


def open_serial(port: str, baud: int) -> serial.Serial:
    try:
        s = serial.Serial(port, baud, timeout=5)
        time.sleep(1.5)  # allow ESP32 to boot/reset
        return s
    except serial.SerialException as exc:
        print(f"[ERROR] Cannot open {port}: {exc}")
        sys.exit(1)


def send_cmd(ser: serial.Serial, cmd: dict) -> dict:
    """Send a JSON command and return the first JSON response."""
    line = json.dumps(cmd) + "\n"
    ser.write(line.encode())
    raw = ser.readline()
    if not raw:
        return {"ok": False, "err": "no_response"}
    return json.loads(raw.decode().strip())


def ping(ser: serial.Serial) -> bool:
    resp = send_cmd(ser, {"cmd": "ping"})
    return resp.get("ok") and resp.get("msg") == "pong"


def learn_code(ser: serial.Serial) -> None:
    name = input("  Enter a descriptor for this code (e.g. tv1_power): ").strip()
    if not name:
        print("  [!] Name cannot be empty.")
        return

    print(f"  Sending learn command… point your remote at the IR receiver now.")
    ack = send_cmd(ser, {"cmd": "learn", "name": name, "timeout_ms": LEARN_TIMEOUT_MS})
    if not ack.get("ok"):
        print(f"  [ERROR] {ack.get('err', 'unknown')}")
        return
    if ack.get("msg") == "learn_ready":
        print(f"  ESP32 ready — you have {LEARN_TIMEOUT_MS // 1000}s to press the button…")

    # Wait for the captured result (second response)
    raw = ser.readline()
    if not raw:
        print("  [ERROR] Timed out waiting for captured signal.")
        return
    result = json.loads(raw.decode().strip())
    if not result.get("ok"):
        print(f"  [ERROR] {result.get('err', 'unknown')}")
        return

    print(f"  ✓ Captured: {result}")
    print("  Code is stored in ESP32 RAM. Use 'w' to save it to codes.json.")


def test_code(ser: serial.Serial) -> None:
    name = input("  Enter the code name to send: ").strip()
    resp = send_cmd(ser, {"cmd": "send", "name": name, "repeats": 0})
    if resp.get("ok"):
        print(f"  ✓ Sent '{name}' successfully.")
    else:
        print(f"  [ERROR] {resp.get('err', 'unknown')}")


def list_codes(ser: serial.Serial) -> None:
    resp = send_cmd(ser, {"cmd": "list"})
    if not resp.get("ok"):
        print(f"  [ERROR] {resp.get('err', 'unknown')}")
        return
    codes = resp.get("codes", [])
    if not codes:
        print("  (no codes stored in ESP32 RAM)")
        return
    print(f"  {'Name':<30} {'Type'}")
    print(f"  {'-'*30} {'-'*10}")
    for c in codes:
        print(f"  {c['name']:<30} {c.get('type', '?')}")


def erase_code(ser: serial.Serial) -> None:
    name = input("  Enter the code name to erase: ").strip()
    resp = send_cmd(ser, {"cmd": "erase", "name": name})
    if resp.get("ok"):
        print(f"  ✓ Erased '{name}'.")
    else:
        print(f"  [ERROR] {resp.get('err', 'unknown')}")


def save_codes(ser: serial.Serial) -> None:
    """
    Fetch all codes metadata via 'list', then ask for any missing context
    and persist a codes.json that the boot script can consume.

    Note: The ESP32 stores the raw signal in RAM only. This function saves
    the *name* and *type* as a manifest so the boot script knows which names
    to send.  The ESP32 must still have the codes in RAM (or be re-learned)
    during the same session for the boot script to work.

    For a permanent store you would need to implement a 'dump' command in the
    firmware that returns the full signal data; this scaffold is ready to be
    extended.
    """
    resp = send_cmd(ser, {"cmd": "list"})
    if not resp.get("ok"):
        print(f"  [ERROR] {resp.get('err', 'unknown')}")
        return
    codes = resp.get("codes", [])
    if not codes:
        print("  (nothing to save)")
        return

    # Path for boot config
    boot_cfg_path = os.path.join(os.path.dirname(__file__), "boot_config.json")

    existing = {}
    if os.path.exists(boot_cfg_path):
        with open(boot_cfg_path) as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError:
                pass

    # Merge new codes into existing config
    config = existing.copy()
    for c in codes:
        name = c["name"]
        if name not in config:
            config[name] = {
                "type": c.get("type", "UNKNOWN"),
                "send_on_boot": False,
                "description": "",
            }

    with open(boot_cfg_path, "w") as f:
        json.dump(config, f, indent=2)

    print(f"  ✓ Saved {len(codes)} code(s) to {boot_cfg_path}")
    print("  Edit boot_config.json to set 'send_on_boot': true for codes")
    print("  that should be fired when the Pi starts up.")


def main() -> None:
    parser = argparse.ArgumentParser(description="TVWIZ IR Recorder")
    parser.add_argument("--port", default=DEFAULT_PORT, help="Serial port of the ESP32")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="Baud rate")
    args = parser.parse_args()

    print("=" * 50)
    print("  TVWIZ IR Recorder")
    print(f"  Port: {args.port}  Baud: {args.baud}")
    print("=" * 50)

    ser = open_serial(args.port, args.baud)

    print("  Pinging ESP32…", end=" ")
    if ping(ser):
        print("OK ✓")
    else:
        print("FAILED — check connection and try again.")
        ser.close()
        sys.exit(1)

    MENU = """
  Commands:
    l  — Learn a new IR code
    t  — Test/send a code
    s  — Show codes in ESP32 RAM
    e  — Erase a code from ESP32 RAM
    w  — Write codes to boot_config.json
    q  — Quit
"""

    while True:
        print(MENU)
        choice = input("  > ").strip().lower()
        if choice == "l":
            learn_code(ser)
        elif choice == "t":
            test_code(ser)
        elif choice == "s":
            list_codes(ser)
        elif choice == "e":
            erase_code(ser)
        elif choice == "w":
            save_codes(ser)
        elif choice == "q":
            break
        else:
            print("  Unknown command.")

    ser.close()
    print("  Bye!")


if __name__ == "__main__":
    main()

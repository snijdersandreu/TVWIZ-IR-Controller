#!/usr/bin/env python3
"""
ir_recorder.py — Interactive IR code recorder for TVWIZ IR Controller
----------------------------------------------------------------------
Run this on a Raspberry Pi connected to the ESP32 over USB serial.

Learned codes are cached in memory (with their full signal payloads) and
written to boot_config.json when you press 'w'.  boot_config.json is the
single source of truth for ir_boot_sender.py.

Usage:
    python3 ir_recorder.py [--port /dev/ttyUSB0] [--baud 115200]

Menu:
    l  — Learn a new IR code  (stores full payload in memory)
    t  — Test / send a code   (fires the code from ESP32 RAM)
    s  — Show codes in memory + ESP32 RAM status
    e  — Erase a code from both memory and ESP32 RAM
    w  — Write boot_config.json  (self-contained, ready for boot sender)
    q  — Quit
"""

import serial
import json
import time
import argparse
import os
import sys

BOOT_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "boot_config.json")
DEFAULT_PORT = "/dev/ttyUSB0"
DEFAULT_BAUD = 115200
LEARN_TIMEOUT_MS = 15000


# ---------------------------------------------------------------------------
# Serial helpers
# ---------------------------------------------------------------------------

def open_serial(port: str, baud: int) -> serial.Serial:
    try:
        s = serial.Serial(port, baud, timeout=10)
        time.sleep(1.5)          # let ESP32 finish boot/reset
        s.reset_input_buffer()   # discard the boot "ok"/"boot" message
        return s
    except serial.SerialException as exc:
        print(f"[ERROR] Cannot open {port}: {exc}")
        sys.exit(1)


def send_cmd(ser: serial.Serial, cmd: dict) -> dict:
    """Send a JSON command and read the first response line."""
    ser.write((json.dumps(cmd) + "\n").encode())
    raw = ser.readline()
    if not raw:
        return {"ok": False, "err": "no_response"}
    try:
        return json.loads(raw.decode().strip())
    except json.JSONDecodeError as exc:
        return {"ok": False, "err": f"json_parse: {exc}"}


def ping(ser: serial.Serial) -> bool:
    resp = send_cmd(ser, {"cmd": "ping"})
    return resp.get("ok") and resp.get("msg") == "pong"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def learn_code(ser: serial.Serial, cache: dict) -> None:
    name = input("  Descriptor for this code (e.g. tv1_power): ").strip()
    if not name:
        print("  [!] Name cannot be empty.")
        return

    print(f"  Sending learn… point remote at IR receiver now.")
    ack = send_cmd(ser, {"cmd": "learn", "name": name, "timeout_ms": LEARN_TIMEOUT_MS})
    if not ack.get("ok"):
        print(f"  [ERROR] {ack.get('err', 'unknown')}")
        return
    if ack.get("msg") == "learn_ready":
        print(f"  ESP32 ready — {LEARN_TIMEOUT_MS // 1000}s to press the button…")

    # Second response: the captured payload (or an error)
    raw = ser.readline()
    if not raw:
        print("  [ERROR] Timed out waiting for capture result.")
        return
    try:
        result = json.loads(raw.decode().strip())
    except json.JSONDecodeError:
        print("  [ERROR] Malformed response from ESP32.")
        return

    if not result.get("ok"):
        print(f"  [ERROR] {result.get('err', 'unknown')}")
        return

    # Store the full payload in memory
    cache[name] = result
    print(f"  ✓ Captured and cached: {result}")
    print("  Press 'w' to write boot_config.json when ready.")


def test_code(ser: serial.Serial, cache: dict) -> None:
    if not cache:
        print("  (no codes in memory — learn something first)")
        return
    print("  Codes in memory: " + ", ".join(cache.keys()))
    name = input("  Code name to send: ").strip()
    if name not in cache:
        print(f"  [!] '{name}' not in local cache. Learn it first with 'l'.")
        return

    payload = cache[name]
    t = payload.get("type", "").upper()

    # Push code into ESP32 RAM first (works even after an ESP32 reboot)
    if t == "RAW":
        push_cmd = {"cmd": "define_raw", "name": name,
                    "freq": payload.get("freq", 38000),
                    "data": payload["data"]}
    else:
        push_cmd = {"cmd": "define", "name": name, "type": t,
                    "value": str(payload.get("value", "0x0")),
                    "bits": payload.get("bits", 32)}

    push_resp = send_cmd(ser, push_cmd)
    if not push_resp.get("ok"):
        print(f"  [ERROR] Could not push '{name}' to ESP32: {push_resp.get('err')}")
        return

    resp = send_cmd(ser, {"cmd": "send", "name": name, "repeats": 0})
    if resp.get("ok"):
        print(f"  ✓ Sent '{name}'.")
    else:
        print(f"  [ERROR] {resp.get('err', 'unknown')}")


def show_codes(ser: serial.Serial, cache: dict) -> None:
    if not cache:
        print("  (no codes in memory)")
    else:
        print(f"  {'Name':<30} {'Type':<12} {'Details'}")
        print(f"  {'-'*30} {'-'*12} {'-'*20}")
        for name, payload in cache.items():
            t = payload.get("type", "?")
            if t == "RAW":
                detail = f"freq={payload.get('freq',38000)} Hz, {len(payload.get('data',[]))} samples"
            else:
                detail = f"value={payload.get('value','?')} bits={payload.get('bits','?')}"
            print(f"  {name:<30} {t:<12} {detail}")

    # Also show what's currently in ESP32 RAM
    resp = send_cmd(ser, {"cmd": "list"})
    if resp.get("ok"):
        esp_codes = resp.get("codes", [])
        print(f"\n  ESP32 RAM ({len(esp_codes)} code(s)): " +
              (", ".join(c["name"] for c in esp_codes) if esp_codes else "(empty)"))


def erase_code(ser: serial.Serial, cache: dict) -> None:
    name = input("  Code name to erase: ").strip()
    removed_local = cache.pop(name, None)
    resp = send_cmd(ser, {"cmd": "erase", "name": name})
    if resp.get("ok") or removed_local:
        lines = []
        if removed_local:
            lines.append("removed from memory")
        if resp.get("ok"):
            lines.append("erased from ESP32 RAM")
        print(f"  ✓ '{name}': " + " + ".join(lines) + ".")
    else:
        print(f"  [ERROR] {resp.get('err', 'not_found')}")


def save_codes(cache: dict) -> None:
    if not cache:
        print("  (no codes in memory to save — learn some first)")
        return

    # Load existing config to preserve send_on_boot / description edits
    existing = {}
    if os.path.exists(BOOT_CONFIG_FILE):
        with open(BOOT_CONFIG_FILE) as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError:
                pass

    boot_cfg = {}
    for name, payload in cache.items():
        t = payload.get("type", "UNKNOWN")
        entry = {
            "type": t,
            # Preserve user edits if the code already existed
            "send_on_boot": existing.get(name, {}).get("send_on_boot", False),
            "description":  existing.get(name, {}).get("description", ""),
            "delay_before_ms": existing.get(name, {}).get("delay_before_ms", 0),
        }
        if t == "RAW":
            entry["freq"] = payload.get("freq", 38000)
            entry["data"] = payload["data"]
        else:
            entry["bits"]  = payload.get("bits", 32)
            entry["value"] = payload.get("value", "0x0")

        boot_cfg[name] = entry

    with open(BOOT_CONFIG_FILE, "w") as f:
        json.dump(boot_cfg, f, indent=2)

    print(f"  ✓ Saved {len(boot_cfg)} code(s) to {BOOT_CONFIG_FILE}")
    print("  Set 'send_on_boot': true for codes you want fired at Pi startup.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="TVWIZ IR Recorder")
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    args = parser.parse_args()

    print("=" * 52)
    print("  TVWIZ IR Recorder")
    print(f"  Port: {args.port}   Baud: {args.baud}")
    print("=" * 52)

    ser = open_serial(args.port, args.baud)

    print("  Pinging ESP32…", end=" ", flush=True)
    if ping(ser):
        print("OK ✓")
    else:
        print("FAILED — check USB connection and try again.")
        ser.close()
        sys.exit(1)

    # In-memory cache: name → full ESP32 JSON payload
    cache: dict = {}

    MENU = """
  l  — Learn a new IR code
  t  — Test / send a code
  s  — Show codes
  e  — Erase a code
  w  — Write boot_config.json
  q  — Quit"""

    while True:
        print(MENU)
        choice = input("  > ").strip().lower()
        if   choice == "l": learn_code(ser, cache)
        elif choice == "t": test_code(ser, cache)
        elif choice == "s": show_codes(ser, cache)
        elif choice == "e": erase_code(ser, cache)
        elif choice == "w": save_codes(cache)
        elif choice == "q": break
        else: print("  Unknown command.")

    ser.close()
    print("  Bye!")


if __name__ == "__main__":
    main()

# TVWIZ IR Controller

A full-stack IR automation system: an **ESP32** acts as a USB-connected IR blaster/receiver, and a **Raspberry Pi** controls it over serial to learn, test, and automatically send IR codes at boot time.

```
┌──────────────────────────────────────────────────────────────┐
│                      Raspberry Pi                            │
│                                                              │
│  ir_recorder.py    ←→   boot_config.json                     │
│  (setup / testing)                                           │
│                                                              │
│  ir_boot_sender.py ←────────────────── reads boot_config     │
│  (runs at boot via systemd)                                  │
└──────────────────────────────┬───────────────────────────────┘
                               │  USB Serial (115200 baud)
                               ▼
                     ┌──────────────────┐
                     │     ESP32        │
                     │  IR Blaster /    │
                     │  Receiver        │
                     └──────────────────┘
                        │           │
                   IR TX (GPIO 4)  IR RX (GPIO 27)
                        │           │
                    ┌───┘       ┌───┘
                   TV / AV    Remote
                  equipment   control
```

---

## Repository Layout

```
TVWIZ IR Controller/
├── ESP32-IR-Read-Send/      # PlatformIO firmware project for the ESP32
│   ├── src/main.cpp
│   └── platformio.ini
└── pi/                      # Raspberry Pi Python scripts
    ├── ir_recorder.py        # Interactive: learn, test, save IR codes
    ├── ir_boot_sender.py     # Boot service: send codes on Pi startup
    ├── boot_config.json      # Created by ir_recorder, edited by hand
    ├── boot_config.json.example
    └── tvwiz-ir.service      # systemd unit file
```

---

## 1 — ESP32 Hardware

### Wiring

| Signal | GPIO | Notes |
|--------|------|-------|
| IR TX  | **4**  | MOSFET gate or IR transmitter module DAT pin |
| IR RX  | **27** | IR demodulator output (e.g. VS1838B, KY-022) |

Both pins can be changed in `platformio.ini`:
```ini
-D IR_SEND_PIN=4
-D IR_RECV_PIN=27
```

**IR Transmitter (KY-005 style):**
```
Module GND  →  ESP32 GND
Module VCC  →  3.3V or 5V
Module DAT  →  GPIO 4
```

**IR Receiver (KY-022 / VS1838B):**
```
Module GND  →  ESP32 GND
Module VCC  →  3.3V
Module OUT  →  GPIO 27
```

### Flashing the Firmware

```bash
cd ESP32-IR-Read-Send
pio run --target upload
```

---

## 2 — Raspberry Pi Setup

### Connecting the ESP32

Plug the ESP32 into any USB port on the Pi. The device will appear as:

- `/dev/ttyUSB0` — CH340/CP2102 USB-to-UART chips (most common)
- `/dev/ttyACM0` — UART-over-USB (native USB ESP32 boards)

Verify with:
```bash
ls /dev/ttyUSB* /dev/ttyACM*
```

```
Raspberry Pi ──USB──► ESP32
               115200 baud, 8N1
```

### Installing Dependencies

```bash
pip3 install pyserial
```

### Give the Pi user access to the serial port

```bash
sudo usermod -aG dialout $USER
# log out and back in, or run: newgrp dialout
```

---

## 3 — Step 1: Record & Test IR Codes (`ir_recorder.py`)

Run this **once** (or whenever you need to update your IR code library) on the Pi while the ESP32 is connected.

```bash
cd /home/pi/tvwiz
python3 ir_recorder.py --port /dev/ttyUSB0
```

### Interactive menu

```
Commands:
  l  — Learn a new IR code
  t  — Test/send a code
  s  — Show codes in ESP32 RAM
  e  — Erase a code from ESP32 RAM
  w  — Write codes to boot_config.json
  q  — Quit
```

### Workflow

1. **Learn** — Press `l`, enter a descriptive name (e.g. `tv1_power`), then point your remote at the IR receiver and press the button. The code is captured and held in ESP32 RAM.
2. **Test** — Press `t`, enter the name. The ESP32 immediately blasts the code — check that your TV responds.
3. **Repeat** for every button you need.
4. **Save** — Press `w`. This writes `boot_config.json` with all learned codes.
5. **Edit `boot_config.json`** — Set `"send_on_boot": true` for the codes that should fire on every boot, and optionally add `"delay_before_ms"` to space them out.

> [!NOTE]
> The ESP32 stores codes in RAM only — they are lost on reboot.
> `boot_config.json` records the full signal data so the boot service can replay them without needing the remote again.

---

## 4 — boot_config.json

Created automatically by `ir_recorder.py`. Edit it to configure boot behaviour.

```json
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
    "data": [9024, 4512, 564, 1692, 564, 564],
    "send_on_boot": true,
    "description": "Breakout room TV — power on",
    "delay_before_ms": 500
  },
  "projector_on": {
    "type": "NEC",
    "value": "0x807F827D",
    "bits": 32,
    "send_on_boot": false,
    "description": "Projector — disabled for now",
    "delay_before_ms": 0
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `NEC`, `SONY`, `RC5`, `RC6`, `SAMSUNG`, `RAW`, … |
| `value` | hex string | For decoded protocols — the IR value |
| `bits` | int | Bit length (decoded protocols) |
| `freq` | int | Carrier frequency in Hz (RAW only) |
| `data` | int[] | Pulse/space timings in µs (RAW only) |
| `send_on_boot` | bool | `true` = send this code at Pi startup |
| `description` | string | Human-readable label (shown in logs) |
| `delay_before_ms` | int | Wait this many ms before sending (useful for sequencing) |

---

## 5 — Step 2: Boot Service (`ir_boot_sender.py`)

This script is normally run automatically by systemd, but you can test it manually first:

```bash
python3 ir_boot_sender.py --port /dev/ttyUSB0 --config boot_config.json
```

It will:
1. Load `boot_config.json`
2. Open the serial connection to the ESP32 (retries up to 5 times)
3. Ping the ESP32
4. For each code with `"send_on_boot": true`, load it into ESP32 RAM and send it
5. Exit with code `0` on success, `1` if any send failed

### Installing as a systemd service

```bash
# Copy files to the Pi
mkdir -p /home/pi/tvwiz
cp pi/ir_boot_sender.py /home/pi/tvwiz/
cp pi/boot_config.json  /home/pi/tvwiz/

# Install the service unit
sudo cp pi/tvwiz-ir.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tvwiz-ir.service

# Test it now (without rebooting)
sudo systemctl start tvwiz-ir.service
sudo systemctl status tvwiz-ir.service
journalctl -u tvwiz-ir.service -f
```

> [!TIP]
> `tvwiz-ir.service` uses `After=dev-ttyUSB0.device` so systemd will wait for the ESP32 USB device to enumerate before starting the script — even if the ESP32 takes a few seconds after boot.

---

## 6 — ESP32 Serial Protocol Reference

Every command is a single-line JSON object (`\n` terminated). Every response is the same.

### `ping`
```json
{"cmd": "ping"}
→ {"ok": true, "msg": "pong"}
```

### `learn` — Capture an IR signal
```json
{"cmd": "learn", "name": "tv_power", "timeout_ms": 15000}
```
1. ESP32 replies `{"ok": true, "msg": "learn_ready"}`
2. Point remote at receiver
3. ESP32 replies with captured code:

```json
{"ok": true, "name": "tv_power", "type": "NEC", "bits": 32, "value": "0x20DF10EF"}
```
RAW fallback:
```json
{"ok": true, "name": "tv_power", "type": "RAW", "freq": 38000, "data": [9024, 4512, 564, ...]}
```

### `send` — Transmit a stored code
```json
{"cmd": "send", "name": "tv_power", "repeats": 0}
→ {"ok": true, "msg": "sent"}
```

### `list` — List stored codes
```json
{"cmd": "list"}
→ {"ok": true, "codes": [{"name": "tv_power", "type": "NEC"}, ...]}
```

### `erase` — Delete a code
```json
{"cmd": "erase", "name": "tv_power"}
→ {"ok": true, "msg": "erased"}
```

### Error responses
```json
{"ok": false, "err": "<error_string>"}
```

| Error | Cause |
|-------|-------|
| `json_parse` | Received line was not valid JSON |
| `unknown_cmd` | `cmd` field not recognised |
| `learn_timeout` | No signal received within `timeout_ms` |
| `not_found` | No code with that name exists |
| `send_failed` | Protocol not supported by irsend |

---

## 7 — ESP32 Limits

| Parameter | Value |
|-----------|-------|
| Max stored codes | **16** (RAM only, lost on reboot) |
| Max RAW buffer entries | **256** per code |
| Carrier frequency (RAW) | 38 kHz |
| Serial baud rate | 115 200 |

---

## 8 — Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Cannot open /dev/ttyUSB0` | Run `sudo usermod -aG dialout $USER`, log out/in |
| ESP32 not responding to ping | Check USB cable, try `pio device monitor` to confirm firmware is running |
| IR code not recognised by TV | Point remote **directly** at receiver (<30 cm), reduce ambient IR |
| `send_failed` error | Protocol not yet in firmware — capture as RAW instead |
| Boot service never starts | Check `journalctl -u tvwiz-ir.service`; verify USB device name matches service file |

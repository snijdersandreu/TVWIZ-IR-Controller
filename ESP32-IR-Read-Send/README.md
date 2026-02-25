# ESP32 IR Blaster — Serial JSON Controller

An IR learn-and-send firmware for ESP32. A host (Raspberry Pi or any computer) controls it over **USB Serial** by sending JSON commands and reading JSON responses.

---

## Hardware

| Signal | GPIO | Notes |
|--------|------|-------|
| IR TX | **4** | MOSFET gate or IR transmitter module DAT pin |
| IR RX | **27** | IR demodulator output (e.g. VS1838B, KY-022) |

Both pins can be overridden in `platformio.ini` via `build_flags`:
```ini
-D IR_SEND_PIN=4
-D IR_RECV_PIN=27
```

**IR Transmitter module wiring (KY-005 style):**
```
Module GND  →  ESP32 GND
Module VCC  →  3.3V or 5V
Module DAT  →  GPIO 4
```

**IR Receiver module wiring (KY-022 / VS1838B):**
```
Module GND  →  ESP32 GND
Module VCC  →  3.3V
Module OUT  →  GPIO 27
```

---

## Serial Connection

Connect the ESP32 via USB. The firmware communicates at **115200 baud**, newline-delimited JSON (`\n` terminated).

```
Host  ──USB──►  ESP32 (/dev/ttyUSB0 or COMx)
         115200 baud, 8N1
         Send: JSON + \n
         Receive: JSON + \n
```

On Linux/macOS the port is typically `/dev/ttyUSB0` or `/dev/tty.usbserial-*`.  
On Windows: `COMx`.

---

## Protocol

Every command is a single JSON object terminated by `\n`.  
Every response is a single JSON object terminated by `\n`.

### Success response
```json
{"ok": true, "msg": "..."}
```

### Error response
```json
{"ok": false, "err": "..."}
```

---

## Commands

### `ping` — Check connectivity
```json
{"cmd": "ping"}
```
**Response:**
```json
{"ok": true, "msg": "pong"}
```

---

### `learn` — Capture an IR signal and store it
```json
{"cmd": "learn", "name": "tv_power", "timeout_ms": 15000}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | required | Unique name to store the code under |
| `timeout_ms` | int | 15000 | How long to wait for a signal (ms) |

**Flow:**
1. Send the command
2. ESP32 immediately replies `{"ok": true, "msg": "learn_ready"}`
3. Point your remote at the IR receiver and press the button
4. On success, ESP32 replies with the captured code:

```json
{"ok": true, "name": "tv_power", "type": "NEC", "bits": 32, "value": "0x20DF10EF"}
```

For unknown protocols it falls back to RAW:
```json
{"ok": true, "name": "tv_power", "type": "RAW", "freq": 38000, "data": [9024, 4512, 564, ...]}
```

**Errors:** `learn_timeout`

> [!NOTE]
> Codes are stored in RAM only. They are lost when the ESP32 reboots.

---

### `send` — Transmit a stored IR code
```json
{"cmd": "send", "name": "tv_power", "repeats": 0}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | required | Name of the previously learned code |
| `repeats` | int | 1 | Extra repeat transmissions after the first (0 = send once) |

**Response:**
```json
{"ok": true, "msg": "sent"}
```

**Errors:** `not_found`, `send_failed`

> [!TIP]
> Use `"repeats": 0` for a single fire. `"repeats": 1` sends the code twice (useful for toggles or unreliable receivers).

---

### `list` — List all stored codes
```json
{"cmd": "list"}
```
**Response:**
```json
{"ok": true, "codes": [{"name": "tv_power", "type": "NEC"}, {"name": "vol_up", "type": "RAW"}]}
```

---

### `erase` — Delete a stored code
```json
{"cmd": "erase", "name": "tv_power"}
```
**Response:**
```json
{"ok": true, "msg": "erased"}
```

**Errors:** `not_found`

---

## Python Example (Raspberry Pi)

```python
import serial, json, time

esp = serial.Serial('/dev/ttyUSB0', 115200, timeout=5)
time.sleep(1)  # wait for ESP32 boot

def send_cmd(cmd: dict) -> dict:
    esp.write((json.dumps(cmd) + '\n').encode())
    return json.loads(esp.readline())

# Check connection
print(send_cmd({"cmd": "ping"}))

# Learn a button (press remote within 10 seconds)
print(send_cmd({"cmd": "learn", "name": "tv_power", "timeout_ms": 10000}))
resp = json.loads(esp.readline())  # wait for the captured result
print(resp)

# Send it back
print(send_cmd({"cmd": "send", "name": "tv_power", "repeats": 0}))

# List all codes
print(send_cmd({"cmd": "list"}))
```

---

## Limits

| Parameter | Value |
|-----------|-------|
| Max stored codes | **16** (RAM only, lost on reboot) |
| Max raw buffer entries | **256** per code |
| Carrier frequency (RAW) | 38 kHz |
| Serial baud rate | 115200 |

---

## Error Reference

| Error string | Cause |
|---|---|
| `json_parse` | Received line was not valid JSON |
| `unknown_cmd` | `cmd` field not recognised |
| `learn_timeout` | No IR signal received within `timeout_ms` |
| `not_found` | No code with that name exists |
| `send_failed` | Protocol not supported by `irsend.send()` |

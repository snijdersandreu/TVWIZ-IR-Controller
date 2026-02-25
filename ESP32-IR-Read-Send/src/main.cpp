#include <Arduino.h>

// IRremoteESP8266 handles both RX and TX on ESP32
#include <IRrecv.h>
#include <IRremoteESP8266.h>
#include <IRsend.h>
#include <IRutils.h>

// ArduinoJson used for Pi <-> ESP structured communication
#include <ArduinoJson.h>

/*
  Pins are injected from platformio.ini:
   - IR_SEND_PIN  -> MOSFET gate driving IR LEDs
   - IR_RECV_PIN  -> IR demodulator output
*/

#ifndef IR_SEND_PIN
#define IR_SEND_PIN 4
#endif

#ifndef IR_RECV_PIN
#define IR_RECV_PIN 27
#endif

#ifndef SERIAL_BAUD
#define SERIAL_BAUD 115200
#endif

// ---------------- IR Configuration ----------------

// Raw capture buffer (enough for most remotes)
static const uint16_t kCaptureBufferSize = 256;

// Gap timeout between IR bursts (ms)
static const uint8_t kTimeout = 50;

// Ignore tiny garbage signals
static const uint16_t kMinUnknownSize = 12;

// Default carrier frequency
static const uint32_t kDefaultFreq = 38000;

// Create receiver + sender objects
IRrecv irrecv(IR_RECV_PIN, kCaptureBufferSize, kTimeout, true);
IRsend irsend(IR_SEND_PIN);

decode_results results;

// ---------------- Storage ----------------

/*
 We store learned codes in RAM.

 Either:
  - decoded protocol + value
 or
  - RAW timings if protocol unknown
*/

struct StoredCode {
  String name;

  bool isRaw = false;

  // Decoded fields
  decode_type_t protocol;
  uint64_t value;
  uint16_t bits;

  // RAW fields
  uint32_t freq;
  uint16_t rawlen;
  uint16_t rawbuf[kCaptureBufferSize];
};

static const size_t kMaxCodes = 16;
StoredCode codes[kMaxCodes];
size_t codeCount = 0;

// ---------------- Utility helpers ----------------

int findCodeIndex(const String &name) {
  for (size_t i = 0; i < codeCount; i++)
    if (codes[i].name == name)
      return i;
  return -1;
}

/*
 Insert or overwrite existing code
*/
bool upsertCode(const StoredCode &c) {
  int idx = findCodeIndex(c.name);
  if (idx >= 0) {
    codes[idx] = c;
    return true;
  }
  if (codeCount >= kMaxCodes)
    return false;
  codes[codeCount++] = c;
  return true;
}

/*
 Send JSON reply over Serial
*/
void sendJson(const JsonDocument &doc) {
  serializeJson(doc, Serial);
  Serial.println();
}

void replyOk(const char *msg) {
  JsonDocument doc;
  doc["ok"] = true;
  doc["msg"] = msg;
  sendJson(doc);
}

void replyErr(const char *msg) {
  JsonDocument doc;
  doc["ok"] = false;
  doc["err"] = msg;
  sendJson(doc);
}

// ---------------- Learning IR ----------------

/*
 Waits for next IR signal and fills StoredCode.

 Returns false if timeout.
*/
bool learnOnce(StoredCode &out, uint32_t timeoutMs) {
  uint32_t start = millis();

  while (millis() - start < timeoutMs) {

    if (irrecv.decode(&results)) {

      // Ignore tiny garbage pulses
      if (results.decode_type == UNKNOWN && results.rawlen < kMinUnknownSize) {
        irrecv.resume();
        continue;
      }

      // Prefer decoded protocol
      out.isRaw = false;
      out.protocol = results.decode_type;
      out.bits = results.bits;
      out.value = results.value;

      // If unknown → fall back to RAW capture
      if (results.decode_type == UNKNOWN) {
        // Signal was too long for the buffer — data is truncated, skip it
        if (results.overflow) {
          irrecv.resume();
          continue;
        }

        out.isRaw = true;
        out.freq = kDefaultFreq;

        uint16_t len = min(results.rawlen, kCaptureBufferSize);

        uint16_t rawLen = 0;
        for (uint16_t i = 1; i < len; i++) {
          uint32_t us = results.rawbuf[i] * kRawTick;
          if (us > 0xFFFF)
            us = 0xFFFF;
          out.rawbuf[rawLen++] = (uint16_t)us;
        }

        out.rawlen = rawLen;
      }

      irrecv.resume();
      return true;
    }

    delay(5);
  }

  return false;
}

// Send learned info back to Pi
void emitLearnedResponse(const StoredCode &c) {
  JsonDocument doc;

  doc["ok"] = true;
  doc["name"] = c.name;

  if (!c.isRaw) {
    doc["type"] = typeToString(c.protocol);
    doc["bits"] = c.bits;

    char buf[24];
    snprintf(buf, sizeof(buf), "0x%llX", (unsigned long long)c.value);
    doc["value"] = buf;

  } else {
    doc["type"] = "RAW";
    doc["freq"] = c.freq;

    JsonArray arr = doc["data"].to<JsonArray>();
    for (uint16_t i = 0; i < c.rawlen; i++)
      arr.add(c.rawbuf[i]);
  }

  sendJson(doc);
}

// ---------------- Sending IR ----------------

bool sendStored(const StoredCode &c, uint8_t repeats) {

  // Disable receiver so it doesn't capture our own IR transmission
  irrecv.disableIRIn();

  bool ok = true;
  for (uint8_t r = 0; r <= repeats; r++) {

    if (!c.isRaw) {
      if (!irsend.send(c.protocol, c.value, c.bits)) {
        ok = false;
        break;
      }
    } else {
      irsend.sendRaw(c.rawbuf, c.rawlen, (uint16_t)c.freq);
    }

    if (r < repeats)
      delay(80);
  }

  // Re-enable receiver after transmission is done
  irrecv.enableIRIn();

  return ok;
}

// ---------------- Commands ----------------

void handleList() {
  JsonDocument doc;
  doc["ok"] = true;

  JsonArray arr = doc["codes"].to<JsonArray>();

  for (size_t i = 0; i < codeCount; i++) {
    JsonObject o = arr.add<JsonObject>();
    o["name"] = codes[i].name;
    o["type"] = codes[i].isRaw ? "RAW" : typeToString(codes[i].protocol);
  }

  sendJson(doc);
}

void handleErase(const String &name) {
  int idx = findCodeIndex(name);
  if (idx < 0)
    return replyErr("not_found");

  for (size_t i = idx; i + 1 < codeCount; i++)
    codes[i] = codes[i + 1];

  codeCount--;
  replyOk("erased");
}

void handlePing() {
  JsonDocument doc;
  doc["ok"] = true;
  doc["msg"] = "pong";
  sendJson(doc);
}

// ---------------- Arduino lifecycle ----------------

void setup() {

  Serial.begin(SERIAL_BAUD);
  delay(200);

  // Start IR subsystems
  irsend.begin();
  irrecv.enableIRIn();

  replyOk("boot");
}

/*
 Main loop:

 1) Read JSON line from Serial
 2) Parse
 3) Execute command
*/
String line;

void loop() {

  while (Serial.available()) {

    char c = Serial.read();

    if (c == '\n') {

      JsonDocument cmd;
      if (deserializeJson(cmd, line)) {
        line = "";
        return replyErr("json_parse");
      }

      line = "";

      const char *command = cmd["cmd"] | "";

      if (!strcmp(command, "ping"))
        handlePing();

      else if (!strcmp(command, "list"))
        handleList();

      else if (!strcmp(command, "erase"))
        handleErase(cmd["name"] | "");

      else if (!strcmp(command, "learn")) {

        StoredCode sc;
        sc.name = cmd["name"] | "";

        replyOk("learn_ready");

        if (!learnOnce(sc, cmd["timeout_ms"] | 15000))
          return replyErr("learn_timeout");

        upsertCode(sc);
        emitLearnedResponse(sc);
      }

      else if (!strcmp(command, "send")) {

        String name = cmd["name"] | "";
        int idx = findCodeIndex(name);

        if (idx < 0)
          return replyErr("not_found");

        if (!sendStored(codes[idx], cmd["repeats"] | 1))
          return replyErr("send_failed");

        replyOk("sent");
      }

      else
        replyErr("unknown_cmd");

    }

    else if (c != '\r')
      line += c;
  }
}
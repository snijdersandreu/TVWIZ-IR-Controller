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

// Maximum entries in a define_raw payload
static const uint16_t kMaxRawLen = 512;

// Create receiver + sender objects
IRrecv irrecv(IR_RECV_PIN, kCaptureBufferSize, kTimeout, true);
IRsend irsend(IR_SEND_PIN);

decode_results results;

// ---------------- Storage ----------------

/*
 Codes are stored in RAM using dynamic allocation for RAW timings so
 that non-RAW codes don't waste kCaptureBufferSize * 2 bytes each.
*/

struct StoredCode {
  String name;
  bool isRaw = false;

  // Decoded protocol fields
  decode_type_t protocol = UNKNOWN;
  uint64_t value = 0;
  uint16_t bits = 0;

  // RAW fields (heap-allocated, nullptr when unused)
  uint32_t freq = kDefaultFreq;
  uint16_t rawlen = 0;
  uint16_t *rawbuf = nullptr;
};

static const size_t kMaxCodes = 16;
StoredCode codes[kMaxCodes];
size_t codeCount = 0;

// ---------------- RAW memory helpers ----------------

void freeRaw(StoredCode &c) {
  if (c.rawbuf) {
    free(c.rawbuf);
    c.rawbuf = nullptr;
  }
  c.rawlen = 0;
}

bool allocAndCopyRaw(StoredCode &dst, const uint16_t *src, uint16_t len) {
  freeRaw(dst);
  dst.rawbuf = (uint16_t *)malloc(len * sizeof(uint16_t));
  if (!dst.rawbuf)
    return false;
  memcpy(dst.rawbuf, src, len * sizeof(uint16_t));
  dst.rawlen = len;
  return true;
}

// ---------------- Utility helpers ----------------

int findCodeIndex(const String &name) {
  for (size_t i = 0; i < codeCount; i++)
    if (codes[i].name == name)
      return i;
  return -1;
}

/*
 Insert or overwrite existing code.

 Always deep-copies RAW data: the stored slot owns its own heap buffer.
 Callers may pass a StoredCode with rawbuf pointing to a stack/static
 temp array — upsertCode allocates a fresh copy and does NOT take ownership
 of the caller's buffer.
*/
bool upsertCode(const StoredCode &c) {
  // Determine destination slot
  int idx = findCodeIndex(c.name);
  StoredCode *dst;

  if (idx >= 0) {
    dst = &codes[idx];
    freeRaw(*dst); // free any old RAW buffer owned by this slot
  } else {
    if (codeCount >= kMaxCodes)
      return false;
    dst = &codes[codeCount];
    dst->rawbuf = nullptr; // slot is uninitialised; make sure freeRaw is safe
    dst->rawlen = 0;
  }

  // Copy all scalar fields
  dst->name = c.name;
  dst->isRaw = c.isRaw;
  dst->protocol = c.protocol;
  dst->value = c.value;
  dst->bits = c.bits;
  dst->freq = c.freq;

  // Deep-copy RAW buffer so the stored slot is the sole owner
  if (c.isRaw) {
    if (!c.rawbuf || c.rawlen == 0)
      return false;
    dst->rawbuf = (uint16_t *)malloc(c.rawlen * sizeof(uint16_t));
    if (!dst->rawbuf)
      return false;
    memcpy(dst->rawbuf, c.rawbuf, c.rawlen * sizeof(uint16_t));
    dst->rawlen = c.rawlen;
  }

  if (idx < 0)
    codeCount++;

  return true;
}

/*
 Map a protocol name string (as returned by typeToString / sent by Pi) to
 a decode_type_t value.  Returns UNKNOWN on no match.
*/
decode_type_t typeFromString(const char *s) { return strToDecodeType(s); }

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

        out.freq = kDefaultFreq;

        uint16_t len = min(results.rawlen, kCaptureBufferSize);

        // Static temp buffer — µs values, skip index 0 (mark start).
        // learnOnce does NOT allocate heap; upsertCode deep-copies from here.
        static uint16_t tmp[kCaptureBufferSize];
        uint16_t rawLen = 0;
        for (uint16_t i = 1; i < len; i++) {
          uint32_t us = results.rawbuf[i] * kRawTick;
          if (us > 0xFFFF)
            us = 0xFFFF;
          tmp[rawLen++] = (uint16_t)us;
        }

        out.rawbuf = tmp; // points at static; upsertCode will deep-copy
        out.rawlen = rawLen;
        out.isRaw = true;
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

  // Free heap-allocated RAW buffer before removing the slot
  freeRaw(codes[idx]);

  for (size_t i = idx; i + 1 < codeCount; i++)
    codes[i] = codes[i + 1];

  // Null out the now-hidden last slot so its rawbuf pointer can never be
  // double-freed or silently lost when that slot is reused by a future upsert.
  codes[codeCount - 1].rawbuf = nullptr;
  codes[codeCount - 1].rawlen = 0;

  codeCount--;
  replyOk("erased");
}

void handlePing() {
  JsonDocument doc;
  doc["ok"] = true;
  doc["msg"] = "pong";
  sendJson(doc);
}

/*
 define — load a decoded IR code from the Pi into RAM
 Input:
   {"cmd":"define","name":"tv1_power","type":"NEC","value":"0x20DF10EF","bits":32}
*/
void handleDefine(const JsonDocument &cmd) {
  const char *name = cmd["name"] | "";
  const char *typeStr = cmd["type"] | "";
  const char *valueStr = cmd["value"] | "";
  uint16_t bits = cmd["bits"] | 32;

  if (!name[0])
    return replyErr("missing_name");
  if (!typeStr[0])
    return replyErr("missing_type");
  if (!valueStr[0])
    return replyErr("missing_value");

  decode_type_t proto = typeFromString(typeStr);
  if (proto == UNKNOWN)
    return replyErr("unknown_type");

  // Parse hex string value (accepts "0x..." or plain decimal)
  uint64_t value = (uint64_t)strtoull(valueStr, nullptr, 0);

  StoredCode sc;
  sc.name = name;
  sc.isRaw = false;
  sc.protocol = proto;
  sc.value = value;
  sc.bits = bits;

  if (!upsertCode(sc))
    return replyErr("storage_full");

  replyOk("defined");
}

/*
 define_raw — load a RAW IR code from the Pi into RAM
 Input:
   {"cmd":"define_raw","name":"tv2_power","freq":38000,"data":[9024,4512,...]}
*/
void handleDefineRaw(const JsonDocument &cmd) {
  const char *name = cmd["name"] | "";
  uint32_t freq = cmd["freq"] | kDefaultFreq;

  if (!name[0])
    return replyErr("missing_name");

  JsonArrayConst arr = cmd["data"];
  if (arr.isNull())
    return replyErr("missing_data");

  uint16_t len = arr.size();
  if (len == 0)
    return replyErr("empty_data");
  if (len > kMaxRawLen)
    return replyErr("raw_too_long");

  // Static temp buffer — 1 KB, kept off the task stack.
  // upsertCode() deep-copies from here into the stored slot's own heap block.
  static uint16_t tmp[kMaxRawLen];
  for (uint16_t i = 0; i < len; i++) {
    uint32_t v = arr[i] | 0;
    tmp[i] = (uint16_t)(v > 0xFFFF ? 0xFFFF : v);
  }

  // Build a lightweight descriptor pointing at tmp.
  // upsertCode() will malloc its own copy — this sc never owns heap memory.
  StoredCode sc;
  sc.name = name;
  sc.isRaw = true;
  sc.freq = freq;
  sc.rawbuf = tmp;
  sc.rawlen = len;

  if (!upsertCode(sc))
    return replyErr("storage_full");

  replyOk("defined");
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

        // Reject empty name before committing to listen
        if (sc.name.length() == 0)
          return replyErr("missing_name");

        replyOk("learn_ready");

        if (!learnOnce(sc, cmd["timeout_ms"] | 15000))
          return replyErr("learn_timeout");

        upsertCode(sc);          // deep-copies RAW buffer into codes[]
        emitLearnedResponse(sc); // sc.rawbuf points to static tmp — not heap
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

      else if (!strcmp(command, "define"))
        handleDefine(cmd);

      else if (!strcmp(command, "define_raw"))
        handleDefineRaw(cmd);

      else
        replyErr("unknown_cmd");

    }

    else if (c != '\r')
      line += c;
  }
}
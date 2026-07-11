// ===========================================================================
// Persephone badge firmware — ESP32-S3 (MakerEdu MKE-K01 N16R8)
//
// Hold the button to record a question; release to upload. The badge uploads a
// standard PCM16 WAV to the Persephone API, receives a question id (HTTP 202), polls
// until the transcript is ready, and shows it on the TFT.
//
// Proven hardware (do NOT change pins unless you also change the wiring):
//   INMP441 I2S mic : BCLK=4  WS=5  SD=6           (16 kHz mono, 32-bit slots)
//   Button          : GPIO7 -> GND (INPUT_PULLUP, pressed = LOW)
//   ILI9341 TFT     : RST=8 DC=9 CS=10 MOSI=11 SCLK=12 MISO=13 (landscape clone)
//
// Libraries (Arduino ESP32 core 3.x):
//   WiFi, HTTPClient, ESP_I2S, SPI, Adafruit_GFX, Adafruit_ILI9341
//
// Copy config.example.h -> config.h and fill in credentials before building.
// ===========================================================================

#include "config.h"

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ESP_I2S.h>
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>

// --------------------------- Pins ------------------------------------------
static const int PIN_I2S_BCLK = 4;
static const int PIN_I2S_WS   = 5;
static const int PIN_I2S_SD   = 6;
static const int PIN_BUTTON   = 7;

static const int PIN_TFT_RST  = 8;
static const int PIN_TFT_DC   = 9;
static const int PIN_TFT_CS   = 10;
static const int PIN_TFT_MOSI = 11;
static const int PIN_TFT_SCLK = 12;
static const int PIN_TFT_MISO = 13;

// --------------------------- Audio -----------------------------------------
static const uint32_t SAMPLE_RATE = 16000;
static const uint32_t MAX_SECONDS = 10;
static const uint32_t MAX_SAMPLES = SAMPLE_RATE * MAX_SECONDS;   // 160000
static const uint32_t WAV_HEADER  = 44;
static const uint32_t BUFFER_BYTES = WAV_HEADER + MAX_SAMPLES * 2;

// Known-good INMP441 conversion for this build: 32-bit slot >> 14 -> PCM16.
static const int I2S_SHIFT = 14;

// --------------------------- Timing ----------------------------------------
static const uint32_t DEBOUNCE_MS      = 30;
static const uint32_t POLL_INTERVAL_MS = 900;    // question poll cadence
static const uint32_t POLL_TIMEOUT_MS  = 30000;  // give up after ~30s
static const uint32_t STATE_POLL_MS    = 4000;   // badge/state poll when idle
static const uint32_t RESULT_HOLD_MS   = 12000;  // keep result on screen

// --------------------------- Globals ---------------------------------------
Adafruit_ILI9341 tft(PIN_TFT_CS, PIN_TFT_DC, PIN_TFT_RST);

uint8_t* wavBuffer = nullptr;   // PSRAM: [44 header][PCM16 data...]
uint32_t sampleCount = 0;

I2SClass I2S;

WiFiClient      plainClient;
WiFiClientSecure secureClient;

enum State {
  BOOTING, CONNECTING, READY, LISTENING, UPLOADING, TRANSCRIBING, RESULT, FAILED
};
State state = BOOTING;

String currentRoundId = "";
String currentPrompt  = "";
uint32_t lastStatePoll = 0;
uint32_t resultShownAt = 0;

// Colors
#define C_BG      0x0000
#define C_TEAL    0x0679
#define C_RED     0xF9A6
#define C_AMBER   0xFD20
#define C_GREEN   0x2FE8
#define C_TEXT    0xFFFF
#define C_MUTED   0x8410

// --------------------------- Display ---------------------------------------
// Central display init with the MANDATORY landscape workaround. Never call
// setRotation() again elsewhere without immediately reapplying MADCTL 0x40.
void displayInit() {
  tft.begin();
  tft.setRotation(1);
  uint8_t madctl = 0x40;
  tft.sendCommand(ILI9341_MADCTL, &madctl, 1);
  tft.fillScreen(C_BG);
}

void drawHeader() {
  // "PERSEPHONE" is far longer than the old "WHISP", so measure the wordmark and
  // drop a size if it would not fit the left side, then vertically center it and
  // RIGHT-align the badge id so the two never overlap on the 320x28 header.
  const int HEADER_H = 28;
  tft.fillRect(0, 0, 320, HEADER_H, C_BG);
  int16_t bx, by; uint16_t bw, bh;

  // Wordmark (teal). Prefer size 2; fall back to size 1 if too wide.
  uint8_t markSize = 2;
  tft.setTextSize(markSize);
  tft.getTextBounds("PERSEPHONE", 0, 0, &bx, &by, &bw, &bh);
  if (bw > 200) {  // keep clear room for the badge id on the right
    markSize = 1;
    tft.setTextSize(markSize);
    tft.getTextBounds("PERSEPHONE", 0, 0, &bx, &by, &bw, &bh);
  }
  int16_t markY = (HEADER_H - (int16_t)bh) / 2;
  if (markY < 2) markY = 2;
  tft.setTextColor(C_TEAL);
  tft.setCursor(6, markY);
  tft.print("PERSEPHONE");

  // Badge id (muted, size 1), right-aligned; clamped so it never collides with
  // the wordmark even for long ids.
  tft.setTextSize(1);
  int16_t ix, iy; uint16_t iw, ih;
  tft.getTextBounds(BADGE_ID, 0, 0, &ix, &iy, &iw, &ih);
  int16_t idX = 320 - (int16_t)iw - 8;
  int16_t minX = 6 + (int16_t)bw + 10;
  if (idX < minX) idX = minX;
  tft.setTextColor(C_MUTED);
  tft.setCursor(idX, (HEADER_H - (int16_t)ih) / 2);
  tft.print(BADGE_ID);
}

// Simple word-wrap for landscape (320x240). Returns next y.
int drawWrapped(const String& text, int x, int y, int maxWidth, int size, uint16_t color) {
  tft.setTextSize(size);
  tft.setTextColor(color);
  int charW = 6 * size;
  int lineH = 8 * size + 4;
  int maxChars = (maxWidth - x) / charW;
  if (maxChars < 1) maxChars = 1;

  String line = "";
  int cursorY = y;
  int start = 0;
  while (start < (int)text.length()) {
    int space = text.indexOf(' ', start);
    if (space < 0) space = text.length();
    String word = text.substring(start, space);
    String candidate = line.length() ? line + " " + word : word;
    if ((int)candidate.length() > maxChars) {
      tft.setCursor(x, cursorY);
      tft.print(line);
      cursorY += lineH;
      line = word;
    } else {
      line = candidate;
    }
    start = space + 1;
    if (cursorY > 220) break;
  }
  if (line.length()) {
    tft.setCursor(x, cursorY);
    tft.print(line);
    cursorY += lineH;
  }
  return cursorY;
}

void showStatus(const char* title, uint16_t color, const String& sub) {
  tft.fillScreen(C_BG);
  drawHeader();
  tft.fillRect(0, 40, 320, 4, color);
  tft.setTextColor(color);
  tft.setTextSize(3);
  tft.setCursor(8, 70);
  tft.print(title);
  if (sub.length()) {
    drawWrapped(sub, 8, 120, 312, 2, C_TEXT);
  }
}

// --------------------------- Wi-Fi -----------------------------------------
bool isHttps() {
  return String(API_BASE_URL).startsWith("https");
}

void configureSecure() {
#if defined(PERSEPHONE_INSECURE_TLS) && PERSEPHONE_INSECURE_TLS
  secureClient.setInsecure();  // DEV ONLY — no cert validation.
#else
  if (strlen(PERSEPHONE_ROOT_CA) > 0) {
    secureClient.setCACert(PERSEPHONE_ROOT_CA);
  } else {
    // No CA pinned and insecure disabled: https will fail (by design).
    Serial.println("[TLS] No root CA and PERSEPHONE_INSECURE_TLS off — https will fail.");
  }
#endif
}

void connectWifi() {
  state = CONNECTING;
  showStatus("CONNECTING", C_AMBER, String("Joining ") + WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("[wifi] connecting");
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("[wifi] connected ip=");
    Serial.println(WiFi.localIP());
    if (isHttps()) configureSecure();
  } else {
    Serial.println("[wifi] FAILED");
  }
}

void ensureWifi() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWifi();
  }
}

// HTTPClient begin() with the right transport for http/https.
bool httpBegin(HTTPClient& http, const String& url) {
  if (isHttps()) {
    return http.begin(secureClient, url);
  }
  return http.begin(plainClient, url);
}

// --------------------------- Tiny JSON helpers -----------------------------
// The API responses are small and flat; a minimal extractor avoids an
// ArduinoJson dependency. These are tolerant, not a full parser.
String jsonString(const String& body, const char* key) {
  String needle = String("\"") + key + "\"";
  int k = body.indexOf(needle);
  if (k < 0) return "";
  int colon = body.indexOf(':', k + needle.length());
  if (colon < 0) return "";
  int q1 = body.indexOf('"', colon + 1);
  if (q1 < 0) return "";
  int q2 = body.indexOf('"', q1 + 1);
  // handle simple escaped quotes
  while (q2 > 0 && body.charAt(q2 - 1) == '\\') q2 = body.indexOf('"', q2 + 1);
  if (q2 < 0) return "";
  String out = body.substring(q1 + 1, q2);
  out.replace("\\\"", "\"");
  out.replace("\\n", " ");
  out.replace("\\\\", "\\");
  return out;
}

bool jsonBool(const String& body, const char* key) {
  String needle = String("\"") + key + "\"";
  int k = body.indexOf(needle);
  if (k < 0) return false;
  int colon = body.indexOf(':', k + needle.length());
  if (colon < 0) return false;
  int i = colon + 1;
  while (i < (int)body.length() && body.charAt(i) == ' ') i++;
  return body.startsWith("true", i);
}

int jsonInt(const String& body, const char* key, int dflt) {
  String needle = String("\"") + key + "\"";
  int k = body.indexOf(needle);
  if (k < 0) return dflt;
  int colon = body.indexOf(':', k + needle.length());
  if (colon < 0) return dflt;
  int i = colon + 1;
  while (i < (int)body.length() && (body.charAt(i) == ' ')) i++;
  bool neg = false;
  if (i < (int)body.length() && body.charAt(i) == '-') { neg = true; i++; }
  int val = 0; bool any = false;
  while (i < (int)body.length() && isDigit(body.charAt(i))) {
    val = val * 10 + (body.charAt(i) - '0'); i++; any = true;
  }
  if (!any) return dflt;
  return neg ? -val : val;
}

// --------------------------- Audio recording -------------------------------
void i2sInit() {
  I2S.setPins(PIN_I2S_BCLK, PIN_I2S_WS, -1, PIN_I2S_SD, -1);
  if (!I2S.begin(I2S_MODE_STD, SAMPLE_RATE, I2S_DATA_BIT_WIDTH_32BIT,
                 I2S_SLOT_MODE_MONO, I2S_STD_SLOT_LEFT)) {
    Serial.println("[i2s] begin FAILED");
  }
}

void buildWavHeader(uint8_t* p, uint32_t dataBytes) {
  uint32_t chunkSize = 36 + dataBytes;
  uint32_t byteRate  = SAMPLE_RATE * 2;  // mono, 16-bit
  memcpy(p, "RIFF", 4);
  p[4] = chunkSize & 0xff; p[5] = (chunkSize >> 8) & 0xff;
  p[6] = (chunkSize >> 16) & 0xff; p[7] = (chunkSize >> 24) & 0xff;
  memcpy(p + 8, "WAVE", 4);
  memcpy(p + 12, "fmt ", 4);
  p[16] = 16; p[17] = 0; p[18] = 0; p[19] = 0;   // fmt size 16
  p[20] = 1;  p[21] = 0;                          // PCM
  p[22] = 1;  p[23] = 0;                          // mono
  p[24] = SAMPLE_RATE & 0xff; p[25] = (SAMPLE_RATE >> 8) & 0xff;
  p[26] = (SAMPLE_RATE >> 16) & 0xff; p[27] = (SAMPLE_RATE >> 24) & 0xff;
  p[28] = byteRate & 0xff; p[29] = (byteRate >> 8) & 0xff;
  p[30] = (byteRate >> 16) & 0xff; p[31] = (byteRate >> 24) & 0xff;
  p[32] = 2; p[33] = 0;                           // block align
  p[34] = 16; p[35] = 0;                          // bits per sample
  memcpy(p + 36, "data", 4);
  p[40] = dataBytes & 0xff; p[41] = (dataBytes >> 8) & 0xff;
  p[42] = (dataBytes >> 16) & 0xff; p[43] = (dataBytes >> 24) & 0xff;
}

void recordWhileHeld() {
  state = LISTENING;
  showStatus("LISTENING", C_RED, "Speak your question...");
  sampleCount = 0;
  int16_t* pcm = (int16_t*)(wavBuffer + WAV_HEADER);

  static int32_t raw[256];
  uint32_t startedAt = millis();
  while (digitalRead(PIN_BUTTON) == LOW) {
    size_t bytesRead = I2S.readBytes((char*)raw, sizeof(raw));
    size_t got = bytesRead / sizeof(int32_t);
    for (size_t i = 0; i < got && sampleCount < MAX_SAMPLES; i++) {
      pcm[sampleCount++] = (int16_t)(raw[i] >> I2S_SHIFT);
    }
    if (sampleCount >= MAX_SAMPLES) break;         // 10s cap
    if (millis() - startedAt > (MAX_SECONDS + 1) * 1000) break;
  }
  Serial.printf("[rec] captured %u samples (%.2fs)\n",
                sampleCount, sampleCount / (float)SAMPLE_RATE);
}

// --------------------------- Upload + poll ---------------------------------
String uploadRecording() {
  if (sampleCount < SAMPLE_RATE / 4) {   // < 0.25s -> ignore accidental taps
    Serial.println("[upload] too short, ignoring");
    return "";
  }
  ensureWifi();
  if (WiFi.status() != WL_CONNECTED) return "";

  state = UPLOADING;
  showStatus("UPLOADING", C_TEAL, "Sending audio...");

  uint32_t dataBytes = sampleCount * 2;
  buildWavHeader(wavBuffer, dataBytes);
  uint32_t total = WAV_HEADER + dataBytes;

  HTTPClient http;
  String url = String(API_BASE_URL) + "/api/v1/questions";
  if (!httpBegin(http, url)) {
    Serial.println("[upload] begin failed");
    return "";
  }
  http.addHeader("Content-Type", "audio/wav");
  http.addHeader("X-Persephone-Key", BADGE_API_KEY);
  http.addHeader("X-Badge-Id", BADGE_ID);
  if (currentRoundId.length()) http.addHeader("X-Round-Id", currentRoundId);

  int code = http.POST(wavBuffer, total);
  Serial.printf("[upload] HTTP %d (%u bytes)\n", code, total);
  String qid = "";
  if (code == 202 || code == 200) {
    String body = http.getString();
    qid = jsonString(body, "question_id");
  } else if (code > 0) {
    showStatus("FAILED", C_RED, String("Upload HTTP ") + code);
  } else {
    showStatus("FAILED", C_RED, "Network error");
  }
  http.end();
  return qid;
}

bool pollResult(const String& qid) {
  state = TRANSCRIBING;
  showStatus("PROCESSING", C_AMBER, "Transcribing your question...");

  String url = String(API_BASE_URL) + "/api/v1/questions/" + qid;
  uint32_t startedAt = millis();
  while (millis() - startedAt < POLL_TIMEOUT_MS) {
    delay(POLL_INTERVAL_MS);
    if (WiFi.status() != WL_CONNECTED) { ensureWifi(); continue; }

    HTTPClient http;
    if (!httpBegin(http, url)) { http.end(); continue; }
    http.addHeader("X-Persephone-Key", BADGE_API_KEY);
    int code = http.GET();
    if (code != 200) { http.end(); continue; }
    String body = http.getString();
    http.end();

    String status = jsonString(body, "status");
    if (status == "done") {
      String transcript = jsonString(body, "transcript");
      String provider   = jsonString(body, "provider");
      bool fallback     = jsonBool(body, "fallback_used");
      int similar       = jsonInt(body, "similar_count", 1);
      showResult(transcript, provider, fallback, similar);
      return true;
    } else if (status == "error") {
      showStatus("FAILED", C_RED, jsonString(body, "message"));
      return false;
    } else if (status == "empty") {
      showStatus("NO SPEECH", C_MUTED, "Didn't catch that. Try again.");
      return false;
    }
    // queued / transcribing -> keep polling
  }
  showStatus("TIMED OUT", C_RED, "No result in time. Try again.");
  return false;
}

void showResult(const String& transcript, const String& provider, bool fallback, int similar) {
  state = RESULT;
  resultShownAt = millis();
  tft.fillScreen(C_BG);
  drawHeader();
  tft.fillRect(0, 30, 320, 3, C_GREEN);

  int y = drawWrapped(transcript.length() ? transcript : "(no transcript)", 8, 44, 312, 2, C_TEXT);

  // provider / fallback line
  tft.setTextSize(1);
  tft.setTextColor(C_MUTED);
  tft.setCursor(8, 210);
  String meta = provider.length() ? provider : "?";
  if (fallback) meta += " (fallback)";
  tft.print(meta);

  if (similar > 1) {
    tft.setTextColor(C_TEAL);
    tft.setCursor(8, 224);
    tft.setTextSize(1);
    tft.printf("%d people asked something similar", similar);
  }
}

// --------------------------- Badge state poll ------------------------------
void pollBadgeState() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  String url = String(API_BASE_URL) + "/api/v1/badge/state?badge_id=" + BADGE_ID;
  if (!httpBegin(http, url)) { http.end(); return; }
  http.addHeader("X-Persephone-Key", BADGE_API_KEY);
  int code = http.GET();
  if (code == 200) {
    String body = http.getString();
    currentRoundId = jsonString(body, "round_id");
    String prompt = jsonString(body, "round_prompt");
    if (prompt != currentPrompt) {
      currentPrompt = prompt;
      if (state == READY) drawReady();
    }
  }
  http.end();
}

void drawReady() {
  showStatus("READY", C_TEAL, currentPrompt.length()
             ? String("Host asks: ") + currentPrompt
             : String("Hold the button and speak."));
}

// --------------------------- Arduino lifecycle -----------------------------
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[persephone] booting");

  pinMode(PIN_BUTTON, INPUT_PULLUP);

  displayInit();
  showStatus("BOOTING", C_TEAL, "Persephone badge starting...");

  if (!psramFound()) {
    Serial.println("[psram] NOT found — cannot allocate audio buffer");
    showStatus("FAILED", C_RED, "PSRAM not available");
    while (true) delay(1000);
  }
  wavBuffer = (uint8_t*)ps_malloc(BUFFER_BYTES);
  if (!wavBuffer) {
    showStatus("FAILED", C_RED, "Out of PSRAM");
    while (true) delay(1000);
  }
  Serial.printf("[psram] allocated %u byte audio buffer\n", BUFFER_BYTES);

  i2sInit();
  connectWifi();
  pollBadgeState();

  state = READY;
  drawReady();
}

void loop() {
  // Button pressed (active LOW) with debounce -> record + upload + poll.
  if (digitalRead(PIN_BUTTON) == LOW) {
    delay(DEBOUNCE_MS);
    if (digitalRead(PIN_BUTTON) == LOW) {
      recordWhileHeld();                 // blocks until release / 10s cap
      String qid = uploadRecording();
      if (qid.length()) {
        pollResult(qid);
      }
      // wait for physical release so we don't immediately re-trigger
      while (digitalRead(PIN_BUTTON) == LOW) delay(10);
      resultShownAt = millis();
    }
  }

  // Return to READY after showing a result for a while.
  if (state == RESULT && millis() - resultShownAt > RESULT_HOLD_MS) {
    state = READY;
    pollBadgeState();
    drawReady();
  }

  // Periodic idle state poll (round prompt / reconnect).
  if ((state == READY) && millis() - lastStatePoll > STATE_POLL_MS) {
    lastStatePoll = millis();
    ensureWifi();
    pollBadgeState();
  }

  delay(10);
}

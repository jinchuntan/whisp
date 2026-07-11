# Whisp Badge Firmware (ESP32-S3)

Arduino sketch for the Whisp lanyard badge. Hold the button to record a
question, release to upload; the badge polls the API and shows the transcript.

## Hardware (proven — do not change pins without rewiring)

| Part | Pins |
|------|------|
| INMP441 I2S mic | BCLK **4**, WS **5**, SD **6** (VDD→3V3, GND→GND, L/R→GND) |
| Button | **GPIO7** → GND (INPUT_PULLUP, pressed = LOW) |
| ILI9341 TFT (landscape clone) | RST **8**, DC **9**, CS **10**, MOSI **11**, SCLK **12**, MISO **13** (VCC→5V, LED→3V3) |

**Mandatory display workaround** (already in `displayInit()`): after
`setRotation(1)`, write `MADCTL = 0x40`. Never call `setRotation()` again without
immediately reapplying `0x40`, or the landscape clone flips.

## Board settings (Arduino IDE)

- Board: **ESP32S3 Dev Module**
- Flash Size: 16 MB · Flash Mode: QIO 80 MHz · **PSRAM: OPI PSRAM**
- CPU: 240 MHz · USB Mode: Hardware CDC and JTAG · USB CDC On Boot: Disabled
- Upload via the UART/COM USB-C port · Serial baud **115200**

## Libraries (Arduino ESP32 core 3.x)

`WiFi`, `WiFiClientSecure`, `HTTPClient`, `ESP_I2S`, `SPI`, `Adafruit_GFX`,
`Adafruit_ILI9341`. Install **Adafruit GFX** and **Adafruit ILI9341** via the
Library Manager; the rest ship with the ESP32 core. No `ArduinoJson` is needed —
a tiny built-in extractor parses the small, flat API responses.

## Configuration

1. Copy `config.example.h` → `config.h` (this folder). `config.h` is gitignored.
2. Fill in `WIFI_SSID`, `WIFI_PASSWORD` (2.4 GHz only), `API_BASE_URL`,
   `BADGE_API_KEY`, `BADGE_ID`.
   - Local dev (Windows hotspot): `http://192.168.137.1:8000`
   - Vercel: `https://your-project.vercel.app`
3. For `https://` URLs, either paste a root CA into `WHISP_ROOT_CA`, or (dev
   only) uncomment `#define WHISP_INSECURE_TLS 1`. Insecure mode disables
   certificate validation — **never ship it**.

## Flow (async upload-and-poll)

1. Connect Wi-Fi, poll `GET /api/v1/badge/state` for the active round prompt.
2. Hold button → record 16 kHz mono PCM16 into PSRAM (10 s cap).
3. Release → build a 44-byte WAV header → `POST /api/v1/questions`
   (`X-Whisp-Key`, `X-Badge-Id`, `X-Round-Id`).
4. Parse `question_id` from the `202` response.
5. Poll `GET /api/v1/questions/{id}` every ~900 ms (≤ ~30 s).
6. On `done`, show the transcript, provider/fallback line, and
   "N people asked something similar" when `similar_count > 1`.

States: `BOOTING → CONNECTING → READY → LISTENING → UPLOADING → TRANSCRIBING →
RESULT` (or `FAILED`). Serial Monitor prints diagnostics (never secrets).

## Optional: compile check with arduino-cli

Not required, and not part of the Python test suite. If you have `arduino-cli`:

```bash
arduino-cli core install esp32:esp32
arduino-cli lib install "Adafruit GFX Library" "Adafruit ILI9341"
# config.h must exist first (copy from config.example.h)
arduino-cli compile --fqbn esp32:esp32:esp32s3 firmware/whisp_badge
```

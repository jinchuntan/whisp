# Persephone Badge Hardware

This document describes the Persephone badge hardware, wiring, and the proven firmware behaviour. For build and flash steps, see [`firmware/persephone_badge/README.md`](../firmware/persephone_badge/README.md).

## Microcontroller

- **Board:** MakerEdu MKE-K01 ESP32-S3 Dev Kit N16R8
- **Module:** ESP32-S3-WROOM-1 (16 MB flash, 8 MB OPI PSRAM)
- **Toolchain:** Arduino ESP32 core 3.x
- **Board selection:** "ESP32S3 Dev Module"

## Arduino IDE Board Settings

| Setting | Value |
| --- | --- |
| Flash Size | 16MB |
| Flash Mode | QIO 80MHz |
| PSRAM | OPI PSRAM |
| CPU Frequency | 240MHz |
| USB Mode | Hardware CDC and JTAG |
| USB CDC On Boot | Disabled |
| Upload port | UART / COM (USB-C port) |
| Serial baud | 115200 |

## Wiring

### INMP441 I2S Microphone

| INMP441 pin | Connects to |
| --- | --- |
| VDD | 3V3 |
| GND | GND |
| L/R | GND (selects the left channel) |
| BCLK / SCK | GPIO4 |
| WS | GPIO5 |
| SD / DIN | GPIO6 |

Audio is captured as **16 kHz mono**. The I2S peripheral delivers 32-bit slots; each raw sample is converted to PCM16 by shifting the raw value **right by 14 bits**.

### Push Button

| Button pin | Connects to |
| --- | --- |
| One side | GPIO7 |
| Other side | GND |

- Configured as `INPUT_PULLUP`; a press reads **LOW**.
- Approximately **30 ms** debounce.
- **Hold to record, release to stop.**

### ILI9341-compatible 2.8" TFT

This is a landscape-native ILI9342 clone.

| TFT pin | Connects to |
| --- | --- |
| VCC | 5V |
| GND | GND |
| RST | GPIO8 |
| DC | GPIO9 |
| CS | GPIO10 |
| MOSI | GPIO11 |
| SCLK | GPIO12 |
| MISO | GPIO13 |
| LED | 3V3 |

## Mandatory Display Workaround

This panel is a landscape-native ILI9342 clone, so the standard rotation is wrong until the `MADCTL` register is written manually. **After** calling `tft.setRotation(1);`, write `MADCTL` = `0x40`:

```cpp
tft.setRotation(1);
uint8_t madctl = 0x40;
tft.sendCommand(ILI9341_MADCTL, &madctl, 1);
```

> **Important:** Calling `setRotation()` again overwrites this fix. Display init is therefore centralized in the firmware's `displayInit()`. Do **not** call `setRotation()` again without reapplying `MADCTL` `0x40`.

After the fix, the canvas is **320x240 landscape**.

## Proven Behaviour

- Allocates a maximum **10-second PCM16** buffer in PSRAM.
- Builds a **44-byte PCM WAV header**.
- Records **16 kHz mono** audio.
- Uploads the full WAV via **HTTP POST**.
- Recordings are typically **~100-350 KB**.
- Faster-Whisper "base" on CPU transcribed a sample in **~1.3s**.
- Example transcript: *"How can artificial intelligence improve audience participation?"*

## Firmware Configuration

Build and flash instructions live in [`firmware/persephone_badge/README.md`](../firmware/persephone_badge/README.md).

Before building, copy `config.example.h` to `config.h`. The `config.h` file is **gitignored** — **never commit Wi-Fi credentials.**

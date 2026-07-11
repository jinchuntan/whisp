// ===========================================================================
// Persephone badge configuration — TEMPLATE.
//
// Copy this file to `config.h` (same folder) and fill in real values.
// `config.h` is gitignored — NEVER commit real Wi-Fi or API credentials.
// ===========================================================================
#pragma once

// ---- Wi-Fi (2.4 GHz only; ESP32-S3 does not join 5 GHz networks) ----
#define WIFI_SSID     "YOUR_WIFI_SSID"
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"

// ---- Persephone API base URL (no trailing slash) ----
// Local Windows Mobile Hotspot / dev server:
//   #define API_BASE_URL "http://192.168.137.1:8000"
// Vercel deployment:
//   #define API_BASE_URL "https://your-project.vercel.app"
#define API_BASE_URL  "http://192.168.137.1:8000"

// ---- Prototype API key (sent as X-Persephone-Key) ----
#define BADGE_API_KEY "replace-with-badge-key"

// ---- This badge's identity (must match ^[A-Za-z0-9_-]{1,64}$) ----
#define BADGE_ID      "badge-001"

// ---------------------------------------------------------------------------
// HTTPS / TLS
// ---------------------------------------------------------------------------
// For https:// URLs the badge uses WiFiClientSecure. Prefer proper CA
// validation: paste the server's root CA PEM into PERSEPHONE_ROOT_CA below.
//
// If you cannot pin a CA during the hackathon, you MAY enable the INSECURE
// fallback by uncommenting the next line. This DISABLES certificate validation
// and is NOT production-safe — it exposes the badge to man-in-the-middle
// attacks. Leave it OFF unless you are actively debugging TLS.
//
// #define PERSEPHONE_INSECURE_TLS 1

// Root CA (PEM). Leave empty to rely on PERSEPHONE_INSECURE_TLS for https.
#define PERSEPHONE_ROOT_CA ""

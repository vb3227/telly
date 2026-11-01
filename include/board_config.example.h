#pragma once

// Copy to board_config.h and fill in private values. Do not commit that file.
#define WIFI_SSID ""
#define WIFI_PASSWORD ""
#define BOARD_TOKEN ""

#define MAIN_PORT 8765
#define RESCUE_PORT 8766
#define SCREEN_DIR 2

// Configure the Groq key on the laptop, never in published firmware.
#define VOICE_GATEWAY_URL "http://<laptop-LAN-IP>:8767"
#define VOICE_GATEWAY_TOKEN ""

// Cross-network relay URL is configured over USB with TELLY_RELAY_URL and saved in NVS.
// Keep VOICE_GATEWAY_TOKEN identical to TELLY_VOICE_GATEWAY_TOKEN on the laptop.

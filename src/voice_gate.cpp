#include "voice_gate.h"
#include <atomic>
#include <esp_system.h>
namespace {
std::atomic<uint32_t> epoch{1};
std::atomic<bool> blocked{false};
uint32_t localUntil = 0, quietSince = 0;
bool wasAwake = false;
}
void initVoiceGate() { epoch.store(esp_random() | 1U); }
void localVoiceDetected() {
    epoch.fetch_add(1);
    localUntil = millis() + 7500;
    quietSince = 0;
    blocked.store(true);
}
void serviceVoiceGate(bool awake, uint32_t rms) {
    const uint32_t now = millis();
    if (awake && !wasAwake) localVoiceDetected();
    wasAwake = awake;
    if (rms < 350) { if (!quietSince) quietSince = now; }
    else quietSince = 0;
    if (awake) {
        blocked.store(true);
        if ((int32_t)(localUntil - now) < 800) localUntil = now + 800;
    } else if (blocked.load() && (int32_t)(now - localUntil) >= 0 &&
               ((quietSince && now - quietSince >= 800) || now - localUntil >= 1000)) {
        // Classroom noise may never fall below the absolute RMS threshold.
        // Once the ASR wake window has ended, silence gets at most one extra
        // second; ambient noise must never lock the button recorder forever.
        blocked.store(false);
    }
}
void releaseIdleLocalVoice() {
    // A new physical button press is explicit input after the wake window.
    // Do not override an ASR utterance that is still awake, or revive its epoch.
    if (!wasAwake) blocked.store(false);
}
bool localVoiceActive() { return blocked.load(); }
uint32_t currentVoiceEpoch() { return epoch.load(); }
bool cloudVoiceEpochValid(uint32_t value) { return !blocked.load() && value == epoch.load(); }
bool containsLocalWake(const String &text) {
    String normalized = text;
    normalized.toLowerCase();
    normalized.replace(",", " "); normalized.replace(".", " "); normalized.replace("!", " ");
    while (normalized.indexOf("  ") >= 0) normalized.replace("  ", " ");
    return normalized.indexOf("hi telly") >= 0 || normalized.indexOf("high telly") >= 0 ||
           normalized.indexOf("hey telly") >= 0 || normalized.indexOf("hi telli") >= 0;
}

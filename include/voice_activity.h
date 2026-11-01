#pragma once
#include <Arduino.h>

enum class VoiceActivity : uint8_t { Idle, Listening, Transcribing, Sending, Matching,
    Thinking, Writing, Validating, Executing, Started, Done, Error, Cancelled };

// State updates are safe from worker tasks; LVGL is touched only by service.
void beginVoiceActivity(const char *captureId, uint32_t epoch);
void setVoiceActivity(VoiceActivity stage);
bool receiveVoiceActivity(const char *captureId, uint32_t epoch, const char *stage);
String voiceActivityCaptureId();
const char *voiceActivityName();
uint8_t voiceActivityPercent();
void serviceVoiceActivity();
bool previewVoiceActivity();

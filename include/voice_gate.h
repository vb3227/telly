#pragma once
#include <Arduino.h>
void initVoiceGate();
void localVoiceDetected();
void serviceVoiceGate(bool wakeActive, uint32_t rms);
void releaseIdleLocalVoice();
bool localVoiceActive();
uint32_t currentVoiceEpoch();
bool cloudVoiceEpochValid(uint32_t epoch);
bool containsLocalWake(const String &text);

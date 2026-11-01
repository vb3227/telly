#pragma once
#include <Arduino.h>
#include <atomic>
#include "unihiker_k10.h"

// Record PCM and submit it once to the laptop; the laptop dispatches directly.
bool transcribeVoiceClip(UNIHIKER_K10 &board, String &transcript, String &error,
    const std::atomic<bool> *cancelled, bool preRoll, const std::atomic<bool> *finishRequested, uint32_t epoch);
void initVoiceTranscription();
bool voiceCaptureBufferReady();

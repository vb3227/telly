#include "voice_transcription.h"
#include "board_config.h"
#include "relay_client.h"
#include "audio_capture.h"
#include "voice_gate.h"
#include "voice_activity.h"
#include <WiFi.h>
#include <ArduinoJson.h>
#include <esp_heap_caps.h>
#include <cstring>
#include <cmath>

namespace {
constexpr uint32_t SAMPLE_RATE = 16000;
constexpr uint32_t MAX_CLIP_SECONDS = 10;
constexpr size_t MAX_SAMPLE_COUNT = SAMPLE_RATE * MAX_CLIP_SECONDS;
constexpr uint32_t SILENCE_RMS_THRESHOLD = 350;


int16_t *voiceSamples=nullptr;
void put16(uint8_t *p, uint16_t v) { p[0] = v & 0xff; p[1] = (v >> 8) & 0xff; }
void put32(uint8_t *p, uint32_t v) {
    p[0] = v & 0xff; p[1] = (v >> 8) & 0xff; p[2] = (v >> 16) & 0xff; p[3] = (v >> 24) & 0xff;
}
void wavHeader(uint8_t *h, uint32_t dataBytes) {
    memcpy(h, "RIFF", 4); put32(h + 4, 36 + dataBytes); memcpy(h + 8, "WAVEfmt ", 8);
    put32(h + 16, 16); put16(h + 20, 1); put16(h + 22, 1); put32(h + 24, SAMPLE_RATE);
    put32(h + 28, SAMPLE_RATE * 2); put16(h + 32, 2); put16(h + 34, 16);
    memcpy(h + 36, "data", 4); put32(h + 40, dataBytes);
}
}

void initVoiceTranscription(){
    if(!voiceSamples)voiceSamples=(int16_t*)heap_caps_malloc(MAX_SAMPLE_COUNT*sizeof(int16_t),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    Serial.printf("[voice] reserved mono recording buffer: %s (%u bytes)\n",voiceSamples?"ready":"FAILED",(unsigned)(MAX_SAMPLE_COUNT*sizeof(int16_t)));
}
bool voiceCaptureBufferReady(){return voiceSamples!=nullptr;}

bool transcribeVoiceClip(UNIHIKER_K10 &board, String &transcript, String &error, const std::atomic<bool> *cancelled, bool preRoll, const std::atomic<bool> *finishRequested, uint32_t epoch) {
    transcript = "";
    if (!relayConnected()) { error="Laptop WebSocket is offline"; return false; }
    if (WiFi.status() != WL_CONNECTED) { error = "Wi-Fi is not connected"; return false; }
    int16_t *samples=voiceSamples;
    if(!samples){error="Reserved voice buffer unavailable; reboot device";return false;}

    Serial.println("[voice] listening: press the same control to finish (max 10 seconds)");
    if (!startAudioCapture(samples, MAX_SAMPLE_COUNT, preRoll, true)) {
        error = "Microphone capture is already active"; return false;
    }
    size_t sampleCount = 0, lastSpeechSample = 0;
    bool heardSpeech = false;
    bool captureStopped = false;
    const uint32_t deadline = millis() + 11500;
    while (sampleCount < MAX_SAMPLE_COUNT && (int32_t)(deadline - millis()) > 0) {
        if (cancelled && cancelled->load()) break;
        if (!captureStopped && finishRequested && finishRequested->load()) {
            // Freeze the PCM on the matching stop control, then drain every captured frame,
            // including the last partial 20 ms window, before transcription.
            stopAudioCapture(); captureStopped = true;
            Serial.println("[voice] recording finished; transcribing now");
        }
        const size_t available = audioCapturedFrames();
        if (captureStopped && available <= sampleCount) break;
        if (!captureStopped && available - sampleCount < SAMPLE_RATE / 50) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        const size_t end = min(available, sampleCount + SAMPLE_RATE / 50);
        uint64_t energy[2] = {0, 0};
        for (size_t i = sampleCount; i < end; ++i) {
            const int32_t value=samples[i];energy[0]+=(int64_t)value*value;
        }
        const size_t window = end - sampleCount;
        sampleCount = end;
        const uint32_t rms = sqrt((double)max(energy[0], energy[1]) / window);
        if (rms >= SILENCE_RMS_THRESHOLD) { heardSpeech = true; lastSpeechSample = sampleCount; }

        // Manual toggle mode: silence does not submit a partial instruction.
        // The finish flag or the 10-second safety cap ends this recording.
    }
    stopAudioCapture();
    const uint32_t captureFinishedAt = millis();
    if (cancelled && cancelled->load()) { error = "Voice recording cancelled"; return false; }
    if (!heardSpeech || sampleCount < SAMPLE_RATE / 4) {
        error = "No speech detected in microphone PCM"; return false;
    }
    // Keep 250 ms after the last audible window, discard only trailing silence.
    sampleCount = min(sampleCount, lastSpeechSample + SAMPLE_RATE / 4);
    Serial.printf("Voice PCM: %u mono frames\n",(unsigned)sampleCount);

    setVoiceActivity(VoiceActivity::Transcribing);
    markVoiceCaptureStopped(voiceActivityCaptureId());
    const size_t total=44+sampleCount*2;
    uint8_t *wav=(uint8_t*)heap_caps_malloc(total,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    if(!wav){error="WAV buffer unavailable";return false;}
    wavHeader(wav,sampleCount*2);memcpy(wav+44,samples,sampleCount*2);
    JsonDocument meta;meta["type"]="audio";meta["id"]=voiceActivityCaptureId();meta["epoch"]=epoch;
    String reply;bool ok=relaySendBlob(meta,wav,total,reply,error,true);
    if(!ok)return false;
    JsonDocument parsed;if(deserializeJson(parsed,reply)){error="Invalid speech reply";return false;}
    transcript=parsed["text"]|"";
    Serial.printf("[voice timing] stop_to_transcript_ms=%lu\n",(unsigned long)(millis()-captureFinishedAt));
    if(!transcript.length()){error="No speech detected in microphone PCM";return false;}
    error="";return true;
}

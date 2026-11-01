// Copy the PCM already read by the vendor ASR task. Never steal its I2S frames.
#include "audio_capture.h"
#include <driver/i2s.h>
#include <cmath>
#include <cstring>
#include <esp_heap_caps.h>

namespace {
portMUX_TYPE captureLock = portMUX_INITIALIZER_UNLOCKED;
int16_t *captureBuffer = nullptr;
size_t captureCapacity = 0;
size_t captureCount = 0;
bool captureMono=false;
int latestChannel=0;
uint32_t latestRms = 0;
uint32_t observedFrames = 0;
constexpr size_t PRE_ROLL_FRAMES = 3200;
int16_t *preRollBuffer = nullptr;
size_t preRollWrite = 0, preRollCount = 0;
}

extern "C" esp_err_t __real_i2s_read(i2s_port_t port, void *dest, size_t size,
                                    size_t *bytesRead, TickType_t timeout);
extern "C" esp_err_t __wrap_i2s_read(i2s_port_t port, void *dest, size_t size,
                                    size_t *bytesRead, TickType_t timeout) {
    esp_err_t status = __real_i2s_read(port, dest, size, bytesRead, timeout);
    if (status != ESP_OK || port != I2S_NUM_0 || !bytesRead || !*bytesRead) return status;
    const int16_t *pcm = static_cast<const int16_t *>(dest);
    const size_t frames = *bytesRead / (2 * sizeof(int16_t));
    if (!frames) return status;
    uint64_t energy[2] = {0, 0};
    for (size_t i = 0; i < frames; ++i) {
        for (size_t channel = 0; channel < 2; ++channel) {
            int32_t sample = pcm[2 * i + channel];
            energy[channel] += (int64_t)sample * sample;
        }
    }
    uint32_t rms = sqrt((double)max(energy[0], energy[1]) / frames);
    portENTER_CRITICAL(&captureLock);
    latestChannel=energy[1]>energy[0]?1:0;
    latestRms = rms;
    observedFrames += frames;
    if (preRollBuffer) {
        for (size_t i = 0; i < frames; ++i) {
            preRollBuffer[2 * preRollWrite] = pcm[2 * i];
            preRollBuffer[2 * preRollWrite + 1] = pcm[2 * i + 1];
            preRollWrite = (preRollWrite + 1) % PRE_ROLL_FRAMES;
            if (preRollCount < PRE_ROLL_FRAMES) ++preRollCount;
        }
    }
    if (captureBuffer && captureCount < captureCapacity) {
        size_t count = min(frames, captureCapacity - captureCount);
        if(captureMono){for(size_t i=0;i<count;++i)captureBuffer[captureCount+i]=pcm[2*i+latestChannel];}
        else memcpy(captureBuffer + captureCount * 2, pcm, count * 2 * sizeof(int16_t));
        captureCount += count;
    }
    portEXIT_CRITICAL(&captureLock);
    return status;
}

void initAudioCapture() {
    if (!preRollBuffer) preRollBuffer = (int16_t *)heap_caps_malloc(
        PRE_ROLL_FRAMES * 2 * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
}
bool startAudioCapture(int16_t *stereo, size_t capacityFrames, bool preRoll, bool mono) {
    portENTER_CRITICAL(&captureLock);
    bool available = captureBuffer == nullptr;
    if (available) {
        captureCount = 0;
        captureCapacity = capacityFrames;
        captureBuffer = stereo;
        captureMono=mono;
        if (preRoll && preRollBuffer) {
            const size_t count = min(preRollCount, capacityFrames);
            const size_t start = (preRollWrite + PRE_ROLL_FRAMES - count) % PRE_ROLL_FRAMES;
            for (size_t i = 0; i < count; ++i) {
                size_t source = (start + i) % PRE_ROLL_FRAMES;
                if(mono)stereo[i]=preRollBuffer[2*source+latestChannel];
                else {stereo[2 * i] = preRollBuffer[2 * source];stereo[2 * i + 1] = preRollBuffer[2 * source + 1];}
            }
            captureCount = count;
        }
    }
    portEXIT_CRITICAL(&captureLock);
    return available;
}
size_t audioCapturedFrames() {
    portENTER_CRITICAL(&captureLock);
    size_t count = captureCount;
    portEXIT_CRITICAL(&captureLock);
    return count;
}
bool audioCaptureActive() {
    portENTER_CRITICAL(&captureLock);
    bool active = captureBuffer != nullptr;
    portEXIT_CRITICAL(&captureLock);
    return active;
}
void stopAudioCapture() {
    portENTER_CRITICAL(&captureLock);
    captureBuffer = nullptr;
    captureCapacity = 0;
    portEXIT_CRITICAL(&captureLock);
}
uint32_t microphoneRms() {
    portENTER_CRITICAL(&captureLock);
    uint32_t value = latestRms;
    portEXIT_CRITICAL(&captureLock);
    return value;
}
uint32_t microphoneFramesObserved() {
    portENTER_CRITICAL(&captureLock);
    uint32_t value = observedFrames;
    portEXIT_CRITICAL(&captureLock);
    return value;
}


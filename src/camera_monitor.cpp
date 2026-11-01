#include "camera_monitor.h"
#include <esp_camera.h>
#include <img_converters.h>
#include <esp_heap_caps.h>
#include <atomic>
#include <cstring>
namespace {
portMUX_TYPE observationLock = portMUX_INITIALIZER_UNLOCKED;
CameraObservation latest;
constexpr size_t SNAPSHOT_BYTES = 320 * 240 * 2;
uint8_t *snapshotPixels = nullptr;
camera_fb_t snapshotFrame = {};
std::atomic<int> snapshotState{0}; // idle, requested, copying, ready, invalid
std::atomic<bool> snapshotBusy{false};
uint32_t snapshotMs = 0;
}
extern "C" camera_fb_t *__real_esp_camera_fb_get();
extern "C" camera_fb_t *__wrap_esp_camera_fb_get() {
    camera_fb_t *frame = __real_esp_camera_fb_get();
    if (!frame) return nullptr;
    int expected=1;
    if (snapshotState.compare_exchange_strong(expected,2)) {
        if (snapshotPixels && frame->len <= SNAPSHOT_BYTES) {
            memcpy(snapshotPixels,frame->buf,frame->len);
            snapshotFrame=*frame; snapshotFrame.buf=snapshotPixels;
            snapshotMs=millis(); snapshotState.store(3);
        } else snapshotState.store(4);
    }
    uint32_t checksum = 2166136261UL;
    for (size_t i = 0; i < frame->len; i += 4093) { checksum ^= frame->buf[i]; checksum *= 16777619UL; }
    portENTER_CRITICAL(&observationLock);
    ++latest.frames;
    latest.width = frame->width; latest.height = frame->height;
    latest.bytes = frame->len; latest.format = frame->format;
    latest.checksum = checksum; latest.updatedMs = millis();
    portEXIT_CRITICAL(&observationLock);
    return frame;
}
CameraObservation cameraObservation() {
    portENTER_CRITICAL(&observationLock);
    CameraObservation copy = latest;
    portEXIT_CRITICAL(&observationLock);
    return copy;
}

bool captureCameraJpeg(uint8_t **jpeg, size_t *bytes, uint32_t *capturedMs, uint32_t timeoutMs) {
    bool expected=false;
    if (!snapshotBusy.compare_exchange_strong(expected,true)) return false;
    *jpeg=nullptr; *bytes=0;
    if (!snapshotPixels) snapshotPixels=(uint8_t *)heap_caps_malloc(SNAPSHOT_BYTES,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    if (!snapshotPixels) { snapshotBusy.store(false); return false; }
    snapshotState.store(1);
    uint32_t started=millis();
    while (snapshotState.load() < 3 && millis()-started < timeoutMs) vTaskDelay(pdMS_TO_TICKS(5));
    int waiting=1;
    snapshotState.compare_exchange_strong(waiting,0); // Cancel only a not-yet-started copy.
    while (snapshotState.load()==2) vTaskDelay(pdMS_TO_TICKS(1));
    bool ok=false;
    if (snapshotState.load()==3) {
        if (snapshotFrame.format==PIXFORMAT_JPEG) {
            *jpeg=(uint8_t *)malloc(snapshotFrame.len);
            if (*jpeg) { memcpy(*jpeg,snapshotPixels,snapshotFrame.len); *bytes=snapshotFrame.len; ok=true; }
        } else ok=frame2jpg(&snapshotFrame,65,jpeg,bytes);
        if (ok && capturedMs) *capturedMs=snapshotMs;
    }
    snapshotState.store(0); snapshotBusy.store(false);
    return ok;
}

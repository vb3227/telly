#pragma once
#include <Arduino.h>
struct CameraObservation {
    uint32_t frames = 0, width = 0, height = 0, bytes = 0, format = 0;
    uint32_t checksum = 0, updatedMs = 0;
};
CameraObservation cameraObservation();

// Copy one preview frame without stealing it from the vendor camera task.
// Caller owns the JPEG and frees it with free().
bool captureCameraJpeg(uint8_t **jpeg, size_t *bytes, uint32_t *capturedMs, uint32_t timeoutMs = 2000);

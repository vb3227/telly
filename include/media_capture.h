#pragma once
#include <Arduino.h>
struct MediaCaptureStatus {
    String session,state="idle",error,filename;
    uint32_t frames=0,startedMs=0;
    bool photo=true;
};
void initMediaCapture();
bool beginMediaCapture(bool photo);
void finishMediaCapture();
bool mediaCaptureBusy();
MediaCaptureStatus mediaCaptureStatus();

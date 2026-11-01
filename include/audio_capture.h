#pragma once
#include <Arduino.h>
void initAudioCapture();
bool startAudioCapture(int16_t *stereo, size_t capacityFrames, bool preRoll = false, bool mono = false);
size_t audioCapturedFrames();
bool audioCaptureActive();
void stopAudioCapture();
uint32_t microphoneRms();
uint32_t microphoneFramesObserved();



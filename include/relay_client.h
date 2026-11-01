#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

void startRelayClient();
void serviceRelaySerial();
String relayGatewayUrl();

bool relayConnected();
void markVoiceCaptureStopped(const String &capture);
bool relaySendBlob(JsonDocument &meta,const uint8_t *data,size_t size,String &result,String &error,bool takeOwnership=false);

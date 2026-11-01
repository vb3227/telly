#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
bool validateBoardProgram(JsonObjectConst program, String &error);

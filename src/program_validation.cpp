#include "program_validation.h"
#include <cstring>
struct FieldRule { const char *op; const char *name; int64_t low; int64_t high; uint8_t type; };
static const FieldRule rules[] = {
    {"delay", "duration_ms", 0, 3600000, 0},
    {"rgb.set", "r", 0, 255, 0},
    {"rgb.set", "g", 0, 255, 0},
    {"rgb.set", "b", 0, 255, 0},
    {"rgb.set", "brightness", 0, 9, 0},
    {"rgb.blink", "r", 0, 255, 0},
    {"rgb.blink", "g", 0, 255, 0},
    {"rgb.blink", "b", 0, 255, 0},
    {"rgb.blink", "brightness", 0, 9, 0},
    {"rgb.blink", "count", 1, 60, 0},
    {"rgb.blink", "period_ms", 40, 2000, 0},
    {"display.clear", "color", 0, 0, 2},
    {"display.text", "text", 0, 0, 1},
    {"display.text", "x", -320, 640, 0},
    {"display.text", "y", -320, 960, 0},
    {"display.text", "color", 0, 0, 2},
    {"display.square", "x", -320, 640, 0},
    {"display.square", "y", -320, 960, 0},
    {"display.square", "w", 1, 640, 0},
    {"display.square", "h", 1, 640, 0},
    {"display.square", "size", 1, 640, 0},
    {"display.square", "color", 0, 0, 2},
    {"display.rect", "x", -320, 640, 0},
    {"display.rect", "y", -320, 960, 0},
    {"display.rect", "w", 1, 640, 0},
    {"display.rect", "h", 1, 640, 0},
    {"display.rect", "size", 1, 640, 0},
    {"display.rect", "color", 0, 0, 2},
    {"display.line", "x1", -320, 640, 0},
    {"display.line", "y1", -320, 960, 0},
    {"display.line", "x2", -320, 640, 0},
    {"display.line", "y2", -320, 960, 0},
    {"display.line", "color", 0, 0, 2},
    {"camera.preview", "duration_ms", 0, 120000, 0},
};
static const char *operations[] = {"delay","rgb.set","rgb.off","rgb.blink","display.clear","display.text","display.square","display.rect","display.line","sensor.read","sensor.read.display","mic.sample","asr.start","asr.last","camera.preview","camera.stop","camera.status","camera.photo","camera.record","camera.finish","voice.transcribe"};
bool validateBoardProgram(JsonObjectConst program, String &error) {
    if (program.isNull() || program.size() != 1 || !program["steps"].is<JsonArrayConst>()) {
        error = "Program must contain only steps"; return false;
    }
    JsonArrayConst steps = program["steps"].as<JsonArrayConst>();
    if (!steps.size() || steps.size() > 32) { error = "Program needs 1-32 steps"; return false; }
    for (JsonVariantConst item : steps) {
        if (!item.is<JsonObjectConst>() || !item["op"].is<const char *>()) {
            error = "Each step needs an operation"; return false;
        }
        const char *op = item["op"].as<const char *>();
        bool known = false;
        for (const char *name : operations) if (!strcmp(op,name)) known = true;
        if (!known) { error = "Unsupported operation"; return false; }
        for (JsonPairConst pair : item.as<JsonObjectConst>()) {
            if (!strcmp(pair.key().c_str(),"op")) continue;
            const FieldRule *rule = nullptr;
            for (const auto &candidate : rules)
                if (!strcmp(candidate.op,op) && !strcmp(candidate.name,pair.key().c_str())) { rule=&candidate; break; }
            if (!rule) { error = "Unsupported operation field"; return false; }
            JsonVariantConst value = pair.value();
            bool valid = false;
            if (!rule->type) valid = !value.is<bool>() && value.is<int64_t>() &&
                value.as<int64_t>() >= rule->low && value.as<int64_t>() <= rule->high;
            else if (rule->type == 1) valid = value.is<const char *>() && strlen(value.as<const char *>()) <= 512;
            else {
                valid = !value.is<bool>() && value.is<uint32_t>() && value.as<uint32_t>() <= 0xFFFFFF;
                if (value.is<const char *>()) {
                    const char *text=value.as<const char *>(); if (*text=='#') ++text;
                    valid = strlen(text)==6;
                    for (size_t i=0; valid && i<6; ++i) valid=isxdigit((unsigned char)text[i]);
                }
            }
            if (!valid) { error=String("Invalid field: ")+pair.key().c_str(); return false; }
        }
    }
    return true;
}

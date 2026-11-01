#pragma once
#include <Arduino.h>
#include <lvgl.h>
// Pixel-width wrapping, Latin/Cyrillic glyphs, no temporary framebuffer.
void drawScreenText(const String &text, int16_t x=8, int16_t y=8, uint32_t color=0xFFFFFF);
const lv_font_t *screenTextFont();

#include "screen_text.h"
#include <lvgl.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "telly_font_data.h"

// Canvas and lock exported by the pinned UNIHIKER display SDK. Access only
// under its LVGL mutex; never use the allocating clearLocalCanvas helper.
extern lv_obj_t *_canvas;
extern SemaphoreHandle_t xLvglMutex;

namespace {
const TellyGlyph *glyph(uint32_t cp) {
    size_t lo=0,hi=sizeof(tellyGlyphs)/sizeof(tellyGlyphs[0]);
    while(lo<hi){size_t mid=lo+(hi-lo)/2;if(tellyGlyphs[mid].codepoint<cp)lo=mid+1;else hi=mid;}
    if(lo<sizeof(tellyGlyphs)/sizeof(tellyGlyphs[0]) && tellyGlyphs[lo].codepoint==cp)return &tellyGlyphs[lo];
    return &tellyGlyphs['?'-32];
}
bool descriptor(const lv_font_t *,lv_font_glyph_dsc_t *out,uint32_t cp,uint32_t) {
    if(!cp)return false;
    if(cp=='\n' || cp=='\r'){*out={};return true;}
    const TellyGlyph *g=glyph(cp);
    out->adv_w=g->advance;out->box_w=20;out->box_h=20;
    out->ofs_x=0;out->ofs_y=-4;out->bpp=1;out->is_placeholder=false;
    return true;
}
const uint8_t *bitmap(const lv_font_t *,uint32_t cp){return glyph(cp)->bitmap;}
const lv_font_t font={
    .get_glyph_dsc=descriptor,.get_glyph_bitmap=bitmap,
    .line_height=20,.base_line=4,.subpx=LV_FONT_SUBPX_NONE,
    .underline_position=0,.underline_thickness=1,.dsc=nullptr,.fallback=nullptr,
#if LV_USE_USER_DATA
    .user_data=nullptr,
#endif
};
}

const lv_font_t *screenTextFont(){return &font;}

void drawScreenText(const String &text,int16_t x,int16_t y,uint32_t color) {
    if(!_canvas || !xLvglMutex || !text.length())return;
    xSemaphoreTake(xLvglMutex,portMAX_DELAY);
    const int width=lv_obj_get_width(_canvas),height=lv_obj_get_height(_canvas);
    if(x<width && y<height){
        lv_draw_label_dsc_t label;lv_draw_label_dsc_init(&label);
        label.font=&font;label.color=lv_color_hex(color);label.opa=LV_OPA_COVER;
        label.align=LV_TEXT_ALIGN_LEFT;label.flag=LV_TEXT_FLAG_NONE;
        lv_canvas_draw_text(_canvas,x,y,width-max(0,(int)x),&label,text.c_str());
    }
    xSemaphoreGive(xLvglMutex);
}

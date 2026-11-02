#include "boot_animation.h"
#include "boot_symbol.h"
#include "screen_text.h"
#include <unihiker_k10.h>
#include <lvgl.h>
#include <freertos/semphr.h>
extern UNIHIKER_K10 k10;
extern lv_obj_t *_canvas;
extern SemaphoreHandle_t xLvglMutex;

static void drawBootFrame(uint8_t percent, const char *stage) {
    if (!_canvas || !xLvglMutex) return;
    xSemaphoreTake(xLvglMutex,portMAX_DELAY);
    lv_canvas_fill_bg(_canvas,lv_color_hex(0x070D0C),LV_OPA_COVER);
    renderBootSymbol(percent,[](int x1,int y1,int x2,int y2,uint32_t color){
        lv_point_t points[2]={{(lv_coord_t)x1,(lv_coord_t)y1},{(lv_coord_t)x2,(lv_coord_t)y2}};
        lv_draw_line_dsc_t style;lv_draw_line_dsc_init(&style);style.color=lv_color_hex(color);style.width=1;style.opa=LV_OPA_COVER;
        lv_canvas_draw_line(_canvas,points,2,&style);
    });
    lv_draw_rect_dsc_t bar;lv_draw_rect_dsc_init(&bar);bar.bg_opa=LV_OPA_COVER;bar.bg_color=lv_color_hex(0x26352F);bar.border_width=0;bar.radius=2;
    lv_canvas_draw_rect(_canvas,30,235,180,4,&bar);
    bar.bg_color=lv_color_hex(0x48E89B);if(percent)lv_canvas_draw_rect(_canvas,30,235,180*percent/100,4,&bar);
    xSemaphoreGive(xLvglMutex);
    drawScreenText("INITIALIZING",48,16,0xA8B5AE);
    drawScreenText(String(percent)+"%",100,247,0x48E89B);
    drawScreenText(stage,20,271,0xA8B5AE);
    drawScreenText("by vb3227",70,298,0x718278);
    k10.canvas->updateCanvas();
}

void showBootProgress(uint8_t percent, const char *stage) {
    static uint8_t previous = 0;
    static bool started = false;
    percent = percent > 100 ? 100 : percent;
    if (!started || percent <= previous) {
        drawBootFrame(percent, stage);
    } else {
        for (unsigned frame = 1; frame <= 6; ++frame) {
            drawBootFrame(previous + (percent - previous) * frame / 6, stage);
            delay(12);
        }
    }
    previous = percent;
    started = true;
}

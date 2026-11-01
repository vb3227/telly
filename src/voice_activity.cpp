#include "voice_activity.h"
#include "screen_text.h"
#include "voice_gate.h"
#include <lvgl.h>
#include <cmath>
#include <cstring>

extern SemaphoreHandle_t xLvglMutex;
namespace {
portMUX_TYPE stateLock=portMUX_INITIALIZER_UNLOCKED;
struct State {
    VoiceActivity stage=VoiceActivity::Idle;
    char capture[41]={};
    uint32_t epoch=0,changed=0,started=0;
    uint8_t percent=0;
    bool preview=false;
} state;
lv_obj_t *panel=nullptr,*caption=nullptr,*percentLabel=nullptr,*footer=nullptr;
uint32_t lastFrame=0;
float phase=0;
lv_opa_t fade=LV_OPA_COVER;
uint8_t renderedPercent=255;
VoiceActivity renderedStage=VoiceActivity::Idle;
constexpr int WIDTH=152,HEIGHT=72,BARS=24;
const char *names[]={"idle","listening","transcribing","sending","matching","thinking","writing",
    "validating","executing","started","done","error","cancelled"};
const char *captions[]={"","Listening","Transcribing","Sending","Finding skill","Thinking","Writing code",
    "Checking code","Executing","Started","Done","Failed","Cancelled"};
const uint8_t floors[]={0,3,18,44,48,56,58,82,88,100,100,0,0};
const uint8_t ceilings[]={0,14,43,47,55,81,81,87,97,100,100,0,0};
bool terminal(VoiceActivity s){return s>=VoiceActivity::Started;}
State snapshot(){portENTER_CRITICAL(&stateLock);State copy=state;portEXIT_CRITICAL(&stateLock);return copy;}

void drawBars(lv_event_t *event){
    if(lv_event_get_code(event)!=LV_EVENT_DRAW_MAIN)return;
    State s=snapshot();
    lv_draw_ctx_t *ctx=lv_event_get_draw_ctx(event);
    lv_area_t bounds;lv_obj_get_coords(panel,&bounds);
    float head=fmodf(phase*13.0f,(float)(BARS+7))-3;
    for(int i=0;i<BARS;++i){
        float distance=(i-head)/2.1f;
        float gleam=expf(-distance*distance);
        bool filled=i*100 < BARS*s.percent;
        if(terminal(s.stage))gleam=(s.stage==VoiceActivity::Done || s.stage==VoiceActivity::Started)?0.65f:0.15f;
        float wave=0.5f+0.5f*sinf(phase*4.0f-i*0.31f);
        int h=terminal(s.stage)?14:10+(int)(6*wave+3*gleam);
        uint8_t intensity=(uint8_t)min(255.0f,(filled?100.0f:20.0f)+gleam*155.0f);
        lv_draw_rect_dsc_t d;lv_draw_rect_dsc_init(&d);
        d.radius=2;d.bg_opa=fade;
        d.bg_color=lv_color_mix(lv_color_hex(0x55FFAD),lv_color_hex(0x102A1C),intensity);
        lv_area_t bar={(lv_coord_t)(bounds.x1+10+i*5),(lv_coord_t)(bounds.y1+37-h/2),
            (lv_coord_t)(bounds.x1+12+i*5),(lv_coord_t)(bounds.y1+37+(h-1)/2)};
        // A restrained halo and bright core, with no shadow/framebuffer allocation.
        if(gleam>0.2f){
            lv_area_t halo=bar;halo.x1--;halo.x2++;halo.y1--;halo.y2++;
            d.bg_opa=(lv_opa_t)(gleam*45*fade/255);lv_draw_rect(ctx,&d,&halo);d.bg_opa=fade;
        }
        lv_draw_rect(ctx,&d,&bar);
        if(gleam>0.45f){d.bg_color=lv_color_hex(0xC0FFE0);d.bg_opa=(lv_opa_t)(gleam*170*fade/255);bar.x1=bar.x2-1;lv_draw_rect(ctx,&d,&bar);}
    }
}

void createPanel(){
    panel=lv_obj_create(lv_layer_top());
    lv_obj_remove_style_all(panel);lv_obj_set_size(panel,WIDTH,HEIGHT);
    lv_obj_align(panel,LV_ALIGN_TOP_RIGHT,-7,7);
    lv_obj_clear_flag(panel,(lv_obj_flag_t)(LV_OBJ_FLAG_SCROLLABLE|LV_OBJ_FLAG_CLICKABLE));
    lv_obj_set_style_bg_color(panel,lv_color_hex(0x050D09),0);
    lv_obj_set_style_bg_grad_color(panel,lv_color_hex(0x0B1B12),0);
    lv_obj_set_style_bg_grad_dir(panel,LV_GRAD_DIR_VER,0);
    lv_obj_set_style_bg_opa(panel,LV_OPA_90,0);
    lv_obj_set_style_border_width(panel,1,0);
    lv_obj_set_style_border_color(panel,lv_color_hex(0x24523A),0);
    lv_obj_set_style_radius(panel,11,0);
    lv_obj_add_event_cb(panel,drawBars,LV_EVENT_DRAW_MAIN,nullptr);
    caption=lv_label_create(panel);lv_obj_set_pos(caption,10,4);lv_obj_set_width(caption,134);
    lv_label_set_long_mode(caption,LV_LABEL_LONG_CLIP);
    lv_obj_set_style_text_font(caption,screenTextFont(),0);
    lv_obj_set_style_text_color(caption,lv_color_hex(0xD3FFE5),0);
    percentLabel=lv_label_create(panel);lv_obj_set_pos(percentLabel,10,49);
    lv_obj_set_style_text_font(percentLabel,screenTextFont(),0);
    lv_obj_set_style_text_color(percentLabel,lv_color_hex(0x66FFAE),0);
    footer=lv_label_create(panel);lv_obj_set_pos(footer,91,50);
    lv_obj_set_style_text_font(footer,screenTextFont(),0);
    lv_obj_set_style_text_color(footer,lv_color_hex(0x56846A),0);
    lv_obj_add_flag(panel,LV_OBJ_FLAG_HIDDEN);
}
}

void beginVoiceActivity(const char *captureId,uint32_t epoch){
    if(!captureId || !*captureId || strlen(captureId)>40)return;
    uint32_t now=millis();
    portENTER_CRITICAL(&stateLock);
    state=State{};strlcpy(state.capture,captureId,sizeof(state.capture));state.epoch=epoch;
    state.stage=VoiceActivity::Listening;state.changed=state.started=now;state.percent=3;
    portEXIT_CRITICAL(&stateLock);
}

void setVoiceActivity(VoiceActivity stage){
    if(stage==VoiceActivity::Idle || stage>VoiceActivity::Cancelled)return;
    portENTER_CRITICAL(&stateLock);
    if(state.stage!=VoiceActivity::Idle && !terminal(state.stage) && (stage>=state.stage || stage==VoiceActivity::Error || stage==VoiceActivity::Cancelled)){
        if(stage!=state.stage){state.stage=stage;state.changed=millis();}
        if(stage==VoiceActivity::Started || stage==VoiceActivity::Done)state.percent=100;
        else if(!terminal(stage))state.percent=max(state.percent,floors[(int)stage]);
    }
    portEXIT_CRITICAL(&stateLock);
}

bool receiveVoiceActivity(const char *captureId,uint32_t epoch,const char *stage){
    if(!captureId || !stage || !cloudVoiceEpochValid(epoch))return false;
    State s=snapshot();
    if(s.preview || s.stage==VoiceActivity::Idle || s.epoch!=epoch || strcmp(s.capture,captureId))return false;
    for(int i=(int)VoiceActivity::Matching;i<=(int)VoiceActivity::Error;++i){
        if(!strcmp(stage,names[i])){setVoiceActivity((VoiceActivity)i);return true;}
    }
    return false;
}

String voiceActivityCaptureId(){State s=snapshot();return String(s.capture);}
const char *voiceActivityName(){return names[(int)snapshot().stage];}
uint8_t voiceActivityPercent(){return snapshot().percent;}

bool previewVoiceActivity(){
    State s=snapshot();if(s.stage!=VoiceActivity::Idle && !terminal(s.stage))return false;
    beginVoiceActivity("preview",currentVoiceEpoch());
    portENTER_CRITICAL(&stateLock);state.preview=true;portEXIT_CRITICAL(&stateLock);return true;
}

void serviceVoiceActivity(){
    uint32_t now=millis();if(now-lastFrame<33)return;lastFrame=now;
    State s=snapshot();
    if(s.stage!=VoiceActivity::Idle && !terminal(s.stage)){
        if(s.preview){
            const VoiceActivity demo[]={VoiceActivity::Listening,VoiceActivity::Transcribing,VoiceActivity::Matching,VoiceActivity::Writing,VoiceActivity::Validating,VoiceActivity::Executing,VoiceActivity::Done};
            setVoiceActivity(demo[min((now-s.started)/1100,(uint32_t)6)]);
        } else if(currentVoiceEpoch()!=s.epoch)setVoiceActivity(VoiceActivity::Cancelled);
        else if(now-s.changed>90000)setVoiceActivity(VoiceActivity::Error);
        s=snapshot();
        if(!terminal(s.stage)){
            float t=((int32_t)(now-s.changed)>0?now-s.changed:0)/1000.0f;
            float ease=1.0f-expf(-t/(s.stage==VoiceActivity::Writing?8.0f:4.0f));
            uint8_t value=floors[(int)s.stage]+(uint8_t)((ceilings[(int)s.stage]-floors[(int)s.stage])*ease);
            portENTER_CRITICAL(&stateLock);state.percent=max(state.percent,value);portEXIT_CRITICAL(&stateLock);
        }
    }
    s=snapshot();
    // Register the capture immediately for epoch/ID arbitration, but do not
    // present processing as loading while the user is still recording.
    if(s.stage==VoiceActivity::Listening && !s.preview){
        if(panel && xLvglMutex){
            xSemaphoreTake(xLvglMutex,portMAX_DELAY);
            if(!lv_obj_has_flag(panel,LV_OBJ_FLAG_HIDDEN)){
                lv_obj_add_flag(panel,LV_OBJ_FLAG_HIDDEN);lv_timer_handler();
            }
            xSemaphoreGive(xLvglMutex);
        }
        return;
    }
    uint32_t age=(int32_t)(now-s.changed)>0?now-s.changed:0;
    uint32_t linger=s.stage==VoiceActivity::Error?2200:s.stage==VoiceActivity::Cancelled?500:1200;
    bool hide=s.stage==VoiceActivity::Idle || (terminal(s.stage) && age>=linger+300);
    if(!panel && hide)return;
    if(!xLvglMutex)return;
    xSemaphoreTake(xLvglMutex,portMAX_DELAY);
    if(!panel)createPanel();
    if(hide){
        lv_obj_add_flag(panel,LV_OBJ_FLAG_HIDDEN);
        portENTER_CRITICAL(&stateLock);if(state.stage==s.stage)state.stage=VoiceActivity::Idle;portEXIT_CRITICAL(&stateLock);
    } else {
        lv_obj_clear_flag(panel,LV_OBJ_FLAG_HIDDEN);lv_obj_move_foreground(panel);
        lv_obj_align(panel,LV_ALIGN_TOP_RIGHT,-7,7);
        lv_opa_t opacity=terminal(s.stage) && age>linger?(lv_opa_t)(255*(linger+300-age)/300):LV_OPA_COVER;
        // Per-primitive opacity avoids LVGL's off-screen object-opacity layer.
        fade=opacity;lv_obj_set_style_bg_opa(panel,(lv_opa_t)(230*opacity/255),0);
        lv_obj_set_style_border_opa(panel,opacity,0);
        lv_obj_set_style_text_opa(caption,opacity,0);lv_obj_set_style_text_opa(percentLabel,opacity,0);lv_obj_set_style_text_opa(footer,opacity,0);
        if(renderedStage!=s.stage){lv_label_set_text(caption,captions[(int)s.stage]);renderedStage=s.stage;}
        if(renderedPercent!=s.percent){
            char label[12];snprintf(label,sizeof(label),s.percent==100?"%u%%":"~%u%%",s.percent);
            lv_label_set_text(percentLabel,label);renderedPercent=s.percent;
        }
        lv_label_set_text_static(footer,s.preview?"DEMO":"TELLY");
        phase=now/1000.0f;lv_obj_invalidate(panel);
    }
    lv_timer_handler();xSemaphoreGive(xLvglMutex);
}

#include "media_capture.h"
#include "camera_monitor.h"
#include "relay_client.h"
#include <ArduinoJson.h>
#include <atomic>
namespace {
SemaphoreHandle_t lock=nullptr;
TaskHandle_t worker=nullptr;
MediaCaptureStatus status;
std::atomic<bool> stop{false};
void run(void*){
    for(;;){
        ulTaskNotifyTake(pdTRUE,portMAX_DELAY);
        auto current=mediaCaptureStatus();String error,reply,filename;uint32_t frames=0;
        for(uint32_t index=0;index<(current.photo?1:50);++index){
            if(index && (stop.load() || millis()-current.startedMs>=10000))break;
            uint8_t *jpeg=nullptr;size_t bytes=0;uint32_t at=0;
            if(!captureCameraJpeg(&jpeg,&bytes,&at,2500)){error="Camera frame unavailable";break;}
            if(!bytes || bytes>65536){free(jpeg);error="JPEG exceeds 64 KB";break;}
            JsonDocument meta;meta["type"]="media";meta["session"]=current.session;meta["mode"]=current.photo?"photo":"video";
            meta["index"]=index;meta["at_ms"]=at-current.startedMs;
            bool ok=relaySendBlob(meta,jpeg,bytes,reply,error);free(jpeg);if(!ok)break;
            JsonDocument received;if(deserializeJson(received,reply)){error="Invalid media acknowledgment";break;}
            filename=received["filename"]|"";++frames;
            xSemaphoreTake(lock,portMAX_DELAY);status.frames=frames;xSemaphoreGive(lock);
            uint32_t due=current.startedMs+(index+1)*200;
            while(!stop.load() && (int32_t)(millis()-due)<0)vTaskDelay(pdMS_TO_TICKS(5));
        }
        if(!error.length() && !current.photo && frames){
            JsonDocument meta;meta["type"]="media.finish";meta["session"]=current.session;
            if(relaySendBlob(meta,nullptr,0,reply,error)){
                JsonDocument received;if(!deserializeJson(received,reply))filename=received["filename"]|"";
            }
        }
        xSemaphoreTake(lock,portMAX_DELAY);status.error=error;status.filename=filename;
        status.state=error.length()?"error":"done";xSemaphoreGive(lock);
    }
}
}
void initMediaCapture(){lock=xSemaphoreCreateMutex();if(lock)xTaskCreate(run,"telly-media",12288,nullptr,1,&worker);}
MediaCaptureStatus mediaCaptureStatus(){if(!lock)return MediaCaptureStatus();xSemaphoreTake(lock,portMAX_DELAY);auto copy=status;xSemaphoreGive(lock);return copy;}
bool mediaCaptureBusy(){return mediaCaptureStatus().state=="recording";}
bool beginMediaCapture(bool photo){
    if(!worker || mediaCaptureBusy() || !relayConnected())return false;
    char id[40];snprintf(id,sizeof(id),"%08lx-%08lx",(unsigned long)millis(),(unsigned long)esp_random());
    xSemaphoreTake(lock,portMAX_DELAY);status=MediaCaptureStatus();status.session=id;status.photo=photo;status.startedMs=millis();status.state="recording";xSemaphoreGive(lock);
    stop.store(false);xTaskNotifyGive(worker);return true;
}
void finishMediaCapture(){stop.store(true);}

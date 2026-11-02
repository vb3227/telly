#include "relay_client.h"
#include "relay_root_ca.h"
#include "board_config.h"
#include "voice_activity.h"
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <time.h>
#include <sys/time.h>
#include <atomic>
#include <map>
#include <WebSocketsClient.h>
#include <esp_heap_caps.h>

namespace {
Preferences settings;
SemaphoreHandle_t urlLock = nullptr;
String savedUrl;
String serialLine;
constexpr size_t MAX_URL = 128;
std::atomic<int> relayPhase{0};
std::atomic<int> lastPollStatus{0};

bool allowedPath(const String &requestPath) {
    if (requestPath.length() > 256) return false;
    for (char c : requestPath) if (static_cast<unsigned char>(c) < 32) return false;
    String path = requestPath;
    int query = path.indexOf('?');
    if (query >= 0) path = path.substring(0, query);
    return path=="/ping" || path=="/status" || path=="/run" || path=="/stop" || path=="/reset" ||
        path=="/voice/start" || path=="/voice/finish" || path=="/voice/latest" || path=="/voice/result" || path=="/voice/activity" || path=="/voice/activity/preview" ||
        path=="/skills/delete" || path=="/skills/list" || path=="/skills/install" || path=="/skills/run" ||
        path=="/media/start" || path=="/media/stop" || path=="/media/status";
}

bool validUrl(const String &url) {
    if (!url.startsWith("https://") || !url.endsWith(".trycloudflare.com") ||
        url.length() < 27 || url.length() > MAX_URL) return false;
    String host = url.substring(8, url.length() - 18);
    if (!host.length() || host[0] == '-' || host[host.length() - 1] == '-') return false;
    for (size_t i = 0; i < host.length(); ++i) {
        char c = host[i];
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-')) return false;
    }
    return true;
}

bool setUrl(const String &url) {
    if (xSemaphoreTake(urlLock, portMAX_DELAY) == pdTRUE) {
        savedUrl = url;
        xSemaphoreGive(urlLock);
    }
    return settings.putString("url", url)==url.length();
}


struct Outbound {
    String meta, result, error;
    uint8_t *data=nullptr; size_t size=0;
    uint32_t deadline=0;
    SemaphoreHandle_t ready=xSemaphoreCreateBinary();
    std::atomic<int> refs{2};
    ~Outbound(){free(data);if(ready)vSemaphoreDelete(ready);}
};
QueueHandle_t outgoing=nullptr;
std::map<String,Outbound*> pending;
WebSocketsClient socketClient;
std::atomic<bool> connected{false};
SemaphoreHandle_t voiceTimingLock=nullptr;
std::atomic<uint32_t> voiceStoppedAt{0};
String activeCapture;
void release(Outbound *job){if(job->refs.fetch_sub(1)==1)delete job;}
void complete(Outbound *job,const String &result,const String &error){
    job->result=result;job->error=error;xSemaphoreGive(job->ready);release(job);
}
void onSocket(WStype_t type,uint8_t *payload,size_t length){
    if(type==WStype_CONNECTED){connected.store(true);lastPollStatus.store(101);Serial.println("[link] WebSocket connected");return;}
    if(type==WStype_DISCONNECTED){
        connected.store(false);lastPollStatus.store(0);
        Serial.printf("[link] disconnected: %.*s\n",(int)min(length,(size_t)120),payload?(const char*)payload:"");
        for(auto &entry:pending)complete(entry.second,"","Connection lost; command not replayed");
        pending.clear();return;
    }
    if(type!=WStype_TEXT || length>16000)return;
    JsonDocument packet;if(deserializeJson(packet,payload,length))return;
    String kind=packet["type"]|"";
    if(kind=="reply"){
        String id=packet["id"]|"";auto found=pending.find(id);if(found==pending.end())return;
        String result;serializeJson(packet,result);
        complete(found->second,result,packet["ok"]|false?String():String(packet["error"]|"Upload failed"));
        pending.erase(found);return;
    }
    if(kind!="command")return;
    if(packet["activity"].is<JsonArrayConst>())for(JsonObjectConst e:packet["activity"].as<JsonArrayConst>()){
        if(e["activity_id"].is<const char*>() && e["voice_epoch"].is<uint32_t>() && e["stage"].is<const char*>())
            receiveVoiceActivity(e["activity_id"],e["voice_epoch"],e["stage"]);
    }
    String id=packet["id"]|"",path=packet["path"]|"",method=packet["method"]|"",body=packet["body"]|"";
    if(id.length()!=32 || !allowedPath(path) || (method!="GET" && method!="POST") || body.length()>6000)return;
    HTTPClient local;String result;int code=-1;
    if(local.begin(String("http://")+WiFi.localIP().toString()+":"+MAIN_PORT+path)){
        local.setTimeout(5000);local.addHeader("X-Board-Token",BOARD_TOKEN);
        if(packet["voice_epoch"].is<uint32_t>())local.addHeader("X-Voice-Epoch",String(packet["voice_epoch"].as<uint32_t>()));
        local.addHeader("Content-Type","application/json");
        code=method=="POST"?local.POST((uint8_t*)body.c_str(),body.length()):local.GET();
        if(code>0)result=local.getString();local.end();
    }
    JsonDocument reply;reply["type"]="result";reply["id"]=id;reply["body"]=result;
    if(code<200 || code>=300)reply["error"]=String("Board HTTP ")+code+": "+result;
    String raw;serializeJson(reply,raw);socketClient.sendTXT(raw);
    xSemaphoreTake(voiceTimingLock,portMAX_DELAY);
    if(code>=200 && code<300 && packet["activity_id"].is<const char*>() && activeCapture==String(packet["activity_id"].as<const char*>()) &&
       (path=="/voice/result" || path=="/skills/run" || path=="/skills/install" || path=="/run")){
        Serial.printf("[voice timing] stop_to_action_ms=%lu\n",(unsigned long)(millis()-voiceStoppedAt.load()));activeCapture="";
    }
    xSemaphoreGive(voiceTimingLock);
}
void relayTask(void*){
    String currentBase,headers;
    socketClient.onEvent(onSocket);socketClient.setReconnectInterval(2000);socketClient.enableHeartbeat(15000,5000,2);
    for(;;){
        String base=relayGatewayUrl();
        if(!base.length())base=VOICE_GATEWAY_URL;
        if(WiFi.status()!=WL_CONNECTED || !base.length() || time(nullptr)<1704067200){vTaskDelay(pdMS_TO_TICKS(100));continue;}
        if(currentBase!=base){
            socketClient.disconnect();currentBase=base;
            headers=String("X-Voice-Gateway-Token: ")+VOICE_GATEWAY_TOKEN;
            socketClient.setExtraHeaders(headers.c_str());
            if(base.startsWith("https://"))socketClient.beginSslWithCA(base.substring(8).c_str(),443,"/board/ws",RELAY_ROOT_CA,"arduino");
            else if(base.startsWith("http://")){
                String authority=base.substring(7);int colon=authority.indexOf(':');
                uint16_t port=colon>=0?authority.substring(colon+1).toInt():8767;
                String host=colon>=0?authority.substring(0,colon):authority;
                socketClient.begin(host,port,"/board/ws","arduino");
            }
        }
        socketClient.loop();relayPhase.store(connected.load()?3:1);
        uint32_t now=millis();
        for(auto it=pending.begin();it!=pending.end();){
            if((int32_t)(now-it->second->deadline)>=0){complete(it->second,"","Response timeout; command not replayed");it=pending.erase(it);}else ++it;
        }
        Outbound *job=nullptr;
        if(outgoing && xQueueReceive(outgoing,&job,0)==pdTRUE){
            if(!connected.load() || (int32_t)(millis()-job->deadline)>=0){complete(job,"","WebSocket offline or upload expired");}
            else {
                JsonDocument meta;deserializeJson(meta,job->meta);String id=meta["id"]|"";
                bool sent=socketClient.sendTXT(job->meta);
                if(sent && job->size)sent=socketClient.sendBIN(job->data,job->size);
                free(job->data);job->data=nullptr;
                if(sent)pending[id]=job;else complete(job,"","WebSocket send failed; no replay");
            }
        }
        vTaskDelay(pdMS_TO_TICKS(2));
    }
}
}

bool relayConnected(){return connected.load();}
void markVoiceCaptureStopped(const String &capture){if(!voiceTimingLock)return;xSemaphoreTake(voiceTimingLock,portMAX_DELAY);voiceStoppedAt.store(millis());activeCapture=capture;xSemaphoreGive(voiceTimingLock);}
bool relaySendBlob(JsonDocument &meta,const uint8_t *data,size_t size,String &result,String &error,bool takeOwnership){
    if(!outgoing || !connected.load()){if(takeOwnership)free((void*)data);error="Laptop link is offline";return false;}
    auto *job=new Outbound;
    if(takeOwnership)job->data=(uint8_t*)data;
    if(!job->ready){delete job;error="Upload semaphore unavailable";return false;}
    if(!meta["id"].is<const char*>()){
        char id[40];snprintf(id,sizeof(id),"%08lx-%08lx",(unsigned long)millis(),(unsigned long)esp_random());meta["id"]=id;
    }
    meta["bytes"]=size;serializeJson(meta,job->meta);job->deadline=millis()+15000;job->size=size;
    if(size && !takeOwnership){job->data=(uint8_t*)heap_caps_malloc(size,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
        if(!job->data){delete job;error="Upload buffer unavailable";return false;}memcpy(job->data,data,size);}
    if(xQueueSend(outgoing,&job,0)!=pdTRUE){delete job;error="Upload queue busy";return false;}
    bool ready=xSemaphoreTake(job->ready,pdMS_TO_TICKS(15000))==pdTRUE;
    if(ready){result=job->result;error=job->error;}else error="Upload timeout; no automatic retry";
    release(job);return ready && !error.length();
}
String relayGatewayUrl() {
    if (!urlLock) return "";
    String result;
    if (xSemaphoreTake(urlLock, portMAX_DELAY) == pdTRUE) {
        result = savedUrl;
        xSemaphoreGive(urlLock);
    }
    return result;
}

void startRelayClient() {
    urlLock = xSemaphoreCreateMutex();
    if (!urlLock) { Serial.println("Relay mutex allocation failed"); return; }
    if (settings.begin("tellyrelay", false)) {
        String stored = settings.getString("url", "");
        if (validUrl(stored)) savedUrl = stored;
    }
    Serial.println("Relay setup: TELLY_RELAY_URL https://<name>.trycloudflare.com");
    if (savedUrl.length()) Serial.println(String("Relay URL: ") + savedUrl);
    voiceTimingLock=xSemaphoreCreateMutex();
    outgoing=xQueueCreate(2,sizeof(Outbound*));
    if (!outgoing || !voiceTimingLock || xTaskCreate(relayTask, "telly-link", 16384, nullptr, 1, nullptr) != pdPASS)
        Serial.println("Relay task allocation failed");
}

void serviceRelaySerial() {
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\r') continue;
        if (c != '\n') {
            if (serialLine.length() < 160) serialLine += c;
            else serialLine = "";
            continue;
        }
        String line = serialLine;
        serialLine = "";
        line.trim();
        if (line == "TELLY_STATUS") {
            Serial.printf("Relay status: wifi=%d ip=%s rssi=%d clock=%lu heap=%u\n",
                (int)WiFi.status(), WiFi.localIP().toString().c_str(), (int)WiFi.RSSI(),
                (unsigned long)time(nullptr), (unsigned)ESP.getFreeHeap());
            Serial.printf("Relay worker: phase=%d last_http=%d url_bytes=%u token_set=%d\n",
                relayPhase.load(), lastPollStatus.load(), (unsigned)relayGatewayUrl().length(), !!VOICE_GATEWAY_TOKEN[0]);
        } else if (line == "TELLY_NETTEST") {
            String base = relayGatewayUrl();
            IPAddress address;
            bool found = base.length() && WiFi.hostByName(base.substring(8).c_str(), address);
            Serial.printf("Relay DNS: ok=%d ip=%s\n", found, address.toString().c_str());
        } else if (line.startsWith("TELLY_TIME ")) {
            String value = line.substring(11);
            char *end = nullptr;
            unsigned long epoch = strtoul(value.c_str(), &end, 10);
            if (end && *end == '\0' && epoch >= 1704067200UL && epoch < 2145916800UL) {
                struct timeval tv = {(time_t)epoch, 0};
                settimeofday(&tv, nullptr);
                Serial.println("Relay clock synchronized");
            } else Serial.println("Invalid Unix timestamp");
        } else if (line == "TELLY_RELAY_CLEAR") {
            setUrl("");
            Serial.println("Relay URL cleared");
        } else if (line.startsWith("TELLY_RELAY_URL ")) {
            String url = line.substring(16);
            url.trim();
            if (!validUrl(url)) Serial.println("Invalid relay URL; expected https://<name>.trycloudflare.com");
            else { bool saved=setUrl(url); Serial.println(String(saved?"Relay URL saved: ":"Relay URL active but NOT persisted: ") + url); }
        }
    }
}

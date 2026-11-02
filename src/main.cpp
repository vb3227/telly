#include <Arduino.h>
#include "boot_animation.h"
#include <WiFi.h>
#include <WebServer.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <asr.h>
#include <unihiker_k10.h>

#include "board_config.h"
#include "voice_transcription.h"
#include "relay_client.h"
#include "audio_capture.h"
#include "secure_requests.h"
#include <atomic>
#include <esp_heap_caps.h>
#include "camera_monitor.h"
#include "media_capture.h"
#include "button_controls.h"
#include "voice_gate.h"
#include "screen_text.h"
#include "voice_activity.h"
#include "program_validation.h"
#include <Preferences.h>
#include <time.h>

// MCP-facing server endpoints keep the same ports, token header, and paths as
// the previous MicroPython supervisor. The /run body is now a JSON program.
UNIHIKER_K10 k10;
// Use the base driver: the vendor AHT20 wrapper starts a task in its global constructor.
DFRobot_AHT20 aht20;
ASR asr;
WebServer mainServer(MAIN_PORT);
WebServer rescueServer(RESCUE_PORT);

static const char *TOKEN_HEADER[] = {"X-Board-Token", "X-Voice-Epoch"};
static const size_t MAX_PROGRAM_BYTES = 4096;
static const size_t MAX_STEPS = 32;
static const size_t MAX_JOBS = 4;

struct BoardJob {
    bool allocated = false;
    bool buttonVoice = false;
    bool cloudVoice = false;
    uint32_t cloudEpoch = 0;
    String id;
    String status = "idle";
    String error;
    JsonDocument program;
    JsonDocument result;
    size_t stepIndex = 0;
    bool stopRequested = false;
    bool stepStarted = false;
    uint32_t stepStartedAt = 0;
    bool blinkOn = false;
    uint16_t blinkRemaining = 0;
    uint8_t blinkR = 40, blinkG = 40, blinkB = 40;
    uint16_t blinkPeriodMs = 250;
    uint32_t nextBlinkAt = 0;
};

BoardJob jobs[MAX_JOBS];
BoardJob *currentJob = nullptr;
String lastJobId;
String cameraJobId;
String mediaOwnedCameraJob;
String mediaResultShown;
String rgbJobId;
String lastVoiceTranscript;
String lastVoiceError;
String lastVoiceAgentResult;
uint32_t lastVoiceUpdatedAt = 0;
Preferences skillStore;
bool skillStoreReady = false;
static const size_t MAX_VOICE_SKILLS = 8;
char skillCommandBuffers[MAX_VOICE_SKILLS][64];
String skillNames[MAX_VOICE_SKILLS];
String skillTriggers[MAX_VOICE_SKILLS];
size_t registeredSkillCount = 0;

bool cameraInitialized = false;
bool cameraActive = false;
uint32_t screenBackground = 0x000000;

bool ahtInitialized = false;
bool ahtAvailable = false;

uint32_t jobCounter = 0;
uint32_t scheduledResetAt = 0;
uint32_t lastWiFiAttempt = 0;
uint32_t lastAsrId = 0;
uint32_t lastAsrLogAt = 0;
uint32_t lastLoggedAsrId = 0;
uint8_t pendingAsrLedCommand = 0;
bool asrInitRequested = false;
bool asrReady = false;
uint32_t asrInitAt = 0;
bool wifiWasConnected = false;

static char asrCommandLightsOn[] = "lights on";
static char asrCommandLightsOff[] = "lights off";
static char asrCommandHelloRobot[] = "hello robot";
static char asrCommandCameraOn[] = "camera on";
static char asrCommandCameraOff[] = "camera off";
static char asrCommandWhisper[] = "whisper";

struct VoiceMessage { String text; String error; bool forwarded = false; bool suppressed = false; };
TaskHandle_t voiceTaskHandle = nullptr;
QueueHandle_t voiceResults = nullptr;
std::atomic<bool> voiceCancelled{false};
std::atomic<bool> voiceFinishRequested{false};
bool voiceBusy = false;
String voiceJobId;
uint32_t voiceJobEpoch = 0;
bool lastVoiceSuppressed = false;
uint32_t voiceButtonPresses = 0;
uint32_t voiceSendButtonPresses = 0;


void voiceWorker(void *) {
    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        VoiceMessage *message = new VoiceMessage;
        const uint32_t epoch = voiceJobEpoch;
        Serial.println("[voice] recording...");
        const bool transcribed = transcribeVoiceClip(k10, message->text, message->error,
                                                     &voiceCancelled, false, &voiceFinishRequested, epoch);
        if (message->text.length()) {
            // JSON escaping keeps each transcript on one UTF-8 console line.
            JsonDocument log; log["text"] = message->text;
            Serial.print("[voice] transcript: ");
            serializeJson(log, Serial); Serial.println();
        }
        const bool emptyClip = !transcribed && message->error == "No speech detected in microphone PCM";
        message->suppressed = emptyClip || voiceCancelled.load() || !cloudVoiceEpochValid(epoch) || containsLocalWake(message->text);
        message->forwarded = transcribed && !message->suppressed;
        xQueueSend(voiceResults, &message, portMAX_DELAY);
    }
}

void startVoiceWorker() {
    voiceResults = xQueueCreate(1, sizeof(VoiceMessage *));
    if (!voiceResults || xTaskCreate(voiceWorker, "telly-voice", 16384, nullptr, 1, &voiceTaskHandle) != pdPASS)
        Serial.println("Voice worker allocation failed");
}

void serviceVoiceResults() {
    VoiceMessage *message = nullptr;
    if (!voiceResults || xQueueReceive(voiceResults, &message, 0) != pdTRUE) return;
    lastVoiceSuppressed = message->suppressed;
    lastVoiceUpdatedAt = millis();
    lastVoiceTranscript = message->text;
    lastVoiceError = message->suppressed ? String("Cancelled: local ASR priority or empty recording") : message->error;
    if(message->suppressed)setVoiceActivity(VoiceActivity::Cancelled);
    else if(message->error.length())setVoiceActivity(VoiceActivity::Error);
    voiceBusy = false;
    if (message->suppressed)
        Serial.println("[voice] ignored: local ASR priority, background speech, or empty clip");
    else if (message->error.length())
        Serial.printf("[voice] error: %s\n", message->error.c_str());
    else if (message->forwarded)
        Serial.println("[voice] command sent to gateway");
    else
        Serial.println("[voice] command was not sent");
    delete message;
    Serial.printf("[memory] voice complete: heap=%u largest=%u psram=%u largest_psram=%u\n",
        (unsigned)ESP.getFreeHeap(),
        (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT),
        (unsigned)ESP.getFreePsram(),
        (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
}


uint32_t hexColor(JsonVariantConst value, uint32_t fallback) {
    if (value.is<uint32_t>()) return value.as<uint32_t>();
    if (value.is<const char *>()) {
        const char *s = value.as<const char *>();
        if (s && s[0] == '#') ++s;
        char *end = nullptr;
        unsigned long parsed = strtoul(s, &end, 16);
        if (end && *end == '\0') return (uint32_t)parsed;
    }
    return fallback;
}

bool readLightSafely(float &lux) {
    uint8_t bytes[4] = {};
    if (k10.readData(LTR303ALS_ADDR, LTR303_DATA_CH1_0, bytes, sizeof(bytes)) != sizeof(bytes))
        return false;
    const uint32_t ch1 = ((uint16_t)bytes[1] << 8) | bytes[0];
    const uint32_t ch0 = ((uint16_t)bytes[3] << 8) | bytes[2];
    const uint32_t total = ch0 + ch1;
    if (!total) { lux = 0; return true; }
    const float ratio = (float)ch1 / total;
    lux = ratio < 0.45f ? 1.7743f * ch0 + 1.1059f * ch1 :
          ratio < 0.64f ? 4.2785f * ch0 - 1.9548f * ch1 :
          ratio < 0.85f ? 0.5926f * ch0 + 0.1185f * ch1 : 0;
    lux = max(0.0f, lux);
    return true;
}

void clearRgb() {
    if (k10.rgb) k10.rgb->write(-1, 0x000000);
    rgbJobId = "";
}

void stopCamera() {
    if (cameraActive) {
        k10.setBgCamerImage(false);
        cameraActive = false;
        cameraJobId = "";
    }
}

void finishJob(const char *finalStatus, const String &error = String()) {
    if (!currentJob) return;
    if (voiceBusy && voiceJobId == currentJob->id) voiceCancelled.store(true);
    if (cameraActive && cameraJobId == currentJob->id) stopCamera();
    if (rgbJobId == currentJob->id) clearRgb();
    currentJob->status = finalStatus;
    currentJob->error = error;
    currentJob->stopRequested = false;
    currentJob->stepStarted = false;
    currentJob->blinkOn = false;
    currentJob->blinkRemaining = 0;
    Serial.printf("Job %s: %s%s\n", currentJob->id.c_str(), currentJob->status.c_str(),
                  currentJob->error.length() ? (String(" - ") + currentJob->error).c_str() : "");
}

BoardJob *findJob(const String &id) {
    for (size_t i = 0; i < MAX_JOBS; ++i)
        if (jobs[i].allocated && jobs[i].id == id) return &jobs[i];
    return nullptr;
}

BoardJob *allocateJob() {
    for (size_t i = 0; i < MAX_JOBS; ++i)
        if (!jobs[i].allocated) return &jobs[i];
    for (size_t i = 0; i < MAX_JOBS; ++i) {
        if (jobs[i].status == "done" || jobs[i].status == "stopped" || jobs[i].status == "error")
            return &jobs[i];
    }
    return nullptr;
}

void sendJson(WebServer &server, int code, JsonDocument &doc) {
    String body;
    serializeJson(doc, body);
    server.send(code, "application/json; charset=utf-8", body);
}

void sendError(WebServer &server, int code, const String &message) {
    JsonDocument response;
    response["error"] = message;
    response["job_id"] = lastJobId;
    sendJson(server, code, response);
}

bool authorized(WebServer &server) {
    if (!server.hasHeader("X-Board-Token") || server.header("X-Board-Token") != BOARD_TOKEN) return false;
    if (!server.hasHeader("X-Voice-Epoch")) return true;
    String value = server.header("X-Voice-Epoch");
    if (!value.length() || value.length() > 10) return false;
    uint64_t parsed = 0;
    for (size_t i = 0; i < value.length(); ++i) {
        if (value[i] < '0' || value[i] > '9') return false;
        parsed = parsed * 10 + value[i] - '0';
        if (parsed > UINT32_MAX) return false;
    }
    return cloudVoiceEpochValid((uint32_t)parsed);
}

void sendJobStatus(WebServer &server) {
    String requestedId = server.arg("job_id");
    if (!requestedId.length()) requestedId = server.arg("id");
    BoardJob *job = requestedId.length() ? findJob(requestedId) : findJob(lastJobId);
    if (!job) {
        JsonDocument response;
        response["error"] = "Unknown job_id";
        response["job_id"] = requestedId;
        sendJson(server, 404, response);
        return;
    }

    JsonDocument response;
    response["id"] = job->id;
    response["job_id"] = job->id;
    response["status"] = job->status;
    response["error"] = job->error;
    response["stop_requested"] = job->stopRequested;
    response["step"] = job->stepIndex;
    response["steps"] = job->program["steps"].is<JsonArrayConst>()
                             ? job->program["steps"].as<JsonArrayConst>().size()
                             : 0;
    response["wifi_connected"] = WiFi.status() == WL_CONNECTED;
    response["ip"] = WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : "";
    response["asr_ready"] = asrReady;
    response["voice_transcript"] = lastVoiceTranscript;
    response["voice_error"] = lastVoiceError;
    response["voice_agent_result"] = lastVoiceAgentResult;
    response["skills_storage_ready"] = skillStoreReady;
    response["camera_active"] = cameraActive;
    CameraObservation observedCamera = cameraObservation();
    response["camera_frames"] = observedCamera.frames;
    response["camera_frame_age_ms"] = observedCamera.frames ? millis() - observedCamera.updatedMs : 0;
    response["camera_job_id"] = cameraJobId;
    response["rgb_job_id"] = rgbJobId;
    response["voice_busy"] = voiceBusy;
    response["continuous_voice"] = false;
    response["voice_input_mode"] = "button";
    response["voice_button"] = "A";
    response["voice_button_presses"] = voiceButtonPresses;
    response["voice_send_button"] = "A";
    response["voice_send_button_presses"] = voiceSendButtonPresses;
    response["voice_recording"] = audioCaptureActive();
    response["voice_finish_requested"] = voiceFinishRequested.load();
    response["voice_clip_max_ms"] = 10000;
    response["voice_silence_ms"] = 0;
    response["button_control_scheme"]="A voice toggle; B unassigned";
    response["local_voice_priority"] = localVoiceActive();
    response["voice_buffer_ready"]=voiceCaptureBufferReady();
    response["psram_free_bytes"]=(uint32_t)ESP.getFreePsram();
    response["voice_epoch"] = currentVoiceEpoch();
    response["voice_suppressed"] = lastVoiceSuppressed;
    response["voice_job_id"] = voiceJobId;
    response["microphone_rms"] = microphoneRms();
    response["microphone_frames"] = microphoneFramesObserved();
    response["heap_free"] = ESP.getFreeHeap();
    response["activity_stage"] = voiceActivityName();
    response["activity_percent"] = voiceActivityPercent();
    response["activity_id"] = voiceActivityCaptureId();
    response["heap_min_free"] = ESP.getMinFreeHeap();
    response["heap_largest_free"] = (uint32_t)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    response["psram_largest_free"] = (uint32_t)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    response["psram_free"] = ESP.getFreePsram();
    JsonArray active = response["active_jobs"].to<JsonArray>();
    for (const BoardJob &candidate : jobs) {
        if (!candidate.allocated || candidate.status != "running") continue;
        JsonObject item = active.add<JsonObject>();
        item["id"] = candidate.id;
        item["step"] = candidate.stepIndex;
        item["op"] = candidate.program["steps"][candidate.stepIndex]["op"] | "";
    }

    if (lastAsrId) {
        response["last_asr_id"] = lastAsrId;
        response["last_asr_phrase"] = lastAsrId == 1 ? "lights on" :
                                       lastAsrId == 2 ? "lights off" :
                                       lastAsrId == 3 ? "hello robot" :
                                       lastAsrId == 4 ? "camera on" :
                                       lastAsrId == 5 ? "camera off" :
                                       lastAsrId == 6 ? "whisper (local only)" :
                                       (lastAsrId >= 10 && lastAsrId < 10 + registeredSkillCount)
                                           ? skillCommandBuffers[lastAsrId - 10] : "unknown";
    } else {
        response["last_asr_id"] = nullptr;
        response["last_asr_phrase"] = nullptr;
    }
    String resultJson;
    serializeJson(job->result, resultJson);
    response["result"] = resultJson;
    sendJson(server, 200, response);
}

uint32_t skillHash(const String &name) {
    uint32_t hash = 2166136261UL;
    for (size_t i = 0; i < name.length(); ++i) { hash ^= (uint8_t)name[i]; hash *= 16777619UL; }
    return hash;
}
String skillKey(const String &name, char prefix) {
    char key[12]; snprintf(key, sizeof(key), "%c%08lx", prefix, (unsigned long)skillHash(name)); return String(key);
}
bool validSkillName(const String &name) {
    if (!name.length() || name.length() > 31) return false;
    for (size_t i = 0; i < name.length(); ++i) {
        char c = name[i];
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_')) return false;
    }
    return true;
}
BoardJob *enqueueProgram(JsonDocument &incoming) {
    String validationError;
    if (!validateBoardProgram(incoming.as<JsonObjectConst>(), validationError)) return nullptr;
    JsonArrayConst steps = incoming["steps"].as<JsonArrayConst>();
    if (steps.isNull() || steps.size() == 0 || steps.size() > MAX_STEPS) return nullptr;
    BoardJob *job = allocateJob();
    if (!job) return nullptr;
    job->program.clear(); job->result.clear(); job->error = ""; job->stepIndex = 0;
    job->stopRequested = false; job->stepStarted = false; job->stepStartedAt = 0;
    job->blinkOn = false; job->blinkRemaining = 0; job->allocated = true; job->buttonVoice = false; job->cloudVoice = false; job->cloudEpoch = 0;
    job->program.set(incoming); job->id = String(millis()) + "-" + String(++jobCounter);
    job->status = "running"; lastJobId = job->id;
    Serial.printf("Accepted JSON job %s (%u steps)\n", job->id.c_str(), (unsigned)steps.size());
    return job;
}
void registerVoiceSkills() {
    if (!skillStoreReady) return;
    String index = skillStore.getString("index", "");
    size_t cursor = 0; registeredSkillCount = 0;
    while (cursor < index.length() && registeredSkillCount < MAX_VOICE_SKILLS) {
        int comma = index.indexOf(',', cursor);
        String name = index.substring(cursor, comma < 0 ? index.length() : comma);
        cursor = comma < 0 ? index.length() : comma + 1;
        if (!validSkillName(name)) continue;
        String phrase = skillStore.getString(skillKey(name, 't').c_str(), "");
        if (!phrase.length() || phrase.length() >= sizeof(skillCommandBuffers[0])) continue;
        skillNames[registeredSkillCount] = name;
        skillTriggers[registeredSkillCount] = phrase;
        strlcpy(skillCommandBuffers[registeredSkillCount], phrase.c_str(), sizeof(skillCommandBuffers[0]));
        ++registeredSkillCount;
    }
    if (asrReady) {
        for (size_t i = 0; i < registeredSkillCount; ++i)
            asr.addASRCommand(10 + i, skillCommandBuffers[i]);
    }
}
BoardJob *runStoredSkill(const String &name) {
    if (!skillStoreReady || !validSkillName(name)) return nullptr;
    String json = skillStore.getString(skillKey(name, 'p').c_str(), "");
    if (!json.length()) return nullptr;
    JsonDocument program;
    if (deserializeJson(program, json)) return nullptr;
    return enqueueProgram(program);
}
void startCameraSkill() {
    if (cameraActive) return;
    JsonDocument program;
    JsonArray steps = program["steps"].to<JsonArray>();
    JsonObject step = steps.add<JsonObject>();
    step["op"] = "camera.preview"; step["duration_ms"] = 0;
    if (!enqueueProgram(program)) Serial.println("Camera skill could not start: no free job slot");
}
void stopCameraSkill() {
    if (cameraJobId.length()) {
        BoardJob *owner = findJob(cameraJobId);
        if (owner && owner->status == "running") owner->stopRequested = true;
    }
    stopCamera();
}
void showVoiceFeedback(const String &text) {
    lastVoiceAgentResult = text;
    if (!cameraActive && k10.canvas) {
        k10.canvas->canvasClear();
        k10.canvas->canvasRectangle(0,0,240,320,0x000000,0x000000,true);
        drawScreenText(text);
        k10.canvas->updateCanvas();
    }
}

void handleSkillInstall(WebServer &server) {
    if (!authorized(server)) { server.send(403, "text/plain; charset=utf-8", "Invalid board token"); return; }
    JsonDocument request;
    if (deserializeJson(request, server.arg("plain"))) { sendError(server, 400, "Expected JSON skill"); return; }
    String name = request["name"] | "";
    String phrase = request["voice_phrase"] | "";
    JsonObjectConst program = request["program"].as<JsonObjectConst>();
    if (!validSkillName(name) || program.isNull()) { sendError(server, 400, "Skill needs a lowercase name and a program object"); return; }
    if (phrase.length() >= sizeof(skillCommandBuffers[0])) { sendError(server, 400, "Voice phrase is too long (max 63 bytes)"); return; }
    String validationError;
    if (!validateBoardProgram(program, validationError)) { sendError(server, 400, validationError); return; }
    String body; serializeJson(program, body);
    if (body.length() > 3800) { sendError(server, 413, "Stored skill program is limited to 3800 bytes"); return; }
    if (!skillStoreReady) { sendError(server, 503, "Persistent skill storage is unavailable"); return; }
    String index = skillStore.getString("index", "");
    size_t totalStoredBytes = body.length();
    size_t sizeCursor = 0;
    while (sizeCursor < index.length()) {
        int comma = index.indexOf(',', sizeCursor);
        String existing = index.substring(sizeCursor, comma < 0 ? index.length() : comma);
        sizeCursor = comma < 0 ? index.length() : comma + 1;
        if (existing != name) totalStoredBytes += skillStore.getString(skillKey(existing, 'p').c_str(), "").length();
    }
    if (totalStoredBytes > 12000) { sendError(server, 413, "Combined stored skill programs are limited to 12 KB"); return; }
    bool listed = false; size_t cursor = 0;
    while (cursor < index.length()) {
        int comma = index.indexOf(',', cursor);
        String item = index.substring(cursor, comma < 0 ? index.length() : comma);
        if (item == name) listed = true;
        cursor = comma < 0 ? index.length() : comma + 1;
    }
    if (!listed) { if (index.length()) index += ","; index += name; }
    if (!skillStore.putString(skillKey(name, 'p').c_str(), body) ||
        !skillStore.putString(skillKey(name, 't').c_str(), phrase) ||
        !skillStore.putString("index", index)) { sendError(server, 507, "Could not save the skill in NVS"); return; }
    registerVoiceSkills();
    JsonDocument response; response["name"] = name; response["stored"] = true;
    response["voice_phrase"] = phrase; response["voice_registered"] = phrase.length() > 0 && asrReady;
    if (request["run"] | false) {
        JsonDocument runnable; runnable.set(program);
        BoardJob *job = enqueueProgram(runnable);
        if (!job) { sendError(server, 503, "Skill saved but all task slots are busy"); return; }
        if (server.hasHeader("X-Voice-Epoch")) { job->cloudVoice = true; job->cloudEpoch = strtoul(server.header("X-Voice-Epoch").c_str(), nullptr, 10); }
        response["job_id"] = job->id; response["status"] = job->status;
        String feedback=request["feedback"] | "";
        if (!feedback.length()) feedback=String("Saved and started: ")+name;
        showVoiceFeedback(feedback);
    }
    sendJson(server, 200, response);
}
void handleSkillRun(WebServer &server) {
    if (!authorized(server)) { server.send(403, "text/plain; charset=utf-8", "Invalid board token"); return; }
    JsonDocument request;
    if (deserializeJson(request, server.arg("plain"))) { sendError(server, 400, "Expected JSON with skill name"); return; }
    String name = request["name"] | "";
    if (!validSkillName(name) || !skillStoreReady || !skillStore.getString(skillKey(name, 'p').c_str(), "").length()) {
        sendError(server, 404, "Skill not found in board storage"); return;
    }
    BoardJob *job = runStoredSkill(name);
    if (!job) { sendError(server, 503, "Skill invalid or all job slots are busy"); return; }
    if (server.hasHeader("X-Voice-Epoch")) { job->cloudVoice = true; job->cloudEpoch = strtoul(server.header("X-Voice-Epoch").c_str(), nullptr, 10); }
    JsonDocument response; response["skill"] = name; response["job_id"] = job->id; response["status"] = job->status;
    if (request["feedback"].is<const char *>()) showVoiceFeedback(request["feedback"].as<String>());
    sendJson(server, 200, response);
}
void handleSkillList(WebServer &server) {
    if (!authorized(server)) { server.send(403, "text/plain; charset=utf-8", "Invalid board token"); return; }
    JsonDocument response; JsonArray names = response["skills"].to<JsonArray>();
    String index = skillStoreReady ? skillStore.getString("index", "") : "";
    size_t cursor = 0;
    while (cursor < index.length()) {
        int comma = index.indexOf(',', cursor);
        String name = index.substring(cursor, comma < 0 ? index.length() : comma);
        cursor = comma < 0 ? index.length() : comma + 1;
        if (validSkillName(name)) names.add(name);
    }
    response["storage_ready"] = skillStoreReady; sendJson(server, 200, response);
}
void handleVoiceLatest(WebServer &server) {
    if (!authorized(server)) { server.send(403, "text/plain; charset=utf-8", "Invalid board token"); return; }
    JsonDocument response; response["text"] = lastVoiceTranscript; response["error"] = lastVoiceError;
    response["agent_result"] = lastVoiceAgentResult;
    response["updated_ms"] = lastVoiceUpdatedAt;
    sendJson(server, 200, response);
}
void handleRun(WebServer &server) {
    if (!authorized(server)) {
        server.send(403, "text/plain; charset=utf-8", "Invalid board token");
        return;
    }
    String body = server.arg("plain");
    if (body.length() == 0 || body.length() > MAX_PROGRAM_BYTES) {
        sendError(server, 400, "Program body is empty or too large");
        return;
    }

    JsonDocument incoming;
    DeserializationError parseError = deserializeJson(incoming, body);
    if (parseError) {
        sendError(server, 400, String("Expected a JSON program: ") + parseError.c_str());
        return;
    }
    JsonDocument runnable;
    String feedback;
    if(incoming["program"].is<JsonObject>()){
        if(incoming.size()>2 || (!incoming["feedback"].isNull() && !incoming["feedback"].is<const char*>())){sendError(server,400,"Invalid run wrapper");return;}
        runnable.set(incoming["program"]);feedback=incoming["feedback"]|"";
        if(feedback.length()>480){sendError(server,400,"Feedback too long");return;}
    }else runnable.set(incoming);
    String validationError;
    if(!validateBoardProgram(runnable.as<JsonObjectConst>(),validationError)){sendError(server,400,validationError);return;}
    BoardJob *job=enqueueProgram(runnable);
    if (!job) { sendError(server, 503, "All task slots are busy"); return; }
    if (server.hasHeader("X-Voice-Epoch")) { job->cloudVoice = true; job->cloudEpoch = strtoul(server.header("X-Voice-Epoch").c_str(), nullptr, 10); }
    JsonDocument response;
    response["job_id"] = job->id;
    response["status"] = "running";
    if(feedback.length())showVoiceFeedback(feedback);
    sendJson(server, 200, response);
}

void handleVoiceResult(WebServer &server) {
    if (!authorized(server)) { server.send(403, "text/plain; charset=utf-8", "Invalid board token"); return; }
    JsonDocument request;
    if (deserializeJson(request, server.arg("plain"))) { sendError(server, 400, "Expected JSON voice result"); return; }
    String text = request["text"] | "";
    if (!text.length()) { sendError(server, 400, "Voice result is empty"); return; }
    if (text.length() > 480) {
        size_t end=480;
        while(end && ((uint8_t)text[end]&0xC0)==0x80)--end;
        text=text.substring(0,end);
    }
    showVoiceFeedback(text);
    JsonDocument response; response["accepted"] = true; response["text"] = lastVoiceAgentResult;
    sendJson(server, 200, response);
}

bool beginButtonVoice() {
    const char *reason=nullptr;
    if(voiceBusy)reason="previous recording/transcription is still running";
    else if(!asrReady || !voiceTaskHandle)reason="voice worker is not ready";
    else if(WiFi.status()!=WL_CONNECTED)reason="Wi-Fi disconnected";
    else if(!relayConnected())reason="laptop link is offline";
    if(!reason) {
        releaseIdleLocalVoice();
        if(localVoiceActive())reason="Hi Telly wake window is still active";
    }
    if(reason){Serial.printf("[voice] cannot start: %s\n",reason);return false;}
    JsonDocument program;program["steps"][0]["op"]="voice.transcribe";
    BoardJob *job=enqueueProgram(program);if(!job){Serial.println("[voice] cannot start: all four task slots are occupied");return false;}
    voiceFinishRequested.store(false);job->buttonVoice=true;
    showVoiceFeedback("Listening... press A to finish (max 10s)");
    return true;
}
bool startMediaCommand(bool photo) {
    if(mediaCaptureBusy())return false;
    mediaOwnedCameraJob="";
    if(!cameraActive){
        JsonDocument p;p["steps"][0]["op"]="camera.preview";p["steps"][0]["duration_ms"]=0;
        BoardJob *job=enqueueProgram(p);if(!job)return false;mediaOwnedCameraJob=job->id;
    }
    if(!beginMediaCapture(photo)){
        BoardJob *job=findJob(mediaOwnedCameraJob);if(job)job->stopRequested=true;mediaOwnedCameraJob="";return false;
    }
    return true;
}
void writeMediaStatus(JsonDocument &response){
    auto status=mediaCaptureStatus();response["session"]=status.session;response["state"]=status.state;
    response["frames"]=status.frames;response["error"]=status.error;response["filename"]=status.filename;
    response["mode"]=status.photo?"photo":"video";response["max_duration_ms"]=10000;
}
void serviceMediaState(){
    auto status=mediaCaptureStatus();
    if(mediaOwnedCameraJob.length() && status.state!="recording"){
        BoardJob *job=findJob(mediaOwnedCameraJob);if(job && job->status=="running")job->stopRequested=true;
        if(cameraJobId==mediaOwnedCameraJob)stopCamera();mediaOwnedCameraJob="";
    }
    if((status.state=="done" || status.state=="error") && status.session!=mediaResultShown){
        mediaResultShown=status.session;
        String text=status.state=="done"?String("Saved: ")+status.filename:status.error;
        Serial.println(String("[media] ")+text);if(!voiceBusy)showVoiceFeedback(text);
    }
}

bool buttonVoiceQueued();
void configureServer(WebServer &server, bool rescueOnly) {
    server.collectHeaders(TOKEN_HEADER, 2);
    server.on("/ping", HTTP_GET, [&server]() {
        server.send(200, "text/plain; charset=utf-8", "pong");
    });
    server.on("/status", HTTP_GET, [&server, rescueOnly]() {
        if (rescueOnly) {
            server.send(404, "text/plain; charset=utf-8", "Not found");
            return;
        }
        if (!authorized(server)) {
            server.send(403, "text/plain; charset=utf-8", "Invalid board token");
            return;
        }
        sendJobStatus(server);
    });
    server.on("/skills/install", HTTP_POST, [&server, rescueOnly]() { if (rescueOnly) { server.send(404, "text/plain", "Not found"); return; } handleSkillInstall(server); });
    server.on("/skills/run", HTTP_POST, [&server, rescueOnly]() { if (rescueOnly) { server.send(404, "text/plain", "Not found"); return; } handleSkillRun(server); });
    server.on("/skills/list", HTTP_GET, [&server, rescueOnly]() { if (rescueOnly) { server.send(404, "text/plain", "Not found"); return; } handleSkillList(server); });
    server.on("/skills/delete",HTTP_POST,[&server,rescueOnly](){
        if(rescueOnly || !authorized(server)){sendError(server,403,"Unauthorized");return;}
        JsonDocument request;if(deserializeJson(request,server.arg("plain"))){sendError(server,400,"Invalid request");return;}
        String name=request["name"]|"";if(!validSkillName(name)){sendError(server,400,"Invalid skill name");return;}
        String index=skillStore.getString("index", ""),kept;size_t cursor=0;
        while(cursor<index.length()){
            int comma=index.indexOf(',',cursor);String item=index.substring(cursor,comma<0?index.length():comma);
            cursor=comma<0?index.length():comma+1;if(item==name || !item.length())continue;
            if(kept.length())kept+=',';kept+=item;
        }
        skillStore.remove(skillKey(name,'p').c_str());skillStore.remove(skillKey(name,'t').c_str());
        skillStore.putString("index",kept);
        server.send(200,"application/json","{\"removed\":true,\"native_phrase_refresh_requires_restart\":true}");
    });
    server.on("/voice/latest", HTTP_GET, [&server, rescueOnly]() { if (rescueOnly) { server.send(404, "text/plain", "Not found"); return; } handleVoiceLatest(server); });
    server.on("/voice/result", HTTP_POST, [&server, rescueOnly]() { if (rescueOnly) { server.send(404, "text/plain", "Not found"); return; } handleVoiceResult(server); });
    server.on("/voice/activity",HTTP_POST,[&server,rescueOnly](){
        if(rescueOnly || !authorized(server)){server.send(403,"text/plain","Unauthorized");return;}
        JsonDocument request;
        if(deserializeJson(request,server.arg("plain")) || !request["activity_id"].is<const char*>() || !request["voice_epoch"].is<uint32_t>() || !request["stage"].is<const char*>()){
            sendError(server,400,"Invalid activity update");return;
        }
        bool accepted=receiveVoiceActivity(request["activity_id"],request["voice_epoch"],request["stage"]);
        server.send(accepted?200:409,"application/json",accepted?"{\"accepted\":true}":"{\"error\":\"Stale activity update\"}");
    });
    server.on("/voice/activity/preview",HTTP_POST,[&server,rescueOnly](){
        if(rescueOnly || !authorized(server)){server.send(403,"text/plain","Unauthorized");return;}
        if(!previewVoiceActivity()){sendError(server,409,"A voice request is active");return;}
        server.send(200,"application/json","{\"accepted\":true,\"preview\":true}");
    });
    server.on("/voice/start",HTTP_POST,[&server,rescueOnly](){
        if(rescueOnly || !authorized(server)){sendError(server,403,"Unauthorized");return;}
        if(server.hasHeader("X-Voice-Epoch")){sendError(server,403,"Recursive voice capture denied");return;}
        if(!beginButtonVoice()){sendError(server,409,"Voice capture unavailable");return;}
        server.send(202,"application/json","{\"accepted\":true}");
    });
    server.on("/voice/finish",HTTP_POST,[&server](){
        if(!authorized(server)){sendError(server,403,"Unauthorized");return;}
        if(!voiceBusy && !buttonVoiceQueued()){sendError(server,409,"No active voice capture");return;}
        voiceFinishRequested.store(true);server.send(200,"application/json","{\"accepted\":true}");
    });
    server.on("/media/start",HTTP_POST,[&server,rescueOnly](){
        if(rescueOnly || !authorized(server)){sendError(server,403,"Unauthorized");return;}
        JsonDocument options;if(deserializeJson(options,server.arg("plain"))){sendError(server,400,"Invalid media request");return;}
        String mode=options["mode"]|"";if(mode!="photo" && mode!="video"){sendError(server,400,"mode must be photo or video");return;}
        if(!startMediaCommand(mode=="photo")){sendError(server,409,"Media busy or laptop offline");return;}
        JsonDocument result;writeMediaStatus(result);sendJson(server,200,result);
    });
    server.on("/media/stop",HTTP_POST,[&server](){
        if(!authorized(server)){sendError(server,403,"Unauthorized");return;}
        finishMediaCapture();JsonDocument result;writeMediaStatus(result);sendJson(server,200,result);
    });
    server.on("/media/status",HTTP_GET,[&server](){
        if(!authorized(server)){sendError(server,403,"Unauthorized");return;}
        JsonDocument result;writeMediaStatus(result);sendJson(server,200,result);
    });
    server.on("/run", HTTP_POST, [&server, rescueOnly]() {
        if (rescueOnly) {
            server.send(404, "text/plain; charset=utf-8", "Not found");
            return;
        }
        handleRun(server);
    });
    server.on("/stop", HTTP_POST, [&server]() {
        if (!authorized(server)) {
            server.send(403, "text/plain; charset=utf-8", "Invalid board token");
            return;
        }
        String body = server.arg("plain");
        JsonDocument request;
        deserializeJson(request, body);
        String requestedId = request["job_id"] | "";
        BoardJob *job = requestedId.length() ? findJob(requestedId) : findJob(lastJobId);
        if (!job || job->status != "running") {
            sendError(server, 404, "No running task with that job_id");
            return;
        }
        job->stopRequested = true;
        JsonDocument response;
        response["id"] = job->id;
        response["job_id"] = job->id;
        response["status"] = job->status;
        response["stop_requested"] = true;
        sendJson(server, 200, response);
    });
    server.on("/reset", HTTP_POST, [&server]() {
        if (!authorized(server)) {
            server.send(403, "text/plain; charset=utf-8", "Invalid board token");
            return;
        }
        server.send(200, "text/plain; charset=utf-8", "Restarting board");
        scheduledResetAt = millis() + 5000;
    });
    server.onNotFound([&server]() {
        server.send(404, "text/plain; charset=utf-8", "Not found");
    });
    server.begin();
}

void completeStep() {
    if (!currentJob) return;
    ++currentJob->stepIndex;
    currentJob->stepStarted = false;
}

void failJob(const String &message) {
    finishJob("error", message);
}

void startAsrIfNeeded() {
    if (!asrInitRequested) {
        static bool checked=false;
        static bool available=false;
        if(!checked){
            checked=true;
            srmodel_list_t *models=esp_srmodel_init("model");
            available=models && esp_srmodel_filter(models,"wn","hitelly") && esp_srmodel_filter(models,"mn","en");
            if(models)esp_srmodel_deinit(models);
            if(!available)Serial.println("[asr] English wake/command model missing. Provision unihiker_model_en once; network control remains available.");
        }
        if(!available)return;
        Serial.println("Starting K10 English ASR model...");
        asr.asrInit(CONTINUOUS, EN_MODE, 6000);
        asrInitRequested = true;
        asrInitAt = millis();
    }
}

void processVoiceCommand(uint32_t id) {
    if (id == 4) { startCameraSkill(); return; }
    if (id == 5) { stopCameraSkill(); return; }
    if (id >= 10 && id < 10 + registeredSkillCount) {
        BoardJob *job = runStoredSkill(skillNames[id - 10]);
        if (!job) Serial.println("Voice skill failed to start");
        return;
    }
    // Hi Telly owns the entire utterance. "whisper" must not start cloud capture.

}
void serviceAsr() {
    if (!asrInitRequested || asrReady) {
        if (asrReady) {
            serviceVoiceGate(asr.isWakeUp(), microphoneRms());
            if (localVoiceActive()) {
                voiceCancelled.store(true);
                // Release only the button recording slot for a local skill.
                BoardJob *recording = findJob(voiceJobId);
                if (recording && recording->buttonVoice && recording->status == "running") {
                    BoardJob *previous = currentJob; currentJob = recording;
                    finishJob("stopped"); currentJob = previous;
                }
            }
            uint32_t detectedId = 0;
            if (asr.isDetectCmdID(1)) detectedId = 1;
            else if (asr.isDetectCmdID(2)) detectedId = 2;
            else if (asr.isDetectCmdID(3)) detectedId = 3;
            else if (asr.isDetectCmdID(4)) detectedId = 4;
            else if (asr.isDetectCmdID(5)) detectedId = 5;
            else if (asr.isDetectCmdID(6)) detectedId = 6;
            else for (size_t i = 0; i < registeredSkillCount; ++i) { if (asr.isDetectCmdID(10 + i)) { detectedId = 10 + i; break; } }

            if (detectedId) {
                lastAsrId = detectedId;
                uint32_t now = millis();
                // Avoid flooding Serial if the ASR library holds one detection
                // active for multiple loop iterations, but allow repeated speech.
                if (detectedId != lastLoggedAsrId || now - lastAsrLogAt > 5000) {
                    const char *phrase = detectedId == 1 ? "lights on" :
                                         detectedId == 2 ? "lights off" :
                                         detectedId == 3 ? "hello robot" :
                                         detectedId == 4 ? "camera on" :
                                         detectedId == 5 ? "camera off" :
                                         detectedId == 6 ? "whisper" : skillCommandBuffers[detectedId-10];
                    Serial.printf("Recognized voice command: %s (ID %lu)\n",
                                  phrase, (unsigned long)detectedId);
                    lastLoggedAsrId = detectedId;
                    lastAsrLogAt = now;
                    localVoiceDetected();
                    voiceCancelled.store(true);
                    processVoiceCommand(detectedId);
                }
                if (detectedId == 1 || detectedId == 2) pendingAsrLedCommand = detectedId;
            }
        }
        // Voice LED commands wait while a scheduled RGB blink owns the LEDs.
        // This keeps ASR active without interrupting another job's blink.
        if (pendingAsrLedCommand && !rgbJobId.length() && k10.rgb) {
            if (pendingAsrLedCommand == 1) {
                k10.rgb->brightness(9);
                k10.rgb->write(-1, 0x00FF00);
            } else {
                k10.rgb->write(-1, 0x000000);
            }
            pendingAsrLedCommand = 0;
        }
        return;
    }
    if (asr._asrState != 0) {
        asr.addASRCommand(1, asrCommandLightsOn);
        asr.addASRCommand(2, asrCommandLightsOff);
        asr.addASRCommand(3, asrCommandHelloRobot);
        asr.addASRCommand(4, asrCommandCameraOn);
        asr.addASRCommand(5, asrCommandCameraOff);
        asr.addASRCommand(6, asrCommandWhisper);
        asrReady = true;
        registerVoiceSkills();
        Serial.println("ASR ready. Say Hi Telly for local skills; press button A for cloud voice.");
    } else if (millis() - asrInitAt > 30000) {
        asrInitRequested = false;
        Serial.println("ASR initialization timed out");
    }
}

bool buttonVoiceQueued() {
    for(const BoardJob &job:jobs)if(job.allocated && job.buttonVoice && job.status=="running" && !job.stepStarted)return true;
    return false;
}
void serviceCaptureButtons() {
    if(!k10.buttonA || !k10.buttonB)return;
    static ButtonControls controls;
    ButtonControls::Action action=controls.update(k10.buttonA->isPressed(),k10.buttonB->isPressed(),millis());
    if(action==ButtonControls::None)return;
    ++voiceButtonPresses;
    if(audioCaptureActive() || buttonVoiceQueued()){
        voiceFinishRequested.store(true);Serial.println("[voice] A: finish and submit");
    }else if(!beginButtonVoice())Serial.println("[voice] busy or unavailable");
}

void runStep(JsonObjectConst step) {
    if (!currentJob) return;
    const char *op = step["op"] | "";
    uint32_t now = millis();

    if (!strcmp(op, "delay")) {
        uint32_t duration = step["duration_ms"] | 1000;
        duration = min(duration, (uint32_t)3600000);
        if (!currentJob->stepStarted) {
            currentJob->stepStarted = true;
            currentJob->stepStartedAt = now;
        }
        if (now - currentJob->stepStartedAt >= duration) completeStep();
        return;
    }

    if (!strcmp(op, "rgb.set")) {
        if (rgbJobId.length() && rgbJobId != currentJob->id) return;
        uint8_t brightness = step["brightness"] | 5;
        if (k10.rgb) {
            k10.rgb->brightness(min(brightness, (uint8_t)9));
            k10.rgb->write(-1, (uint8_t)(step["r"] | 0),
                           (uint8_t)(step["g"] | 0), (uint8_t)(step["b"] | 0));
        }
        completeStep();
        return;
    }

    if (!strcmp(op, "rgb.off")) {
        if (rgbJobId.length() && rgbJobId != currentJob->id) return;
        clearRgb();
        completeStep();
        return;
    }

    if (!strcmp(op, "rgb.blink")) {
        if (rgbJobId.length() && rgbJobId != currentJob->id) return;
        if (!currentJob->stepStarted) {
            rgbJobId = currentJob->id;
            currentJob->blinkRemaining = constrain((int)(step["count"] | 3), 1, 60);
            currentJob->blinkPeriodMs = constrain((int)(step["period_ms"] | 250), 40, 2000);
            currentJob->blinkR = step["r"] | 40;
            currentJob->blinkG = step["g"] | 40;
            currentJob->blinkB = step["b"] | 40;
            currentJob->blinkOn = true;
            if (k10.rgb) {
                k10.rgb->brightness(min((uint8_t)(step["brightness"] | 5), (uint8_t)9));
                k10.rgb->write(-1, currentJob->blinkR, currentJob->blinkG, currentJob->blinkB);
            }
            currentJob->nextBlinkAt = now + currentJob->blinkPeriodMs;
            currentJob->stepStarted = true;
        } else if ((int32_t)(now - currentJob->nextBlinkAt) >= 0) {
            if (currentJob->blinkOn) {
                currentJob->blinkOn = false;
                if (k10.rgb) k10.rgb->write(-1, 0x000000);
                currentJob->nextBlinkAt = now + currentJob->blinkPeriodMs;
            } else if (--currentJob->blinkRemaining == 0) {
                rgbJobId = "";
                completeStep();
            } else {
                currentJob->blinkOn = true;
                if (k10.rgb) k10.rgb->write(-1, currentJob->blinkR, currentJob->blinkG, currentJob->blinkB);
                currentJob->nextBlinkAt = now + currentJob->blinkPeriodMs;
            }
        }
        return;
    }

    if (!strcmp(op, "display.clear")) {
        if (cameraActive && cameraJobId != currentJob->id) return;
        uint32_t color = hexColor(step["color"], 0x000000);
        screenBackground = color;
        k10.setScreenBackground(color);
        if (k10.canvas) {
            k10.canvas->canvasClear();
            k10.canvas->updateCanvas();
        }
        completeStep();
        return;
    }

    if (!strcmp(op, "display.text")) {
        if (cameraActive && cameraJobId != currentJob->id) return;
        const char *text = step["text"] | "";
        int16_t x = step["x"] | 8;
        int16_t y = step["y"] | 8;
        uint32_t color = hexColor(step["color"], 0xFFFFFF);
        if (!k10.canvas) {
            failJob("Screen canvas is not initialized");
            return;
        }
        int clearX=constrain((int)x,0,240),clearY=constrain((int)y,0,320);
        if(clearX<240 && clearY<320)k10.canvas->canvasRectangle(clearX,clearY,240-clearX,320-clearY,screenBackground,screenBackground,true);
        drawScreenText(String(text),x,y,color);
        k10.canvas->updateCanvas();
        completeStep();
        return;
    }

    if (!strcmp(op, "display.square") || !strcmp(op, "display.rect")) {
        if (cameraActive && cameraJobId != currentJob->id) return;
        int x = step["x"] | 80;
        int y = step["y"] | 100;
        int w = step["w"] | (int)(step["size"] | 80);
        int h = step["h"] | (int)(step["size"] | 80);
        uint32_t color = hexColor(step["color"], 0xFFFFFF);
        if (!k10.canvas) {
            failJob("Screen canvas is not initialized");
            return;
        }
        k10.canvas->canvasRectangle(x, y, w, h, color, color, true);
        k10.canvas->updateCanvas();
        completeStep();
        return;
    }

    if (!strcmp(op, "display.line")) {
        if (cameraActive && cameraJobId != currentJob->id) return;
        if (!k10.canvas) {
            failJob("Screen canvas is not initialized");
            return;
        }
        k10.canvas->canvasLine(step["x1"] | 0, step["y1"] | 0,
                               step["x2"] | 100, step["y2"] | 100,
                               hexColor(step["color"], 0xFFFFFF));
        k10.canvas->updateCanvas();
        completeStep();
        return;
    }

    if (!strcmp(op, "camera.preview")) {
        uint32_t duration = step["duration_ms"] | 0;
        if (cameraActive && cameraJobId != currentJob->id) return;
        if (!currentJob->stepStarted) {
            if (!cameraInitialized) {
                Serial.println("Initializing K10 camera...");
                k10.initBgCamerImage();
                cameraInitialized = true;
            }
            if(k10.canvas){k10.canvas->canvasClear();k10.canvas->updateCanvas();}
            k10.setBgCamerImage(true);
            cameraActive = true;
            cameraJobId = currentJob->id;
            currentJob->stepStartedAt = now;
            currentJob->stepStarted = true;
        }
        CameraObservation liveFrame=cameraObservation();
        if(now-currentJob->stepStartedAt>5000 && (!liveFrame.frames || millis()-liveFrame.updatedMs>5000)){
            failJob("Camera not delivering frames; check memory/camera hardware and reboot");return;
        }
        if (duration && now - currentJob->stepStartedAt >= min(duration, (uint32_t)120000)) {
            stopCamera();
            completeStep();
        }
        return;
    }

    if (!strcmp(op, "camera.stop")) {
        if (cameraJobId.length()) {
            BoardJob *owner = findJob(cameraJobId);
            if (owner && owner != currentJob && owner->status == "running") owner->stopRequested = true;
        }
        stopCamera();
        completeStep();
        return;
    }

    if (!strcmp(op, "sensor.read") || !strcmp(op, "sensor.read.display")) {
        const bool showSensors = !strcmp(op, "sensor.read.display");
        if (showSensors && cameraActive && cameraJobId != currentJob->id) return;
        if (showSensors && !k10.canvas) { failJob("Screen canvas is not initialized"); return; }
        currentJob->result.clear();
        JsonObject sensors = currentJob->result["sensors"].to<JsonObject>();
        float light = NAN;
        sensors["light_available"] = readLightSafely(light);
        sensors["light"] = light;
        sensors["accel_x"] = k10.getAccelerometerX();
        sensors["accel_y"] = k10.getAccelerometerY();
        sensors["accel_z"] = k10.getAccelerometerZ();
        if (!ahtInitialized) {
            ahtAvailable = aht20.begin() == 0;
            ahtInitialized = true;
        }
        const bool measured = ahtAvailable && aht20.startMeasurementReady(true);
        sensors["temperature_humidity_available"] = measured;
        sensors["temperature_c"] = measured ? aht20.getTemperature_C() : NAN;
        sensors["humidity_percent"] = measured ? aht20.getHumidity_RH() : NAN;
        if (showSensors) {
            String report = String("Light: ") + String((int)sensors["light"]) +
                "\nAccel: " + String((int)sensors["accel_x"]) + ", " + String((int)sensors["accel_y"]) + ", " + String((int)sensors["accel_z"]) +
                "\nTemp: " + String((float)(sensors["temperature_c"] | NAN), 1) + " C" +
                "\nHumidity: " + String((float)(sensors["humidity_percent"] | NAN), 1) + "%";
            k10.canvas->canvasClear();
            k10.canvas->canvasRectangle(0,0,240,320,0x000000,0x000000,true);
            drawScreenText(report);
            k10.canvas->updateCanvas();
        }
        completeStep();
        return;
    }

    if (!strcmp(op, "mic.sample")) {
        currentJob->result.clear();
        currentJob->result["microphone_rms"] = microphoneRms();
        currentJob->result["microphone_frames"] = microphoneFramesObserved();
        completeStep();
        return;
    }

    if (!strcmp(op,"camera.photo") || !strcmp(op,"camera.record") || !strcmp(op,"camera.finish")) {
        if(!strcmp(op,"camera.finish")){finishMediaCapture();completeStep();return;}
        if(!startMediaCommand(!strcmp(op,"camera.photo"))){failJob("Media capture unavailable");return;}
        completeStep();return;
    }
    if (!strcmp(op, "voice.transcribe")) {
        if (!currentJob->stepStarted) {
            if (!currentJob->buttonVoice) { failJob("Press button A to start cloud voice recording"); return; }
            if (voiceBusy) return;
            if (localVoiceActive()) { finishJob("stopped"); return; }
            if (!asrReady || !voiceTaskHandle) { failJob("Voice capture requires a ready ASR worker"); return; }
            voiceJobEpoch = currentVoiceEpoch();
            voiceBusy = true;
            voiceCancelled.store(false);
            voiceJobId = currentJob->id;
            beginVoiceActivity(voiceJobId.c_str(),voiceJobEpoch);
            lastVoiceTranscript = "";
            lastVoiceError = "";
            currentJob->stepStarted = true;
            xTaskNotifyGive(voiceTaskHandle);
        } else if (!voiceBusy && voiceJobId == currentJob->id) {
            currentJob->result["text"] = lastVoiceTranscript;
            currentJob->result["error"] = lastVoiceError;
            voiceJobId = "";
            if (lastVoiceError.length()) failJob(lastVoiceError);
            else completeStep();
        }
        return;
    }

    if (!strcmp(op, "camera.status")) {
        currentJob->result.clear();
        currentJob->result["active"] = cameraActive;
        if (!cameraInitialized) { failJob("Camera has not been initialized"); return; }
        CameraObservation frame = cameraObservation();
        if (!frame.frames) { failJob("Camera has not delivered a frame"); return; }
        currentJob->result["width"] = frame.width;
        currentJob->result["height"] = frame.height;
        currentJob->result["frame_bytes"] = frame.bytes;
        currentJob->result["format"] = frame.format;
        currentJob->result["frame_checksum"] = frame.checksum;
        currentJob->result["frames"] = frame.frames;
        currentJob->result["frame_age_ms"] = millis() - frame.updatedMs;
        completeStep();
        return;
    }

    if (!strcmp(op, "asr.start")) {
        startAsrIfNeeded();
        currentJob->result.clear();
        currentJob->result["asr"] = "initializing";
        completeStep();
        return;
    }

    if (!strcmp(op, "asr.last")) {
        currentJob->result.clear();
        currentJob->result["id"] = lastAsrId ? lastAsrId : 0;
        currentJob->result["phrase"] = lastAsrId == 1 ? "lights on" :
                              lastAsrId == 2 ? "lights off" :
                              lastAsrId == 3 ? "hello robot" : "";
        currentJob->result["ready"] = asrReady;
        completeStep();
        return;
    }

    failJob(String("Unsupported operation: ") + op);
}

void processJobs() {
    for (size_t i = 0; i < MAX_JOBS; ++i) {
        BoardJob &job = jobs[i];
        if (!job.allocated || job.status != "running") continue;
        currentJob = &job;
        if (job.stopRequested || (job.cloudVoice && !cloudVoiceEpochValid(job.cloudEpoch))) {
            finishJob("stopped");
            continue;
        }
        JsonArrayConst steps = job.program["steps"].as<JsonArrayConst>();
        if (job.stepIndex >= steps.size()) {
            finishJob("done");
            continue;
        }
        JsonObjectConst step = steps[job.stepIndex].as<JsonObjectConst>();
        if (step.isNull()) {
            failJob("Each step must be a JSON object");
            continue;
        }
        runStep(step);
    }
    currentJob = nullptr;
}

void maintainWiFi() {
    if (WiFi.status() == WL_CONNECTED) {
        if (!wifiWasConnected) {
            wifiWasConnected = true;
            configTime(0, 0, "time.cloudflare.com", "pool.ntp.org");
            Serial.print("Wi-Fi connected, board IP: ");
            Serial.println(WiFi.localIP());
            drawScreenText(String("IP: ")+WiFi.localIP().toString(),8,28,0x00FF00);
            k10.canvas->updateCanvas();
        }
        return;
    }
    wifiWasConnected = false;
    if (millis() - lastWiFiAttempt < 10000) return;
    lastWiFiAttempt = millis();
    Serial.println("Wi-Fi reconnecting...");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.println("\nUNIHIKER K10 C++ MCP supervisor starting");

    k10.begin();
    k10.initScreen(SCREEN_DIR);
    k10.creatCanvas();
    k10.setScreenBackground(0x000000);
    showBootProgress(5, "Display ready");
    initSecureRequests();
    showBootProgress(15, "Security ready");
    initVoiceGate();
    initAudioCapture();
    initVoiceTranscription();
    showBootProgress(30, "Audio ready");
    skillStoreReady = skillStore.begin("tellyskills", false);
    if (skillStoreReady) registerVoiceSkills();
    Serial.printf("Persistent skill storage: %s (%u saved voice skills)\n", skillStoreReady ? "ready" : "unavailable", (unsigned)registeredSkillCount);
    showBootProgress(45, "Storage ready");

    startVoiceWorker();
    initMediaCapture();
    showBootProgress(60, "Starting ASR");
    startAsrIfNeeded();
    Serial.println("[buttons] A toggles voice; B is unassigned; max 10s");
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    lastWiFiAttempt = millis();

    showBootProgress(80, "Starting network");
    startRelayClient();
    configureServer(mainServer, false);
    configureServer(rescueServer, true);
    Serial.printf("HTTP control listening on %u; rescue on %u\n", MAIN_PORT, RESCUE_PORT);
    showBootProgress(100, "Core ready");
    delay(400);
    k10.canvas->canvasClear();
    k10.canvas->canvasRectangle(0,0,240,320,0x000000,0x000000,true);
    drawScreenText("Ready / Wi-Fi...");
    k10.canvas->updateCanvas();
}

void loop() {
    mainServer.handleClient();
    rescueServer.handleClient();
    serviceRelaySerial();
    maintainWiFi();
    serviceAsr();
    serviceVoiceResults();
    serviceCaptureButtons();
    serviceMediaState();
    processJobs();
    serviceVoiceActivity();
    if (scheduledResetAt && (int32_t)(millis() - scheduledResetAt) >= 0) ESP.restart();
    delay(1);
}













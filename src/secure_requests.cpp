#include "secure_requests.h"
#include <Arduino.h>
#include <esp_heap_caps.h>
#include <mbedtls/platform.h>
namespace {
bool configured=false;
void *tlsCalloc(size_t count, size_t size) {
    // Match ESP-IDF's external SPIRAM allocation option without replacing its SDK.
    void *memory = heap_caps_calloc(count, size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!memory) memory = heap_caps_calloc(count, size, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    return memory;
}
void tlsFree(void *memory) { heap_caps_free(memory); }
}
void initSecureRequests() {
    if (configured) return;
    configured=true;
    int installed = mbedtls_platform_set_calloc_free(tlsCalloc, tlsFree);
    configASSERT(installed == 0);
}

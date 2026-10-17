#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "esp_http_client.h"

#include "secrets.h"   // WIFI_SSID, WIFI_PASSWORD, BACKEND_URL

static const char *TAG = "LID_SENSORS";
static const char *NET_TAG = "NETWORK";

#define DEVICE_ID "medstation01"

#define NUM_SENSORS 2
static const gpio_num_t SENSOR_PINS[NUM_SENSORS] = {GPIO_NUM_4, GPIO_NUM_5};
static const char *COMPARTMENT_NAMES[NUM_SENSORS] = {"Compartment A", "Compartment B"};
static const int COMPARTMENT_NUMBERS[NUM_SENSORS] = {1, 2};

#define DEBOUNCE_DELAY_MS 50
#define POLL_INTERVAL_MS 10
#define LEVELS_WHEN_CLOSED 0

// Re-arm guard: a new OPEN only sends an event if the lid had stayed
// closed for at least this long first. Measured on B: flicker "closed"
#define REARM_CLOSED_MS 1000

#define WIFI_WAIT_AT_BOOT_MS 15000
#define HTTP_TIMEOUT_MS 5000
#define EVENT_QUEUE_LENGTH 10
#define WIFI_CONNECTED_BIT (1 << 0)

static EventGroupHandle_t wifi_events;
static QueueHandle_t event_queue;

static int last_reading[NUM_SENSORS];
static int stable_state[NUM_SENSORS];
static int64_t last_change_time_ms[NUM_SENSORS];
static int64_t closed_since_ms[NUM_SENSORS];   // when the lid last became CLOSED
static int ignored_count[NUM_SENSORS];         // OPENs ignored by the re-arm guard

static int64_t now_ms(void) {
    return esp_timer_get_time() / 1000;
}

static void print_state(int i) {
    ESP_LOGI(TAG, "%s: %s", COMPARTMENT_NAMES[i],
             stable_state[i] == LEVELS_WHEN_CLOSED ? "CLOSED" : "OPEN");
}

/* Wi-Fi */

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data) {
    (void)arg;
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *info = (wifi_event_sta_disconnected_t *)event_data;
        xEventGroupClearBits(wifi_events, WIFI_CONNECTED_BIT);
        ESP_LOGW(NET_TAG, "Wi-Fi disconnected (reason %d), retrying...", (int)info->reason);
        esp_wifi_connect();
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *info = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(NET_TAG, "Connected. ESP32 IP address: " IPSTR, IP2STR(&info->ip_info.ip));
        xEventGroupSetBits(wifi_events, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init_sta(void) {
    wifi_events = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_cfg));

    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                               &wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               &wifi_event_handler, NULL));

    wifi_config_t wifi_cfg = {
        .sta = {
            .ssid = WIFI_SSID,
            .password = WIFI_PASSWORD,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    // Turn off Wi-Fi power saving so POSTs go out without extra delay.
    esp_wifi_set_ps(WIFI_PS_NONE);

    // Network name only. The password is never printed.
    ESP_LOGI(NET_TAG, "Connecting to Wi-Fi network \"%s\"...", WIFI_SSID);
}

/* HTTP POST */

static void post_event(int compartment) {
    char body[128];
    snprintf(body, sizeof(body),
             "{\"device_id\":\"%s\",\"compartment\":%d,\"lid_open\":true}",
             DEVICE_ID, compartment);

    esp_http_client_config_t cfg = {
        .url = BACKEND_URL,
        .method = HTTP_METHOD_POST,
        .timeout_ms = HTTP_TIMEOUT_MS,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        ESP_LOGE(NET_TAG, "Could not create HTTP client");
        return;
    }
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_post_field(client, body, (int)strlen(body));

    int64_t start = now_ms();
    esp_err_t err = esp_http_client_perform(client);
    long long elapsed = (long long)(now_ms() - start);

    if (err == ESP_OK) {
        int status = esp_http_client_get_status_code(client);
        if (status == 201) {
            ESP_LOGI(NET_TAG, "POST ok: 201 Created (%lld ms)", elapsed);
        } else if (status == 404) {
            ESP_LOGW(NET_TAG, "POST returned 404: device_id \"%s\" not in devices table?", DEVICE_ID);
        } else {
            ESP_LOGW(NET_TAG, "POST returned status %d (%lld ms)", status, elapsed);
        }
    } else {
        ESP_LOGE(NET_TAG, "POST failed: %s (%lld ms)", esp_err_to_name(err), elapsed);
    }
    esp_http_client_cleanup(client);
}

// Runs separately from the lid loop, so a slow network never delays lid polling.
static void post_task(void *arg) {
    (void)arg;
    int compartment;
    while (1) {
        if (xQueueReceive(event_queue, &compartment, portMAX_DELAY) == pdTRUE) {
            if (!(xEventGroupGetBits(wifi_events) & WIFI_CONNECTED_BIT)) {
                ESP_LOGW(NET_TAG, "No Wi-Fi: compartment %d event not sent", compartment);
                continue;
            }
            ESP_LOGI(NET_TAG, "Sending event: compartment %d, lid_open true", compartment);
            post_event(compartment);
        }
    }
}

/* Main */

void app_main(void) {
    // NVS is required by the Wi-Fi driver.
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << SENSOR_PINS[0]) | (1ULL << SENSOR_PINS[1]),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_conf);

    ESP_LOGI(TAG, "=== MediSense: Lid Sensors + Wi-Fi + HTTP POST (re-arm %d ms) ===",
             REARM_CLOSED_MS);

    event_queue = xQueueCreate(EVENT_QUEUE_LENGTH, sizeof(int));
    wifi_init_sta();
    xTaskCreate(post_task, "post_task", 6144, NULL, 5, NULL);

    EventBits_t bits = xEventGroupWaitBits(wifi_events, WIFI_CONNECTED_BIT,
                                           pdFALSE, pdTRUE,
                                           pdMS_TO_TICKS(WIFI_WAIT_AT_BOOT_MS));
    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(NET_TAG, "Backend URL: %s", BACKEND_URL);
    } else {
        ESP_LOGW(NET_TAG, "No Wi-Fi yet. Lids still work; will keep retrying.");
    }

    for (int i = 0; i < NUM_SENSORS; i++) {
        stable_state[i] = gpio_get_level(SENSOR_PINS[i]);
        last_reading[i] = stable_state[i];
        last_change_time_ms[i] = now_ms();
        // Treat a lid that is closed at boot as already settled,
        // so the first real opening always counts.
        closed_since_ms[i] = now_ms() - REARM_CLOSED_MS;
        ignored_count[i] = 0;
        print_state(i);
    }
    ESP_LOGI(TAG, "Waiting for lid changes...");

    while (1) {
        for (int i = 0; i < NUM_SENSORS; i++) {
            int reading = gpio_get_level(SENSOR_PINS[i]);
            if (reading != last_reading[i]) {
                last_change_time_ms[i] = now_ms();
                last_reading[i] = reading;
            }
            if ((now_ms() - last_change_time_ms[i]) > DEBOUNCE_DELAY_MS) {
                if (reading != stable_state[i]) {
                    stable_state[i] = reading;
                    print_state(i);
                    int64_t t = now_ms();

                    if (stable_state[i] == LEVELS_WHEN_CLOSED) {
                        // Lid just closed: start timing how long it stays closed.
                        closed_since_ms[i] = t;
                    } else {
                        // CLOSED -> OPEN. Send only if the lid had been closed
                        // long enough; otherwise it's a flicker or a fumble.
                        long long closed_for = (long long)(t - closed_since_ms[i]);
                        if (closed_for >= REARM_CLOSED_MS) {
                            int compartment = COMPARTMENT_NUMBERS[i];
                            if (xQueueSend(event_queue, &compartment, 0) != pdTRUE) {
                                ESP_LOGW(TAG, "Event queue full: compartment %d dropped", compartment);
                            }
                        } else {
                            ignored_count[i]++;
                            ESP_LOGW(TAG, "%s: OPEN ignored, lid was closed only %lld ms (needs %d). Ignored so far: %d",
                                     COMPARTMENT_NAMES[i], closed_for, REARM_CLOSED_MS, ignored_count[i]);
                        }
                    }
                }
            }
        }
        vTaskDelay(pdMS_TO_TICKS(POLL_INTERVAL_MS));
    }
}
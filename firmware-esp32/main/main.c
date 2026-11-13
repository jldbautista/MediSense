#include <stdio.h>
#include <string.h>
#include <stdbool.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/event_groups.h"
#include "driver/gpio.h"
#include "driver/uart.h"
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
static const char *CAM_TAG = "CAMERA";

#define DEVICE_ID "medstation01"

// One entry per lid, left to right
// Reed switch between the pin and GND, internal pull up: magnet near (closed) = LOW, magnet away (open) = HIGH.
// Set enabled = true as each switch is wired to the rail; disabled lids are ignored (an unwired pin with a pull up would read "open").
typedef struct {
    gpio_num_t pin;
    int compartment;
    const char *name;
    bool enabled;
} lid_cfg_t;

static const lid_cfg_t LIDS[] = {
    {GPIO_NUM_13, 1, "Sun", true},
    {GPIO_NUM_14, 2, "Mon", true},
    {GPIO_NUM_18, 3, "Tue", true},
    {GPIO_NUM_19, 4, "Wed", true},
    {GPIO_NUM_21, 5, "Thu", true},
    {GPIO_NUM_22, 6, "Fri", true},
    {GPIO_NUM_23, 7, "Sat", true},
};
#define NUM_LIDS ((int)(sizeof(LIDS) / sizeof(LIDS[0])))

#define DEBOUNCE_DELAY_MS 50
#define POLL_INTERVAL_MS 10
#define LEVELS_WHEN_CLOSED 0

// Re-arm guard
#define REARM_CLOSED_MS 1000

#define WIFI_WAIT_AT_BOOT_MS 15000
#define HTTP_TIMEOUT_MS 5000
#define EVENT_QUEUE_LENGTH 10
#define WIFI_CONNECTED_BIT (1 << 0)

// Camera link

#define CAM_UART_PORT UART_NUM_2
#define CAM_UART_TX_PIN GPIO_NUM_17
#define CAM_UART_RX_PIN GPIO_NUM_16
#define CAM_UART_BAUD 115200
#define CAM_UART_RX_BUF 1024
#define CAM_LINE_MAX 64
#define MAX_COMPARTMENT 7

// After a lid opens, wait up to this long for the camera to report the same compartment OPEN. 
#define CAMERA_WAIT_MS 2500
// A camera OPEN this long before the lid event still counts.
#define CAMERA_LOOKBACK_MS 1000
#define CAMERA_POLL_MS 50
// No camera message (heartbeat every ~4 s) for this long = link lost.
#define CAM_LINK_TIMEOUT_MS 10000

typedef struct {
    int compartment;
    int64_t opened_ms;   // when the lid sensor reported OPEN
} lid_event_t;

static EventGroupHandle_t wifi_events;
static QueueHandle_t event_queue;

static int last_reading[NUM_LIDS];
static int stable_state[NUM_LIDS];
static int64_t last_change_time_ms[NUM_LIDS];
static int64_t closed_since_ms[NUM_LIDS];   // when the lid last became CLOSED
static int ignored_count[NUM_LIDS];         // OPENs ignored by the re-arm guard

// Latest camera report per compartment (index = compartment number).
// Shared between uart_task (writes) and post_task (reads), so guarded by cam_lock.
static SemaphoreHandle_t cam_lock;
static int64_t cam_open_ms[MAX_COMPARTMENT + 1];      // 0 = never seen OPEN
static float cam_open_margin[MAX_COMPARTMENT + 1];
static int64_t cam_last_msg_ms = 0;                   // 0 = never heard from camera
static bool cam_link_up = false;                      // used by uart_task only

static int64_t now_ms(void) {
    return esp_timer_get_time() / 1000;
}

static void print_state(int i) {
    ESP_LOGI(TAG, "Lid %d (%s): %s", LIDS[i].compartment, LIDS[i].name,
             stable_state[i] == LEVELS_WHEN_CLOSED ? "CLOSED" : "OPEN");
}

/* Camera UART */

static void camera_uart_init(void) {
    uart_config_t cfg = {
        .baud_rate = CAM_UART_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    // RX buffer only; the ESP32 never writes to the camera (yet).
    ESP_ERROR_CHECK(uart_driver_install(CAM_UART_PORT, CAM_UART_RX_BUF, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(CAM_UART_PORT, &cfg));
    ESP_ERROR_CHECK(uart_set_pin(CAM_UART_PORT, CAM_UART_TX_PIN, CAM_UART_RX_PIN,
                                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE,
                                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
    ESP_LOGI(CAM_TAG, "Camera UART: RX GPIO %d, TX GPIO %d, %d baud",
             (int)CAM_UART_RX_PIN, (int)CAM_UART_TX_PIN, CAM_UART_BAUD);
}

// Lines from the camera:
//   "CAM,<compartment>,OPEN,<margin>"    tag seen, lid open
//   "CAM,<compartment>,CLOSED,<margin>"  tag gone, lid closed (peak margin)
//   "HB,<scan_ms>"                       heartbeat every 20 scans
static void handle_camera_line(const char *line) {
    int64_t t = now_ms();
    int comp = 0;
    int scan_ms = 0;
    char state[8] = {0};
    float margin = 0.0f;

    if (sscanf(line, "HB,%d", &scan_ms) == 1) {
        xSemaphoreTake(cam_lock, portMAX_DELAY);
        cam_last_msg_ms = t;
        xSemaphoreGive(cam_lock);
        if (!cam_link_up) {
            cam_link_up = true;
            ESP_LOGI(CAM_TAG, "Camera Link Up (scan time %d ms)", scan_ms);
        }
        return;
    }

    if (sscanf(line, "CAM,%d,%7[A-Z],%f", &comp, state, &margin) == 3 &&
        comp >= 1 && comp <= MAX_COMPARTMENT) {
        bool is_open = (strcmp(state, "OPEN") == 0);
        bool is_closed = (strcmp(state, "CLOSED") == 0);
        if (!is_open && !is_closed) {
            ESP_LOGW(CAM_TAG, "Ignored Camera Line: \"%s\"", line);
            return;
        }
        xSemaphoreTake(cam_lock, portMAX_DELAY);
        cam_last_msg_ms = t;
        if (is_open) {
            cam_open_ms[comp] = t;
            cam_open_margin[comp] = margin;
        }
        xSemaphoreGive(cam_lock);
        if (!cam_link_up) {
            cam_link_up = true;
            ESP_LOGI(CAM_TAG, "Camera Link Up");
        }
        ESP_LOGI(CAM_TAG, "Camera: Compartment %d %s (margin %.2f)",
                 comp, is_open ? "OPEN" : "CLOSED", (double)margin);
        return;
    }

    ESP_LOGW(CAM_TAG, "Ignored Camera Line: \"%s\"", line);
}

// Reads bytes from the camera, splits them into lines, handles each line.
static void uart_task(void *arg) {
    (void)arg;
    uint8_t buf[64];
    char line[CAM_LINE_MAX];
    int len = 0;
    bool overflow = false;

    while (1) {
        int n = uart_read_bytes(CAM_UART_PORT, buf, sizeof(buf), pdMS_TO_TICKS(100));
        for (int i = 0; i < n; i++) {
            char ch = (char)buf[i];
            if (ch == '\n') {
                if (!overflow && len > 0) {
                    line[len] = '\0';
                    handle_camera_line(line);
                }
                len = 0;
                overflow = false;
            } else if (ch == '\r') {
                // ignore
            } else if (len < CAM_LINE_MAX - 1) {
                line[len++] = ch;
            } else {
                overflow = true;   // drop the rest of an over long line
            }
        }

        if (cam_link_up) {
            xSemaphoreTake(cam_lock, portMAX_DELAY);
            int64_t last = cam_last_msg_ms;
            xSemaphoreGive(cam_lock);
            if (now_ms() - last > CAM_LINK_TIMEOUT_MS) {
                cam_link_up = false;
                ESP_LOGW(CAM_TAG, "Camera Link Lost (no message for %d ms)", CAM_LINK_TIMEOUT_MS);
            }
        }
    }
}

// True if the camera reported compartment c OPEN at or after since_ms.
static bool camera_saw_open(int c, int64_t since_ms, float *margin) {
    bool found = false;
    xSemaphoreTake(cam_lock, portMAX_DELAY);
    if (cam_open_ms[c] > 0 && cam_open_ms[c] >= since_ms) {
        *margin = cam_open_margin[c];
        found = true;
    }
    xSemaphoreGive(cam_lock);
    return found;
}

// Most recent compartment other than lid_comp that the camera reported
// OPEN at or after since_ms, or 0 if none.
static int camera_other_open(int lid_comp, int64_t since_ms, float *margin) {
    int best = 0;
    int64_t best_ms = 0;
    xSemaphoreTake(cam_lock, portMAX_DELAY);
    for (int c = 1; c <= MAX_COMPARTMENT; c++) {
        if (c == lid_comp) {
            continue;
        }
        if (cam_open_ms[c] > 0 && cam_open_ms[c] >= since_ms && cam_open_ms[c] > best_ms) {
            best = c;
            best_ms = cam_open_ms[c];
            *margin = cam_open_margin[c];
        }
    }
    xSemaphoreGive(cam_lock);
    return best;
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
        ESP_LOGW(NET_TAG, "Wi-Fi Disconnected (reason %d), retrying...", (int)info->reason);
        esp_wifi_connect();
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *info = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(NET_TAG, "Connected. ESP32 IP Address: " IPSTR, IP2STR(&info->ip_info.ip));
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

    // Network name printed
    ESP_LOGI(NET_TAG, "Connecting to Wi-Fi Network \"%s\"...", WIFI_SSID);
}

/* HTTP POST */

// cam_comp = 0 means no camera reading: the event is sent lid only.
static void post_event(int compartment, int cam_comp, float cam_conf) {
    char body[256];
    if (cam_comp > 0) {
        if (cam_conf < 0.0f) cam_conf = 0.0f;
        if (cam_conf > 1.0f) cam_conf = 1.0f;
        snprintf(body, sizeof(body),
                 "{\"device_id\":\"%s\",\"compartment\":%d,\"lid_open\":true,"
                 "\"camera_compartment\":%d,\"camera_confidence\":%.2f}",
                 DEVICE_ID, compartment, cam_comp, (double)cam_conf);
    } else {
        snprintf(body, sizeof(body),
                 "{\"device_id\":\"%s\",\"compartment\":%d,\"lid_open\":true}",
                 DEVICE_ID, compartment);
    }

    esp_http_client_config_t cfg = {
        .url = BACKEND_URL,
        .method = HTTP_METHOD_POST,
        .timeout_ms = HTTP_TIMEOUT_MS,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        ESP_LOGE(NET_TAG, "Could Not Create HTTP Client");
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

// Runs separately from the lid loop, so waiting for the camera or a slow network never delays lid polling. 
// The lid is the trigger; the camera reading is attached if one arrives in time.
static void post_task(void *arg) {
    (void)arg;
    lid_event_t ev;
    while (1) {
        if (xQueueReceive(event_queue, &ev, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        int cam_comp = 0;
        float cam_conf = 0.0f;
        int64_t since = ev.opened_ms - CAMERA_LOOKBACK_MS;
        int64_t deadline = ev.opened_ms + CAMERA_WAIT_MS;

        while (1) {
            if (camera_saw_open(ev.compartment, since, &cam_conf)) {
                cam_comp = ev.compartment;
                break;
            }
            if (now_ms() >= deadline) {
                cam_comp = camera_other_open(ev.compartment, since, &cam_conf);
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(CAMERA_POLL_MS));
        }
        long long waited = (long long)(now_ms() - ev.opened_ms);

        if (cam_comp == ev.compartment) {
            ESP_LOGI(CAM_TAG, "Camera Agrees: Compartment %d (confidence %.2f), %lld ms after lid",
                     cam_comp, (double)cam_conf, waited);
        } else if (cam_comp > 0) {
            ESP_LOGW(CAM_TAG, "Camera Disagrees: Lid %d, Camera Saw %d (confidence %.2f)",
                     ev.compartment, cam_comp, (double)cam_conf);
        } else {
            xSemaphoreTake(cam_lock, portMAX_DELAY);
            int64_t last = cam_last_msg_ms;
            xSemaphoreGive(cam_lock);
            if (last > 0) {
                ESP_LOGW(CAM_TAG, "No Camera Reading for Compartment %d within %d ms (last camera message %lld ms ago)",
                         ev.compartment, CAMERA_WAIT_MS, (long long)(now_ms() - last));
            } else {
                ESP_LOGW(CAM_TAG, "No Camera Reading for Compartment %d within %d ms (never heard from camera)",
                         ev.compartment, CAMERA_WAIT_MS);
            }
        }

        if (!(xEventGroupGetBits(wifi_events) & WIFI_CONNECTED_BIT)) {
            ESP_LOGW(NET_TAG, "No Wi-Fi: Compartment %d Event Not Sent", ev.compartment);
            continue;
        }
        if (cam_comp > 0) {
            ESP_LOGI(NET_TAG, "Sending Event: Compartment %d, Camera %d (%.2f)",
                     ev.compartment, cam_comp, (double)cam_conf);
        } else {
            ESP_LOGI(NET_TAG, "Sending Event: Compartment %d, Lid Only", ev.compartment);
        }
        post_event(ev.compartment, cam_comp, cam_conf);
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

    ESP_LOGI(TAG, "--- MediSense: Reed Rail (7 lids) + Camera UART + Wi-Fi + HTTP POST (re-arm %d ms, camera wait %d ms) ---",
             REARM_CLOSED_MS, CAMERA_WAIT_MS);

    // Reed rail has no external resistors
    uint64_t pin_mask = 0;
    for (int i = 0; i < NUM_LIDS; i++) {
        if (LIDS[i].enabled) {
            pin_mask |= (1ULL << LIDS[i].pin);
        }
    }
    if (pin_mask != 0) {
        gpio_config_t io_conf = {
            .pin_bit_mask = pin_mask,
            .mode = GPIO_MODE_INPUT,
            .pull_up_en = GPIO_PULLUP_ENABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type = GPIO_INTR_DISABLE,
        };
        ESP_ERROR_CHECK(gpio_config(&io_conf));
    }

    cam_lock = xSemaphoreCreateMutex();
    camera_uart_init();
    xTaskCreate(uart_task, "uart_task", 4096, NULL, 5, NULL);

    event_queue = xQueueCreate(EVENT_QUEUE_LENGTH, sizeof(lid_event_t));
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

    for (int i = 0; i < NUM_LIDS; i++) {
        if (!LIDS[i].enabled) {
            ESP_LOGI(TAG, "Lid %d (%s): GPIO %d, not enabled",
                     LIDS[i].compartment, LIDS[i].name, (int)LIDS[i].pin);
            continue;
        }
        stable_state[i] = gpio_get_level(LIDS[i].pin);
        last_reading[i] = stable_state[i];
        last_change_time_ms[i] = now_ms();
        // Treat a lid that is closed at boot as already settled, so the first real opening always counts.
        closed_since_ms[i] = now_ms() - REARM_CLOSED_MS;
        ignored_count[i] = 0;
        ESP_LOGI(TAG, "Lid %d (%s): GPIO %d, enabled",
                 LIDS[i].compartment, LIDS[i].name, (int)LIDS[i].pin);
        print_state(i);
        if (stable_state[i] != LEVELS_WHEN_CLOSED) {
            ESP_LOGW(TAG, "Lid %d (%s) reads OPEN at boot: lid open, magnet too far, or switch/wire not connected",
                     LIDS[i].compartment, LIDS[i].name);
        }
    }
    ESP_LOGI(TAG, "Waiting for lid changes...");

    while (1) {
        for (int i = 0; i < NUM_LIDS; i++) {
            if (!LIDS[i].enabled) {
                continue;
            }
            int reading = gpio_get_level(LIDS[i].pin);
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
                        // Lid just closed, starts timing how long it stays closed.
                        closed_since_ms[i] = t;
                    } else {
                        // CLOSED -> OPEN. Sends only if the lid had been closed long enough, otherwise it's a flicker or a fumble.
                        long long closed_for = (long long)(t - closed_since_ms[i]);
                        if (closed_for >= REARM_CLOSED_MS) {
                            lid_event_t ev = {
                                .compartment = LIDS[i].compartment,
                                .opened_ms = t,
                            };
                            if (xQueueSend(event_queue, &ev, 0) != pdTRUE) {
                                ESP_LOGW(TAG, "Event queue full: compartment %d dropped", ev.compartment);
                            }
                        } else {
                            ignored_count[i]++;
                            ESP_LOGW(TAG, "Lid %d (%s): OPEN ignored, lid was closed only %lld ms (needs %d). Ignored so far: %d",
                                     LIDS[i].compartment, LIDS[i].name, closed_for, REARM_CLOSED_MS, ignored_count[i]);
                        }
                    }
                }
            }
        }
        vTaskDelay(pdMS_TO_TICKS(POLL_INTERVAL_MS));
    }
}
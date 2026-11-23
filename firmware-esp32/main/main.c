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
#include "driver/rmt_tx.h"
#include "esp_rom_sys.h"
#include <stdlib.h>

#include "secrets.h"   // WIFI_SSID, WIFI_PASSWORD, BACKEND_URL

static const char *TAG = "LID_SENSORS";
static const char *NET_TAG = "NETWORK";
static const char *CAM_TAG = "CAMERA";
static const char *REM_TAG = "REMINDER";

#define DEVICE_ID "medstation01"
#define FIRMWARE_VERSION "0.4.0"

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

// Heartbeat (POST /api/devices/heartbeat). The reply lists the compartments with a dose due
#define HEARTBEAT_INTERVAL_MS 30000

// No successful heartbeat for this long = forget the due list (stops buzzing while offline)
#define DUE_STALE_MS 120000
#define HTTP_RESPONSE_MAX 1024

// Buzzer: active buzzer module (VCC to 3.3 V, GND, I/O to D27).
#define BUZZER_PIN GPIO_NUM_27
#define BUZZER_ACTIVE_LOW 1

// Beep pattern while a dose is due: beep, pause, beep, then quiet, repeating.
#define BEEP_ON_MS 150
#define BEEP_GAP_MS 150
#define BEEP_QUIET_MS 2500

// Silence button: tactile button between D4 and GND (internal pull up, pressed = LOW).
#define BUTTON_PIN GPIO_NUM_4
#define BUTTON_DEBOUNCE_MS 50

// After Silence, the buzzer stays quiet this long (the LED stays lit) unless a new compartment becomes due.
#define SNOOZE_MS (10 * 60 * 1000)

// WS2812B compartment LEDs: data D32 -> SN74AHCT125N -> 330 ohm -> strip DIN.
#define LED_PIN GPIO_NUM_32
#define LED_COUNT 14              // LEDs on the cut piece
#define LED_SPACING 2             // light every other LED (60/m strip, ~32 mm compartments)
#define LED_OFFSET 0              // first lit LED (0 = the LED nearest DIN)
#define LED_DIN_ON_RIGHT 1        // 1 = DIN end under Sat (7), 0 = DIN end under Sun (1)
#define LED_BRIGHTNESS 40         // 0..255 cap: kept low for USB power and the camera's locked exposure
#define FLASH_MS 3000             // green/yellow/red result flash after an opening
#define REMINDER_TICK_MS 20

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
static int cam_last_scan_ms = -1;                     // from the camera's HB line, -1 = none yet

// Reminder state. Written by heartbeat_task / post_task / reminder_task, guarded by ui_lock.
typedef enum { FLASH_NONE = 0, FLASH_GREEN, FLASH_YELLOW, FLASH_RED } flash_t;
static SemaphoreHandle_t ui_lock;
static uint8_t due_mask = 0;                          // bit c = compartment c has a dose due
static int64_t due_updated_ms = 0;                    // last successful heartbeat
static uint8_t silenced_mask = 0;                     // due compartments when Silence was pressed
static int64_t snooze_until_ms = 0;
static flash_t flash_color[MAX_COMPARTMENT + 1];
static int64_t flash_until_ms[MAX_COMPARTMENT + 1];
static TaskHandle_t heartbeat_task_handle = NULL;

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
        cam_last_scan_ms = scan_ms;
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

// POST a JSON body. Returns the HTTP status (or -1 on a network error) and copies the start of the reply into resp (always null terminated).
static int http_post(const char *url, const char *body, char *resp, int resp_size, long long *elapsed_ms) {
    resp[0] = '\0';
    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = HTTP_TIMEOUT_MS,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        ESP_LOGE(NET_TAG, "Could Not Create HTTP Client");
        return -1;
    }
    esp_http_client_set_header(client, "Content-Type", "application/json");

    int64_t start = now_ms();
    int status = -1;
    int len = (int)strlen(body);
    esp_err_t err = esp_http_client_open(client, len);
    if (err == ESP_OK) {
        if (esp_http_client_write(client, body, len) == len) {
            esp_http_client_fetch_headers(client);
            int n = esp_http_client_read_response(client, resp, resp_size - 1);
            resp[n > 0 ? n : 0] = '\0';
            status = esp_http_client_get_status_code(client);
        } else {
            ESP_LOGE(NET_TAG, "POST Write Failed");
        }
    } else {
        ESP_LOGE(NET_TAG, "POST failed: %s", esp_err_to_name(err));
    }
    *elapsed_ms = (long long)(now_ms() - start);
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    return status;
}

// Reads "status":"..." from the stored event the backend returns. Empty if not found.
static void parse_status(const char *resp, char *out, int out_size) {
    out[0] = '\0';
    const char *p = strstr(resp, "\"status\":\"");
    if (p == NULL) {
        return;
    }
    p += strlen("\"status\":\"");
    int i = 0;
    while (p[i] != '\0' && p[i] != '"' && i < out_size - 1) {
        out[i] = p[i];
        i++;
    }
    out[i] = '\0';
}

static void show_result(int compartment, const char *status);

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

    static char resp[HTTP_RESPONSE_MAX];   // only post_task calls this
    long long elapsed = 0;
    int status = http_post(BACKEND_URL, body, resp, sizeof(resp), &elapsed);

    if (status == 201) {
        char result[32];
        parse_status(resp, result, sizeof(result));
        ESP_LOGI(NET_TAG, "POST ok: 201 Created (%lld ms), status %s", elapsed,
                 result[0] ? result : "(not in reply)");
        show_result(compartment, result);
    } else if (status == 404) {
        ESP_LOGW(NET_TAG, "POST returned 404: device_id \"%s\" not in devices table?", DEVICE_ID);
    } else if (status > 0) {
        ESP_LOGW(NET_TAG, "POST returned status %d (%lld ms)", status, elapsed);
    }
}

/* Reminders: heartbeat, compartment LEDs, buzzer, Silence button */

static char heartbeat_url[160];

static void heartbeat_url_init(void) {
#ifdef HEARTBEAT_URL
    snprintf(heartbeat_url, sizeof(heartbeat_url), "%s", HEARTBEAT_URL);
#else
    const char *suffix = "/api/events";
    size_t n = strlen(BACKEND_URL), m = strlen(suffix);
    if (n > m && strcmp(BACKEND_URL + n - m, suffix) == 0) {
        snprintf(heartbeat_url, sizeof(heartbeat_url), "%.*s/api/devices/heartbeat",
                 (int)(n - m), BACKEND_URL);
    } else {
        heartbeat_url[0] = '\0';
        ESP_LOGE(REM_TAG, "BACKEND_URL does not end in /api/events: add HEARTBEAT_URL to secrets.h");
    }
#endif
}

// Reads "due_compartments":[4,6] into a bit mask. Returns false if the field is missing.
static bool parse_due(const char *resp, uint8_t *mask) {
    const char *p = strstr(resp, "\"due_compartments\":[");
    if (p == NULL) {
        return false;
    }
    p += strlen("\"due_compartments\":[");
    uint8_t m = 0;
    while (*p != '\0' && *p != ']') {
        char *end = NULL;
        long c = strtol(p, &end, 10);
        if (end == p) {
            p++;              // skip ',' or spaces
            continue;
        }
        if (c >= 1 && c <= MAX_COMPARTMENT) {
            m |= (uint8_t)(1u << c);
        }
        p = end;
    }
    *mask = m;
    return true;
}

static void log_mask(const char *prefix, uint8_t mask) {
    char text[32] = {0};
    int len = 0;
    for (int c = 1; c <= MAX_COMPARTMENT; c++) {
        if (mask & (1u << c)) {
            len += snprintf(text + len, sizeof(text) - len, "%s%d", len ? "," : "", c);
        }
    }
    ESP_LOGI(REM_TAG, "%s %s", prefix, len ? text : "none");
}

// Ask heartbeat_task to send a heartbeat now (e.g. right after a dose was taken).
static void heartbeat_now(void) {
    if (heartbeat_task_handle != NULL) {
        xTaskNotifyGive(heartbeat_task_handle);
    }
}

static void heartbeat_task(void *arg) {
    (void)arg;
    static char resp[HTTP_RESPONSE_MAX];
    char body[320];
    while (1) {
        if (heartbeat_url[0] != '\0' && (xEventGroupGetBits(wifi_events) & WIFI_CONNECTED_BIT)) {
            int64_t t = now_ms();

            xSemaphoreTake(cam_lock, portMAX_DELAY);
            int64_t cam_last = cam_last_msg_ms;
            int scan_ms = cam_last_scan_ms;
            xSemaphoreGive(cam_lock);
            bool link = cam_last > 0 && (t - cam_last) <= CAM_LINK_TIMEOUT_MS;

            int rssi = 0;
            wifi_ap_record_t ap;
            bool have_rssi = (esp_wifi_sta_get_ap_info(&ap) == ESP_OK);
            if (have_rssi) {
                rssi = ap.rssi;
            }

            // lids: index 0 = compartment 1 (Sun) ... index 6 = compartment 7 (Sat); 1 = open
            char lids[32];
            int len = snprintf(lids, sizeof(lids), "[");
            for (int i = 0; i < NUM_LIDS; i++) {
                int open = LIDS[i].enabled && stable_state[i] != LEVELS_WHEN_CLOSED;
                len += snprintf(lids + len, sizeof(lids) - len, "%s%d", i ? "," : "", open);
            }
            snprintf(lids + len, sizeof(lids) - len, "]");

            len = snprintf(body, sizeof(body),
                           "{\"device_id\":\"%s\",\"firmware_version\":\"%s\",\"uptime_s\":%lld,"
                           "\"camera_link\":%s,\"lids\":%s",
                           DEVICE_ID, FIRMWARE_VERSION, (long long)(t / 1000),
                           link ? "true" : "false", lids);
            if (have_rssi) {
                len += snprintf(body + len, sizeof(body) - len, ",\"wifi_rssi\":%d", rssi);
            }
            if (link && scan_ms >= 0) {
                len += snprintf(body + len, sizeof(body) - len, ",\"camera_scan_ms\":%d", scan_ms);
            }
            snprintf(body + len, sizeof(body) - len, "}");

            long long elapsed = 0;
            int status = http_post(heartbeat_url, body, resp, sizeof(resp), &elapsed);
            uint8_t mask = 0;
            if (status == 200 && parse_due(resp, &mask)) {
                xSemaphoreTake(ui_lock, portMAX_DELAY);
                uint8_t old = due_mask;
                due_mask = mask;
                due_updated_ms = now_ms();
                silenced_mask &= mask;      // forget silences for doses that are no longer due
                xSemaphoreGive(ui_lock);
                if (mask != old) {
                    log_mask("Due Now:", mask);
                }
                ESP_LOGD(REM_TAG, "Heartbeat ok (%lld ms)", elapsed);
            } else if (status == 404) {
                ESP_LOGW(REM_TAG, "Heartbeat 404: is migration 005 applied and the backend restarted?");
            } else if (status > 0) {
                ESP_LOGW(REM_TAG, "Heartbeat returned status %d (%lld ms)", status, elapsed);
            }
        }
        // Wait for the next interval, or wake early when heartbeat_now() is called.
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(HEARTBEAT_INTERVAL_MS));
    }
}

// Called by post_task with the backend's result for an opening.
static void show_result(int compartment, const char *status) {
    flash_t color = FLASH_NONE;
    bool counts_as_taken = false;
    if (strncmp(status, "verified", 8) == 0) {
        color = FLASH_GREEN;
        counts_as_taken = true;
    } else if (strcmp(status, "needs_confirmation") == 0) {
        color = FLASH_YELLOW;
        counts_as_taken = true;     // the backend stops reminding for it too
    } else if (strcmp(status, "unexpected_access") == 0) {
        color = FLASH_RED;
    }
    if (color == FLASH_NONE || compartment < 1 || compartment > MAX_COMPARTMENT) {
        return;
    }
    xSemaphoreTake(ui_lock, portMAX_DELAY);
    flash_color[compartment] = color;
    flash_until_ms[compartment] = now_ms() + FLASH_MS;
    if (counts_as_taken) {
        due_mask &= (uint8_t)~(1u << compartment);   // stop at once; the next heartbeat confirms
    }
    xSemaphoreGive(ui_lock);
    if (counts_as_taken) {
        heartbeat_now();
    }
}

/* WS2812B over RMT (10 MHz: 1 tick = 0.1 us) */

static rmt_channel_handle_t led_chan = NULL;
static rmt_encoder_handle_t led_encoder = NULL;
static uint8_t led_buf[LED_COUNT * 3];   // G, R, B per LED

static void leds_init(void) {
    rmt_tx_channel_config_t chan_cfg = {
        .gpio_num = LED_PIN,
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10 * 1000 * 1000,
        .mem_block_symbols = 64,
        .trans_queue_depth = 4,
    };
    ESP_ERROR_CHECK(rmt_new_tx_channel(&chan_cfg, &led_chan));
    rmt_bytes_encoder_config_t enc_cfg = {
        .bit0 = {.level0 = 1, .duration0 = 3, .level1 = 0, .duration1 = 9},   // 0.3 us high, 0.9 us low
        .bit1 = {.level0 = 1, .duration0 = 9, .level1 = 0, .duration1 = 3},   // 0.9 us high, 0.3 us low
        .flags.msb_first = 1,
    };
    ESP_ERROR_CHECK(rmt_new_bytes_encoder(&enc_cfg, &led_encoder));
    ESP_ERROR_CHECK(rmt_enable(led_chan));
}

static void leds_show(void) {
    rmt_transmit_config_t tx_cfg = {.loop_count = 0};
    if (rmt_transmit(led_chan, led_encoder, led_buf, sizeof(led_buf), &tx_cfg) == ESP_OK) {
        rmt_tx_wait_all_done(led_chan, pdMS_TO_TICKS(100));
    }
    esp_rom_delay_us(80);   // WS2812B latch/reset time (> 50 us low)
}

static int led_index(int compartment) {
    int pos = LED_DIN_ON_RIGHT ? (MAX_COMPARTMENT - compartment) : (compartment - 1);
    return LED_OFFSET + LED_SPACING * pos;
}

// r, g, b are 0..255 before the brightness cap.
static void led_set(int compartment, uint8_t r, uint8_t g, uint8_t b) {
    int i = led_index(compartment);
    if (i < 0 || i >= LED_COUNT) {
        return;
    }
    led_buf[i * 3 + 0] = (uint8_t)(g * LED_BRIGHTNESS / 255);
    led_buf[i * 3 + 1] = (uint8_t)(r * LED_BRIGHTNESS / 255);
    led_buf[i * 3 + 2] = (uint8_t)(b * LED_BRIGHTNESS / 255);
}

static void buzzer_set(bool on) {
    gpio_set_level(BUZZER_PIN, on ? !BUZZER_ACTIVE_LOW : BUZZER_ACTIVE_LOW);
}

static void reminder_hw_init(void) {
    gpio_config_t out_cfg = {
        .pin_bit_mask = 1ULL << BUZZER_PIN,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&out_cfg));
    buzzer_set(false);

    gpio_config_t in_cfg = {
        .pin_bit_mask = 1ULL << BUTTON_PIN,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&in_cfg));

    leds_init();
    memset(led_buf, 0, sizeof(led_buf));
    leds_show();
}

// Boot check: each compartment LED lights blue from Sun to Sat, then one short beep.
// Confirms the strip, the LED mapping and the buzzer every time the station starts.
static void reminder_self_test(void) {
    for (int c = 1; c <= MAX_COMPARTMENT; c++) {
        memset(led_buf, 0, sizeof(led_buf));
        led_set(c, 0, 0, 255);
        leds_show();
        vTaskDelay(pdMS_TO_TICKS(150));
    }
    memset(led_buf, 0, sizeof(led_buf));
    leds_show();
    buzzer_set(true);
    vTaskDelay(pdMS_TO_TICKS(100));
    buzzer_set(false);
    ESP_LOGI(REM_TAG, "Self Test Done: LEDs Sun to Sat, one beep");
}

// Drives the LEDs and buzzer from due_mask, the result flashes, and the Silence button.
static void reminder_task(void *arg) {
    (void)arg;
    int last_button = 1;
    int stable_button = 1;
    int64_t button_change_ms = 0;
    int64_t beep_cycle_start = 0;
    bool was_buzzing = false;
    bool was_stale = false;
    uint8_t last_leds_key[LED_COUNT * 3] = {0};

    while (1) {
        int64_t t = now_ms();

        // Silence button (debounced, acts on press)
        int b = gpio_get_level(BUTTON_PIN);
        if (b != last_button) {
            last_button = b;
            button_change_ms = t;
        }
        if (t - button_change_ms > BUTTON_DEBOUNCE_MS && b != stable_button) {
            stable_button = b;
            if (stable_button == 0) {
                xSemaphoreTake(ui_lock, portMAX_DELAY);
                silenced_mask = due_mask;
                snooze_until_ms = t + SNOOZE_MS;
                uint8_t m = due_mask;
                xSemaphoreGive(ui_lock);
                if (m) {
                    ESP_LOGI(REM_TAG, "Silence Pressed: buzzer quiet for %d min (LED stays on)", SNOOZE_MS / 60000);
                } else {
                    ESP_LOGI(REM_TAG, "Silence Pressed (nothing due)");
                }
            }
        }

        // Snapshot the shared state
        xSemaphoreTake(ui_lock, portMAX_DELAY);
        bool stale = due_updated_ms == 0 || (t - due_updated_ms) > DUE_STALE_MS;
        uint8_t due = stale ? 0 : due_mask;
        uint8_t silenced = silenced_mask;
        bool snoozed = t < snooze_until_ms;
        flash_t flash[MAX_COMPARTMENT + 1];
        for (int c = 1; c <= MAX_COMPARTMENT; c++) {
            flash[c] = (t < flash_until_ms[c]) ? flash_color[c] : FLASH_NONE;
        }
        xSemaphoreGive(ui_lock);

        if (stale && !was_stale && due_updated_ms != 0) {
            ESP_LOGW(REM_TAG, "No Heartbeat Reply for %d s: reminders paused until the backend answers", DUE_STALE_MS / 1000);
        }
        was_stale = stale;

        // LEDs: result flash wins, otherwise blue while due
        memset(led_buf, 0, sizeof(led_buf));
        for (int c = 1; c <= MAX_COMPARTMENT; c++) {
            if (flash[c] == FLASH_GREEN) {
                led_set(c, 0, 255, 0);
            } else if (flash[c] == FLASH_YELLOW) {
                led_set(c, 255, 140, 0);
            } else if (flash[c] == FLASH_RED) {
                led_set(c, 255, 0, 0);
            } else if (due & (1u << c)) {
                led_set(c, 0, 0, 255);
            }
        }
        if (memcmp(led_buf, last_leds_key, sizeof(led_buf)) != 0) {
            leds_show();
            memcpy(last_leds_key, led_buf, sizeof(led_buf));
        }

        // Buzzer: beeps while something is due, unless silenced (a newly due compartment or the end of the snooze starts it again)
        bool new_due = (due & (uint8_t)~silenced) != 0;
        bool buzzing = due != 0 && (new_due || !snoozed);
        if (buzzing && !was_buzzing) {
            beep_cycle_start = t;
        }
        was_buzzing = buzzing;
        bool on = false;
        if (buzzing) {
            int64_t phase = (t - beep_cycle_start) % (2 * BEEP_ON_MS + BEEP_GAP_MS + BEEP_QUIET_MS);
            on = phase < BEEP_ON_MS || (phase >= BEEP_ON_MS + BEEP_GAP_MS && phase < 2 * BEEP_ON_MS + BEEP_GAP_MS);
        }
        buzzer_set(on);

        vTaskDelay(pdMS_TO_TICKS(REMINDER_TICK_MS));
    }
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

    ESP_LOGI(TAG, "--- MediSense v%s: 7 Lids + Camera UART + Wi-Fi + Events + Heartbeat + Reminders (re-arm %d ms, camera wait %d ms) ---",
             FIRMWARE_VERSION, REARM_CLOSED_MS, CAMERA_WAIT_MS);

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
    ui_lock = xSemaphoreCreateMutex();
    reminder_hw_init();
    reminder_self_test();
    camera_uart_init();
    xTaskCreate(uart_task, "uart_task", 4096, NULL, 5, NULL);

    event_queue = xQueueCreate(EVENT_QUEUE_LENGTH, sizeof(lid_event_t));
    wifi_init_sta();
    xTaskCreate(post_task, "post_task", 6144, NULL, 5, NULL);
    heartbeat_url_init();
    xTaskCreate(heartbeat_task, "heartbeat_task", 6144, NULL, 4, &heartbeat_task_handle);
    xTaskCreate(reminder_task, "reminder_task", 4096, NULL, 4, NULL);

    EventBits_t bits = xEventGroupWaitBits(wifi_events, WIFI_CONNECTED_BIT,
                                           pdFALSE, pdTRUE,
                                           pdMS_TO_TICKS(WIFI_WAIT_AT_BOOT_MS));
    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(NET_TAG, "Backend URL: %s", BACKEND_URL);
        ESP_LOGI(NET_TAG, "Heartbeat URL: %s (every %d s)", heartbeat_url, HEARTBEAT_INTERVAL_MS / 1000);
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
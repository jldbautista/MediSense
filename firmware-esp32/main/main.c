/*
 * MediSense - Week 3: Lid Sensor Test (ESP-IDF version)
 * Reads 2 Hall-effect/reed switch lid sensors and logs state and changes only, with debouncing to avoid false triggers.
 */

#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "LID_SENSORS";

#define NUM_SENSORS 2
static const gpio_num_t SENSOR_PINS[NUM_SENSORS] = {GPIO_NUM_4, GPIO_NUM_5};
static const char *COMPARTMENT_NAMES[NUM_SENSORS] = {"Compartment A", "Compartment B"};

#define DEBOUNCE_DELAY_MS 50
#define POLL_INTERVAL_MS 10

static int last_reading[NUM_SENSORS];
static int stable_state[NUM_SENSORS];
static int64_t last_change_time_ms[NUM_SENSORS];

static int64_t now_ms(void) {
    return esp_timer_get_time() / 1000;
}

static void print_state(int i) {
    ESP_LOGI(TAG, "%s: %s", COMPARTMENT_NAMES[i],
             stable_state[i] == 0 ? "OPEN" : "CLOSED");
}

void app_main(void) {
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << SENSOR_PINS[0]) | (1ULL << SENSOR_PINS[1]),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_conf);

    ESP_LOGI(TAG, "=== MediSense Week 1: Lid Sensor Test (ESP-IDF) ===");
    for (int i = 0; i < NUM_SENSORS; i++) {
        stable_state[i] = gpio_get_level(SENSOR_PINS[i]);
        last_reading[i] = stable_state[i];
        last_change_time_ms[i] = now_ms();
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
                }
            }
        }
        vTaskDelay(pdMS_TO_TICKS(POLL_INTERVAL_MS));
    }
}
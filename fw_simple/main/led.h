#pragma once

#include <stdint.h>

void c6_zero_led_init();
void c6_zero_led_set(uint8_t r, uint8_t g, uint8_t b);
void c6_zero_led_blink(uint8_t n, size_t on_ms, size_t off_ms, uint8_t r, uint8_t g, uint8_t b);
void c6_zero_led_clear();


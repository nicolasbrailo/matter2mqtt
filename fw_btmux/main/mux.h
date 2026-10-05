#pragma once

#include <stddef.h>

#include "esp_check.h"

enum mux_msg_type {
  MUX_MSG_SYNC=4,
  MUX_MSG_PING=5,
  MUX_MSG_LOG=6,
  MUX_MSG_BT=7,
  MUX_MSG_SPINEL=8,
};

// Max bytes carried in a single mux frame payload. Must be >= the largest packet
// any channel sends (see the BT H4 caps in app_bt.c, which static_assert against
// this). 528 leaves headroom over the current max (H4 ACL = 522).
#define MUX_TX_MAX_PAYLOAD 528

// Stand up the USB link, TX/log path, and RX/demux task.
// Call before any subsystem that should log over the mux.
esp_err_t mux_init(void);

// Run the blocking USB RX loop. Never returns.
void mux_run(void);

// Enqueue a frame on the given channel for transmission to the host. Safe from
// any task. Non-blocking, droppable: drops on a full TX queue. Use for logs and
// ping/sync, NOT for BT/Spinel.
int mux_tx(enum mux_msg_type msg, void *data, size_t len);

// Reliable, synchronous send for BT/Spinel: writes straight to USB under the TX
// mutex, blocking the caller (backpressure) instead of dropping. Returns 0 on
// success, -1 if no host is attached or it isn't draining (timeout). Call from a
// high-priority producer so it isn't held up by the log drainer. Must NOT be
// called from the ESP_LOG path.
int mux_tx_bt(void *data, size_t len);
int mux_tx_spinel(void *data, size_t len);


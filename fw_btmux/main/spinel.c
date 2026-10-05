/*
 * OpenThread RCP bridged to the USB mux (Spinel over MUX_MSG_SPINEL).
 *
 * The Linux host runs the full OpenThread stack; this firmware is the radio
 * co-processor, exchanging an HDLC-framed Spinel byte stream with the host over the
 * mux. OT does its own HDLC framing, so the mux just carries that opaque stream as
 * MUX_MSG_SPINEL -- a chunk of HDLC bytes per mux frame, reassembled host-side by
 * HDLC framing (not by mux boundaries).
 *
 *   host (OT stack) --MUX_MSG_SPINEL--> demux --> spinel_rx_from_host()
 *       --otNcpHdlcReceive() under OT lock--> NCP
 *   NCP --spinel_ncp_send() (HDLC bytes)--> mux_tx_spinel() --MUX_MSG_SPINEL--> host
 *
 * Transport seam: we run host_connection_mode = NONE so esp_openthread installs no
 * UART/USB host driver (the standalone RCP's HOST_CONNECTION_MODE_RCP_USB would grab
 * the USB-Serial-JTAG the mux owns). But with CONFIG_OPENTHREAD_RADIO + RCP_UART,
 * ot_task_worker still auto-inits an HDLC NCP wired to otPlatUartSend/a dead fd. We
 * can't override that symbol, so after esp_openthread_start() we re-init the NCP
 * (otNcpHdlcInit) with our own send callback -- redirecting TX to the mux. Re-init
 * is safe under the OT lock: the NcpBase callbacks it registers are single-pointer
 * overwrites / dedup'd, and the lock stalls the OT task so no NCP code runs mid-swap.
 *
 * SPDX-License-Identifier: Unlicense OR CC0-1.0
 */

#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_openthread.h"
#include "esp_openthread_lock.h"
#include "esp_openthread_types.h"
#include "esp_vfs_eventfd.h"
#include "openthread/ncp.h"

#include "esp_ot_config.h"
#include "mux.h"
#include "spinel.h"

static const char *tag = "SPINEL";

// Called by the OT NCP -- on the OT task, under the OT lock, from EncodeAndSend --
// when it has an HDLC-encoded Spinel frame for the host. The mux carries an opaque
// HDLC byte stream, so split one encoded buffer across several reliable mux frames
// if it exceeds the per-frame cap; the host reassembles by HDLC framing. The NCP
// encoder asserts we accept the whole buffer (rval == len), so always report the
// full length: if no host is draining, mux_tx_spinel() drops and the host re-syncs
// on connect (same best-effort posture as the BT ctrl->host path). Then signal
// otNcpHdlcSendDone() so the NCP can encode the next frame -- mirrors otPlatUartSend.
static int spinel_ncp_send(const uint8_t *buf, uint16_t len) {
  size_t off = 0;
  while (off < len) {
    size_t chunk = (size_t)len - off;
    if (chunk > MUX_TX_MAX_PAYLOAD)
      chunk = MUX_TX_MAX_PAYLOAD;
    mux_tx_spinel((void *)(buf + off), chunk); // drop on no-host/timeout; host re-syncs
    off += chunk;
  }
  otNcpHdlcSendDone();
  return len;
}

// A chunk of HDLC-Spinel bytes arrived from the host (mux demux). Feed the NCP's
// HDLC decoder, which dispatches whole Spinel frames once reassembled. OT is
// single-threaded, so this must hold the OT lock. Block (portMAX) per the mux's
// host->fw backpressure invariant: the one finite timeout in the chain is the
// reliable TX write, not here.
void spinel_rx_from_host(const uint8_t *buf, size_t len) {
  if (len == 0)
    return;
  if (!esp_openthread_lock_acquire(portMAX_DELAY))
    return; // lock not yet initialized -> OT not up; drop
  otNcpHdlcReceive(buf, (uint16_t)len);
  esp_openthread_lock_release();
}

void spinel_init(void) {
  // OT uses eventfds for its task queue and the radio driver.
  esp_vfs_eventfd_config_t eventfd_config = {.max_fds = 2};
  ESP_ERROR_CHECK(esp_vfs_eventfd_register(&eventfd_config));

  // OT posts OPENTHREAD_EVENT_* on the default event loop; create it if nobody
  // else has (bt_init() doesn't). NVS is already initialized by bt_init().
  esp_err_t err = esp_event_loop_create_default();
  if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
    ESP_ERROR_CHECK(err);
  }

  // static: esp_openthread_start may retain the pointer beyond this call.
  static esp_openthread_config_t config = {
      .netif_config = {0}, // radio-only RCP: no netif
      .platform_config = {
          .radio_config = ESP_OPENTHREAD_DEFAULT_RADIO_CONFIG(),
          .host_config  = ESP_OPENTHREAD_DEFAULT_HOST_CONFIG(), // NONE: no built-in host driver
          .port_config  = ESP_OPENTHREAD_DEFAULT_PORT_CONFIG(),
      },
  };

  // Inits the OT stack + radio and spawns its own mainloop task (non-blocking).
  // This also auto-inits an HDLC NCP wired to a dead UART fd (see file header).
  ESP_ERROR_CHECK(esp_openthread_start(&config));

  // Re-init the NCP with our send callback so its HDLC output goes to the mux. Hold
  // the OT lock: it touches OT core state and stalls the OT task during the swap.
  ESP_ERROR_CHECK(esp_openthread_lock_acquire(portMAX_DELAY) ? ESP_OK : ESP_FAIL);
  otNcpHdlcInit(esp_openthread_get_instance(), spinel_ncp_send);
  esp_openthread_lock_release();

  ESP_LOGI(tag, "OpenThread RCP started (Spinel bridged to mux)");
}

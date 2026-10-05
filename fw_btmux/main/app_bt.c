/*
 * BLE controller + VHCI <-> mux bridge.
 *
 * The ESP32-C6 runs *controller-only*: the Linux box is the Bluetooth host
 * (BlueZ), and we expose the controller to it over the USB mux. The clean cut is
 * the HCI transport -- we talk to the controller through VHCI (esp_vhci_host_*),
 * which carries raw H4 packets (0x01 cmd / 0x02 ACL / 0x04 event). There is no
 * on-device host stack (no Bluedroid/NimBLE), so all HCI command building lives
 * on the Linux side; the BT channel is an opaque H4 pipe.
 *
 *   host (BlueZ) --MUX_MSG_BT--> demux --> bt_rx_from_host() --reassemble-->
 *       g_bt_to_ctrl_q --> bt_to_ctrl_task --(flow control)--> esp_vhci_send
 *   controller --notify_host_recv--> bt_rx_from_controller() --> mux_tx(BT)
 *
 * Reliability note: the controller->host path uses the best-effort mux TX (drops
 * if the host isn't draining) and is gated on USB attach. HCI doesn't tolerate
 * lost packets, so this is provisional -- the SYNC handshake milestone will make
 * it reliable once we can tell a host is actually attached.
 *
 * SPDX-License-Identifier: Unlicense OR CC0-1.0
 */

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "esp_bt.h"
#include "esp_log.h"
#include "nvs_flash.h"

#include "bt.h"
#include "mux.h"
#include "spinel.h"

static const char *tag = "mt2m";

// Largest H4 packet we move in one piece, derived from the controller's own buffer
// sizes (CONFIG_* come from sdkconfig.h, force-included by the build) so it tracks
// menuconfig automatically. The HCI-level ACL bound is the controller's LE ACL data
// packet length (CONFIG_BT_LE_ACL_BUF_SIZE), NOT the 251-byte over-the-air DLE max
// -- the controller fragments to 251 on the link itself.
//   ACL:   H4 type(1) + ACL header(4) + data(CONFIG_BT_LE_ACL_BUF_SIZE)
//   Event: H4 type(1) + HCI event packet(CONFIG_BT_LE_HCI_EVT_BUF_SIZE, hdr+params)
#define BT_H4_ACL_MAX  (5 + CONFIG_BT_LE_ACL_BUF_SIZE)
#define BT_H4_EVT_MAX  (1 + CONFIG_BT_LE_HCI_EVT_BUF_SIZE)
#define BT_PKT_MAX     (BT_H4_ACL_MAX > BT_H4_EVT_MAX ? BT_H4_ACL_MAX : BT_H4_EVT_MAX)

_Static_assert(BT_PKT_MAX <= MUX_TX_MAX_PAYLOAD,
               "H4 packet exceeds mux payload cap; bump MUX_TX_MAX_PAYLOAD in mux.h");

#define BT_TO_CTRL_DEPTH  16    // host->controller packet queue depth
#define BT_ACC_SZ         2048  // H4 reassembly: holds a partial packet + a fresh chunk

struct bt_pkt {
  uint16_t len;
  uint8_t  data[BT_PKT_MAX];
};

static QueueHandle_t    g_bt_to_ctrl_q;   // whole H4 packets, host -> controller
static SemaphoreHandle_t g_ctrl_ready;    // given when the controller can accept a packet

// ---- controller -> host -------------------------------------------------

// VHCI callback: the controller has an H4 event/ACL packet for the host. Runs in
// the controller task context; `data` is only valid for this call, but mux_tx_bt
// copies it before returning. Reliable send: mux_tx_bt blocks here until the
// frame is on the wire (or times out), which backpressures the controller -- it
// won't free this buffer / produce more until we return. Drops only when no host
// is attached (mux_tx_bt's own check) or after a timeout, so a wedged host can't
// stall us forever. NB: this assumes blocking in this callback is permitted (the
// open question from the design notes) -- confirm if events go missing.
static int bt_rx_from_controller(uint8_t *data, uint16_t len) {
  mux_tx_bt(data, len);
  return 0;
}

// VHCI callback: the controller can accept another host->controller packet.
// Called in controller task context (per the IDF controller_vhci_ble_adv example),
// so a plain give is correct here.
static void bt_controller_tx_ready(void) {
  xSemaphoreGive(g_ctrl_ready);
}

static esp_vhci_host_callback_t vhci_cb = {
    bt_controller_tx_ready,
    bt_rx_from_controller,
};

// ---- host -> controller -------------------------------------------------

// Length of the H4 packet at buf[0..avail): >0 = total packet size, 0 = need more
// bytes to decide, -1 = unknown type (caller should resync).
static int h4_pkt_len(const uint8_t *buf, size_t avail) {
  if (avail < 1)
    return 0;
  switch (buf[0]) {
    case 0x01: // HCI Command: type, opcode(2), param_len(1)
      return (avail < 4) ? 0 : 4 + buf[3];
    case 0x02: // ACL data: type, handle(2), data_len(2 LE)
      return (avail < 5) ? 0 : 5 + (buf[3] | (buf[4] << 8));
    case 0x03: // SCO data: type, handle(2), data_len(1)
      return (avail < 4) ? 0 : 4 + buf[3];
    case 0x04: // Event: type, evt_code(1), param_len(1) (host->ctrl normally N/A)
      return (avail < 3) ? 0 : 3 + buf[2];
    default:
      return -1;
  }
}

// Reassemble H4 packets out of the (possibly fragmented/coalesced) mux payloads
// and hand whole packets to the controller path. Single caller (demux task).
void bt_rx_from_host(const uint8_t *data, size_t len) {
  static uint8_t acc[BT_ACC_SZ];
  static size_t  acc_len;

  if (!g_bt_to_ctrl_q)
    return; // bt not up yet

  if (len > sizeof(acc) - acc_len) {
    ESP_LOGE(tag, "BT host->ctrl overflow, flushing %u staged bytes", (unsigned)acc_len);
    acc_len = 0;
    if (len > sizeof(acc))
      return; // single chunk too big to ever hold; drop it
  }
  memcpy(acc + acc_len, data, len);
  acc_len += len;

  size_t off = 0;
  while (off < acc_len) {
    const int plen = h4_pkt_len(acc + off, acc_len - off);
    if (plen == 0)
      break; // need more header bytes
    if (plen < 0 || plen > BT_PKT_MAX) {
      ESP_LOGE(tag, "BT bad/oversized H4 (type 0x%02x len %d), flushing", acc[off], plen);
      acc_len = 0;
      off = 0;
      break;
    }
    if (acc_len - off < (size_t)plen)
      break; // whole packet not here yet

    struct bt_pkt p;
    p.len = (uint16_t)plen;
    memcpy(p.data, acc + off, plen);
    // Reliable: block (don't drop) so backpressure propagates up the RX chain
    // (stream buffer -> read loop -> USB flow control -> Linux). We run in the
    // demux task; bt_to_ctrl_task drains under controller flow control, so this
    // only waits while the controller isn't accepting.
    xQueueSend(g_bt_to_ctrl_q, &p, portMAX_DELAY);
    off += plen;
  }

  if (off > 0) {
    memmove(acc, acc + off, acc_len - off);
    acc_len -= off;
  }
}

// Drains whole H4 packets to the controller, respecting its flow control. A
// dedicated task so the demux task never blocks waiting on the controller.
static void bt_to_ctrl_task(void *arg) {
  (void)arg;
  struct bt_pkt p;
  for (;;) {
    if (xQueueReceive(g_bt_to_ctrl_q, &p, portMAX_DELAY) != pdTRUE)
      continue;
    while (!esp_vhci_host_check_send_available()) {
      xSemaphoreTake(g_ctrl_ready, portMAX_DELAY); // wait for tx_ready callback
    }
    esp_vhci_host_send_packet(p.data, p.len);
  }
}

// ---- init ---------------------------------------------------------------

void bt_init(void) {
  // Bridge plumbing must exist before the controller can call our VHCI callbacks.
  g_ctrl_ready = xSemaphoreCreateBinary();
  g_bt_to_ctrl_q = xQueueCreate(BT_TO_CTRL_DEPTH, sizeof(struct bt_pkt));
  if (!g_ctrl_ready || !g_bt_to_ctrl_q) {
    ESP_LOGE(tag, "BT bridge alloc failed");
    abort();
  }
  if (xTaskCreate(bt_to_ctrl_task, "bt_to_ctrl", 4096, NULL, 5, NULL) != pdPASS) {
    ESP_LOGE(tag, "Can't create bt_to_ctrl task");
    abort();
  }

  // NVS holds PHY calibration data the controller needs.
  esp_err_t ret = nvs_flash_init();
  if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    ESP_ERROR_CHECK(nvs_flash_erase());
    ret = nvs_flash_init();
  }
  ESP_ERROR_CHECK(ret);

  // C6 is BLE-only; reclaim the classic-BT memory if this build allows it
  // (no-op / not supported on some targets, so don't treat failure as fatal).
  ret = esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT);
  if (ret != ESP_OK) {
    ESP_LOGW(tag, "classic-BT mem release: %s (continuing)", esp_err_to_name(ret));
  }

  esp_bt_controller_config_t bt_cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
  ESP_ERROR_CHECK(esp_bt_controller_init(&bt_cfg));
  ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_BLE));
  ESP_ERROR_CHECK(esp_vhci_host_register_callback(&vhci_cb));

  ESP_LOGI(tag, "BLE controller up; VHCI bridged to mux (controller-only)");
}

void app_main(void) {
  ESP_ERROR_CHECK(mux_init()); // USB link + log hook up first, so all logs are muxed
  bt_init();                   // BLE controller; its logs flow over the mux
  spinel_init();               // OpenThread RCP + 802.15.4 radio (no host transport yet, S1)
  mux_run();                   // blocking USB RX loop, never returns
}

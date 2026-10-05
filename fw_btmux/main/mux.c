#include "mux.h"

#include <stdarg.h>
#include <stdio.h>

#include "esp_log.h"

#include "driver/usb_serial_jtag.h"

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"

#include "bt.h"
#include "spinel.h"

static const char *tag = "MUX";

// ESP_LOG callback installed before ours (so we can chain calls)
static vprintf_like_t g_orig_log_vprintf = NULL;

#define MSG_TAIL "/mt2mqtt"

#define MUX_HDR_SZ     3                  // int8 type + int16 len
#define MUX_TAIL_SZ    (sizeof(MSG_TAIL)) // 9, includes NUL (matches mux_tx)
                                          //
/*
 * RX accumulation buffer. A FreeRTOS StreamBuffer (ringbuffer)
 *   - producer = mux_rx() (the USB read loop)
 *   - consumer = frame parser, via xStreamBufferReceive()
 * lock-free for single-writer/single-reader case (uses task notifications, no mutex).
 */
#define MUX_RB_SZ 2048u
static StreamBufferHandle_t g_rx_rb;

// Size of a buffer to reassemble frames out of the RB; must exceed a whole frame
// (hdr + payload + tail), i.e. > MUX_HDR_SZ + MUX_TX_MAX_PAYLOAD + MUX_TAIL_SZ.
#define MUX_STAGING_SZ 1024

/*
 * TX path. Every caller (log hook, demux replies, future USB/SPINEL) enqueues a
 * whole frame descriptor; a single TX task is the *only* writer to the USB
 * peripheral. This guarantees frames are never interleaved on the wire, and
 * lets producers be non-blocking (xQueueSend with 0 timeout, drop on full).
 *
 * A FreeRTOS queue (not a StreamBuffer/MessageBuffer) is used on purpose: the
 * queue is multi-producer safe, so the log hook -- which may run concurrently
 * from several tasks, with the log lock held -- can enqueue without taking an
 * extra lock that could block.
 */
#define MUX_TX_Q_DEPTH          16
#define MUX_TX_TIMEOUT_MS       100   // droppable (log) write: bound, then drop to avoid backlog
#define MUX_TX_RELIABLE_TIMEOUT_MS 1000 // reliable (BT/Spinel) write: block this long, then degrade
                                        // (MUX_TX_MAX_PAYLOAD lives in mux.h: shared with BT sizing)

struct mux_frame {
  uint8_t  type;                       // enum mux_msg_type
  uint16_t len;                        // payload bytes in use
  uint8_t  payload[MUX_TX_MAX_PAYLOAD];
};

static QueueHandle_t g_tx_q;
static volatile uint32_t g_tx_dropped; // frames dropped due to a full queue

// Per-channel mux frame counters. tx = fw->host, rx = host->fw. Each counter is
// incremented from a single task (a channel's tx from its producer task, all rx
// from mux_rx_task) and only read by the stats task, so plain volatile is enough --
// a lost increment would at worst miscount a periodic log line.
static volatile uint32_t g_cnt_bt_tx, g_cnt_bt_rx;
static volatile uint32_t g_cnt_spinel_tx, g_cnt_spinel_rx;

// Serializes every USB write. A real mutex (priority inheritance) on purpose:
//   - multiple writer tasks now exist (the log drainer + reliable BT/Spinel
//     senders), so the lock is what keeps frames from interleaving on the wire;
//   - on release FreeRTOS hands it to the highest-priority waiter, so a flood of
//     low-priority logs cannot starve a high-priority BT/Spinel sender;
//   - inheritance bounds the inversion to one in-flight write.
static SemaphoreHandle_t g_tx_mutex;

// Assemble one frame and write it to USB under the TX mutex. `timeout` bounds the
// USB write itself; the mutex is taken with portMAX (held only for that one
// bounded write, so the wait is bounded too). Returns 0 on a full write, -1 on a
// short write (timeout/partial -> dropped; the host resyncs on the tail). Must
// not log: reachable from the log drainer and from reliable senders.
static int mux_write_locked(const struct mux_frame *f, TickType_t timeout) {
  uint8_t buf[MUX_HDR_SZ + MUX_TX_MAX_PAYLOAD + MUX_TAIL_SZ];
  size_t n = 0;
  buf[n++] = (uint8_t)f->type;
  buf[n++] = (uint8_t)(f->len & 0xff);        // len, little-endian (matches host)
  buf[n++] = (uint8_t)((f->len >> 8) & 0xff);
  memcpy(&buf[n], f->payload, f->len);
  n += f->len;
  memcpy(&buf[n], MSG_TAIL, MUX_TAIL_SZ);
  n += MUX_TAIL_SZ;

  xSemaphoreTake(g_tx_mutex, portMAX_DELAY);
  const int w = usb_serial_jtag_write_bytes(buf, n, timeout);
  xSemaphoreGive(g_tx_mutex);
  return (w == (int)n) ? 0 : -1;
}

// Drains the droppable (log/ping/sync) queue. Low priority: with the bounded
// write it times out and drops while no host drains -> no stale backlog. Runs at
// lower priority than the reliable senders, which therefore win the mutex.
static void mux_tx_task(void* arg) {
  (void)arg;
  struct mux_frame f;
  for (;;) {
    if (xQueueReceive(g_tx_q, &f, portMAX_DELAY) == pdTRUE) {
      mux_write_locked(&f, pdMS_TO_TICKS(MUX_TX_TIMEOUT_MS));
    }
  }
}

// Non-blocking enqueue of an already-built frame. Safe from any task. MUST NOT
// log on any path -- this is reachable from the ESP_LOG hook, so a log here
// would recurse. Drops (and counts) on a full queue rather than blocking.
static int mux_tx_enqueue(const struct mux_frame *f) {
  if (xQueueSend(g_tx_q, f, 0) != pdTRUE) {
    g_tx_dropped++;
    return -1;
  }
  return 0;
}

// Enqueue a frame for transmission. Safe to call from any task.
int mux_tx(enum mux_msg_type msg, void* data, size_t len) {
  if (len > MUX_TX_MAX_PAYLOAD) {
    len = MUX_TX_MAX_PAYLOAD; // truncate rather than reject; host frame stays valid
  }

  struct mux_frame f;
  f.type = (uint8_t)msg;
  f.len  = (uint16_t)len;
  if (len > 0) {
    memcpy(f.payload, data, len);
  }
  return mux_tx_enqueue(&f);
}

// Reliable, synchronous send for BT/Spinel. Writes straight to USB under the
// mutex (no queue), blocking up to MUX_TX_RELIABLE_TIMEOUT_MS so the *caller* is
// backpressured rather than the frame being dropped. Returns 0 on success, -1 if
// no host is attached or the host isn't draining (timeout -> degrade to drop).
// Called from high-priority producers (e.g. the BT controller callback), so it
// wins the mutex over the log drainer. NOT for logs -- those must stay async via
// mux_tx() (the log hook runs with the log lock held and must not block).
static int mux_tx_reliable(enum mux_msg_type msg, void *data, size_t len) {
  if (!usb_serial_jtag_is_connected())
    return -1; // no host -> drop instead of blocking the caller (e.g. controller)
  if (len > MUX_TX_MAX_PAYLOAD)
    return -1; // reliable channels must not truncate -- that would corrupt the packet

  struct mux_frame f;
  f.type = (uint8_t)msg;
  f.len  = (uint16_t)len;
  if (len > 0) {
    memcpy(f.payload, data, len);
  }
  const int rc = mux_write_locked(&f, pdMS_TO_TICKS(MUX_TX_RELIABLE_TIMEOUT_MS));
  if (rc == 0) {
    if (msg == MUX_MSG_BT)          g_cnt_bt_tx++;
    else if (msg == MUX_MSG_SPINEL) g_cnt_spinel_tx++;
  }
  return rc;
}

int mux_tx_bt(void *data, size_t len)     { return mux_tx_reliable(MUX_MSG_BT, data, len); }
int mux_tx_spinel(void *data, size_t len) { return mux_tx_reliable(MUX_MSG_SPINEL, data, len); }

static int mux_demux_frame(enum mux_msg_type type, const void* payload, size_t len) {
  switch (type) {
    case MUX_MSG_SYNC:
      mux_tx(MUX_MSG_SYNC, "mt2mqtt sync", sizeof("mt2mqtt sync"));
      break;
    case MUX_MSG_PING:
      mux_tx(MUX_MSG_PING, "PONG", sizeof("PONG"));
      break;
    case MUX_MSG_LOG:
      ESP_LOGW(tag, "fw rx'd muxed LOG message; only fw can tx LOGs");
      break;
    case MUX_MSG_BT:
      g_cnt_bt_rx++;
      bt_rx_from_host(payload, len);
      break;
    case MUX_MSG_SPINEL:
      g_cnt_spinel_rx++;
      spinel_rx_from_host((const uint8_t *)payload, len);
      break;
    default:
      // TODO handle error msg
      return -1;
  }
  return 0;
}

// Consumer task: drains the RX stream buffer, reassembles frames, dispatches.
// Single reader of g_rx_rb (SPSC) -- do not start a second one.
static void mux_rx_task(void* arg) {
  (void)arg;
  static uint8_t staging[MUX_STAGING_SZ]; // reassembly buffer (static: off the task stack)
  size_t staging_len = 0;

  for (;;) {
    // Blocks until the producer sends (trigger level 1 -> first byte). No poll.
    const size_t n = xStreamBufferReceive(g_rx_rb, &staging[staging_len],
                                          sizeof(staging) - staging_len, portMAX_DELAY);
    staging_len += n;

    // Process as many whole frames as we now have.
    size_t off = 0;
    while (staging_len - off >= MUX_HDR_SZ) {
      const enum mux_msg_type type = (enum mux_msg_type)staging[off];
      const size_t len = (size_t)staging[off + 1] | ((size_t)staging[off + 2] << 8);
      const size_t frame_sz = MUX_HDR_SZ + len + MUX_TAIL_SZ;

      if (frame_sz > sizeof(staging)) {
        // len can never fit -> oversized or we're desynced. Drop and resync.
        // TODO: Real resync needs a tail/COBS scan; for now we just flush
        ESP_LOGE(tag, "demux: frame len %zu too big, discarding %zu staged bytes", len, staging_len);
        staging_len = 0;
        off = 0;
        break;
      }
      if (staging_len - off < frame_sz) {
        break; // whole frame not here yet -> go back to sleep for more bytes
      }

      mux_demux_frame(type, &staging[off + MUX_HDR_SZ], len);
      off += frame_sz; // consumes header + payload + tail
    }

    // Keep the unconsumed remainder for the next round.
    if (off > 0) {
      memmove(staging, staging + off, staging_len - off);
      staging_len -= off;
    }
  }
}



// ESP_LOG capture hook. Called in the context of whatever task logged, with the log lock held, so
// no blocking possible here. Note the callback may be invoked concurrently from different tasks --
// hence we format into a per-call stack frame (no shared buffer) and enqueue it atomically.
static int mux_log_hook(const char *fmt, va_list ap) {
  struct mux_frame f;
  f.type = MUX_MSG_LOG;

  va_list ap_fmt;
  va_copy(ap_fmt, ap);
  const int n = vsnprintf((char *)f.payload, sizeof(f.payload), fmt, ap_fmt);
  va_end(ap_fmt);

  if (n > 0) {
    f.len = (n < (int)sizeof(f.payload)) ? (uint16_t)n : (uint16_t)sizeof(f.payload);
    mux_tx_enqueue(&f);
  }

  // Keep the line on the local console too.
  return g_orig_log_vprintf ? g_orig_log_vprintf(fmt, ap) : vprintf(fmt, ap);
}

// Periodic mux frame-count report. Logs per-channel tx/rx over the last interval
// (plus running totals) every MUX_STATS_INTERVAL_MS. Goes out via ESP_LOG, so it
// rides the mux LOG channel to the host as well as the UART0 console. A plain task
// (not the log hook / a reliable sender), so logging here is safe.
#define MUX_STATS_INTERVAL_MS 5000
static void mux_stats_task(void *arg) {
  (void)arg;
  uint32_t p_bt_tx = 0, p_bt_rx = 0, p_sp_tx = 0, p_sp_rx = 0;
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(MUX_STATS_INTERVAL_MS));
    const uint32_t bt_tx = g_cnt_bt_tx, bt_rx = g_cnt_bt_rx;
    const uint32_t sp_tx = g_cnt_spinel_tx, sp_rx = g_cnt_spinel_rx;
    ESP_LOGI(tag,
             "frames/%ds: BT tx=%u rx=%u | SPINEL tx=%u rx=%u "
             "(totals BT %u/%u SPINEL %u/%u)",
             MUX_STATS_INTERVAL_MS / 1000,
             (unsigned)(bt_tx - p_bt_tx), (unsigned)(bt_rx - p_bt_rx),
             (unsigned)(sp_tx - p_sp_tx), (unsigned)(sp_rx - p_sp_rx),
             (unsigned)bt_tx, (unsigned)bt_rx, (unsigned)sp_tx, (unsigned)sp_rx);
    p_bt_tx = bt_tx; p_bt_rx = bt_rx; p_sp_tx = sp_tx; p_sp_rx = sp_rx;
  }
}

esp_err_t mux_init(void) {
  usb_serial_jtag_driver_config_t cfg = {
      .rx_buffer_size = 1024, .tx_buffer_size = 1024,
  };
  usb_serial_jtag_driver_install(&cfg);

  // Receiver side ringbuffer queue.
  // Trigger level 1: a blocked reader wakes as soon as any byte is available.
  g_rx_rb = xStreamBufferCreate(MUX_RB_SZ, 1);
  if (g_rx_rb == NULL) {
    ESP_LOGE(tag, "Can't create RX SB");
    return ESP_FAIL;
  }

  // Sender side queue (not single producer, so not a RB)
  g_tx_q = xQueueCreate(MUX_TX_Q_DEPTH, sizeof(struct mux_frame));
  if (g_tx_q == NULL) {
    ESP_LOGE(tag, "Can't create TX queue");
    return ESP_ERR_NO_MEM;
  }

  // Serializes all USB writes (log drainer + reliable BT/Spinel senders). Must
  // exist before any writer task runs and before bt_init() registers callbacks.
  g_tx_mutex = xSemaphoreCreateMutex();
  if (g_tx_mutex == NULL) {
    ESP_LOGE(tag, "Can't create TX mutex");
    return ESP_ERR_NO_MEM;
  }

  // Create bg tasks to process tx and rx
  if (xTaskCreate(mux_tx_task, "mux_tx", 4096, NULL, 5, NULL) != pdPASS) {
    ESP_LOGE(tag, "Can't create TX task");
    return ESP_ERR_NO_MEM;
  }
  if (xTaskCreate(mux_rx_task, "mux_rx", 4096, NULL, 5, NULL) != pdPASS) {
    ESP_LOGE(tag, "Can't create RX task");
    return ESP_ERR_NO_MEM;
  }
  // Low priority: stats are best-effort and must never preempt the data path.
  if (xTaskCreate(mux_stats_task, "mux_stats", 3072, NULL, 1, NULL) != pdPASS) {
    ESP_LOGE(tag, "Can't create stats task");
    return ESP_ERR_NO_MEM;
  }

  // USB link and TX path are up now -> safe to start shipping logs over the mux.
  // Install the ESP_LOG hook; it saves (and chains to) the previous writer (UART0 console).
  g_orig_log_vprintf = esp_log_set_vprintf(mux_log_hook);
  return ESP_OK;
}

// Blocking USB RX loop: feed bytes into the demux stream buffer. Never returns.
void mux_run(void) {
  ESP_LOGI(tag, "Spinel/BT/Log muxer running...");
  uint8_t buf[512];
  for (;;) {
      const int n = usb_serial_jtag_read_bytes(buf, sizeof(buf), pdMS_TO_TICKS(20));
      if (n == 0) continue;

      // Block (don't drop): host->controller HCI can't lose bytes. If the demux
      // can't keep up this stalls the read loop, the USB RX buffer fills, and the
      // host's write() blocks -- lossless backpressure all the way to Linux.
      xStreamBufferSend(g_rx_rb, buf, n, portMAX_DELAY);
  }
}

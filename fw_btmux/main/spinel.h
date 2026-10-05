#pragma once

#include <stddef.h>
#include <stdint.h>

// Bring up the OpenThread RCP (OT stack + native 802.15.4 radio) and bridge its
// HDLC-Spinel stream to the USB mux (MUX_MSG_SPINEL). host_connection_mode = NONE
// so OT installs no host driver; we re-init the NCP onto the mux ourselves.
// Call after mux_init() (so OT logs are muxed) and bt_init() (NVS already up).
void spinel_init(void);

// Feed a chunk of HDLC-Spinel bytes received from the host (via the mux demux) into
// the OT NCP. Takes the OT lock internally; call from the mux RX task.
void spinel_rx_from_host(const uint8_t *buf, size_t len);

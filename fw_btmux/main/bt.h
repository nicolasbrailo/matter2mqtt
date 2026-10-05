#pragma once

#include <stddef.h>
#include <stdint.h>

// Bring up the BLE controller and the VHCI <-> mux bridge.
void bt_init(void);

// Host -> controller: raw H4 bytes arriving on the MUX_MSG_BT channel. A call may
// carry a partial packet, exactly one, or several; bt reassembles whole H4
// packets before handing them to the controller. Called from the demux task.
void bt_rx_from_host(const uint8_t *data, size_t len);

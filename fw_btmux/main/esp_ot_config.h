#pragma once

#include "esp_openthread_types.h"

// Native IEEE 802.15.4 radio co-processor.
#define ESP_OPENTHREAD_DEFAULT_RADIO_CONFIG() \
    { .radio_mode = RADIO_MODE_NATIVE }

// No built-in host transport: the Spinel stream is bridged to the USB mux in code
// (S2). The standalone RCP uses HOST_CONNECTION_MODE_RCP_USB, which would grab the
// USB-Serial-JTAG that the mux owns -- NONE keeps OT off the link until we wire it.
#define ESP_OPENTHREAD_DEFAULT_HOST_CONFIG() \
    { .host_connection_mode = HOST_CONNECTION_MODE_NONE }

#define ESP_OPENTHREAD_DEFAULT_PORT_CONFIG()                     \
    {                                                            \
        .storage_partition_name = "nvs",                         \
        .netif_queue_size = 10,                                  \
        .task_queue_size = 10,                                   \
    }

# List sources explicitly (no GLOB): IDF evaluates this file in script mode during
# requirement expansion, where file(GLOB ... CONFIGURE_DEPENDS) is illegal, and a
# plain GLOB silently misses files added after the last reconfigure. Add new .c
# files here.
idf_component_register(SRCS "app_bt.c" "mux.c" "spinel.c"
                    PRIV_REQUIRES bt nvs_flash esp_driver_usb_serial_jtag
                                  openthread esp_event vfs
                    INCLUDE_DIRS ".")

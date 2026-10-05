# Matter2Mqtt

TODO

Contents

- 3dprint: A mount for an ESP32 C6
- fw\_btmux: WIP, a firmware for ESP32 C6 multiplexing bluetooth and spinel over a USB channel. The idea of this fw is that the docker container running the matter2mqtt bridge needs no bluetooth proxying from the host, and can run the entire commissioning process from the same ESP32 device.
- fw\_simple: A copy of the ot\_rcp ESP32 sample project; just a dumb Thread radio co-processor. Matter2Mqtt can use this to control a Thread network, but isn't enough to commission new devices (which requires bluetooth)
- mqtt\_bridge: Bridge between Matter server and MQTT, ran inside the docker container
- docker/s6-overlay: services definition running inside the docker container
- docker/scripts: helpers tools for the container


## Building a new network

1. Flash the firmware; use fw\_simple for now, some day fw\_btmux will work. Usually this will look something like
    - `source "/home/batman/.espressif/tools/activate_idf_v6.1.sh"`
    - `idf.py set-target esp32c6`
    - `idf.py build flash`
    - `idf.py monitor` to verify the firmware comes up and logs something like "OpenThread enter mainloop"

2. Build a docker image with `make rebuild`


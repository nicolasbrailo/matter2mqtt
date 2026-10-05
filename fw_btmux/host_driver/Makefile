CC      ?= cc
CFLAGS  ?= -O2 -Wall -Wextra -std=c11
DEV     ?= /dev/ttyACM0

host: host.c
	$(CC) $(CFLAGS) -o $@ host.c

# `make run` or `make run DEV=/dev/ttyACM1`
run: host
	./host $(DEV)

clean:
	rm -f host

.PHONY: run clean

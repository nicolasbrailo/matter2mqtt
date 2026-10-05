// Host-side driver for the mt2m USB mux.
//
// Opens the ESP32-C6 CDC device and demuxes its channels: log frames are printed,
// the BT channel is bridged to a kernel virtual HCI controller via /dev/vhci (so
// BlueZ sees a new hciN adapter backed by the ESP32 controller), and the Spinel
// channel is bridged to a PTY whose slave path (/dev/pts/N) is printed -- point
// ot-daemon/otbr-agent at spinel+hdlc+uart:///dev/pts/N to run Thread off the same
// device. A PING is sent once a second for link liveness. Run as root (needs
// CAP_NET_ADMIN for /dev/vhci); without it the BT channel is hex-dumped instead.
// Either bridge can be absent independently -- the corresponding channel is then
// hex-dumped and everything else still works.
//
// Wire frame (must match app_bt.c mux_tx):
//   [type : u8]                  // enum mux_msg_type, 1 byte
//   [len  : u16 little-endian]   // 2 bytes
//   [payload : len bytes]
//   [tail : "/mt2mqtt\0"]        // 9 bytes, sizeof(MSG_TAIL) incl. NUL
//
// Framing & resync -- why the tail exists:
//   The USB link is a raw byte stream with no inherent message boundaries, and
//   we can join it at any point (the device boots and starts sending before the
//   host opens the port, a byte can be dropped under backpressure, etc.). When
//   that happens we're "desynced": the byte we think is `type` is really in the
//   middle of someone else's payload, so the `len` we read is garbage and points
//   nowhere useful. The header alone can't get us out of this -- a bogus `len`
//   is indistinguishable from a real one until we check whether a frame actually
//   ends where it claims to.
//
//   The fixed tail "/mt2mqtt\0" is a sync word that marks end-of-frame. It lets
//   us (a) *validate* a candidate frame -- the bytes at offset len must equal
//   the tail, otherwise the header was noise -- and (b) *recover* when desynced
//   by scanning forward for the next tail; the byte after it is, by definition,
//   the start of a real frame. Crucially we resync off the tail rather than off
//   `len`: trusting a bogus length means blocking until that many bytes arrive
//   (minutes on an idle link) only to then reject it. Scanning for the tail
//   bounds recovery to bytes already buffered. See process_frames().
//
//   Two caveats this scheme accepts for now: a payload containing the literal
//   "/mt2mqtt" can cause a false realign (rare; the real fix is a MUX_MSG_SYNC
//   handshake), and we tcflush() the stale backlog on open so we don't begin
//   parsing in the middle of the boot-time burst.
//
// Note: the firmware currently replies to PING on the LOG channel (type 6) with
// "PONG" -- that's a known quirk we'll fix; the reply still proves the link.
//
// Both the C6 and any x86/ARM host are little-endian, so the u32 fields are
// packed/unpacked explicitly LE here (don't rely on struct layout).

#define _XOPEN_SOURCE 600 // posix_openpt/grantpt/unlockpt/ptsname
#define _DEFAULT_SOURCE   // cfmakeraw
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

enum mux_msg_type {
    MUX_MSG_SYNC = 4,
    MUX_MSG_PING = 5,
    MUX_MSG_LOG = 6,
    MUX_MSG_BT = 7,
    MUX_MSG_SPINEL = 8,
};

#define MSG_TAIL "/mt2mqtt"
#define MSG_TAIL_LEN (sizeof(MSG_TAIL)) // 9, includes the NUL, matches fw sizeof()
#define HDR_LEN 3                       // u8 type + u16 len

// /dev/vhci control protocol (kernel drivers/bluetooth/hci_vhci.c): the first
// write creates the virtual controller as [HCI_VENDOR_PKT][opcode], where
// opcode & 0x03 is the device type (0 = primary). The kernel acks by returning a
// [0xff][opcode][id_lo][id_hi] packet on the read queue. After that, reads return
// H4 command/ACL packets and writes take H4 event/ACL packets -- i.e. exactly the
// MUX_MSG_BT payloads, so the bridge is a near-verbatim pump.
#define HCI_VENDOR_PKT      0xff
#define VHCI_DEV_PRIMARY    0x00

// HCI/H4 packets can be large once ACL flows (bounded by the controller's LE ACL
// buffer, 517 + headers); size the vhci read buffer generously.
#define VHCI_PKT_MAX 2048

static int g_vhci_fd = -1; // /dev/vhci bridge; -1 => not bridging (hexdump BT instead)

// Spinel channel <-> a PTY: ot-daemon/otbr-agent open the slave (/dev/pts/N) and
// talk HDLC-Spinel, which we pump to/from the ESP's MUX_MSG_SPINEL channel. The
// slave-holder fd stays open purely so the master never reports POLLHUP (which
// would busy-spin our poll loop) while no OT tool is attached.
static int g_pty_fd = -1;       // PTY master; -1 => not bridging Spinel (hexdump instead)
static int g_pty_slave_fd = -1; // held-open slave; keeps the master from hanging up

// Max Spinel bytes per mux frame on host->fw. Spinel is a byte stream, so we split
// freely; this must stay <= the firmware's MUX_TX_MAX_PAYLOAD (528) or the reliable
// RX path rejects the frame.
#define SPINEL_CHUNK_MAX 512

#define ACC_SZ 4096 // reassembly buffer; bigger than any plausible frame

static void put_u16_le(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

static uint16_t get_u16_le(const uint8_t *p) {
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static int open_device(const char *path) {
    int fd = open(path, O_RDWR | O_NOCTTY);
    if (fd < 0) {
        perror("open");
        return -1;
    }
    struct termios tio;
    if (tcgetattr(fd, &tio) != 0) {
        perror("tcgetattr");
        close(fd);
        return -1;
    }
    cfmakeraw(&tio);     // raw mode in-code, so no `stty` dance needed
    tio.c_cc[VMIN] = 0;  // read() returns whatever is available...
    tio.c_cc[VTIME] = 0; // ...immediately; we gate on poll() instead
    cfsetispeed(&tio, B115200); // baud is ignored over USB CDC, set anything sane
    cfsetospeed(&tio, B115200);
    if (tcsetattr(fd, TCSANOW, &tio) != 0) {
        perror("tcsetattr");
        close(fd);
        return -1;
    }
    // Discard whatever the kernel buffered from the device before we opened it
    // (boot logs, replies to a previous run). Without this we'd start reading
    // mid-frame and have to resync from a stale backlog.
    tcflush(fd, TCIFLUSH);
    return fd;
}

static int write_all(int fd, const void *buf, size_t len) {
    const uint8_t *p = buf;
    while (len > 0) {
        ssize_t n = write(fd, p, len);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            perror("write");
            return -1;
        }
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

static int send_frame(int fd, uint8_t type, const void *payload, uint16_t len) {
    uint8_t hdr[HDR_LEN];
    hdr[0] = type;
    put_u16_le(hdr + 1, len);
    if (write_all(fd, hdr, HDR_LEN) != 0)
        return -1;
    if (len > 0 && write_all(fd, payload, len) != 0)
        return -1;
    if (write_all(fd, MSG_TAIL, MSG_TAIL_LEN) != 0)
        return -1;
    return 0;
}

// Open /dev/vhci and create a primary virtual controller. Returns the fd, or -1
// on failure (e.g. not root / CAP_NET_ADMIN missing) -- the caller falls back to
// hex-dumping BT frames so the tool still works for log viewing without it.
static int vhci_open(void) {
    int fd = open("/dev/vhci", O_RDWR);
    if (fd < 0) {
        perror("open /dev/vhci (need root/CAP_NET_ADMIN)");
        return -1;
    }
    uint8_t create[2] = {HCI_VENDOR_PKT, VHCI_DEV_PRIMARY};
    if (write_all(fd, create, sizeof(create)) != 0) {
        fprintf(stderr, "vhci: create-device write failed\n");
        close(fd);
        return -1;
    }
    return fd;
}

// Seconds since the first call -- a small relative timestamp so stdout (TX/RX)
// and stderr ([resync]) lines can be correlated when captured together.
static double now_ts(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    double t = (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
    static double t0 = -1.0;
    if (t0 < 0.0)
        t0 = t;
    return t - t0;
}

// Open a PTY pair for the Spinel channel. Returns the master fd (or -1 on failure
// -- Spinel bridging is then disabled and SPINEL frames are hex-dumped, like BT
// without /dev/vhci). Sets the slave to raw mode so OT's HDLC framing bytes pass
// through untouched, and holds the slave open (g_pty_slave_fd) so the master never
// sees POLLHUP before an OT tool attaches. Prints the slave path to point OT at.
static int pty_open(void) {
    int m = posix_openpt(O_RDWR | O_NOCTTY);
    if (m < 0) {
        perror("posix_openpt");
        return -1;
    }
    if (grantpt(m) != 0 || unlockpt(m) != 0) {
        perror("grantpt/unlockpt");
        close(m);
        return -1;
    }
    // Raw line discipline (no CR/LF translation, XON/XOFF, or echo) so HDLC bytes
    // aren't mangled. Set on the master fd, which configures the slave terminal;
    // OT reopens the slave with its own termios, this is just a sane default.
    struct termios tio;
    if (tcgetattr(m, &tio) == 0) {
        cfmakeraw(&tio);
        tcsetattr(m, TCSANOW, &tio);
    }
    const char *slave = ptsname(m);
    if (slave == NULL) {
        perror("ptsname");
        close(m);
        return -1;
    }
    g_pty_slave_fd = open(slave, O_RDWR | O_NOCTTY);
    if (g_pty_slave_fd < 0) {
        perror("open pts slave");
        close(m);
        return -1;
    }
    printf("[%8.3f] spinel PTY ready -- point OT at: spinel+hdlc+uart://%s\n",
           now_ts(), slave);
    fflush(stdout);
    return m;
}

// Read and discard whatever the device dumps right after we open the port, until
// the link goes quiet. tcflush() can't handle this: the kernel only starts
// draining the device IN endpoint once the port is opened, so the device's
// pre-connection backlog arrives *after* the flush. Draining to idle here means
// we start parsing on a clean boundary instead of mid-backlog.
//
// TODO: replace this with a proper MUX_MSG_SYNC handshake -- send SYNC and
// discard everything until the device's SYNC reply -- for a guaranteed clean
// boundary that also handles mid-session reconnects. Done later.
static void drain_on_open(int fd) {
    uint8_t scratch[256];
    double start = now_ts();
    for (;;) {
        struct pollfd pfd = {.fd = fd, .events = POLLIN};
        int r = poll(&pfd, 1, 50); // "quiet" == no bytes for 50ms
        if (r <= 0)
            break;                 // timeout/error => backlog drained
        if ((pfd.revents & POLLIN) && read(fd, scratch, sizeof(scratch)) <= 0)
            break;
        if (now_ts() - start > 0.200)
            break; // hard cap, in case the device never goes quiet
    }
}

// Scan for the next MSG_TAIL marker at or after `start`. Returns the offset of
// the byte *after* the tail (i.e. the start of the next frame), or SIZE_MAX if
// no complete tail is present in [start, acc_len).
static size_t find_after_tail(const uint8_t *acc, size_t acc_len, size_t start) {
    if (acc_len < MSG_TAIL_LEN)
        return SIZE_MAX;
    for (size_t i = start; i + MSG_TAIL_LEN <= acc_len; i++) {
        if (memcmp(acc + i, MSG_TAIL, MSG_TAIL_LEN) == 0)
            return i + MSG_TAIL_LEN;
    }
    return SIZE_MAX;
}

// Pull as many complete frames out of the accumulator as are available.
// Returns the number of bytes consumed from the front.
//
// Resync uses the tail as a sync word rather than trusting `len`: a desynced
// read can produce a plausible-but-bogus length, and blindly waiting to fill
// `need` bytes before rejecting it stalls for as long as it takes that many
// bytes to trickle in (minutes, on an idle link). Instead, whenever a frame
// fails to validate, we jump to just after the next real tail in the buffer.
static size_t process_frames(uint8_t *acc, size_t acc_len) {
    size_t off = 0;
    while (acc_len - off >= HDR_LEN) {
        uint8_t type = acc[off];
        uint16_t len = get_u16_le(acc + off + 1);
        size_t need = HDR_LEN + len + MSG_TAIL_LEN;

        if (len <= ACC_SZ && acc_len - off >= need) {
            // Whole candidate frame is here -- validate its tail.
            const uint8_t *payload = acc + off + HDR_LEN;
            if (memcmp(payload + len, MSG_TAIL, MSG_TAIL_LEN) == 0) {
                if (type == MUX_MSG_BT) {
                    if (g_vhci_fd >= 0) {
                        // Forward the whole H4 event/ACL packet up to the kernel host.
                        if (write_all(g_vhci_fd, payload, len) != 0)
                            fprintf(stderr, "[%8.3f] vhci write failed\n", now_ts());
                    } else {
                        // No vhci bridge -- hexdump (HCI is binary).
                        printf("[%8.3f] RX  type=%u len=%u H4=", now_ts(),
                               (unsigned)type, (unsigned)len);
                        for (unsigned i = 0; i < len; i++)
                            printf("%02x ", payload[i]);
                        printf("\n");
                    }
                } else if (type == MUX_MSG_SPINEL) {
                    if (g_pty_fd >= 0) {
                        // Opaque HDLC-Spinel bytes -> the PTY for the OT host tool.
                        if (write_all(g_pty_fd, payload, len) != 0)
                            fprintf(stderr, "[%8.3f] pty write failed\n", now_ts());
                    } else {
                        // No PTY bridge -- hexdump (Spinel is binary).
                        printf("[%8.3f] RX  type=%u len=%u SPINEL=", now_ts(),
                               (unsigned)type, (unsigned)len);
                        for (unsigned i = 0; i < len; i++)
                            printf("%02x ", payload[i]);
                        printf("\n");
                    }
                } else {
                    printf("[%8.3f] RX  type=%u len=%u payload=\"%.*s\"\n", now_ts(),
                           (unsigned)type, (unsigned)len, (int)len, payload);
                }
                fflush(stdout);
                off += need;
                continue;
            }
            // Bad tail => not a real frame boundary; fall through to resync.
        } else if (len <= ACC_SZ) {
            // Plausible len but the frame isn't fully here yet. It's either a
            // genuine in-progress frame (wait) or we're desynced on a bogus len
            // (resync). A real tail already sitting in the buffer means bogus.
            size_t r = find_after_tail(acc, acc_len, off + 1);
            if (r == SIZE_MAX)
                break; // no tail yet -> genuinely incomplete, wait for more
            fprintf(stderr, "[%8.3f] [resync] bogus len=%u, realigned after tail\n",
                    now_ts(), (unsigned)len);
            off = r;
            continue;
        }

        // Desynced (implausible len, or complete frame with a bad tail):
        // realign to just after the next tail in the buffer.
        size_t r = find_after_tail(acc, acc_len, off + 1);
        if (r == SIZE_MAX) {
            // No tail anywhere ahead: drop the garbage but keep a possible
            // partial tail at the very end so it can match on the next read.
            size_t keep = MSG_TAIL_LEN - 1;
            size_t dropped = (acc_len - off > keep) ? (acc_len - off - keep) : 0;
            if (dropped > 0) {
                fprintf(stderr, "[%8.3f] [resync] no tail in %zu bytes, dropped %zu\n",
                        now_ts(), acc_len - off, dropped);
                off = acc_len - keep;
            }
            break;
        }
        fprintf(stderr, "[%8.3f] [resync] bad frame (type=%u len=%u), realigned after tail\n",
                now_ts(), (unsigned)type, (unsigned)len);
        off = r;
    }
    return off;
}

int main(int argc, char **argv) {
    const char *path = (argc > 1) ? argv[1] : "/dev/ttyACM0";

    int fd = open_device(path);
    if (fd < 0)
        return 1;
    printf("[%8.3f] opened %s\n", now_ts(), path);
    fflush(stdout);

    drain_on_open(fd); // swallow the device's post-open backlog before we parse

    // Bridge the BT channel to a kernel virtual HCI controller. If this fails
    // (e.g. not root / no CAP_NET_ADMIN) keep running -- BT frames are hex-dumped
    // instead, so the tool still works for log viewing.
    g_vhci_fd = vhci_open();
    if (g_vhci_fd >= 0)
        printf("[%8.3f] /dev/vhci open -- bridging BT to a new hciN adapter\n", now_ts());
    else
        fprintf(stderr, "[%8.3f] no /dev/vhci -- BT frames will be hex-dumped only\n", now_ts());
    fflush(stdout);

    // Bridge the Spinel channel to a PTY for OT host tools. If this fails, keep
    // running -- SPINEL frames are hex-dumped instead, like BT without /dev/vhci.
    g_pty_fd = pty_open();
    if (g_pty_fd < 0)
        fprintf(stderr, "[%8.3f] no PTY -- SPINEL frames will be hex-dumped only\n", now_ts());
    fflush(stdout);

    uint8_t acc[ACC_SZ];
    size_t acc_len = 0;
    double last_ping = 0.0; // 0 => send one immediately

    for (;;) {
        double now = now_ts();
        if (now - last_ping >= 1.0) {
            last_ping = now; // keep PINGing for link liveness (silent now)
            if (send_frame(fd, MUX_MSG_PING, "PING", sizeof("PING")) != 0)
                break; // sizeof incl NUL -> len 5
        }

        // Poll the device plus whichever bridges came up. Track each fd's slot so
        // the optional ones (vhci, pty) can be absent without shifting indices.
        struct pollfd pfds[3];
        int nfds = 0;
        const int i_dev = nfds;
        pfds[nfds].fd = fd; pfds[nfds].events = POLLIN; pfds[nfds].revents = 0; nfds++;
        const int i_vhci = (g_vhci_fd >= 0) ? nfds : -1;
        if (i_vhci >= 0) { pfds[nfds].fd = g_vhci_fd; pfds[nfds].events = POLLIN; pfds[nfds].revents = 0; nfds++; }
        const int i_pty = (g_pty_fd >= 0) ? nfds : -1;
        if (i_pty >= 0) { pfds[nfds].fd = g_pty_fd; pfds[nfds].events = POLLIN; pfds[nfds].revents = 0; nfds++; }

        int r = poll(pfds, nfds, 200);
        if (r < 0) {
            if (errno == EINTR)
                continue;
            perror("poll");
            break;
        }
        if (r == 0)
            continue;

        // Kernel host -> controller: each read returns one whole H4 packet; mux
        // it straight to the ESP on the BT channel.
        if (i_vhci >= 0 && (pfds[i_vhci].revents & POLLIN)) {
            uint8_t pkt[VHCI_PKT_MAX];
            ssize_t n = read(g_vhci_fd, pkt, sizeof(pkt));
            if (n > 0) {
                if (pkt[0] == HCI_VENDOR_PKT) {
                    // device-created ack: [0xff][opcode][id_lo][id_hi]
                    if (n >= 4)
                        printf("[%8.3f] vhci: created hci%u\n", now_ts(),
                               (unsigned)(pkt[2] | (pkt[3] << 8)));
                    fflush(stdout);
                } else if (send_frame(fd, MUX_MSG_BT, pkt, (uint16_t)n) != 0) {
                    break;
                }
            } else if (n < 0 && errno != EINTR && errno != EAGAIN) {
                perror("read vhci");
                break;
            }
        }

        // OT host tool -> RCP: read HDLC-Spinel bytes off the PTY and mux them to
        // the ESP. Spinel is a byte stream, so read up to one mux frame's worth
        // (<= the firmware's payload cap) and let the next iteration take the rest.
        if (i_pty >= 0 && (pfds[i_pty].revents & POLLIN)) {
            uint8_t buf[SPINEL_CHUNK_MAX];
            ssize_t n = read(g_pty_fd, buf, sizeof(buf));
            if (n > 0) {
                if (send_frame(fd, MUX_MSG_SPINEL, buf, (uint16_t)n) != 0)
                    break;
            } else if (n < 0 && errno != EINTR && errno != EAGAIN && errno != EIO) {
                // EIO just means no OT tool has the slave open yet -- ignore.
                perror("read pty");
                break;
            }
        }

        // ESP -> us: accumulate mux bytes and dispatch whole frames (BT frames go
        // out to /dev/vhci, SPINEL to the PTY, inside process_frames).
        if (pfds[i_dev].revents & POLLIN) {
            if (acc_len == ACC_SZ) // full and no frame extractable => give up resync
                acc_len = 0;
            ssize_t n = read(fd, acc + acc_len, ACC_SZ - acc_len);
            if (n < 0) {
                if (errno == EINTR || errno == EAGAIN)
                    continue;
                perror("read");
                break;
            }
            if (n == 0)
                continue;
            acc_len += (size_t)n;

            size_t consumed = process_frames(acc, acc_len);
            if (consumed > 0) {
                memmove(acc, acc + consumed, acc_len - consumed);
                acc_len -= consumed;
            }
        }
    }

    if (g_vhci_fd >= 0)
        close(g_vhci_fd);
    if (g_pty_fd >= 0)
        close(g_pty_fd);
    if (g_pty_slave_fd >= 0)
        close(g_pty_slave_fd);
    close(fd);
    return 1;
}

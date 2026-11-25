"""
diag.py - Print the raw USB traffic with the Mini-X2. NEVER turns X-rays on.

Run (close Amptek's program first):
    py diag.py > diag_out.txt
Then send diag_out.txt to Claude.
"""
import time

import minix2
from minix2 import build_packet

PID_NAMES = {(0x80, 0x01): "status", (0x80, 0x02): "Mini-X2 status", (0x82, 0x0D): "tube table", (0x82, 0x07): "text readback", (0xFF, 0x00): "ACK OK"}


def show(label, data):
    print("  %s %3d bytes: %s" % (label, len(data), data.hex(" ")))


def read_all(t, seconds):
    """Read everything that arrives for some time and print it."""
    end = time.monotonic() + seconds
    buf = b""
    while time.monotonic() < end:
        try:
            chunk = t.read(timeout_ms=100)
        except Exception:
            continue
        show("IN ", chunk)
        buf += chunk
    # split into packets
    while len(buf) >= 8 and buf[0] == 0xF5 and buf[1] == 0xFA:
        n = (buf[4] << 8) | buf[5]
        pkt, buf = buf[:n + 8], buf[n + 8:]
        name = PID_NAMES.get((pkt[2], pkt[3]), "?")
        print("  -> packet PID %02X %02X (%s), data %d bytes" % (pkt[2], pkt[3], name, n))
        if n:
            data = pkt[6:6 + n]
            print("     data hex  :", data.hex(" "))
            print("     data ascii:", "".join(chr(b) if 32 <= b < 127 else "." for b in data))
    if buf:
        print("  -> leftover bytes:", buf.hex(" "))


def send(t, label, pid1, pid2, payload=b""):
    pkt = build_packet(pid1, pid2, payload)
    print("\n== %s" % label)
    show("OUT", pkt)
    t.write(pkt)
    read_all(t, 0.6)


def main():
    t = minix2.UsbTransport()
    print("Found Mini-X2. OUT endpoint 0x%02X (max %d), IN endpoint 0x%02X (max %d)" % (
        t.ep_out.bEndpointAddress, t.ep_out.wMaxPacketSize,
        t.ep_in.bEndpointAddress, t.ep_in.wMaxPacketSize))
    print("\n== Waiting 1 s for data the device sends by itself")
    read_all(t, 1.0)
    for i in range(3):
        send(t, "Status request #%d" % (i + 1), 0x01, 0x01)
        time.sleep(0.3)
    send(t, "Text readback HVSE=?;CUSE=?;", 0x20, 0x03, b"HVSE=?;CUSE=?;")
    send(t, "Request tube & interlock table", 0x03, 0x0B)
    t.close()
    print("\nDone. No X-ray command was sent.")


if __name__ == "__main__":
    main()

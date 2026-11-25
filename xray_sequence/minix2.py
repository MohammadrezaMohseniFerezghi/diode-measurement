"""
minix2.py - Talk to an Amptek Mini-X2 X-ray tube over USB from Python.

How the protocol was found
--------------------------
These details were read out of Amptek's own control program (MiniX2.exe,
from MINI-X2_Controller_Setup.exe) and the Mini-X2 User Manual Rev B2.

  USB device : VID 0x10C4, PID 0x842A, Windows driver = WinUSB
  Packet     : F5 FA PID1 PID2 LEN_HI LEN_LO DATA... CHK_HI CHK_LO
               CHK = two's complement of the 16-bit sum of all bytes before it
  Status     : send PID 01 01 (no data)       -> reply PID 80 02 + 64 data bytes
  Text cmd   : send PID 20 02 + ASCII text     -> reply PID FF 00 (ACK, OK)
  Readback   : send PID 20 03 + "HVSE=?;..."   -> reply PID 82 07 + ASCII text
  Tube ON    : text cmd "HVSE=<kV>;CUSE=<uA>;" (what Amptek's HV ON button sends)
  Tube OFF   : Amptek's HV OFF button sends "HVSE=0.0;CUSE=0.0;", but tube S/N 136
               answers "bad parameter". So hv_off() tries "HVSE=OFF;", "HVSE=0;" and
               "HVSE=0.0;CUSE=0.0;" in turn, and the GUI checks the status bit HV_EN
               to be sure the tube is really off.

Safety facts from the manual (section 10.4)
  * The program must ask for status about once per second. If 3 s pass with
    no status request, the Mini-X2 turns the tube off by itself.
  * If the USB cable is unplugged, the tube turns off at once.

Install on Windows:  py -m pip install pyusb libusb-package
Close Amptek's Mini-X2 program first. Only one program can use the USB port.
"""

import time

VERSION = "2026-11-22 v5"

VID = 0x10C4
PID = 0x842A

# Limits for this tube (50 kV, 10 W). Change only if you know your tube.
KV_MIN, KV_MAX = 10.0, 50.0      # kV
UA_MIN, UA_MAX = 10.0, 50.0      # uA  (lab choice; tube spec is 5 to 200)
P_MAX_MW = 10000.0               # mW  (10 W).  P[mW] = kV * uA

# Status code meanings (from MiniX2.exe). Code 0 means "all OK".
STATUS_TEXT = {
    0: "Interlock CLOSED (OK)",
    1: "Interlock OPEN",
    2: "Interlock SHORTED",
    3: "Supply voltage too LOW",
    4: "Supply voltage too HIGH",
    5: "Tube HV monitor LOW",
    6: "Tube HV monitor HIGH",
    7: "Tube current monitor LOW",
    8: "Tube current monitor HIGH",
    9: "USB disconnected",
    10: "USB stopped talking",
    11: "Warmup done",
}

ACK_TEXT = {
    0x00: "OK", 0x01: "sync error", 0x02: "PID error", 0x03: "length error",
    0x04: "checksum error", 0x05: "bad parameter", 0x07: "unknown command",
    0x0D: "busy",
}


class MiniX2Error(Exception):
    pass


# ----------------------------------------------------------------------------
# Packet helpers (pure functions, easy to test)
# ----------------------------------------------------------------------------
def checksum(data: bytes) -> int:
    return (-sum(data)) & 0xFFFF


def build_packet(pid1: int, pid2: int, payload: bytes = b"") -> bytes:
    n = len(payload)
    head = bytes([0xF5, 0xFA, pid1, pid2, (n >> 8) & 0xFF, n & 0xFF]) + payload
    c = checksum(head)
    return head + bytes([c >> 8, c & 0xFF])


def parse_packet(buf: bytes):
    """Return (pid1, pid2, payload, total_length) or None if not complete yet."""
    if len(buf) < 8:
        return None
    if buf[0] != 0xF5 or buf[1] != 0xFA:
        raise MiniX2Error("bad sync bytes: %s" % buf[:8].hex(" "))
    n = (buf[4] << 8) | buf[5]
    total = n + 8
    if len(buf) < total:
        return None
    chk = (buf[total - 2] << 8) | buf[total - 1]
    if (sum(buf[:total - 2]) + chk) & 0xFFFF != 0:
        raise MiniX2Error("bad checksum in reply")
    return buf[2], buf[3], bytes(buf[6:6 + n]), total


def fmt_cmd(kv: float, ua: float) -> str:
    # Same format as Amptek's program: one decimal place.
    return "HVSE=%0.1f;CUSE=%0.1f;" % (kv, ua)


def check_limits(kv: float, ua: float):
    """Raise ValueError if the setting is outside the safe limits."""
    if not (KV_MIN <= kv <= KV_MAX):
        raise ValueError("kV must be %g to %g" % (KV_MIN, KV_MAX))
    if not (UA_MIN <= ua <= UA_MAX):
        raise ValueError("uA must be %g to %g" % (UA_MIN, UA_MAX))
    if kv * ua > P_MAX_MW:
        raise ValueError("Power %g mW is above %g mW" % (kv * ua, P_MAX_MW))


def parse_status(d: bytes) -> dict:
    """Decode the 64-byte status data (layout read from MiniX2.exe)."""
    if len(d) < 0x1E:
        raise MiniX2Error("status too short (%d bytes)" % len(d))

    def w12(lo, hi):
        return ((d[hi] & 0x0F) << 8) | d[lo]

    def u32(off):
        return d[off] | (d[off + 1] << 8) | (d[off + 2] << 16) | ((d[off + 3] & 0x7F) << 24)

    hv_raw = w12(0x06, 0x07) * 0.001          # V at the HV monitor pin
    i_raw = w12(0x08, 0x09) * 0.001           # V at the current monitor pin
    hv_gain = (d[0x1A] * 256 + d[0x1B]) / 256.0
    i_gain = (d[0x1C] * 256 + d[0x1D]) / 256.0
    temp = d[0x11] - 256 if d[0x11] > 127 else d[0x11]
    code = d[0x10] & 0x0F
    hv_kv = hv_raw * hv_gain
    ua = i_raw * i_gain
    return {
        "serial": u32(0x00),
        "supply_v": w12(0x0E, 0x0F) * 0.00326,
        "tube_supply_v": w12(0x0C, 0x0D) * 0.00326,
        "hv_kv": hv_kv,
        "current_ua": ua,
        "power_mw": hv_kv * ua,
        "interlock_ma": w12(0x0A, 0x0B) * 0.01244,
        "board_temp_c": temp,
        "hv_enabled": bool(d[0x10] & 0x80),
        "status_code": code,
        "status_text": STATUS_TEXT.get(code, "code %d" % code),
        "interlock_ok": code not in (1, 2),
        "prev_code": d[0x12] & 0x0F,
        "runtime_s": u32(0x16),
    }


def parse_readback(text: str) -> dict:
    """'HVSE=15.0;CUSE=15.0;' -> {'HVSE': '15.0', 'CUSE': '15.0'}"""
    out = {}
    for part in text.replace("\x00", "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().upper()] = v.strip()
    return out


# ----------------------------------------------------------------------------
# USB transport
# ----------------------------------------------------------------------------
class UsbTransport:
    def __init__(self, index: int = 0):
        import usb.core
        import usb.util
        self.usb = usb
        backend = None
        try:
            import libusb_package           # bundles libusb-1.0.dll on Windows
            backend = libusb_package.get_libusb1_backend()
        except ImportError:
            pass
        devs = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID,
                                  backend=backend))
        if not devs:
            raise MiniX2Error("Mini-X2 not found on USB (VID 10C4, PID 842A). "
                              "Is it plugged in and powered? Is Amptek's program closed?")
        self.dev = devs[index]
        try:
            self.dev.set_configuration()
        except Exception:
            pass                            # WinUSB: often already configured
        cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]
        usb.util.claim_interface(self.dev, intf.bInterfaceNumber)
        self.intf_num = intf.bInterfaceNumber

        def find(direction):
            return usb.util.find_descriptor(
                intf, custom_match=lambda e:
                usb.util.endpoint_direction(e.bEndpointAddress) == direction and
                usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)

        self.ep_out = find(usb.util.ENDPOINT_OUT)
        self.ep_in = find(usb.util.ENDPOINT_IN)
        if self.ep_out is None or self.ep_in is None:
            raise MiniX2Error("USB bulk endpoints not found")

    def write(self, data: bytes, timeout_ms: int = 1000):
        self.ep_out.write(data, timeout=timeout_ms)
        if len(data) % self.ep_out.wMaxPacketSize == 0:
            self.ep_out.write(b"", timeout=timeout_ms)   # zero-length packet ends the transfer

    def read(self, timeout_ms: int = 1000) -> bytes:
        return bytes(self.ep_in.read(32768, timeout=timeout_ms))

    def flush(self):
        for _ in range(10):
            try:
                self.read(timeout_ms=50)
            except Exception:
                return

    def close(self):
        try:
            self.usb.util.release_interface(self.dev, self.intf_num)
        finally:
            self.usb.util.dispose_resources(self.dev)


# ----------------------------------------------------------------------------
# Device class
# ----------------------------------------------------------------------------
class MiniX2:
    def __init__(self, transport=None):
        if transport is not None:
            self.t = transport
            self.t.flush()
            return
        # Real USB: open each Amptek device until one answers like a Mini-X2.
        errors = []
        for index in range(8):
            try:
                t = UsbTransport(index)
            except IndexError:
                break
            self.t = t
            t.flush()
            try:
                self.status()
                return                       # found the Mini-X2
            except MiniX2Error as e:
                errors.append("device %d: %s" % (index + 1, e))
                t.close()
        raise MiniX2Error("No Mini-X2 found. " + " ".join(errors))

    # -- low level --
    def transact(self, pid1, pid2, payload=b"", timeout_ms=1000):
        try:
            self.t.write(build_packet(pid1, pid2, payload))
        except Exception as e:
            raise MiniX2Error("USB write failed (%s)" % e)
        buf = b""
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            left = int((deadline - time.monotonic()) * 1000)
            if left <= 0:
                raise MiniX2Error("no reply from Mini-X2 (timeout)")
            try:
                buf += self.t.read(timeout_ms=max(left, 10))
            except Exception as e:
                raise MiniX2Error("no reply from Mini-X2 (%s)" % e)
            p = parse_packet(buf)
            if p is not None:
                return p[0], p[1], p[2]

    def text_cmd(self, text: str):
        p1, p2, data = self.transact(0x20, 0x02, text.encode("ascii"))
        if p1 != 0xFF:
            raise MiniX2Error("unexpected reply %02X %02X to '%s'" % (p1, p2, text))
        if p2 != 0x00:
            raise MiniX2Error("Mini-X2 rejected '%s': %s (ACK %02X) %s" % (
                text, ACK_TEXT.get(p2, "error"), p2, data.decode("ascii", "replace")))

    def readback(self, text: str = "HVSE=?;CUSE=?;") -> dict:
        p1, p2, data = self.transact(0x20, 0x03, text.encode("ascii"))
        if (p1, p2) != (0x82, 0x07):
            raise MiniX2Error("unexpected readback reply %02X %02X" % (p1, p2))
        return parse_readback(data.decode("ascii", "replace"))

    # -- high level --
    def status(self) -> dict:
        p1, p2, data = self.transact(0x01, 0x01)
        # The Mini-X2 answers 80 02 (seen on S/N 136). Amptek detectors
        # (DP5, X-123, PX5) answer 80 01: never send HV commands to them.
        if (p1, p2) == (0x80, 0x01):
            raise MiniX2Error("This USB device answered like an Amptek detector "
                              "(DP5 / X-123 / PX5), not like a Mini-X2.")
        if (p1, p2) != (0x80, 0x02):
            raise MiniX2Error("unexpected status reply %02X %02X" % (p1, p2))
        return parse_status(data)

    def hv_on(self, kv: float, ua: float):
        """Turn X-rays ON (or change kV/uA while on). Checks limits first."""
        check_limits(kv, ua)
        self.text_cmd(fmt_cmd(kv, ua))

    OFF_COMMANDS = ("HVSE=OFF;", "HVSE=0;", "HVSE=0.0;CUSE=0.0;")

    def hv_off(self) -> str:
        """Turn X-rays OFF. Tries each OFF command until the tube accepts one.
        Returns the accepted command. Raises MiniX2Error if all are rejected.
        (The caller must still check the status: hv_enabled must be False.)"""
        errors = []
        for cmd in self.OFF_COMMANDS:
            try:
                self.text_cmd(cmd)
                return cmd
            except MiniX2Error as e:
                errors.append(str(e))
        raise MiniX2Error("all OFF commands rejected: " + " | ".join(errors))

    def close(self):
        try:
            self.hv_off()
        except Exception:
            pass
        self.t.close()


# ----------------------------------------------------------------------------
# Simulator, for testing without the real tube:  MiniX2(FakeTransport())
# ----------------------------------------------------------------------------
class FakeTransport:
    def __init__(self, serial=136):
        self.serial = serial
        self.kv = 0.0
        self.ua = 0.0
        self.on = False
        self.interlock_code = 0
        self.stuck_on = False                # test hook: HV ignores OFF
        self.accept_off = ("OFF",)           # OFF values the fake tube ACKs as OK
        self.off_when_rejected = True        # HV goes off even if the OFF text is rejected
        self.heartbeat_off = False           # test hook: emulate 3 s heartbeat timeout
        self.last_status_req = time.monotonic()
        self.out = []
        self.sent = []                       # log of every packet written

    def _status_bytes(self):
        d = bytearray(64)
        s = self.serial
        d[0:4] = bytes([s & 0xFF, (s >> 8) & 0xFF, (s >> 16) & 0xFF, (s >> 24) & 0x7F])
        hv_gain, i_gain = 12.5, 50.0         # kV per V, uA per V
        hv_cnt = int(round(self.kv / hv_gain / 0.001)) if self.on else 0
        i_cnt = int(round(self.ua / i_gain / 0.001)) if self.on else 0
        d[6], d[7] = hv_cnt & 0xFF, (hv_cnt >> 8) & 0x0F
        d[8], d[9] = i_cnt & 0xFF, (i_cnt >> 8) & 0x0F
        sup = int(12.0 / 0.00326)
        d[0x0C], d[0x0D] = sup & 0xFF, sup >> 8
        d[0x0E], d[0x0F] = sup & 0xFF, sup >> 8
        il = int(1.0 / 0.01244)
        d[0x0A], d[0x0B] = il & 0xFF, il >> 8
        d[0x10] = (0x80 if self.on else 0) | (self.interlock_code & 0x0F)
        d[0x11] = 27
        d[0x1A], d[0x1B] = int(hv_gain), int((hv_gain % 1) * 256)
        d[0x1C], d[0x1D] = int(i_gain), int((i_gain % 1) * 256)
        return bytes(d)

    def write(self, data, timeout_ms=1000):
        self.sent.append(bytes(data))
        p1, p2, payload, _ = parse_packet(bytes(data))
        if (p1, p2) == (0x01, 0x01):
            if self.heartbeat_off and time.monotonic() - self.last_status_req > 3.0:
                self.on, self.stuck_on = False, False   # tube's own 3 s safety timer
            self.last_status_req = time.monotonic()
            self.out.append(build_packet(0x80, 0x02, self._status_bytes()))
        elif (p1, p2) == (0x20, 0x02):
            # Like the real tube: commands run in order, stop at the first bad one.
            for part in payload.decode().split(";"):
                if "=" not in part:
                    continue
                k, v = part.split("=", 1)
                is_off = k == "HVSE" and v.upper() in ("OFF", "0", "0.0", "0.00")
                if is_off:
                    if self.off_when_rejected or v in self.accept_off:
                        self.on, self.kv = False, 0.0
                    if self.stuck_on:
                        self.on = True
                    if v not in self.accept_off:
                        self.out.append(build_packet(0xFF, 0x05, part.encode()))
                        return
                    continue
                v = float(v)
                if (k == "CUSE" and not (5.0 <= v <= 200.0)) or \
                   (k == "HVSE" and not (5.0 <= v <= 50.0)):
                    self.out.append(build_packet(0xFF, 0x05, part.encode()))
                    return
                if k == "HVSE":
                    self.kv = v
                    self.on = self.interlock_code == 0
                else:
                    self.ua = v
            self.out.append(build_packet(0xFF, 0x00))
        elif (p1, p2) == (0x20, 0x03):
            txt = "HVSE=%0.1f;CUSE=%0.1f;" % (self.kv, self.ua)
            self.out.append(build_packet(0x82, 0x07, txt.encode()))
        else:
            self.out.append(build_packet(0xFF, 0x07))

    def read(self, timeout_ms=1000):
        if not self.out:
            time.sleep(timeout_ms / 1000.0)
            raise TimeoutError("fake timeout")
        return self.out.pop(0)

    def flush(self):
        self.out.clear()

    def close(self):
        pass

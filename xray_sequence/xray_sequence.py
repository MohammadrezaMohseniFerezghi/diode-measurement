"""
xray_sequence.py - Run one SMU + X-ray sequence:
Diode Measurement app (SMU, through its RPC tab) + Mini-X2 X-ray tube.

Timeline (t = 0 when the SMU has reached V, i.e. Diode Measurement is in
its "continuous" state):

    t1 = SMU only time, before X-ray ON and again after X-ray OFF
    t2 = polarity switch time, counted from X-ray ON (t2 < t3)
    t3 = X-ray ON time
    (The GUI shows plain labels, not the names t1, t2, t3.)

    t = 0             SMU at V, I vs t is being recorded
    t = t1            X-rays ON (kV, uA)
    t = t1 + t2       voltage V -> -V   (polarity switch, a short ramp)
    t = t1 + t3       X-rays OFF
    t = t1 + t3 + t1  SMU stopped (Diode Measurement ramps to 0 V, output off)

Example: V = 50, t1 = 30, t2 = 98, t3 = 200
    0 s SMU at +50 V | 30 s X-rays ON | 128 s switch to -50 V |
    230 s X-rays OFF | 260 s SMU stop

Rules when something goes wrong:
  * SMU / Diode Measurement fails or stops while X-rays are on -> X-rays OFF.
  * X-ray tube fault (interlock open, tube error, USB lost)   -> SMU stop.
  * ABORT button or Esc key -> X-rays OFF and SMU stop.
  * The Mini-X2 heartbeat runs in its own thread (minix2_gui.Worker), so a slow
    RPC call never delays it. If this program dies, the tube turns itself off
    after 3 s.

Before you run:
  1. Start Diode Measurement. Set the instruments (SMU, ELM ...), the current
     compliance and the output folder as usual. Tick "Continuous Meas.".
  2. In its RPC tab: tick "Enabled" and note the port (default 8000).
  3. Close Amptek's Mini-X2 program.
  4. py xray_sequence.py

Logs: one CSV per run in folder MiniX2_Sequence_Logs (next to this program).
Column unix_time is the same clock as "timestamp[s]" in the Diode Measurement
data file, so the two files line up.
"""
import csv
import datetime
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext

import minix2
from minix2_gui import Worker, MAX_ON_TIME_S

SEQ_VERSION = "2026-11-24 v6"
ALIVE = ("continuous", "ramping")   # SMU on and measuring (ramping = a voltage change is running)
# Extra time per voltage step on top of the waiting time (SMU + ELM reading, and
# Diode Measurement noticing the request). Run of 2026-11-23: 5 steps, waiting time
# 0.5 s, 12.7 s in total -> 2.54 s per step -> about 2.0 s extra. Each finished run
# measures it again and saves it in the settings file, for the next estimate.
READ_OVERHEAD_S = 2.0
SLOW_RPC_S = 0.5                 # warn when one call to Diode Measurement takes longer
PICKUP_S = 1.0                   # Diode Measurement starts a requested switch about this late
                                 # (it checks for requests about once per second; learned)
SWITCH_POLL_S = 0.2              # read the voltage this often while the switch runs
CENTER_EXTRA_S = 20.0            # automatic centering: the tube's own safety timer is
                                 # X-ray ON time + this; the program turns it off on time


def base_folder():
    if getattr(sys, "frozen", False):           # running as .exe
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def ramp_points(v_from, v_to, step):
    """Number of set points Diode Measurement uses for a ramp (comet LinearRange)."""
    dist = abs(v_to - v_from)
    if dist == 0:
        return 0
    step = min(abs(step), dist) if step else dist
    return int(round(dist / step)) + 1


def ramp_values(v_from, v_to, step):
    """The set points of a ramp (same count as ramp_points), for the simulator."""
    n = ramp_points(v_from, v_to, step)
    if n == 0:
        return []
    if n == 1:
        return [float(v_to)]
    return [v_from + (v_to - v_from) * k / (n - 1.0) for k in range(n)]


def switch_model(v, step, wait, overhead=None, pickup=None):
    """Timing of the polarity switch V -> -V, in seconds.

    Diode Measurement sets the ramp points one by one, T = wait + overhead apart
    (overhead = SMU and ELM reading). The first point is +V itself; it is set
    'pickup' seconds after the request. Times counted from the request:
        leave   voltage leaves +V           pickup + T
        middle  middle of the switch        pickup + n*T/2
        arrive  voltage reaches -V          pickup + (n-1)*T
        sweep   time between +V and -V      (n-2)*T
    """
    if overhead is None:
        overhead = READ_OVERHEAD_S
    if pickup is None:
        pickup = PICKUP_S
    n = ramp_points(v, -v, step)
    T = wait + overhead
    return {"n": n, "T": T, "pickup": pickup, "leave": pickup + T,
            "middle": pickup + n * T / 2.0, "arrive": pickup + (n - 1) * T,
            "sweep": max(0, n - 2) * T}


# ----------------------------------------------------------------------------
# Diode Measurement remote control (JSON-RPC over TCP, one request per connection)
# ----------------------------------------------------------------------------
class DiodeError(Exception):
    pass


class DiodeClient:
    def __init__(self, host="localhost", port=8000, timeout=3.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._id = 0
        self._good = None               # (family, address) that worked last time

    def address_text(self):
        if self._good is None:
            return "%s:%d" % (self.host, self.port)
        return "%s:%d" % (self._good[1][0], self._good[1][1])

    def _addresses(self):
        try:
            infos = socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM)
        except OSError as e:
            raise OSError("cannot find host '%s': %s" % (self.host, e))
        # IPv4 first. On Windows "localhost" gives ::1 (IPv6) first. If Diode
        # Measurement listens on 127.0.0.1 only, every call then waits about 2 s
        # for ::1 to fail before it tries 127.0.0.1.
        infos.sort(key=lambda i: 0 if i[0] == socket.AF_INET else 1)
        out = []
        for fam, _, _, _, addr in infos:
            if (fam, addr) not in out:
                out.append((fam, addr))
        return out

    def _try(self, fam, addr):
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(addr)
            return s
        except OSError:
            s.close()
            raise

    def _connect(self):
        if self._good is not None:
            try:
                return self._try(*self._good)
            except OSError:
                self._good = None
        last = OSError("no address for host '%s'" % self.host)
        for fam, addr in self._addresses():
            try:
                s = self._try(fam, addr)
                self._good = (fam, addr)
                return s
            except OSError as e:
                last = e
        raise last

    def call(self, method, params=None):
        self._id += 1
        req = {"jsonrpc": "2.0", "method": method, "id": self._id}
        if params is not None:
            req["params"] = params
        try:
            with self._connect() as s:
                s.sendall(json.dumps(req).encode("utf-8"))
                chunks = []
                while True:
                    c = s.recv(65536)
                    if not c:
                        break
                    chunks.append(c)
        except OSError as e:
            raise DiodeError("Cannot reach Diode Measurement at %s:%d (%s). "
                             "Is its RPC tab 'Enabled'?" % (self.host, self.port, e))
        raw = b"".join(chunks)
        if not raw:
            raise DiodeError("Empty reply from Diode Measurement")
        try:
            rep = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise DiodeError("Bad reply from Diode Measurement: %r" % raw[:200])
        if rep.get("error"):
            err = rep["error"]
            msg = err.get("message", err) if isinstance(err, dict) else err
            data = err.get("data") if isinstance(err, dict) else None
            raise DiodeError("Diode Measurement said: %s %s" % (msg, data or ""))
        return rep.get("result")

    def state(self):
        r = self.call("state")
        if not isinstance(r, dict):
            raise DiodeError("Bad 'state' reply")
        return r

    def start(self, **params):
        self.call("start", params)

    def change_voltage(self, end_voltage, step_voltage, waiting_time):
        self.call("change_voltage", {"end_voltage": end_voltage,
                                     "step_voltage": step_voltage,
                                     "waiting_time": waiting_time})

    def stop(self):
        self.call("stop")


class FakeDiodeClient:
    """Simulates Diode Measurement, for practice without the SMU."""

    READ_S = 0.3                          # simulated SMU + ELM reading time per step
    PICKUP_S = 0.6                        # simulated delay before a switch starts

    def __init__(self):
        self.st, self.v = "idle", 0.0
        self.sched = []                       # [(time, voltage, state)], in time order
        self.lock = threading.Lock()
        self.fail_now = False                 # test hook: app stops by itself

    def _tick(self):
        if self.fail_now and self.st != "idle":
            self.st, self.v, self.sched = "idle", 0.0, []
        now = time.monotonic()
        while self.sched and self.sched[0][0] <= now:
            _, self.v, self.st = self.sched.pop(0)

    def _ramp(self, points, t_start, step_s, final_state):
        self.sched = [(t_start + k * step_s, v, "ramping") for k, v in enumerate(points)]
        self.sched.append((t_start + len(points) * step_s, points[-1], final_state))

    def state(self):
        with self.lock:
            self._tick()
            return {"state": self.st, "running": self.st != "idle", "last_error": None,
                    "sample": "SIM", "filename": "SIMULATED (no file)",
                    "source_voltage": self.v, "smu_voltage": self.v,
                    "smu_current": self.v * 2e-13, "elm_current": None}

    def start(self, **p):
        with self.lock:
            self._tick()
            if self.st != "idle":
                raise DiodeError("Cannot start measurement, another measurement is still active.")
            v = float(p.get("end_voltage", 0.0))
            pts = ramp_values(0.0, v, p.get("step_voltage", 1.0)) or [v]
            self.st = "ramping"
            self._ramp(pts, time.monotonic() + 0.3, p.get("waiting_time", 1.0) + 0.05, "continuous")

    def change_voltage(self, end_voltage, step_voltage, waiting_time):
        with self.lock:
            self._tick()
            if self.st != "continuous":
                raise DiodeError("Cannot change voltage in state '%s'." % self.st)
            pts = ramp_values(self.v, end_voltage, step_voltage) or [end_voltage]
            self._ramp(pts, time.monotonic() + self.PICKUP_S, waiting_time + self.READ_S, "continuous")

    def stop(self):
        with self.lock:
            self._tick()
            if self.st != "idle":
                self.st = "stopping"
                self.sched = [(time.monotonic() + 1.5, 0.0, "idle")]


# ----------------------------------------------------------------------------
# The sequence (its own thread)
# ----------------------------------------------------------------------------
class SequenceAbort(Exception):
    pass


class Sequence(threading.Thread):
    def __init__(self, p, diode, tube, events):
        super().__init__(daemon=True)
        self.p = p                      # settings dict
        self.diode = diode              # DiodeClient or FakeDiodeClient
        self.tube = tube                # minix2_gui.Worker (already connected)
        self.events = events            # queue to the GUI
        self.abort_flag = threading.Event()
        self.t0 = None
        self.phase = "starting"
        self.xray_planned = False       # True between X-ray ON and planned OFF
        self.t_off = None               # planned X-ray OFF time (s from t0)
        self.smu_started = False        # only stop the SMU if this sequence started it
        self.switch_v2 = None           # target voltage of the polarity switch, once sent
        self.switch_t_req = None
        self.switch_logged = False
        self.t_on = None                # X-ray ON (s from t0, the tube's own clock)
        self.t_ramp = None              # first time Diode Measurement showed the switch running
        self.t_at_v2 = None             # first time the voltage read -V
        self.switch_took = None
        self.t_leave = None             # voltage left +V (s from t0, about +-0.1 s)
        self.t_middle = None            # middle of the switch
        self.t_cap = None               # the tube's own safety OFF time
        self.read_t = None              # time of the last state read (middle of the call)
        self.prev_read_t = None
        self.read_s = 0.2               # how long one state call takes (learned)
        self.slow_warned = False
        self.last_diode = {}
        self.diode_fails = 0
        self.csvf = None
        self.csvw = None
        self.next_row = 0.0
        self.log_path = ""

    # -- helpers --
    def post(self, *ev):
        self.events.put(ev)

    def log(self, msg):
        self.post("log", "[sequence] " + msg)

    def set_phase(self, name, note=""):
        self.phase = name
        self.post("seq_phase", name)
        self.row(name, note)

    def t(self):
        return None if self.t0 is None else time.monotonic() - self.t0

    def open_log(self):
        folder = os.path.join(base_folder(), "MiniX2_Sequence_Logs")
        os.makedirs(folder, exist_ok=True)
        sample = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.p["sample"]) or "sample"
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.log_path = os.path.join(folder, "%s_%s.csv" % (sample, stamp))
        self.csvf = open(self.log_path, "w", newline="")
        self.csvw = csv.writer(self.csvf)
        self.csvw.writerow(["unix_time", "local_time", "t_s", "event", "phase",
                            "smu_state", "source_voltage_V", "smu_current_A", "elm_current_A",
                            "tube_hv_on", "tube_kV", "tube_uA", "tube_status", "note"])
        self.log("Event log: " + self.log_path)

    def row(self, event, note=""):
        if not self.csvw:
            return
        d, s = self.last_diode or {}, self.tube.last_status or {}
        now = time.time()
        t = self.t()
        self.csvw.writerow([
            "%.3f" % now, datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "" if t is None else "%.2f" % t, event, self.phase,
            d.get("state", ""), d.get("source_voltage", ""), d.get("smu_current", ""),
            d.get("elm_current", ""),
            int(bool(s.get("hv_enabled"))) if s else "",
            "%.3f" % s["hv_kv"] if s else "", "%.3f" % s["current_ua"] if s else "",
            s.get("status_text", "") if s else "", note])
        self.csvf.flush()

    def read_diode(self):
        """Read Diode Measurement state. Two failures in a row = lost."""
        try:
            t_a = time.monotonic()
            self.last_diode = self.diode.state()
            t_b = time.monotonic()
            dt = t_b - t_a
            if self.t0 is not None:
                # When a call is slow, the wait is before Diode Measurement reads its
                # state (connecting, or its GUI thread busy), so the value is from the
                # END of the call.
                self.prev_read_t, self.read_t = self.read_t, t_b - self.t0
            self.read_s = max(dt, 0.8 * self.read_s)          # follows slow calls at once
            if dt > SLOW_RPC_S and not self.slow_warned:
                self.slow_warned = True
                self.log("Note: Diode Measurement took %.1f s to answer. The timeline stays "
                         "on time, but SMU readings in this log are less frequent, and the "
                         "automatic switch centering is less exact (about +-%.0f s). Try host "
                         "127.0.0.1." % (dt, dt))
            self.diode_fails = 0
            return self.last_diode
        except DiodeError as e:
            self.diode_fails += 1
            self.log("Diode state read failed (%d): %s" % (self.diode_fails, e))
            if self.diode_fails >= 2:
                raise SequenceAbort("Lost contact with Diode Measurement: %s" % e)
            return self.last_diode

    def track_switch(self, d):
        """Log when the polarity switch has really finished (may be after X-ray OFF)."""
        if self.switch_v2 is None or self.switch_logged:
            return
        sv = d.get("source_voltage")
        tol = max(1e-3, 1e-3 * abs(self.switch_v2))
        at_v2 = sv is not None and abs(sv - self.switch_v2) <= tol
        left_v1 = sv is not None and abs(sv + self.switch_v2) > tol
        # Diode Measurement updates source_voltage when it SETS a voltage. The change
        # happened between the previous read and this one: take the middle.
        now_r = self.read_t if self.read_t is not None else self.t()
        est = now_r if self.prev_read_t is None else (self.prev_read_t + now_r) / 2.0
        est = max(est, self.switch_t_req)
        if self.t_ramp is None and (d.get("state") == "ramping" or left_v1):
            self.t_ramp = self.t()
        if self.t_leave is None and left_v1:
            self.t_leave = est
            self.row("left_plus_V", "voltage %g V, changed at about t = %.2f s" % (sv, est))
        if self.t_at_v2 is None and at_v2:
            if self.t_leave is None:              # jumped straight to -V
                self.t_leave = est
            self.t_at_v2 = est
            self.row("at_minus_V", "voltage %g V, changed at about t = %.2f s" % (sv, est))
        if d.get("state") == "continuous" and at_v2:
            self.switch_logged = True
            took = self.t() - self.switch_t_req
            self.switch_took = took
            self.row("switch_done", "took %.1f s" % took)
            self.log("Polarity switch done at t = %.1f s (took %.1f s)" % (self.t(), took))
            if self.xray_planned:
                self.set_phase("X-rays ON, voltage switched")

    def switch_running(self):
        return self.switch_v2 is not None and self.t_at_v2 is None

    def sleep_until(self, t_target, allowed=ALIVE, check_tube=False, until=None):
        """Wait until t (seconds from t0), or until until() is True.
        Watch the SMU, the tube and ABORT."""
        next_check = 0.0
        while True:
            if self.abort_flag.is_set():
                raise SequenceAbort("ABORT pressed")
            if until is not None and until():
                return
            now = self.t()
            if now >= t_target:
                return
            left = t_target - now
            if check_tube:
                self.check_tube()                 # local, no waiting
            # A state read must never make the next step late: when the target is
            # closer than one read takes, skip the read and just wait.
            if time.monotonic() >= next_check and left > self.read_s + 0.1:
                # about 1 read per second (5 per second during the switch),
                # and the last one just before the target
                period = SWITCH_POLL_S if self.switch_running() else 1.0
                gap = left - self.read_s - 0.15
                next_check = time.monotonic() + (min(period, gap) if gap > 0.2 else period)
                d = self.read_diode()
                st = d.get("state")
                if st not in allowed:
                    raise SequenceAbort("Diode Measurement state is '%s' (expected %s). "
                                        "Last error: %s" % (st, " or ".join(allowed), d.get("last_error")))
                self.track_switch(d)
                now = self.t()
                left = t_target - now
                self.post("seq_status", self.phase, now, left, d)
                if time.monotonic() >= self.next_row:
                    self.next_row = time.monotonic() + 1.0
                    self.row("reading")
            time.sleep(max(0.0, min(0.05, t_target - self.t())))

    def summary(self):
        """X-ray time at +V, while switching, and at -V (from the state reads, about +-1 s)."""
        if self.t_on is None or self.switch_t_req is None:
            return
        t_leave = self.t_leave if self.t_leave is not None else self.switch_t_req
        plus = min(t_leave, self.t_off) - self.t_on
        if self.t_at_v2 is not None and self.t_at_v2 <= self.t_off:
            switching, minus = self.t_at_v2 - t_leave, self.t_off - self.t_at_v2
        else:
            switching, minus = self.t_off - min(t_leave, self.t_off), 0.0
        note = ("X-rays %.1f s in total: at +V %.1f s, switching %.1f s, at -V %.1f s "
                "(about +-0.2 s)" % (self.t_off - self.t_on, plus, switching, minus))
        self.row("summary", note)
        self.log(note)
        self.post("seq_measured", {"t_req": self.switch_t_req, "t_leave": self.t_leave,
                                   "t_arrive": self.t_at_v2, "wait": self.p["sw_wait"],
                                   "n": ramp_points(self.p["v"], -self.p["v"], self.p["sw_step"])})

    def check_tube(self):
        if self.tube.dev is None:
            raise SequenceAbort("Lost contact with the Mini-X2")
        # near the planned end the tube's own timer turns it off: that is not a fault
        if self.xray_planned and not self.tube.is_on and self.t() < self.t_off - 1.0:
            s = self.tube.last_status or {}
            raise SequenceAbort("X-ray tube stopped by itself: %s" % s.get("status_text", "unknown"))

    def xray_off(self, reason):
        if self.tube.is_on or self.xray_planned:
            self.tube.send("off")
            self.log("X-rays OFF (%s)" % reason)
        self.xray_planned = False

    def smu_stop(self, reason):
        try:
            d = self.diode.state()
            self.last_diode = d
            if d.get("state") not in ("idle", "stopping"):
                self.diode.stop()
                self.log("SMU stop sent (%s)" % reason)
            return True
        except DiodeError as e:
            self.log("SMU stop FAILED (%s): %s" % (reason, e))
            return False

    # -- the sequence --
    def run(self):
        p = self.p
        ok, why = False, ""
        try:
            self.open_log()
            self.row("settings", "V=%g V; ramp %g V/%g s; switch %g V/%g s; kV=%g; uA=%g; "
                     "SMU only before/after X-rays=%g s; switch after X-ray ON=%g s; X-ray ON time=%g s; "
                     "%s; minix2 %s" % (
                         p["v"], p["ramp_step"], p["ramp_wait"], p["sw_step"], p["sw_wait"],
                         p["kv"], p["ua"], p["t1"], p["t2"], p["t3"], SEQ_VERSION, minix2.VERSION))

            # 1) start the SMU (Diode Measurement), wait until it is at V
            self.set_phase("SMU ramp to V")
            d = self.diode.state()                 # must answer now (raises if unreachable)
            self.last_diode = d
            if d.get("state") != "idle":
                raise SequenceAbort("Diode Measurement is not idle (state '%s'). "
                                    "Stop it first." % d.get("state"))
            self.diode.start(continuous=True, measurement_type="iv", sample=p["sample"],
                             begin_voltage=0.0, end_voltage=p["v"],
                             step_voltage=p["ramp_step"], waiting_time=p["ramp_wait"])
            self.smu_started = True
            self.log("SMU start sent: ramp 0 -> %g V" % p["v"])
            limit = time.monotonic() + 60 + 3 * ramp_points(0, p["v"], p["ramp_step"]) * (p["ramp_wait"] + 1)
            seen_running = False
            while True:
                if self.abort_flag.is_set():
                    raise SequenceAbort("ABORT pressed")
                d = self.read_diode()
                st = d.get("state")
                if st == "continuous":
                    break
                if st in ("configure", "ramping"):
                    seen_running = True
                elif st in ("idle", "stopping") and seen_running:
                    raise SequenceAbort("SMU stopped during the ramp. Last error: %s" % d.get("last_error"))
                if time.monotonic() > limit:
                    raise SequenceAbort("SMU did not reach V in time (state '%s')" % st)
                self.post("seq_status", self.phase, None, None, d)
                time.sleep(0.25)
            self.t0 = time.monotonic()
            self.row("smu_at_V", "Diode Measurement file: %s" % d.get("filename"))
            self.log("SMU at %g V. t = 0. Data file: %s" % (p["v"], d.get("filename")))

            # 2) wait t1
            self.set_phase("SMU only (before X-rays)")
            self.sleep_until(p["t1"])

            # 3) X-rays ON for t3 (the tube worker also stops them after t3 by itself).
            #    Automatic centering: the OFF time is set during the switch, so the
            #    tube's own timer is a safety limit a bit later than t3.
            center = bool(p.get("center", False))
            on_s = min(p["t3"] + CENTER_EXTRA_S, MAX_ON_TIME_S) if center else p["t3"]
            self.set_phase("X-rays ON")
            self.tube.send("on", p["kv"], p["ua"], on_s)
            t_wait = time.monotonic() + 3.0
            while not self.tube.is_on:
                if time.monotonic() > t_wait:
                    raise SequenceAbort("The Mini-X2 did not turn on (see the log).")
                if self.abort_flag.is_set():
                    raise SequenceAbort("ABORT pressed")
                time.sleep(0.05)
            t_on = self.tube.on_time - self.t0      # the tube's own timer starts here
            self.t_on = t_on
            self.t_off = t_on + p["t3"]              # automatic centering moves it a little
            self.t_cap = t_on + on_s
            self.xray_planned = True
            self.row("xray_on", "%g kV, %g uA" % (p["kv"], p["ua"]))
            self.log("X-rays ON at t = %.1f s" % t_on)

            # 4) polarity switch. Manual: at t1 + t2 (t2 counted from X-ray ON).
            #    Automatic: early enough that its middle comes at the middle of t3.
            if center:
                m = switch_model(p["v"], p["sw_step"], p["sw_wait"], p.get("overhead"), p.get("pickup"))
                t_req_plan = max(t_on, t_on + p["t3"] / 2.0 - m["middle"])
                self.log("Automatic switch: request at t = %.1f s, so the middle of the switch "
                         "comes at about t = %.1f s. (The tube's own timer is %.0f s only as a "
                         "safety limit; this program turns the X-rays off.)"
                         % (t_req_plan, t_on + p["t3"] / 2.0, on_s))
            else:
                t_req_plan = t_on + p["t2"]
            self.sleep_until(t_req_plan, check_tube=True)
            v2 = -p["v"]
            while True:
                if self.abort_flag.is_set():
                    raise SequenceAbort("ABORT pressed")
                self.check_tube()
                t_req = self.t()                   # time the request leaves this program
                try:
                    self.diode.change_voltage(v2, p["sw_step"], p["sw_wait"])
                    break
                except DiodeError as e:
                    if self.t() > t_on + p["t3"] - 1.0:
                        raise SequenceAbort("Polarity switch refused until X-ray OFF: %s" % e)
                    self.log("Switch not accepted yet, retrying: %s" % e)
                    self.sleep_until(self.t() + 0.5, check_tube=True)
            self.switch_v2, self.switch_t_req = v2, t_req
            # Diode Measurement has the request now and the voltage is still +V:
            # this moment is the "previous read" for finding when the voltage changes.
            self.read_t = self.t()
            self.set_phase("Polarity switch", "%g V -> %g V, sent at t = %.2f s" % (p["v"], v2, t_req))
            self.log("Polarity switch sent at t = %.1f s: %g V -> %g V" % (t_req, p["v"], v2))

            # 5) X-rays OFF. Manual: at t1 + t3.
            #    Automatic: watch the switch (5 reads per second). When -V is reached:
            #    middle = (left +V + reached -V) / 2, and OFF = middle + (middle - ON),
            #    so +V and -V get the same X-ray time.
            if center:
                self.sleep_until(t_on + p["t3"], check_tube=True,
                                 until=lambda: self.t_at_v2 is not None)
                if self.t_at_v2 is not None and self.t_leave is not None:
                    mid = (self.t_leave + self.t_at_v2) / 2.0
                    self.t_middle = mid
                    t_off = 2.0 * mid - t_on
                    note = ""
                    if t_off > self.t_cap - 0.5:
                        t_off = self.t_cap - 0.5
                        note = " (limited by the safety time: NOT symmetric)"
                    self.t_off = max(t_off, self.t())
                    self.row("switch_middle", "middle at t = %.2f s -> X-rays OFF at t = %.2f s, "
                             "ON for %.1f s%s" % (mid, self.t_off, self.t_off - t_on, note))
                    self.log("Switch middle at t = %.1f s, so X-rays OFF at t = %.1f s "
                             "(ON for %.1f s)%s" % (mid, self.t_off, self.t_off - t_on, note))
                else:
                    self.row("not_centered", "switch did not reach -V before t = %.1f s" % self.t_off)
                    self.log("The switch did not reach -V in time, so X-rays OFF at t = %.1f s "
                             "as planned (NOT symmetric)." % self.t_off)
            self.sleep_until(self.t_off, check_tube=True)
            self.xray_planned = False
            if center:
                if self.tube.is_on:
                    self.tube.send("off")             # the program ends it, not the tube timer
                self.row("xray_off", "OFF sent at t = %.2f s" % self.t())
            else:
                self.row("xray_off")                  # the worker's own timer ends it at t3
            self.log("X-rays OFF at t = %.1f s" % self.t())
            time.sleep(0.3)
            if self.tube.is_on:
                self.tube.send("off")

            # 6) SMU only for t1 more, then stop
            self.set_phase("SMU only (after X-rays)")
            self.sleep_until(self.t_off + p["t1"])
            if not self.switch_logged:
                self.log("Note: the polarity switch was still running when the SMU stop was due.")
                self.row("switch_not_finished")
            self.set_phase("SMU stop")
            t_stop = self.t()
            self.diode.stop()
            self.row("smu_stop_sent", "sent at t = %.2f s" % t_stop)
            self.log("SMU stop sent at t = %.1f s" % t_stop)
            self.summary()
            limit = time.monotonic() + 600
            while time.monotonic() < limit:
                d = self.read_diode()
                self.post("seq_status", self.phase, self.t(), None, d)
                if d.get("state") == "idle":
                    break
                time.sleep(0.5)
            self.row("smu_idle")
            ok = True
        except (SequenceAbort, DiodeError) as e:
            why = str(e)
        except Exception as e:                     # anything unexpected: same safe stop
            why = "Unexpected error: %r" % (e,)
        finally:
            if not ok:
                why = "%s\n\n(step: %s, t = %s)" % (
                    why, self.phase, "-" if self.t0 is None else "%.1f s" % self.t())
                self.log("STOPPING: " + why.replace("\n", " "))
                self.row("abort", why)
                self.xray_off("sequence stopped")
                if self.smu_started:
                    self.smu_stop("sequence stopped")
            self.set_phase("Done" if ok else "Stopped")
            if self.csvf:
                self.csvf.close()
            self.post("seq_done", ok, why, self.log_path)


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
FIELDS = [  # key, label, default, unit
    ("sample", "Sample name", "Ch6_test", ""),
    ("v", "SMU voltage V", "50", "V"),
    ("ramp_step", "Ramp 0 -> V: step", "5", "V"),
    ("ramp_wait", "Ramp 0 -> V: waiting time", "0.5", "s"),
    ("sw_step", "Polarity switch: step", "25", "V"),
    ("sw_wait", "Polarity switch: waiting time", "0.7", "s"),
    ("kv", "X-ray voltage", "50", "kV"),
    ("ua", "X-ray current", "50", "uA"),
    ("t1", "SMU only, before X-ray ON and after X-ray OFF", "30", "s"),
    ("t2", "Polarity switch, counted from X-ray ON", "98", "s"),
    ("t3", "X-ray ON time", "200", "s"),
]
SETTINGS_FILE = "xray_sequence_settings.json"


class App:
    def __init__(self, root):
        self.root = root
        root.title("X-ray + SMU Sequence")
        self.events = queue.Queue()
        self.tube = Worker(self.events)
        self.tube.start()
        self.seq = None
        self.connected = False
        self.overhead = READ_OVERHEAD_S     # extra s per switch step, measured by the last run
        self.pickup = PICKUP_S              # s from switch request to switch start, measured
        self.overhead_measured = False
        self.build()
        self.load_settings()
        self.on_center()                      # grey out the manual switch time if automatic
        self.set_buttons()
        root.report_callback_exception = self.on_tk_error
        root.bind("<Escape>", lambda e: self.abort())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)

    def build(self):
        mid = ("Segoe UI", 10)
        top = tk.LabelFrame(self.root, text="Connections", padx=8, pady=4, font=mid)
        top.pack(fill="x", padx=8, pady=(8, 4))
        self.btn_tube = tk.Button(top, text="Connect Mini-X2", width=16, command=self.toggle_tube)
        self.btn_tube.grid(row=0, column=0, sticky="w")
        self.sim_tube = tk.BooleanVar(value=False)
        tk.Checkbutton(top, text="Simulate tube", variable=self.sim_tube).grid(row=0, column=1, sticky="w")
        self.lbl_tube = tk.Label(top, text="Mini-X2: not connected", fg="gray30")
        self.lbl_tube.grid(row=0, column=2, columnspan=3, sticky="w", padx=8)
        tk.Label(top, text="Diode Measurement RPC  host").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.host = tk.StringVar(value="localhost")
        tk.Entry(top, textvariable=self.host, width=12).grid(row=1, column=1, sticky="w", pady=(6, 0))
        tk.Label(top, text="port").grid(row=1, column=2, sticky="e", pady=(6, 0))
        self.port = tk.StringVar(value="8000")
        tk.Entry(top, textvariable=self.port, width=7).grid(row=1, column=3, sticky="w", pady=(6, 0))
        tk.Button(top, text="Test", width=6, command=self.test_diode).grid(row=1, column=4, padx=4, pady=(6, 0))
        self.sim_diode = tk.BooleanVar(value=False)
        tk.Checkbutton(top, text="Simulate Diode app", variable=self.sim_diode).grid(row=1, column=5, sticky="w", pady=(6, 0))
        self.lbl_diode = tk.Label(top, text="Diode Measurement: not tested", fg="gray30")
        self.lbl_diode.grid(row=2, column=0, columnspan=6, sticky="w")

        st = tk.LabelFrame(self.root, text="Sequence settings", padx=8, pady=4, font=mid)
        st.pack(fill="x", padx=8, pady=4)
        self.vars = {}
        self.entries = {}
        for i, (key, label, default, unit) in enumerate(FIELDS):
            r, c = (i % 6), (i // 6) * 3
            tk.Label(st, text=label, anchor="w").grid(row=r, column=c, sticky="w", padx=(0 if c == 0 else 16, 4))
            v = tk.StringVar(value=default)
            e = tk.Entry(st, textvariable=v, width=14 if key == "sample" else 8,
                         justify="left" if key == "sample" else "right")
            e.grid(row=r, column=c + 1, sticky="w")
            self.entries[key] = e
            tk.Label(st, text=unit, fg="gray40").grid(row=r, column=c + 2, sticky="w")
            v.trace_add("write", lambda *a: self.update_preview())
            self.vars[key] = v
        self.center = tk.BooleanVar(value=True)
        tk.Checkbutton(st, text="Put the switch in the middle automatically\n"
                                "(same X-ray time at +V and -V)", justify="left",
                       variable=self.center, command=self.on_center).grid(
            row=5, column=3, columnspan=3, sticky="w", padx=(12, 0))
        self.lbl_preview = tk.Label(st, text="", justify="left", font=("Consolas", 9), anchor="w")
        self.lbl_preview.grid(row=6, column=0, columnspan=6, sticky="w", pady=(6, 0))
        tk.Label(st, text="Instruments, current compliance and output folder: set them in "
                          "Diode Measurement as usual.", fg="gray40").grid(row=7, column=0, columnspan=6, sticky="w")

        bt = tk.Frame(self.root, padx=8, pady=6)
        bt.pack(fill="x")
        big = ("Segoe UI", 13, "bold")
        self.btn_run = tk.Button(bt, text="RUN SEQUENCE", font=big, bg="#f5c518", width=16,
                                 command=self.run, state="disabled")
        self.btn_run.pack(side="left")
        self.btn_abort = tk.Button(bt, text="ABORT  (Esc)", font=big, bg="#d9302c", fg="white",
                                   width=14, command=self.abort)
        self.btn_abort.pack(side="left", padx=8)
        self.lbl_ready = tk.Label(bt, text="", font=("Segoe UI", 10, "bold"), anchor="w", justify="left")
        self.lbl_ready.pack(side="left", padx=8)

        mon = tk.LabelFrame(self.root, text="Now", padx=8, pady=4, font=mid)
        mon.pack(fill="x", padx=8)
        self.lbl_phase = tk.Label(mon, text="Idle", font=("Segoe UI", 15, "bold"), fg="white",
                                  bg="gray50", width=34)
        self.lbl_phase.grid(row=0, column=0, columnspan=4, pady=(0, 4))
        self.lbl_time = tk.Label(mon, text="t = -", font=("Consolas", 11, "bold"))
        self.lbl_time.grid(row=1, column=0, columnspan=4, sticky="w")
        self.lbl_smu = tk.Label(mon, text="SMU: -", font=("Consolas", 10), anchor="w")
        self.lbl_smu.grid(row=2, column=0, columnspan=4, sticky="w")
        self.lbl_x = tk.Label(mon, text="Tube: -", font=("Consolas", 10), anchor="w")
        self.lbl_x.grid(row=3, column=0, columnspan=4, sticky="w")

        lg = tk.Frame(self.root, padx=8, pady=6)
        lg.pack(fill="both", expand=True)
        self.txt = scrolledtext.ScrolledText(lg, height=9, font=("Consolas", 9))
        self.txt.pack(fill="both", expand=True)
        self.log("xray_sequence %s, minix2 %s" % (SEQ_VERSION, minix2.VERSION))

    # -- helpers --
    def log(self, msg):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.txt.insert("end", "%s  %s\n" % (stamp, msg))
        self.txt.see("end")

    def diode_client(self):
        if self.sim_diode.get():
            if not isinstance(getattr(self, "_fake", None), FakeDiodeClient):
                self._fake = FakeDiodeClient()
            return self._fake
        return DiodeClient(self.host.get().strip() or "localhost", int(self.port.get()))

    def read_settings(self):
        p = {}
        for key, label, _, _ in FIELDS:
            s = self.vars[key].get().strip()
            if key == "sample":
                if not s:
                    raise ValueError("Please type a sample name.")
                p[key] = s
                continue
            try:
                p[key] = float(s)
            except ValueError:
                raise ValueError("'%s' must be a number." % label)
        minix2.check_limits(p["kv"], p["ua"])
        if p["v"] == 0:
            raise ValueError("SMU voltage V must not be 0.")
        labels = {k: label for k, label, _, _ in FIELDS}
        p["center"] = bool(self.center.get())
        p["overhead"], p["pickup"] = self.overhead, self.pickup
        keys = ["ramp_step", "ramp_wait", "sw_step", "sw_wait", "t1", "t3"]
        if not p["center"]:
            keys.append("t2")
        for k in keys:
            if p[k] <= 0:
                raise ValueError("'%s' must be above 0." % labels[k])
        if p["t3"] > MAX_ON_TIME_S:
            raise ValueError("X-ray ON time must be at most %d s." % MAX_ON_TIME_S)
        if not p["center"] and p["t2"] >= p["t3"]:
            raise ValueError("The polarity switch must come before the X-rays turn off\n"
                             "(switch time must be shorter than the X-ray ON time).")
        return p

    def on_center(self):
        self.entries["t2"].config(state="disabled" if self.center.get() else "normal")
        self.update_preview()

    def update_preview(self):
        try:
            p = self.read_settings()
        except ValueError as e:
            self.lbl_preview.config(text="Check: %s" % e, fg="red")
            return
        m = switch_model(p["v"], p["sw_step"], p["sw_wait"], self.overhead, self.pickup)
        t1, t3, v = p["t1"], p["t3"], p["v"]
        if p["center"]:
            t2 = t3 / 2.0 - m["middle"]          # request time, counted from X-ray ON
            plus = minus = (t3 - m["sweep"]) / 2.0
            bad = t2 < 0 or plus <= 0
        else:
            t2 = p["t2"]
            plus = t2 + m["leave"]
            minus = t3 - (t2 + m["arrive"])
            bad = minus <= 0
        def tt(x):
            return ("t = %.4g s" % x).ljust(14)
        step_txt = "%d steps x (%.2f + %.2f%s) s" % (m["n"], p["sw_wait"], self.overhead,
                                                     " measured" if self.overhead_measured else "")
        text = (tt(0) + "SMU at %+g V (time to get there: ~%.0f s)\n" % (
                    v, ramp_points(0, v, p["ramp_step"]) * (p["ramp_wait"] + self.overhead)) +
                tt(t1) + "X-rays ON  (%g kV, %g uA)\n" % (p["kv"], p["ua"]) +
                tt(t1 + t2) + "switch request %+g V -> %+g V  (%s%s)\n" % (
                    v, -v, step_txt, ", automatic" if p["center"] else "") +
                tt(t1 + t2 + m["middle"]) + "middle of the switch\n" +
                tt(t1 + t3) + "X-rays OFF%s\n" % (
                    "  (exact time set during the run: +V and -V get the same time)"
                    if p["center"] else "") +
                tt(2 * t1 + t3) + "SMU stop (ramp to 0 V)    total ~%.0f s\n" % (2 * t1 + t3) +
                "X-rays at %+g V: ~%.1f s   switching: ~%.1f s   at %+g V: ~%.1f s" % (
                    v, plus, m["sweep"], -v, minus))
        if bad:
            text += ("\nX-ray ON time too short for this switch: make it longer, or use a bigger "
                     "switch step." if p["center"] else "\nThe switch does not end before X-ray OFF.")
        self.lbl_preview.config(text=text, fg="red" if bad else "black")

    def save_settings(self):
        data = {k: self.vars[k].get() for k, _, _, _ in FIELDS}
        data.update(host=self.host.get(), port=self.port.get())
        data["center"] = bool(self.center.get())
        if self.overhead_measured:
            data["switch_extra_s_per_step"] = round(self.overhead, 3)
            data["switch_start_delay_s"] = round(self.pickup, 3)
        try:
            with open(os.path.join(base_folder(), SETTINGS_FILE), "w") as f:
                json.dump(data, f, indent=1)
        except OSError:
            pass

    def load_settings(self):
        try:
            with open(os.path.join(base_folder(), SETTINGS_FILE)) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if "t1" not in data:                      # settings saved by v3 or older
            for old, new in (("n1", "t1"), ("n3", "t2"), ("n2", "t3")):
                if old in data:
                    data[new] = data[old]
        for k, _, _, _ in FIELDS:
            if k in data:
                self.vars[k].set(data[k])
        if "switch_extra_s_per_step" in data:
            try:
                self.overhead = float(data["switch_extra_s_per_step"])
                self.pickup = float(data.get("switch_start_delay_s", PICKUP_S))
                self.overhead_measured = True
            except (TypeError, ValueError):
                pass
        self.center.set(bool(data.get("center", True)))
        self.on_center()
        self.host.set(data.get("host", "localhost"))
        self.port.set(data.get("port", "8000"))

    def running(self):
        return self.seq is not None and self.seq.is_alive()

    def set_buttons(self):
        run = self.running()
        self.btn_run.config(state="disabled" if run else "normal")
        if run:
            self.lbl_ready.config(text="Running ...", fg="#d9302c")
        elif not self.connected:
            self.lbl_ready.config(text="Not ready:\nconnect the Mini-X2 first", fg="#e67e00")
        else:
            self.lbl_ready.config(text="Ready", fg="green4")
        self.btn_tube.config(text="Disconnect Mini-X2" if self.connected else "Connect Mini-X2",
                             state="disabled" if self.running() else "normal")

    def on_tk_error(self, exc, val, tb):
        import traceback
        text = "".join(traceback.format_exception(exc, val, tb))
        self.log("PROGRAM ERROR: " + text.replace("\n", " | "))
        self.tube.send("off")                              # safe side
        messagebox.showerror("Program error", "Something went wrong:\n\n%s\n\n"
                             "X-rays were sent OFF. Please send this text to Claude." % text[-1500:])

    # -- actions --
    def toggle_tube(self):
        if self.connected:
            self.tube.send("disconnect")
        else:
            self.log("Connecting to the %s ..." % ("SIMULATED tube" if self.sim_tube.get() else "Mini-X2"))
            self.tube.send("connect", self.sim_tube.get())

    def test_diode(self):
        try:
            c = self.diode_client()
            c.state()                                   # first call finds the address
            t_a = time.monotonic()
            d = c.state()
            dt = time.monotonic() - t_a
            addr = c.address_text() if hasattr(c, "address_text") else "simulator"
            slow = dt > SLOW_RPC_S
            self.lbl_diode.config(text="Diode Measurement: OK, state '%s', answers in %.2f s%s" % (
                d.get("state"), dt, "  (slow!)" if slow else ""), fg="#e67e00" if slow else "green4")
            self.log("Diode Measurement reached at %s: state '%s', sample '%s', answer time %.2f s"
                     % (addr, d.get("state"), d.get("sample"), dt))
        except (DiodeError, ValueError) as e:
            self.lbl_diode.config(text="Diode Measurement: NOT reachable", fg="red")
            messagebox.showerror("Diode Measurement", str(e))

    def run(self):
        if self.running():
            return
        if self.tube.dev is None:
            messagebox.showerror("Cannot start", "The Mini-X2 is not connected.\n\n"
                                 "Press 'Connect Mini-X2' at the top first\n"
                                 "(or tick 'Simulate tube' to practice).")
            return
        try:
            p = self.read_settings()
            client = self.diode_client()
            d = client.state()
        except (ValueError, DiodeError) as e:
            messagebox.showerror("Cannot start", str(e))
            return
        if d.get("state") != "idle":
            messagebox.showerror("Cannot start", "Diode Measurement is busy (state '%s')." % d.get("state"))
            return
        if self.tube.dev is None:
            messagebox.showerror("Cannot start", "Connect the Mini-X2 first.")
            return
        m = switch_model(p["v"], p["sw_step"], p["sw_wait"], self.overhead, self.pickup)
        if p["center"]:
            if p["t3"] / 2.0 < m["middle"] or p["t3"] <= m["sweep"]:
                messagebox.showerror("Check timing", "The X-ray ON time is too short for this switch "
                                     "(the switch needs about %.0f s).\nMake the X-ray ON time longer, "
                                     "or use a bigger switch step." % (2 * m["middle"]))
                return
        elif p["t2"] + m["arrive"] > p["t3"]:
            if not messagebox.askyesno("Check timing", "The switch (~%.1f s) ends after the X-rays "
                                       "turn off. Run anyway?" % m["arrive"]):
                return
        q = ("Run this sequence?\n\nSample: %s\nSMU: %+g V, switch to %+g V\n"
             "X-rays: %g kV, %g uA, ON for %g s\n"
             "X-rays turn ON %g s after the SMU is at V\n"
             "%s\n"
             "SMU stops %g s after X-ray OFF\n"
             "Total about %.0f s\n\n"
             "Is the shielding closed?" % (
                 p["sample"], p["v"], -p["v"], p["kv"], p["ua"], p["t3"], p["t1"],
                 "Polarity switch: automatic, in the middle of the X-ray time\n"
                 "(X-ray ON time may change by about 1 s to keep +V and -V equal)"
                 if p["center"] else "Polarity switch %g s after X-ray ON" % p["t2"],
                 p["t1"], 2 * p["t1"] + p["t3"]))
        if not messagebox.askyesno("Run sequence?", q, icon="warning"):
            return
        self.save_settings()
        self.seq = Sequence(p, client, self.tube, self.events)
        self.seq.start()
        self.set_buttons()

    def abort(self):
        if self.running():
            self.log("ABORT pressed")
            self.seq.abort_flag.set()
        self.tube.send("off")                 # always: X-rays off at once

    def on_close(self):
        if self.running():
            if not messagebox.askyesno("Quit?", "A sequence is running. Stop it and quit?"):
                return
            self.seq.abort_flag.set()
            self.seq.join(timeout=8.0)
        self.save_settings()
        self.tube.send("quit")
        self.tube.join(timeout=3.0)
        self.root.destroy()

    # -- events from the threads --
    def poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self.log(ev[1])
                elif kind == "error":
                    self.log("ERROR: " + ev[1].replace("\n", " "))
                    if not self.running():
                        messagebox.showerror("Mini-X2", ev[1])
                elif kind == "connected":
                    self.connected = True
                    self.lbl_tube.config(text="%s  S/N %04d" % ("SIMULATED tube" if ev[2] else "Mini-X2", ev[1]),
                                         fg="green4")
                elif kind == "disconnected":
                    self.connected = False
                    self.lbl_tube.config(text="Mini-X2: not connected", fg="gray30")
                elif kind == "status":
                    s, is_on = ev[1], ev[2]
                    self.lbl_x.config(text="Tube: %s   %.2f kV   %.2f uA   %.0f mW   %d C   %s" % (
                        "X-RAYS ON " if is_on else ("HV ON (!) " if s["hv_enabled"] else "off       "),
                        s["hv_kv"], s["current_ua"], s["power_mw"], s["board_temp_c"], s["status_text"]),
                        fg="red" if (is_on or s["hv_enabled"]) else "black")
                elif kind == "seq_phase":
                    name = ev[1]
                    color = "#d9302c" if name.startswith("X-rays ON") or name == "Polarity switch" else \
                        "#2e7d32" if name == "Done" else "#e67e00" if name == "Stopped" else "#3a6ea5"
                    self.lbl_phase.config(text=name, bg=color)
                elif kind == "seq_status":
                    phase, t, left, d = ev[1], ev[2], ev[3], ev[4]
                    self.lbl_time.config(text="t = %s    next step in %s" % (
                        "-" if t is None else "%.1f s" % t, "-" if left is None else "%.0f s" % left))
                    def num(x, f):
                        return "-" if x is None else f % x
                    self.lbl_smu.config(text="SMU: state %-10s  V = %s V   I_smu = %s A   I_elm = %s A" % (
                        d.get("state"), num(d.get("source_voltage"), "%.3f"),
                        num(d.get("smu_current"), "%.4e"), num(d.get("elm_current"), "%.4e")))
                elif kind == "seq_measured":
                    m = ev[1]
                    n, wait = m["n"], m["wait"]
                    if m["t_leave"] is not None and m["t_arrive"] is not None and n >= 2:
                        if n >= 3:
                            T = (m["t_arrive"] - m["t_leave"]) / (n - 2)
                            self.overhead = min(30.0, max(0.0, T - wait))
                        T = wait + self.overhead
                        self.pickup = min(30.0, max(0.0, m["t_leave"] - m["t_req"] - T))
                        self.overhead_measured = True
                        self.log("This run: %.2f s per switch step (waiting time %.2f s + %.2f s), "
                                 "and the switch started %.1f s after the request. Saved for the "
                                 "next run." % (T, wait, self.overhead, self.pickup))
                        self.save_settings()
                        self.update_preview()
                elif kind == "seq_done":
                    ok, why, path = ev[1], ev[2], ev[3]
                    self.log(("Sequence finished OK." if ok else "Sequence STOPPED: " + why) +
                             (" Log: " + path if path else ""))
                    if not ok:
                        messagebox.showwarning("Sequence stopped", why)
                self.set_buttons()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

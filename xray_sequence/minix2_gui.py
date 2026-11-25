"""
minix2_gui.py - Simple GUI to run an Amptek Mini-X2 X-ray tube.

Set kV, uA and the ON time, press X-RAY ON, and the tube turns off by itself
when the time is up. STOP (or the Esc key) turns it off at once.

Run on Windows (close Amptek's Mini-X2 program first):
    py minix2_gui.py
Practice without the tube: tick "Simulator" before you press Connect.

Safety layers:
  1. Limits: 10-50 kV, 5-200 uA, max 10 W (see minix2.py).
  2. Timer: the worker thread turns HV off when the ON time ends.
  3. STOP button and Esc key.
  4. HV off when you close the window or when an error happens.
  5. Heartbeat: status is read every second. If this program freezes or
     crashes, the Mini-X2 turns the tube off by itself after 3 s.
"""
import csv
import datetime
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext

import minix2

STATUS_PERIOD_S = 1.0       # heartbeat; the Mini-X2 needs one at least every 3 s
MAX_ON_TIME_S = 3600        # longest ON time allowed in one shot
ON_CHECK_GRACE_S = 3.0      # after ON, wait this long before checking HV_EN
OFF_CHECK_GRACE_S = 2.0     # after OFF, the tube must report HV_EN off within this time


# ----------------------------------------------------------------------------
# Worker thread: the ONLY place that talks to the USB device
# ----------------------------------------------------------------------------
class Worker(threading.Thread):
    def __init__(self, events: queue.Queue):
        super().__init__(daemon=True)
        self.events = events
        self.cmds = queue.Queue()
        self.dev = None
        self.running = True
        self.is_on = False
        self.end_time = None
        self.on_time = None
        self.set_kv = 0.0
        self.set_ua = 0.0
        self.last_status = None
        self.next_status = 0.0
        self.fails = 0
        self.off_time = None
        self.stuck_level = 0

    # called from the GUI thread
    def send(self, *cmd):
        self.cmds.put(cmd)

    def post(self, *ev):
        self.events.put(ev)

    def log(self, msg):
        self.post("log", msg)

    # -- device actions --
    def do_connect(self, fake):
        if self.dev is not None:
            return
        try:
            self.dev = minix2.MiniX2(minix2.FakeTransport() if fake else None)
            s = self.dev.status()
            try:
                self.log("Start: sending OFF. Tube accepted: %s" % self.dev.hv_off())
            except Exception as e:
                self.log("Start: tube did not accept the OFF text (%s)" % e)
            self.last_status = s
            self.post("connected", s["serial"], fake)
            self.log("Connected to %s, S/N %04d (minix2.py %s)" % (
                "SIMULATOR" if fake else "Mini-X2", s["serial"], minix2.VERSION))
        except Exception as e:
            self.dev = None
            self.post("error", "Connect failed:\n%s" % e)

    def do_disconnect(self):
        if self.dev is None:
            return
        self.do_off("disconnect")
        try:
            self.dev.close()
        except Exception:
            pass
        self.dev = None
        self.post("disconnected")
        self.log("Disconnected")

    def do_on(self, kv, ua, seconds):
        if self.dev is None:
            self.post("error", "Not connected.")
            return
        s = self.last_status
        if s and s["status_code"] in (1, 2):
            self.post("error", "Cannot turn on: %s.\nClose the interlock first." % s["status_text"])
            return
        try:
            minix2.check_limits(kv, ua)
            self.dev.hv_on(kv, ua)
        except Exception as e:
            self.post("error", "X-ray ON failed:\n%s" % e)
            self.do_off("on failed")
            return
        now = time.monotonic()
        self.is_on, self.on_time, self.end_time = True, now, now + seconds
        self.stuck_level = 0
        self.set_kv, self.set_ua = kv, ua
        self.post("on_started", seconds)          # GUI opens a new exposure log file
        self.log("X-RAY ON: %.1f kV, %.1f uA, for %g s" % (kv, ua, seconds))
        self.readback_settings()
        self.next_status = 0.0                   # read status right away

    def do_set(self, kv, ua):
        """Change kV/uA while ON. The timer keeps running."""
        if not self.is_on:
            self.post("error", "The tube is OFF. Use X-RAY ON.")
            return
        try:
            minix2.check_limits(kv, ua)
            self.dev.hv_on(kv, ua)
            self.set_kv, self.set_ua = kv, ua
            self.log("New setting: %.1f kV, %.1f uA" % (kv, ua))
            self.readback_settings()
        except Exception as e:
            self.post("error", "Change failed:\n%s" % e)
            self.do_off("change failed")

    def readback_settings(self):
        try:
            time.sleep(0.1)
            rb = self.dev.readback()
            self.log("Mini-X2 setpoints now: HVSE=%s kV, CUSE=%s uA"
                     % (rb.get("HVSE", "?"), rb.get("CUSE", "?")))
        except Exception as e:
            self.log("Readback failed: %s" % e)

    def do_off(self, reason):
        was_on = self.is_on
        self.is_on, self.end_time = False, None
        self.off_time = time.monotonic()
        if self.dev is None:
            return
        try:
            cmd = self.dev.hv_off()
            if was_on or reason in ("STOP", "retry"):
                self.log("X-RAY OFF (%s). Tube accepted: %s" % (reason, cmd))
        except Exception as e:
            # Not an alarm by itself: the status check (HV_EN bit) decides.
            self.log("X-RAY OFF (%s). Tube did not accept the OFF text (%s). "
                     "Checking the tube status ..." % (reason, e))
        self.next_status = time.monotonic() + 0.3   # check the tube soon

    # -- main loop --
    def run(self):
        while self.running:
            try:
                cmd = self.cmds.get(timeout=0.05)
            except queue.Empty:
                cmd = None
            if cmd:
                name = cmd[0]
                if name == "connect":
                    self.do_connect(cmd[1])
                elif name == "disconnect":
                    self.do_disconnect()
                elif name == "on":
                    self.do_on(*cmd[1:])
                elif name == "set":
                    self.do_set(*cmd[1:])
                elif name == "off":
                    self.do_off("STOP")
                elif name == "quit":
                    self.do_disconnect()
                    self.running = False
                    break

            now = time.monotonic()
            # 1) timer: turn off when the ON time is over
            if self.is_on and self.end_time is not None and now >= self.end_time:
                self.do_off("time is up")

            # 2) heartbeat + readback, once per second
            if self.dev is not None and now >= self.next_status:
                self.next_status = now + STATUS_PERIOD_S
                try:
                    s = self.dev.status()
                    self.last_status = s
                    self.fails = 0
                except Exception as e:
                    self.fails += 1
                    if self.fails < 2:               # one retry, 0.2 s later
                        self.log("Status read failed, retrying: %s" % e)
                        self.next_status = now + 0.2
                        continue
                    self.fails = 0
                    self.do_off("lost contact")      # try once; may fail too
                    self.post("error", "Lost contact with the Mini-X2:\n%s\n\n"
                                       "The Mini-X2 turns the tube off by itself "
                                       "within 3 s when it gets no status requests." % e)
                    try:
                        self.dev.t.close()
                    except Exception:
                        pass
                    self.dev = None
                    self.post("disconnected")
                    continue
                # tube turned itself off (interlock opened, fault, ...)
                if (self.is_on and not s["hv_enabled"]
                        and now - self.on_time > ON_CHECK_GRACE_S):
                    self.log("Mini-X2 turned the tube OFF: %s" % s["status_text"])
                    self.do_off("tube fault")
                    self.post("error", "The Mini-X2 turned the tube OFF.\nReason: %s"
                              % s["status_text"])
                # after OFF, the tube itself must say HV is off (HV_EN bit)
                if not self.is_on:
                    if not s["hv_enabled"]:
                        if self.stuck_level:
                            self.log("Tube reports HV OFF now.")
                        self.stuck_level = 0
                    elif self.off_time is not None and now - self.off_time > OFF_CHECK_GRACE_S:
                        if self.stuck_level == 0:
                            self.stuck_level = 1
                            self.log("WARNING: tube still reports HV ON. Sending OFF again.")
                            self.do_off("retry")
                        elif self.stuck_level == 1:
                            self.stuck_level = 2
                            self.log("WARNING: tube still reports HV ON. Pausing status "
                                     "messages for 4 s, so the tube's own 3 s safety "
                                     "timer turns it off.")
                            self.next_status = now + 4.0
                            self.post("error", "The tube did not turn off with the OFF command.\n"
                                               "I paused the status messages, so the tube's own "
                                               "3-second safety timer should turn it off now.\n\n"
                                               "If the beeping does not stop, UNPLUG THE USB CABLE.")
                        elif self.stuck_level == 2:
                            self.stuck_level = 3
                            self.post("error", "The tube STILL reports HV ON.\n"
                                               "UNPLUG THE USB CABLE NOW.")
                remaining = max(0.0, self.end_time - now) if self.is_on else None
                elapsed = (now - self.on_time) if self.is_on else None
                self.post("status", s, self.is_on, remaining, elapsed,
                          self.set_kv if self.is_on else 0.0,
                          self.set_ua if self.is_on else 0.0)


# ----------------------------------------------------------------------------
# GUI (Tk main thread)
# ----------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("Mini-X2 X-ray Control")
        self.events = queue.Queue()
        self.worker = Worker(self.events)
        self.worker.start()
        self.connected = False
        self.is_on = False
        self.csv_file = None
        self.csv_writer = None
        self.csv_path = ""
        self.exp_start = time.monotonic()
        self.exp_sec = 0
        self.end_wall = None        # for a smooth countdown display
        self.build()
        root.bind("<Escape>", lambda e: self.stop())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.poll_events)
        self.root.after(100, self.tick)

    # -- layout --
    def build(self):
        f = self.root
        big = ("Segoe UI", 14, "bold")
        mid = ("Segoe UI", 11)

        top = tk.Frame(f, padx=8, pady=6)
        top.pack(fill="x")
        self.btn_conn = tk.Button(top, text="Connect", width=12, command=self.toggle_connect)
        self.btn_conn.pack(side="left")
        self.sim = tk.BooleanVar(value=False)
        tk.Checkbutton(top, text="Simulator (no tube)", variable=self.sim).pack(side="left", padx=8)
        self.lbl_dev = tk.Label(top, text="Not connected", fg="gray30", font=mid)
        self.lbl_dev.pack(side="left", padx=8)

        st = tk.LabelFrame(f, text="Settings", padx=8, pady=6, font=mid)
        st.pack(fill="x", padx=8)
        self.kv = tk.StringVar(value="15.0")
        self.ua = tk.StringVar(value="15.0")
        self.sec = tk.StringVar(value="10")
        rows = [("Voltage (kV)", self.kv, "%g to %g" % (minix2.KV_MIN, minix2.KV_MAX)),
                ("Current (uA)", self.ua, "%g to %g" % (minix2.UA_MIN, minix2.UA_MAX)),
                ("ON time (s)", self.sec, "1 to %d" % MAX_ON_TIME_S)]
        for r, (name, var, rng) in enumerate(rows):
            tk.Label(st, text=name, font=mid, width=13, anchor="w").grid(row=r, column=0, sticky="w")
            e = tk.Entry(st, textvariable=var, width=10, font=mid, justify="right")
            e.grid(row=r, column=1, padx=4, pady=2)
            tk.Label(st, text=rng, fg="gray40").grid(row=r, column=2, sticky="w")
            var.trace_add("write", lambda *a: self.update_power())
        self.lbl_power = tk.Label(st, text="", font=mid)
        self.lbl_power.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.update_power()

        bt = tk.Frame(f, padx=8, pady=8)
        bt.pack(fill="x")
        self.btn_on = tk.Button(bt, text="X-RAY ON", font=big, bg="#f5c518", width=12,
                                command=self.xray_on, state="disabled")
        self.btn_on.pack(side="left")
        self.btn_off = tk.Button(bt, text="STOP  (Esc)", font=big, bg="#d9302c", fg="white",
                                 width=12, command=self.stop, state="disabled")
        self.btn_off.pack(side="left", padx=8)
        self.btn_set = tk.Button(bt, text="Apply new\nkV / uA", width=10,
                                 command=self.apply_set, state="disabled")
        self.btn_set.pack(side="left")

        mon = tk.LabelFrame(f, text="Tube readback (every 1 s)", padx=8, pady=6, font=mid)
        mon.pack(fill="x", padx=8)
        self.lbl_state = tk.Label(mon, text="X-RAYS OFF", font=("Segoe UI", 18, "bold"),
                                  fg="white", bg="gray50", width=22)
        self.lbl_state.grid(row=0, column=0, columnspan=4, pady=(0, 6))
        self.vals = {}
        items = [("hv", "High voltage"), ("i", "Tube current"), ("p", "Power"),
                 ("t", "Board temp"), ("left", "Time left"), ("int", "Interlock / status")]
        for k, (key, name) in enumerate(items):
            r, c = divmod(k, 2)
            tk.Label(mon, text=name + ":", font=mid, anchor="e", width=16).grid(row=r + 1, column=c * 2, sticky="e")
            v = tk.Label(mon, text="-", font=("Consolas", 12, "bold"), anchor="w", width=22)
            v.grid(row=r + 1, column=c * 2 + 1, sticky="w")
            self.vals[key] = v

        lg = tk.Frame(f, padx=8, pady=6)
        lg.pack(fill="both", expand=True)
        self.do_csv = tk.BooleanVar(value=True)
        tk.Checkbutton(lg, text="Save a log file for each exposure (folder MiniX2_Exposure_Logs)",
                       variable=self.do_csv).pack(anchor="w")
        self.txt = scrolledtext.ScrolledText(lg, height=8, font=("Consolas", 9))
        self.txt.pack(fill="both", expand=True)

    # -- helpers --
    def log(self, msg):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.txt.insert("end", "%s  %s\n" % (stamp, msg))
        self.txt.see("end")

    def read_inputs(self):
        try:
            kv = float(self.kv.get())
            ua = float(self.ua.get())
            sec = float(self.sec.get())
        except ValueError:
            raise ValueError("Please type numbers in kV, uA and ON time.")
        minix2.check_limits(kv, ua)
        if not (1 <= sec <= MAX_ON_TIME_S):
            raise ValueError("ON time must be 1 to %d s" % MAX_ON_TIME_S)
        return kv, ua, sec

    def update_power(self):
        try:
            kv, ua = float(self.kv.get()), float(self.ua.get())
            p = kv * ua
            ok = p <= minix2.P_MAX_MW
            self.lbl_power.config(text="Power = %.1f kV x %.1f uA = %.0f mW  (max %.0f mW)"
                                  % (kv, ua, p, minix2.P_MAX_MW),
                                  fg="black" if ok else "red")
        except ValueError:
            self.lbl_power.config(text="Power = ?", fg="red")

    def set_buttons(self):
        c, on = self.connected, self.is_on
        self.btn_on.config(state="normal" if c and not on else "disabled")
        self.btn_off.config(state="normal" if c else "disabled")
        self.btn_set.config(state="normal" if c and on else "disabled")
        self.btn_conn.config(text="Disconnect" if c else "Connect",
                             state="disabled" if on else "normal")

    # one log file per X-ray exposure, in folder MiniX2_Exposure_Logs
    def open_exposure_log(self, seconds):
        self.close_csv()
        if not self.do_csv.get():
            return
        if getattr(sys, "frozen", False):        # running as .exe: next to the .exe
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        folder = os.path.join(base, "MiniX2_Exposure_Logs")
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.datetime.now().strftime("Exposure_%Y-%m-%d_%H-%M-%S")
        path = os.path.join(folder, stamp + ".csv")
        i = 2
        while os.path.exists(path):
            path = os.path.join(folder, "%s_%d.csv" % (stamp, i))
            i += 1
        self.csv_file = open(path, "w", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(["time", "elapsed_s", "xray_on", "set_kV", "set_uA", "set_time_s",
                                  "hv_kV", "current_uA", "power_mW", "board_temp_C", "hv_enabled",
                                  "status_code", "status_text", "time_left_s"])
        self.csv_path = path
        self.exp_start = time.monotonic()
        self.exp_sec = seconds
        self.log("Exposure log: %s" % path)

    def close_csv(self):
        if self.csv_file:
            self.csv_file.close()
            self.csv_file = self.csv_writer = None
            self.log("Exposure log saved: %s" % self.csv_path)

    # -- button actions --
    def toggle_connect(self):
        if self.connected:
            self.worker.send("disconnect")
        else:
            self.log("Connecting ...")
            self.worker.send("connect", self.sim.get())

    def xray_on(self):
        try:
            kv, ua, sec = self.read_inputs()
        except ValueError as e:
            messagebox.showerror("Check settings", str(e))
            return
        if not messagebox.askyesno(
                "Turn X-rays ON?",
                "Turn X-RAYS ON?\n\n%.1f kV,  %.1f uA  (%.0f mW)\nfor %g seconds\n\n"
                "Is the shielding closed?" % (kv, ua, kv * ua, sec), icon="warning"):
            return
        self.worker.send("on", kv, ua, sec)

    def apply_set(self):
        try:
            kv, ua, _ = self.read_inputs()
        except ValueError as e:
            messagebox.showerror("Check settings", str(e))
            return
        self.worker.send("set", kv, ua)

    def stop(self):
        self.worker.send("off")

    def on_close(self):
        self.worker.send("quit")          # sends HV OFF, then closes USB
        self.worker.join(timeout=3.0)
        self.close_csv()
        self.root.destroy()

    # -- events from the worker --
    def poll_events(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self.log(ev[1])
                elif kind == "error":
                    self.log("ERROR: " + ev[1].replace("\n", " "))
                    messagebox.showerror("Mini-X2", ev[1])
                elif kind == "connected":
                    self.connected = True
                    self.lbl_dev.config(text="%s  S/N %04d" % ("SIMULATOR" if ev[2] else "Mini-X2", ev[1]),
                                        fg="green4")
                elif kind == "disconnected":
                    self.connected = self.is_on = False
                    self.lbl_dev.config(text="Not connected", fg="gray30")
                    self.close_csv()
                    self.show_off()
                elif kind == "status":
                    self.show_status(*ev[1:])
                elif kind == "on_started":
                    self.open_exposure_log(ev[1])
                self.set_buttons()
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def show_off(self):
        self.lbl_state.config(text="X-RAYS OFF", bg="gray50")
        self.end_wall = None
        self.vals["left"].config(text="-")

    def show_status(self, s, is_on, remaining, elapsed, set_kv, set_ua):
        self.is_on = is_on
        self.vals["hv"].config(text="%.2f kV" % s["hv_kv"])
        self.vals["i"].config(text="%.2f uA" % s["current_ua"])
        self.vals["p"].config(text="%.0f mW" % s["power_mw"])
        self.vals["t"].config(text="%d C" % s["board_temp_c"])
        self.vals["int"].config(text=s["status_text"],
                                fg="green4" if s["status_code"] == 0 else "red")
        if is_on:
            self.lbl_state.config(text="X-RAYS ON", bg="#d9302c")
            self.end_wall = time.monotonic() + remaining
        elif s["hv_enabled"]:
            # program says OFF, but the tube says HV is still on
            self.lbl_state.config(text="TUBE SAYS HV ON!", bg="#ff8c00")
            self.end_wall = None
            self.vals["left"].config(text="-")
        else:
            self.show_off()
        if self.csv_writer:
            self.csv_writer.writerow([
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                round(time.monotonic() - self.exp_start, 1), int(is_on),
                set_kv, set_ua, self.exp_sec, round(s["hv_kv"], 3), round(s["current_ua"], 3),
                round(s["power_mw"], 1), s["board_temp_c"], int(s["hv_enabled"]),
                s["status_code"], s["status_text"],
                "" if remaining is None else round(remaining, 1)])
            self.csv_file.flush()
            if not is_on and not s["hv_enabled"]:
                self.close_csv()                  # exposure finished

    def tick(self):
        # smooth countdown between the 1 s status updates (display only)
        if self.is_on and self.end_wall is not None:
            left = max(0.0, self.end_wall - time.monotonic())
            self.vals["left"].config(text="%.1f s" % left)
        self.root.after(100, self.tick)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

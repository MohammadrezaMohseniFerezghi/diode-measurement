X-ray + SMU Sequence (xray_sequence.py, v6)
==========================================

What it does
------------
It runs Diode Measurement (the SMU) and the Mini-X2 together, on one timeline.
Andreas' Diode Measurement program is not changed: the sequence talks to it
through its RPC tab, and to the tube with minix2.py.

t = 0 is the moment the SMU has reached V (Diode Measurement is "continuous").

The three times (the window shows plain labels, not these names):
    t1 = "SMU only, before X-ray ON and after X-ray OFF"
    t2 = "Polarity switch, counted from X-ray ON"   (must be shorter than t3)
    t3 = "X-ray ON time"

    t = 0              SMU at V, I vs t is recorded by Diode Measurement
    t = t1             X-rays ON (kV, uA)
    t = t1 + t2        voltage V -> -V (a short ramp)
    t = t1 + t3        X-rays OFF
    t = t1 + t3 + t1   SMU stop (Diode Measurement ramps to 0 V, output off)

Example: V = 50 V, t1 = 30 s, t2 = 98 s, t3 = 200 s
    0 s: SMU at +50 V
    30 s: X-rays ON
    128 s: switch to -50 V. With step 25 V and waiting time 0.7 s:
           5 set points (50, 25, 0, -25, -50) x (0.7 + ~0.1) s = about 4 s
    230 s: X-rays OFF (about 98 s at -50 V after the switch)
    260 s: SMU stop

The window shows this timeline for your numbers before you run.

Files needed in one folder
--------------------------
xray_sequence.py, minix2.py, minix2_gui.py

Step 1. Practice without any hardware
-------------------------------------
    py xray_sequence.py
Tick "Simulate tube" and "Simulate Diode app". Press "Connect Mini-X2",
use small times (for example 3 s before/after, switch after 4 s, X-ray ON time 9 s), press RUN SEQUENCE.
Try ABORT (or the Esc key) during a run.

Step 2. Connect to the real Diode Measurement
---------------------------------------------
1. Start Diode Measurement (your .exe) as usual.
2. Set in it, as usual: instruments (SMU, ELM ...), current compliance,
   output folder, and keep "Output" ticked (so it writes its data file).
3. Open its RPC tab. Tick "Enabled". Look at the port (default 8000).
4. In the sequence window: untick "Simulate Diode app", type the same port,
   press "Test". It should say: Diode Measurement: OK, state 'idle'.

Step 3. Real run
----------------
1. Close Amptek's Mini-X2 program. Untick "Simulate tube". Press Connect Mini-X2.
2. Type the sample name, V, the ramp settings, kV, uA and the three times.
3. Close the shielding. Press RUN SEQUENCE and answer Yes.

The sequence sets these values in Diode Measurement when it starts:
continuous ON, type IV, sample name, begin 0 V, end V, step and waiting time.
Everything else comes from the Diode Measurement window.

Rules when something goes wrong
-------------------------------
* SMU / Diode Measurement stops, fails, or cannot be reached (2 tries, about
  2 s) while the X-rays are on: X-rays OFF at once.
* X-ray tube fault (interlock open, tube error, USB lost): SMU stop.
* ABORT or Esc: X-rays OFF and SMU stop.
* Closing the window during a run: same as ABORT.
* If Diode Measurement is busy when you press RUN, nothing is started and
  nothing is stopped.
* The Mini-X2 heartbeat runs in its own thread. If this program freezes or
  crashes, the tube turns itself off after 3 s.
* The tube's own timer also ends the X-rays after t3, even if the sequence
  is late.

Timing
------
* Automatic centering (box "Put the switch in the middle automatically",
  on by default): the program sends the switch request early enough that the
  middle of the switch comes at the middle of the X-ray ON time. During the
  switch it reads the voltage 5 times per second. When the voltage reaches -V:
      middle = (time the voltage left +V + time it reached -V) / 2
      X-rays OFF = middle + (middle - X-rays ON)
  So the X-ray time at +V and at -V is the same (about +-0.2 s). The total
  X-ray time can differ a little from the box (about 1 s; more in the first
  run after you change the instruments' speed, because the program learns the
  switch speed from each run).
  Example: X-ray ON time 45 s, switch 5 steps x 2.7 s: the voltage is between
  +V and -V for about 8 s, so +V gets about 18.5 s and -V about 18.5 s.
  The tube's own timer is set 20 s longer, only as a safety limit.
* Untick the box to set the switch time by hand ("Polarity switch, counted
  from X-ray ON").
* "Test" shows how fast Diode Measurement answers. It should be about
  0.01 s. If it says "slow!" (about 2 s), type 127.0.0.1 as host instead of
  localhost. (v5 already tries 127.0.0.1 first.)
* The steps (X-ray ON, switch request, SMU stop) do not wait for slow answers:
  they happen on time, within about 0.1 s.
* The preview line "X-rays at +V / switching / at -V" shows how long the
  sample gets X-rays at each polarity. Change the switch time to balance them.
* The switch speed (seconds per voltage step) is measured in each run and
  saved. The next preview uses it. On 2026-11-23 it was about 2.5 s per step
  (waiting time 0.5 s + about 2.0 s for the readings).
* At the end, the log has a "summary" row: X-ray time at +V, while switching,
  and at -V (about +-1 s).
* Diode Measurement checks for a voltage change about once per second, so the
  switch starts up to about 1 s after t1 + t2. The log records the real times.
* The switch time is an estimate: (number of set points) x (waiting time +
  reading time). A slow SMU reading (for example 1 to 2 s) makes it much
  longer. The log line "switch_done ... took X s" gives the real value.
* "ramping" is fine: it means a voltage change is running and the SMU is
  still measuring. If the switch has not finished when the SMU stop is due,
  the SMU is stopped anyway and the log says "switch_not_finished".

Log file
--------
Folder MiniX2_Sequence_Logs (next to the program), one file per run:
    <sample>_<date>_<time>.csv
One line per second plus one line per event (smu_at_V, xray_on,
Polarity switch, switch_done, xray_off, smu_stop_sent, smu_idle, abort).
Columns: unix_time, local_time, t_s, event, phase, smu_state,
source_voltage_V, smu_current_A, elm_current_A, tube_hv_on, tube_kV, tube_uA,
tube_status, note.

unix_time uses the same clock as the "timestamp[s]" column in the Diode
Measurement data file (seconds since 1970), so you can put the X-ray ON/OFF
and switch times directly on your I vs t plot. The smu_at_V line also names
the Diode Measurement data file.

Make an .exe
------------
Double-click build_sequence_exe.bat. The program is dist\XRay_SMU_Sequence.exe.

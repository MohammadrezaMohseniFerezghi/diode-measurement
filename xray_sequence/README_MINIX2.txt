Mini-X2 Python Control
======================

Files
-----
minix2.py        The driver. It talks to the tube over USB.
test_connect.py  SAFE test. It reads the status. It NEVER turns X-rays on.
minix2_gui.py    The GUI: set kV, uA, ON time; X-RAY ON; STOP.

Step 1. Install (one time)
--------------------------
Open "Command Prompt" in this folder and type:

    py -m pip install pyusb libusb-package

(If "py" does not work, use "python" instead.)

Step 2. Close Amptek's Mini-X2 program
--------------------------------------
Only one program can use the USB port at a time.

Step 3. Practice with the simulator (no tube needed)
---------------------------------------------------
    py minix2_gui.py

Tick "Simulator (no tube)", then press Connect. Try X-RAY ON, STOP, Esc.

Step 4. Safe test with the real tube (no X-rays)
------------------------------------------------
    py test_connect.py

You should see 5 status lines and "TEST PASSED".
Check: S/N 0136, board temp about room temperature, "Interlock CLOSED (OK)".

Step 5. First real X-ray test
-----------------------------
Close the shielding first. Then:

    py minix2_gui.py

Connect. Set 15 kV, 15 uA, ON time 5 s. Press X-RAY ON and answer Yes.
You should see:
  * "X-RAYS ON" in red, and the Mini-X2 beeps once per second.
  * Readback near 15 kV and 15 uA after 1 to 2 seconds.
  * After 5 s it turns off by itself, and the beeping stops.

Step 6. Check STOP
------------------
Set ON time 30 s, press X-RAY ON, then press STOP (or the Esc key).
The beeping should stop at once.

Step 7. Check the readback numbers
----------------------------------
Run Amptek's program at 15 kV / 15 uA and write down its "High Voltage
Monitor" and "Current Monitor". Close it. Run this GUI at the same setting.
The numbers should be about the same.

Safety in the GUI
-----------------
* Limits: 10 to 50 kV, 10 to 50 uA, max 10 W (10000 mW). Change them at the
  top of minix2.py only if your tube is different.
* The tube turns off when the ON time ends, when you press STOP or Esc,
  when you close the window, and on any error.
* The GUI reads the status every second. If the GUI freezes or crashes, the
  Mini-X2 turns the tube off by itself after 3 seconds (User Manual 10.4).
* Unplugging the USB cable turns the tube off at once.

Log files (one per exposure)
----------------------------
If "Save a log file for each exposure" is ticked, every X-RAY ON makes a new
file in the folder MiniX2_Exposure_Logs (next to the program), named with the
date and time, for example:
    MiniX2_Exposure_Logs\Exposure_2026-11-22_16-46-35.csv
4 exposures = 4 files. Each file has one line per second, from X-RAY ON until
the tube reports HV off. Columns: time, elapsed_s, xray_on, set_kV, set_uA,
set_time_s, hv_kV, current_uA, power_mW, board_temp_C, hv_enabled,
status_code, status_text, time_left_s.

Use the driver in your own scripts
----------------------------------
    import minix2, time
    dev = minix2.MiniX2()          # connect
    print(dev.status())            # read kV, uA, temp, interlock ...
    dev.hv_on(20, 50)              # 20 kV, 50 uA  (X-RAYS ON)
    for i in range(10):            # keep asking for status (heartbeat!)
        print(dev.status()["hv_kv"]); time.sleep(1)
    dev.hv_off()                   # X-RAYS OFF
    dev.close()

Important: while the tube is on, call dev.status() at least once per second,
or the Mini-X2 turns the tube off after 3 s.

If something goes wrong
-----------------------
"Mini-X2 not found"  -> Check 12 V power and USB. Close Amptek's program.
"Access denied" or "Resource busy" -> Amptek's program is still open.
"No reply from Mini-X2" -> Unplug USB, plug it back in, Connect again.

How the protocol was found
--------------------------
Amptek's download pages are gone, so the commands were read out of
Amptek's own program (MiniX2.exe).
  ON : HVSE=<kV>;CUSE=<uA>;   (same as Amptek's HV ON button)
  OFF: the tube said "bad parameter" to Amptek's OFF text, so the GUI tries
       HVSE=OFF;  then HVSE=0;  then HVSE=0.0;CUSE=0.0;
       A rejected OFF text is only written in the log. The GUI then reads the
       tube's own HV_EN bit. Only if the tube still says HV ON does it warn you:
       it sends OFF again, then pauses the status messages so the tube's own
       3-second safety timer turns it off, then tells you to unplug USB.

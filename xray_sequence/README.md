# X-ray + SMU sequence (Amptek Mini-X2 + Diode Measurement)

Programs to control an Amptek Mini-X2 X-ray tube over USB, and to run it together
with Diode Measurement (through its RPC server) as one timed sequence:

1. SMU ramps to +V (Diode Measurement, continuous mode)
2. X-rays ON (kV, uA)
3. Polarity switch +V -> -V, put in the middle of the X-ray time (automatic), so
   the sample gets the same X-ray time at +V and at -V
4. X-rays OFF
5. SMU stop

The Diode Measurement code is not changed. This folder only talks to it through
the RPC tab (enable it in Diode Measurement; host 127.0.0.1, port 8000).

## Files

| File | What it is |
|---|---|
| `xray_sequence.py` | The sequence program (X-ray tube + Diode Measurement) |
| `minix2_gui.py` | X-ray tube only: set kV, uA, ON time |
| `minix2.py` | The Mini-X2 USB driver used by both programs |
| `test_connect.py` | Safe test: reads the tube status, never turns X-rays on |
| `diag.py` | Safe USB diagnostic |
| `build_sequence_exe.bat` | Builds `XRay_SMU_Sequence.exe` (one file, no Python needed to run it) |
| `build_exe.bat` | Builds `MiniX2_Control.exe` (tube only) |
| `README_SEQUENCE.txt` | How to use the sequence program, the timeline, and the timing notes |
| `README_MINIX2.txt` | How to use the tube-only program |

## Install and run (Windows)

```
py -m pip install -r requirements.txt
py xray_sequence.py
```

Close Amptek's own Mini-X2 program first: only one program can use the tube's USB
port at a time. Both programs have simulators ("Simulate tube", "Simulate Diode app")
for practice without hardware.

Each run writes a log file to `MiniX2_Sequence_Logs/` next to the program.

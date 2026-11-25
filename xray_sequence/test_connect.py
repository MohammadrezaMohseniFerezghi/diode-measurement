"""
test_connect.py - SAFE first test. It NEVER turns the X-rays on.

It does 3 things:
  1. Finds the Mini-X2 on USB.
  2. Reads the status 5 times (once per second) and prints it.
  3. Reads back the kV and uA settings (HVSE=?;CUSE=?;).

Run on Windows (close Amptek's Mini-X2 program first):
    py test_connect.py
Run without the tube (simulator):
    py test_connect.py --fake
"""
import sys
import time

import minix2


def main():
    fake = "--fake" in sys.argv
    print("Connecting to %s ..." % ("SIMULATOR" if fake else "Mini-X2 on USB"))
    dev = minix2.MiniX2(minix2.FakeTransport() if fake else None)
    print("Connected.\n")
    try:
        for i in range(5):
            s = dev.status()
            print("[%d] S/N %04d | HV %.2f kV | I %.2f uA | P %.0f mW | "
                  "Temp %d C | HV_EN %s | %s" % (
                      i + 1, s["serial"], s["hv_kv"], s["current_ua"], s["power_mw"],
                      s["board_temp_c"], "ON" if s["hv_enabled"] else "off",
                      s["status_text"]))
            time.sleep(1.0)
        print("\nSupply %.2f V, tube supply %.2f V, interlock current %.2f mA, runtime %d s"
              % (s["supply_v"], s["tube_supply_v"], s["interlock_ma"], s["runtime_s"]))
        print("Settings readback:", dev.readback())
        print("\nTEST PASSED. The computer can talk to the Mini-X2.")
    finally:
        # Do not call dev.close() here: it sends HV OFF, which is fine,
        # but this test should send no HV command at all.
        dev.t.close()


if __name__ == "__main__":
    main()

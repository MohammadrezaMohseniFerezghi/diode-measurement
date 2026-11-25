@echo off
REM Builds XRay_SMU_Sequence.exe (one file, no Python needed to run it).
REM Run once on a PC that has Python. Double-click it, or type: build_sequence_exe.bat
cd /d "%~dp0"
py -m pip install --upgrade pyinstaller pyusb libusb-package
py -m PyInstaller --noconfirm --onefile --windowed --name XRay_SMU_Sequence ^
  --collect-all libusb_package --hidden-import usb.backend.libusb1 xray_sequence.py
echo.
echo Done. Your program is here: %~dp0dist\XRay_SMU_Sequence.exe
pause

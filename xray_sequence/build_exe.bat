@echo off
REM Builds MiniX2_Control.exe (one file, no Python needed to run it).
REM Run this once, on a PC that has Python. Double-click it, or type: build_exe.bat
cd /d "%~dp0"
py -m pip install --upgrade pyinstaller pyusb libusb-package
py -m PyInstaller --noconfirm --onefile --windowed --name MiniX2_Control ^
  --collect-all libusb_package --hidden-import usb.backend.libusb1 minix2_gui.py
echo.
echo Done. Your program is here: %~dp0dist\MiniX2_Control.exe
pause

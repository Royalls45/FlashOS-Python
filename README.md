# FlashOS Python

Windows desktop application for inspecting USB volumes and Linux ISO files, checking Ventoy metadata, and launching an ISO in QEMU.

## Requirements

- Windows 10/11, x64
- Python 3.12 or newer
- QEMU for Windows (`qemu-system-x86_64.exe`) to run an ISO

## Download the Windows build

Open the [Actions page](https://github.com/Royalls45/FlashOS-Python/actions), choose the latest successful **Windows build**, and download the `FlashOS-Windows` artifact. Extract the ZIP and run `FlashOS/FlashOS.exe`.

The artifact contains the app and its Python/Qt runtime. QEMU is a separate dependency and is needed only to launch a Linux ISO.

## Run from source

```powershell
cd FlashOS-Python
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

You can also install the package with `python -m pip install .` and launch it using `flashos`.

## Current scope and safety

- FlashOS scans only a selected ready volume, with limits on scan time, depth, and directory count.
- `persistence.bat` is shown as text and is never executed.
- Ventoy JSON paths are checked against the selected volume. A matching configuration or ext filesystem label is diagnostic evidence only.
- The current QEMU launch uses the ISO as a read-only virtual CD-ROM with networking disabled. Live Persistence is deliberately not enabled: the full two-launch persistence check has not been completed.
- FlashOS does not write to physical USB devices or pass a physical USB device through to QEMU.
- VM launch uses QProcess arguments (no shell). The app offers a graceful guest shutdown request and warns before force-stopping QEMU.

## Build

GitHub Actions installs Python 3.12, checks Python syntax, builds the Windows app with PyInstaller, and uploads a ZIP artifact.


# Martin Remote Agent and Wake-on-LAN

## Architecture

`martin_ai.remote_background` is a lightweight per-user Windows Session Agent. It starts after login without opening the Martin AI window and owns the existing authenticated Phone Remote API. It reuses the same trusted-device file, session handling, per-device permissions, screen capture, clipboard, files, and mobile UI. Screen encoding remains demand-driven.

This is deliberately not a LocalSystem Windows service. Interactive screen, input, and clipboard operations belong in the logged-in user's Windows session, and Martin does not bypass Session 0 isolation. Pre-login remote access therefore remains unavailable. The supervisor performs at most three recovery starts with 2, 5, and 15 second delays.

Install/start:

```powershell
.\.venv\Scripts\python.exe -m martin_ai.remote_background install
.\.venv\Scripts\python.exe -m martin_ai.remote_background start
```

Status/restart/stop:

```powershell
.\.venv\Scripts\python.exe -m martin_ai.remote_background status
.\.venv\Scripts\python.exe -m martin_ai.remote_background restart
.\.venv\Scripts\python.exe -m martin_ai.remote_background stop
```

Remove startup registration with `uninstall_remote_agent.bat`. This does not alter network-adapter or firmware settings.

## Wake-on-LAN

Martin reads physical adapter metadata, the driver's Magic Packet property when accessible, and `powercfg /devicequery wake_armed`. “Supported” requires Ethernet plus explicit driver or wake-armed evidence. Wi-Fi remains Unknown/Unsupported unless Windows exposes evidence. BIOS/UEFI, hibernate wake, and shutdown wake are always Unknown until tested on the hardware.

The phone PWA stores only the PC name, adapter name, and MAC address. A MAC is not an authentication credential. Browser PWAs cannot send UDP datagrams, so **Wake Martin PC** uses the `martin-wol://` native-provider handoff. Install/configure a trusted native WoL provider that registers this URI, or send the standard packet from another LAN device. Martin reconnects and authenticates normally after waking. Do not expose UDP port 9 or Martin's HTTP endpoint to the public Internet.

The magic packet implementation is standard: six `FF` bytes followed by the validated six-byte MAC repeated sixteen times. Retries are capped at three. Sending a packet never marks the PC online; the authenticated Remote API must answer first.

## Current PC setup

Open Ethernet adapter Properties in Device Manager and verify **Wake on Magic Packet** plus **Allow this device to wake the computer**. Connect Ethernet for reliable wake. Firmware may also require “Wake on LAN”, “Power on by PCI-E”, or an equivalent option. Martin does not modify these settings or BIOS automatically.

Only one process can own port 8765. Start the Remote Agent before Martin Desktop. The desktop UI remains optional for basic remote access; starting the AI Brain from mobile launches only the approved `martin_ai.gui_app` component.

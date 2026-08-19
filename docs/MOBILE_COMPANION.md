# Martin AI mobile companion

The mobile companion is a progressive web app served by Martin's existing Phone Remote service. It uses the same trusted-device authentication, per-device permissions, rate limits, session expiry, screen capture, and control ownership rules as the desktop remote page.

## Start and pair

1. Start Martin AI with `start_gui.bat`.
2. Open **Phone Remote** and enable the server.
3. Generate a pairing code and open the displayed private-network address on the phone.
4. Enter the one-time code and device name. Keep **Trust this device** enabled.
5. On iPhone Safari, use **Share → Add to Home Screen**. Android browsers offer an equivalent install action.

The trusted credential is random and device-specific. Martin stores only its SHA-256 digest. The browser receives it in an HttpOnly, SameSite cookie and exchanges it for an expiring session. Removing or revoking the device invalidates existing credentials and sessions.

## Pages and updates

Bottom navigation opens Home, Remote, Files, Activity, and Settings. Skills, crashes, applications, and companion tools remain available as cards. Dashboard metrics refresh every five seconds while visible. Reconnection uses exponential delays from approximately one to thirty seconds and pauses expensive work while hidden.

Remote view lists each physical monitor separately plus an explicit All Displays choice. Selection is saved on the trusted-device record. The JPEG frame loop adapts to Martin's Low, Balanced, or High profile and pauses while the app is hidden. Capture failures show an error and retry state rather than silently presenting black output.

Mobile API resources are authenticated under `/api/mobile/`: `dashboard`, `skills`, `crashes`, `apps`, `notifications`, and `devices`. They reuse Martin's existing managers and do not expose credentials.

## Security boundary

The service binds only to a private local-network address and rejects non-private exposure. Screen viewing and input are separate per-device permissions. Control is exclusive and expires. Power operations need both device permission and phone confirmation. App close requests enter Martin's approval queue. Remote and Security skills cannot be disabled from mobile.

The built-in server currently uses HTTP on the private LAN. For encrypted transport, place it behind an authenticated TLS/VPN layer you control; do not port-forward it directly to the Internet.

## Companion tools and current limits

The Files page provides authenticated clipboard exchange and transfers to `Martin Shared`. Both are disabled on Martin PC by default and require a per-device permission. Transfers are limited to 25 MB, filename-sanitized, restricted to approved user folders, and never executed.

Favorites reuse the existing application launcher. Command history stores outcomes and permission metadata, not credentials. Terminal Safe Mode is disabled by default and accepts only an exact diagnostic allowlist: `ipconfig`, `systeminfo`, `tasklist`, `ping`, `Get-Process`, `Get-Service`, and `Get-ComputerInfo`.

Settings includes low-bandwidth streaming, inactivity timeout, compatibility information, sanitized diagnostics, and emergency disconnect. Emergency disconnect ends input, stream viewers, camera/audio/terminal state, and active sessions while preserving trusted-device registration. A phone cannot enable protected PC features or grant itself permissions.

Camera and PC-audio endpoints currently report unavailable because this build has no local webcam/loopback streaming provider. Native Face ID/fingerprint integration requires a packaged native client; the PWA does not imitate native biometrics. Offline queue primitives accept only application launches and expire after ten minutes. Native widgets and OS shortcuts remain future integration points because browser-installed PWA support varies by platform.

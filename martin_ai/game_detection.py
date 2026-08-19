from __future__ import annotations

import ctypes
import json
import os
import re
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import psutil

from .memory import MemoryManager


LAUNCHERS = {
    "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "epicwebhelper.exe",
    "goggalaxy.exe", "galaxyclient.exe", "battle.net.exe", "agent.exe", "eadesktop.exe",
    "origin.exe", "ubisoftconnect.exe", "upc.exe", "gamingservices.exe", "xboxpcapp.exe",
}
EMULATORS = {
    "rpcs3.exe": "RPCS3", "xenia.exe": "Xenia", "xenia_canary.exe": "Xenia",
    "pcsx2-qt.exe": "PCSX2", "pcsx2.exe": "PCSX2", "dolphin.exe": "Dolphin",
    "retroarch.exe": "RetroArch", "cemu.exe": "Cemu", "duckstation-qt-x64-release.exe": "DuckStation",
}
NON_GAMES = LAUNCHERS | {
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "discord.exe",
    "spotify.exe", "vlc.exe", "obs64.exe", "obs32.exe", "explorer.exe", "dwm.exe",
    "taskmgr.exe", "python.exe", "pythonw.exe", "ollama.exe", "llama-server.exe",
    "martin_ai.exe", "code.exe", "notepad.exe", "powershell.exe", "pwsh.exe", "cmd.exe",
    "conhost.exe", "windowsterminal.exe", "nvcontainer.exe", "nvidia app.exe", "msiafterburner.exe",
    "chatgpt.exe", "chatgpt classic.exe",
    "shellexperiencehost.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "runtimebroker.exe", "applicationframehost.exe", "svchost.exe", "systemsettings.exe",
    "taskhostw.exe", "textinputhost.exe", "sihost.exe", "ctfmon.exe", "audiodg.exe",
    "openconsole.exe", "phoneexperiencehost.exe", "crossdeviceservice.exe", "widgetservice.exe",
}
GAME_NAME_PATTERNS = ("-win64-shipping", "-win32-shipping", "shipping.exe", "game-win64", "unity")
GAME_PATH_MARKERS = ("steamapps\\common", "epic games", "gog galaxy\\games",
                     "xboxgames", "\\games\\", "\\game\\")
HELPER_PATTERNS = ("overlayrenderer", "crashreport", "crashhandler", "anticheat", "easyanticheat",
                   "launcher", "bootstrapper", "webhelper", "userhelper", "prereq", "unins", "setup", "updater")
GENERIC_TITLES = {"game", "client", "launcher", "shipping", "win64 shipping", "unity", "unreal", "untitled"}
WINDOWS_METADATA_MARKERS = {
    "microsoft windows operating system", "microsoft® windows® operating system",
    "windows operating system", "microsoft windows", "windows shell experience host",
    "windows start menu experience host", "host process for windows services",
    "client server runtime process", "desktop window manager",
}
WINDOWS_CORE_NAMES = NON_GAMES | {
    "system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "fontdrvhost.exe", "securityhealthservice.exe",
    "securityhealthsystray.exe", "spoolsv.exe", "werfault.exe", "wermgr.exe",
}
LAUNCHER_PATH_HINTS = {
    "\\steamapps\\common\\": "Steam", "\\epic games\\": "Epic Games",
    "\\xboxgames\\": "Xbox", "\\windowsapps\\": "Microsoft Store/Xbox",
    "\\ea games\\": "EA App", "\\origin games\\": "EA App",
    "\\ubisoft game launcher\\games\\": "Ubisoft Connect", "\\battle.net\\": "Battle.net",
    "\\gog galaxy\\games\\": "GOG",
}
_PROCESS_CACHE: dict[int, ProcessEvidence] = {}


@dataclass
class ProcessEvidence:
    pid: int
    executable: str
    path: str = ""
    title: str = ""
    foreground: bool = False
    create_time: float = 0.0
    memory_mb: float = 0.0
    cpu_percent: float = 0.0
    ppid: int = 0
    command_line: list[str] = field(default_factory=list)
    window_class: str = ""
    visible_window: bool = False


@dataclass
class GameDetection:
    detected: bool
    game_name: str | None = None
    executable: str | None = None
    path: str | None = None
    pids: list[int] = field(default_factory=list)
    confidence: int = 0
    confidence_label: str = "Low"
    foreground: bool = False
    source: str = "unknown"
    signals: list[str] = field(default_factory=list)
    possible_game: bool = False
    launcher: str | None = None
    app_id: str | None = None
    install_dir: str | None = None
    title_source: str = "unknown"
    metadata: dict[str, str] = field(default_factory=dict)
    window_title: str = ""
    window_class: str = ""
    accepted: bool = False
    rejection_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"detected": self.detected, "game": self.game_name, "executable": self.executable,
                "path": self.path, "pids": self.pids, "pid": self.pids[0] if self.pids else None,
                "confidence": self.confidence, "confidence_label": self.confidence_label,
                "foreground": self.foreground, "source": self.source, "signals": self.signals,
                "possible_game": self.possible_game, "launcher": self.launcher, "app_id": self.app_id,
                "install_dir": self.install_dir, "title_source": self.title_source,
                "metadata": self.metadata, "window_title": self.window_title,
                "window_class": self.window_class, "accepted": self.accepted,
                "rejection_reason": self.rejection_reason}


def window_evidence() -> tuple[int | None, dict[int, tuple[str, str]]]:
    if os.name != "nt": return None, {}
    user32 = ctypes.windll.user32; foreground_hwnd = user32.GetForegroundWindow(); foreground_pid = None; windows = {}
    pid_value = wintypes.DWORD(); user32.GetWindowThreadProcessId(foreground_hwnd, ctypes.byref(pid_value))
    if pid_value.value: foreground_pid = int(pid_value.value)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            pid = wintypes.DWORD(); user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            length = user32.GetWindowTextLengthW(hwnd); text = ctypes.create_unicode_buffer(length + 1)
            class_name = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, text, length + 1); user32.GetClassNameW(hwnd, class_name, 256)
            if pid.value and text.value: windows[int(pid.value)] = (text.value[:300], class_name.value[:120])
        return True
    user32.EnumWindows(callback, 0)
    return foreground_pid, windows


def process_evidence() -> list[ProcessEvidence]:
    foreground_pid, windows = window_evidence(); rows = []; active_pids = set()
    for proc in psutil.process_iter(["pid", "ppid", "name"]):
        try:
            info = proc.info; pid = int(info["pid"]); active_pids.add(pid); name = str(info.get("name") or "").lower()
            if not name: continue
            title, window_class = windows.get(pid, ("", "")); cached = _PROCESS_CACHE.get(pid)
            if cached and cached.executable == name:
                cached.title = title; cached.window_class = window_class; cached.visible_window = bool(title)
                cached.foreground = pid == foreground_pid; cached.ppid = int(info.get("ppid") or 0)
                if cached.foreground or cached.visible_window:
                    try:
                        cached.memory_mb = round(proc.memory_info().rss / 2**20, 1); cached.cpu_percent = float(proc.cpu_percent() or 0)
                    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError): pass
                rows.append(cached); continue
            try:
                path = str(proc.exe() or ""); created = float(proc.create_time() or 0); memory = proc.memory_info()
                memory_mb = round(memory.rss / 2**20, 1); cpu = float(proc.cpu_percent() or 0)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                path, created, memory_mb, cpu = "", 0.0, 0.0, 0.0
            command_line = []
            if name in EMULATORS:
                try: command_line = [str(value) for value in proc.cmdline()]
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError): pass
            evidence = ProcessEvidence(pid, name, path, title, pid == foreground_pid, created,
                        memory_mb, cpu,
                        int(info.get("ppid") or 0), command_line,
                        window_class, bool(title))
            _PROCESS_CACHE[pid] = evidence; rows.append(evidence)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, TypeError, ValueError): pass
    for stale in set(_PROCESS_CACHE) - active_pids: _PROCESS_CACHE.pop(stale, None)
    return rows


def executable_metadata(path: str) -> dict[str, str]:
    """Read Windows version-resource strings without spawning PowerShell."""
    if os.name != "nt" or not path or not Path(path).is_file(): return {}
    try:
        version = ctypes.windll.version; size = version.GetFileVersionInfoSizeW(path, None)
        if not size: return {}
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, buffer): return {}
        pointer = ctypes.c_void_p(); length = wintypes.UINT()
        translations = [(0x0409, 0x04B0), (0x0409, 0x04E4)]
        if version.VerQueryValueW(buffer, r"\VarFileInfo\Translation", ctypes.byref(pointer), ctypes.byref(length)) and length.value >= 4:
            pair = ctypes.cast(pointer, ctypes.POINTER(wintypes.WORD * 2)).contents
            translations.insert(0, (int(pair[0]), int(pair[1])))
        result = {}
        for key, output in (("ProductName", "product_name"), ("FileDescription", "file_description"),
                            ("CompanyName", "company_name"), ("OriginalFilename", "original_filename")):
            for language, codepage in translations:
                query = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{key}"
                if version.VerQueryValueW(buffer, query, ctypes.byref(pointer), ctypes.byref(length)) and pointer.value:
                    value = ctypes.wstring_at(pointer.value).strip()
                    if value: result[output] = value; break
        return result
    except (OSError, ValueError, AttributeError):
        return {}


def _clean_title(value: str) -> str:
    value = re.sub(r"\s+[-|—]\s+(RPCS3|Xenia|PCSX2|Dolphin|RetroArch|Cemu).*$", "", value, flags=re.I)
    value = re.sub(r"\s*\[[^]]*(Vulkan|OpenGL|Direct3D|FPS)[^]]*\]\s*", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip(" -|—")[:120]


class GameDetectionEngine:
    CACHE_SECONDS = 300

    def __init__(self, memory: MemoryManager | None = None, *, games_file: Path | None = None,
                 process_provider: Callable[[], list[ProcessEvidence]] = process_evidence,
                 metadata_provider: Callable[[str], dict[str, str]] = executable_metadata) -> None:
        self.memory = memory; self.games_file = games_file or Path(__file__).with_name("games.json")
        self.process_provider = process_provider; self.metadata_provider = metadata_provider
        self.known = self._load_known(); self.installed: dict[str, dict[str, str]] = {}
        self._manifest_at = 0.0; self._metadata_cache: dict[str, dict[str, str]] = {}; self._engine_cache: dict[str, list[str]] = {}
        self._last_process_signature: tuple[Any, ...] = (); self._last_detect_at = 0.0; self._last_detection: GameDetection | None = None
        self._last_candidates: list[GameDetection] = []; self.pending_unknown: GameDetection | None = None
        self._last_game_identity: tuple[str, str, float] | None = None
        self.refresh_library(force=True)

    def _load_known(self) -> dict[str, str]:
        try:
            data = json.loads(self.games_file.read_text(encoding="utf-8"))
            return {str(key).lower(): str(value) for key, value in data.items()}
        except (OSError, json.JSONDecodeError, ValueError): return {}

    @staticmethod
    def _steam_roots() -> list[Path]:
        root = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam" / "steamapps"
        roots = [root]; library_file = root / "libraryfolders.vdf"
        try:
            text = library_file.read_text(encoding="utf-8", errors="replace")
            for value in re.findall(r'"path"\s+"([^"]+)"', text):
                candidate = Path(value.replace("\\\\", "\\")) / "steamapps"
                if candidate not in roots: roots.append(candidate)
        except OSError: pass
        return roots

    @staticmethod
    def _launcher_metadata() -> dict[str, dict[str, str]]:
        entries: dict[str, dict[str, str]] = {}
        for root in GameDetectionEngine._steam_roots():
            if not root.is_dir(): continue
            for manifest in root.glob("appmanifest_*.acf"):
                try: text = manifest.read_text(encoding="utf-8", errors="replace")
                except OSError: continue
                name = re.search(r'"name"\s+"([^"]+)"', text); folder = re.search(r'"installdir"\s+"([^"]+)"', text)
                app_id = re.search(r'"appid"\s+"([^"]+)"', text)
                if name and folder:
                    path = str((root / "common" / folder.group(1)).resolve()).lower()
                    entries[path] = {"name": name.group(1), "launcher": "Steam", "app_id": app_id.group(1) if app_id else manifest.stem.removeprefix("appmanifest_")}
        epic = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
        if epic.is_dir():
            for manifest in epic.glob("*.item"):
                try: data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
                except (OSError, json.JSONDecodeError): continue
                location, name = data.get("InstallLocation"), data.get("DisplayName")
                if location and name:
                    entries[str(Path(location).resolve()).lower()] = {"name": str(name), "launcher": "Epic Games",
                        "app_id": str(data.get("CatalogItemId") or data.get("AppName") or "")}
        # GOG stores installed games in the registry; no drive scan is needed.
        if os.name == "nt":
            try:
                import winreg
                for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                    for base in (r"SOFTWARE\WOW6432Node\GOG.com\Games", r"SOFTWARE\GOG.com\Games"):
                        try: key = winreg.OpenKey(hive, base)
                        except OSError: continue
                        with key:
                            for index in range(winreg.QueryInfoKey(key)[0]):
                                app_id = winreg.EnumKey(key, index)
                                try:
                                    with winreg.OpenKey(key, app_id) as game:
                                        path = str(winreg.QueryValueEx(game, "path")[0]); name = str(winreg.QueryValueEx(game, "gameName")[0])
                                    entries[str(Path(path).resolve()).lower()] = {"name": name, "launcher": "GOG", "app_id": app_id}
                                except OSError: continue
            except (ImportError, OSError): pass
        return entries

    def refresh_library(self, *, force: bool = False) -> int:
        now = time.monotonic()
        if force or now - self._manifest_at >= self.CACHE_SECONDS:
            self.installed = self._launcher_metadata(); self._manifest_at = now
        return len(self.installed)

    def _learned(self) -> dict[str, dict[str, Any]]:
        if not self.memory: return {}
        learned = {}
        for row in self.memory.list_game_profiles():
            profile = row.get("profile", {}); keys = set(profile.get("executables", [row.get("game_key")]))
            keys.update(profile.get("path_fingerprints", []))
            for key in keys: learned[str(key).lower()] = {"name": row["display_name"], **profile}
        return learned

    def _metadata(self, path: str) -> dict[str, str]:
        key = path.lower()
        if key not in self._metadata_cache: self._metadata_cache[key] = self.metadata_provider(path)
        return self._metadata_cache[key]

    @staticmethod
    def _windows_rejection(proc: ProcessEvidence, metadata: dict[str, str]) -> str | None:
        exe = proc.executable.lower(); path = proc.path.lower().replace("/", "\\")
        product = metadata.get("product_name", "").strip().lower()
        description = metadata.get("file_description", "").strip().lower()
        original = metadata.get("original_filename", "").strip().lower()
        company = metadata.get("company_name", "").strip().lower()
        windows_dir = os.environ.get("WINDIR", r"C:\Windows").lower().rstrip("\\")
        in_core_dir = bool(path and (path.startswith(windows_dir + "\\system32\\")
            or path.startswith(windows_dir + "\\syswow64\\")
            or path.startswith(windows_dir + "\\winsxs\\")
            or path.startswith(windows_dir + "\\systemapps\\")))
        windows_metadata = any(marker in {product, description} for marker in WINDOWS_METADATA_MARKERS)
        microsoft_windows_component = ("microsoft" in company and
            (windows_metadata or original in WINDOWS_CORE_NAMES or exe in WINDOWS_CORE_NAMES))
        if exe in WINDOWS_CORE_NAMES: return "Windows system process"
        if in_core_dir: return "Windows system directory"
        if microsoft_windows_component: return "core Microsoft Windows component"
        return None

    @staticmethod
    def _trustworthy_metadata_title(metadata: dict[str, str]) -> str | None:
        for value in (metadata.get("product_name", ""), metadata.get("file_description", "")):
            clean = _clean_title(value)
            lowered = clean.lower()
            if (clean and lowered not in GENERIC_TITLES
                    and lowered not in WINDOWS_METADATA_MARKERS
                    and not any(word in lowered for word in ("launcher", "crash", "setup", "bootstrapper"))):
                return clean
        return None

    @staticmethod
    def _fallback_title(proc: ProcessEvidence) -> tuple[str, str]:
        window = _clean_title(proc.title)
        executable_key = re.sub(r"(?:-win(?:32|64)-shipping|\.exe)$", "", proc.executable.lower())
        folder_key = Path(proc.path).parent.name.lower() if proc.path else ""
        if (window and window.lower() not in GENERIC_TITLES and window.lower() not in WINDOWS_METADATA_MARKERS
                and window.lower() not in {executable_key, folder_key}):
            return window, "main window title"
        return "Unknown Game", "unidentified high-confidence game"

    def _engine_signals(self, path: str) -> list[str]:
        if not path: return []
        exe = Path(path); folder = exe.parent; key = str(folder).lower()
        if key in self._engine_cache: return self._engine_cache[key]
        signals = []
        try:
            names = {item.name.lower() for item in folder.iterdir()}
            if "chrome_elf.dll" in names or "resources.pak" in names:
                self._engine_cache[key] = ["desktop application runtime"]
                return self._engine_cache[key]
            if "unityplayer.dll" in names or any(name.endswith("_data") for name in names): signals.append("Unity engine files")
            if (folder / "Engine").exists() or "ue4commandline.txt" in names or "uecommandline.txt" in names: signals.append("Unreal Engine files")
            if any(name in names for name in ("dxgi.dll", "d3d11.dll", "d3d12.dll", "vulkan-1.dll", "opengl32.dll")): signals.append("graphics API runtime files")
        except OSError: pass
        self._engine_cache[key] = signals
        return signals

    @staticmethod
    def _emulated_title(proc: ProcessEvidence, emulator: str) -> str | None:
        title = _clean_title(proc.title)
        if title and title.lower() != emulator.lower() and not title.lower().startswith(emulator.lower()): return title
        for arg in reversed(proc.command_line[1:]):
            suffix = Path(arg.strip('"')).suffix.lower()
            if suffix in {".iso", ".elf", ".bin", ".cue", ".chd", ".rvz", ".wbfs", ".xex", ".pkg"}:
                return _clean_title(Path(arg.strip('"')).stem)
        return None

    def _score(self, proc: ProcessEvidence, processes: list[ProcessEvidence], learned: dict[str, dict[str, Any]]) -> GameDetection | None:
        exe = proc.executable.lower(); path_lower = proc.path.lower()
        score, signals, name, source, launcher, app_id, title_source = 0, [], None, "heuristic", None, None, "unknown"
        metadata = self._metadata(proc.path); folder_fingerprint = str(Path(proc.path).parent).lower() if proc.path else ""
        rejected = self._windows_rejection(proc, metadata)
        if rejected or exe in NON_GAMES:
            reason = rejected or "excluded application"
            return GameDetection(False, None, exe, proc.path or None, [proc.pid], 0, "Low", proc.foreground,
                "rejected", [], window_title=proc.title, window_class=proc.window_class,
                metadata=metadata, accepted=False, rejection_reason=reason)
        if any(pattern in exe for pattern in HELPER_PATTERNS) and exe not in EMULATORS:
            return GameDetection(False, None, exe, proc.path or None, [proc.pid], 0, "Low", proc.foreground,
                "rejected", [], window_title=proc.title, window_class=proc.window_class,
                metadata=metadata, accepted=False, rejection_reason="launcher/helper executable")
        profile = learned.get(path_lower) or learned.get(folder_fingerprint) or learned.get(exe)
        if profile:
            score += 72; name = str(profile["name"]); source = "learned"; launcher = profile.get("launcher")
            title_source = "learned fingerprint"; signals.append("persistent learned fingerprint")
        if exe in self.known and not name:
            score += 48; name = self.known[exe]; source = "known database"; title_source = "known executable"; signals.append("known game executable")
        for root, item in sorted(self.installed.items(), key=lambda value: len(value[0]), reverse=True):
            if path_lower == root or path_lower.startswith(root + os.sep.lower()):
                score += 72; name = item["name"]; launcher = item["launcher"]; app_id = item.get("app_id")
                source = launcher; title_source = "launcher manifest"; signals.append(f"{launcher} manifest path match"); break
        emulator = EMULATORS.get(exe)
        if emulator:
            score += 58; launcher = emulator; source = "emulator"; signals.append(f"{emulator} emulator")
            emulated = self._emulated_title(proc, emulator)
            name = emulated or emulator; title_source = "emulator window/command line" if emulated else "emulator"
        metadata_title = self._trustworthy_metadata_title(metadata)
        if metadata_title:
            score += 8; signals.append("trustworthy executable metadata")
            if not name: name = metadata_title; source = "file metadata"; title_source = "executable metadata"
        engine = self._engine_signals(proc.path)
        if "desktop application runtime" in engine and not (profile or launcher or emulator or exe in self.known): return None
        if engine: score += min(24, 14 + len(engine) * 4); signals.extend(engine)
        if any(marker in path_lower for marker in GAME_PATH_MARKERS): score += 22; signals.append("game installation directory")
        for marker, inferred_launcher in LAUNCHER_PATH_HINTS.items():
            if marker in path_lower:
                launcher = launcher or inferred_launcher; score += 8; signals.append(f"{inferred_launcher} installation path"); break
        if any(pattern in exe for pattern in GAME_NAME_PATTERNS): score += 16; signals.append("game-engine executable pattern")
        if proc.visible_window: score += 10; signals.append("visible titled window")
        if proc.foreground: score += 40; signals.append("foreground window")
        if proc.memory_mb >= 750: score += 10; signals.append("large process working set")
        elif proc.memory_mb >= 300: score += 5; signals.append("substantial process working set")
        if proc.cpu_percent >= 10: score += 6; signals.append("active CPU usage")
        if proc.create_time and time.time() - proc.create_time < 900: score += 4; signals.append("recently launched")
        process_by_pid = {row.pid: row for row in processes}
        parent = process_by_pid.get(proc.ppid); ancestor = parent; depth = 0
        while ancestor and depth < 6:
            if ancestor.executable.lower() in LAUNCHERS:
                score += 12; signals.append(f"launcher ancestry: {ancestor.executable}"); break
            ancestor = process_by_pid.get(ancestor.ppid); depth += 1
        children = [row for row in processes if row.ppid == proc.pid]
        if children and any("crash" in row.executable or "anticheat" in row.executable for row in children):
            score += 5; signals.append("game helper child process")
        recent_identity = bool(self._last_game_identity and
            (exe == self._last_game_identity[0] or path_lower == self._last_game_identity[1]))
        if recent_identity:
            score += 60; signals.append("active game session retained after focus loss")
        manifest_match = bool(launcher and source not in {"heuristic", "file metadata", "known database"})
        active_standalone = bool(proc.foreground and proc.visible_window and
            (proc.memory_mb >= 300 or proc.cpu_percent >= 5) and
            (bool(engine) or bool(metadata_title) or proc.memory_mb >= 750))
        if active_standalone:
            score += 10; signals.append("resource-intensive foreground application")
        strong_anchor = bool(profile or manifest_match or emulator or exe in self.known or active_standalone
                             or recent_identity
                             or any(marker in path_lower for marker in GAME_PATH_MARKERS)
                             or any(item in engine for item in ("Unity engine files", "Unreal Engine files"))
                             or (proc.visible_window and any(pattern in exe for pattern in GAME_NAME_PATTERNS)))
        if metadata.get("company_name", "").lower() == "openai" or "chatgpt" in (metadata_title or "").lower(): return None
        score = min(score, 100); accepted = score >= 75 and strong_anchor
        if accepted and not name:
            name, title_source = self._fallback_title(proc)
        label = "High" if score >= 75 else "Medium" if score >= 55 else "Low"
        reason = "" if accepted else ("confidence below activation threshold" if score >= 45 else "insufficient game evidence")
        return GameDetection(accepted, name, exe, proc.path or None, [proc.pid], score, label, proc.foreground,
            source, signals, possible_game=score >= 45 and not accepted, launcher=launcher, app_id=app_id,
            install_dir=str(Path(proc.path).parent) if proc.path else None, title_source=title_source,
            metadata=metadata, window_title=proc.title, window_class=proc.window_class,
            accepted=accepted, rejection_reason=reason)

    def detect(self, *, force: bool = False) -> GameDetection | None:
        self.refresh_library(); learned = self._learned(); processes = self.process_provider()
        signature = tuple(sorted((proc.pid, proc.create_time, proc.title, proc.foreground) for proc in processes))
        foreground = next((proc.pid for proc in processes if proc.foreground), 0)
        signature += ((-1, float(foreground)),)
        if not force and signature == self._last_process_signature and time.monotonic() - self._last_detect_at < 2.0:
            return self._last_detection
        self._last_process_signature = signature
        self._last_detect_at = time.monotonic()
        process_by_pid = {proc.pid: proc for proc in processes}
        def worth_scoring(proc: ProcessEvidence) -> bool:
            path = proc.path.lower(); parent = process_by_pid.get(proc.ppid)
            return bool(proc.foreground or proc.visible_window or proc.memory_mb >= 300 or proc.executable in self.known
                        or proc.executable in EMULATORS or any(marker in path for marker in GAME_PATH_MARKERS)
                        or any(marker in path for marker in LAUNCHER_PATH_HINTS)
                        or any(pattern in proc.executable for pattern in GAME_NAME_PATTERNS)
                        or (parent and parent.executable in LAUNCHERS))
        candidates = [item for proc in processes if worth_scoring(proc)
                      if (item := self._score(proc, processes, learned)) is not None]
        self._last_candidates = sorted(candidates, key=lambda item: (item.detected, item.foreground, item.confidence), reverse=True)[:12]
        accepted = [item for item in candidates if item.detected]
        detection = max(accepted, key=lambda item: (item.foreground, item.confidence), default=None)
        if detection is None:
            detection = max((item for item in candidates if item.possible_game),
                            key=lambda item: (item.foreground, item.confidence), default=None)
        if detection:
            selected = process_by_pid.get(detection.pids[0]) if detection.pids else None
            family = {selected.pid} if selected else set()
            changed = True
            while changed:
                before = len(family); family.update(proc.pid for proc in processes if proc.ppid in family); changed = len(family) != before
            family.update(proc.pid for proc in processes if proc.executable.lower() == detection.executable)
            detection.pids = sorted(family)
            if detection.detected:
                self._last_game_identity = (str(detection.executable), str(detection.path or "").lower(), time.monotonic())
                self._learn_detection(detection)
        elif self._last_game_identity:
            live_names = {proc.executable.lower() for proc in processes}
            if self._last_game_identity[0] not in live_names: self._last_game_identity = None
        self.pending_unknown = detection if detection and (detection.game_name == "Unknown Game" or detection.possible_game) else (
            self._last_candidates[0] if self._last_candidates and self._last_candidates[0].possible_game else None)
        self._last_detection = detection
        return detection

    def _learn_detection(self, detection: GameDetection) -> None:
        if not self.memory or not detection.executable or not detection.game_name: return
        key = detection.executable.lower(); profile = self.memory.get_game_profile(key) or {}
        fingerprint = str(Path(detection.path).parent).lower() if detection.path else ""
        values = sorted(set(profile.get("path_fingerprints", []) + ([fingerprint] if fingerprint else [])))
        updated = {**profile, "executables": sorted(set(profile.get("executables", []) + [key])),
                   "path_fingerprints": values, "path": detection.path, "install_dir": detection.install_dir,
                   "launcher": detection.launcher, "app_id": detection.app_id, "metadata": detection.metadata,
                   "detection_source": detection.source, "confidence": detection.confidence}
        if updated != profile: self.memory.update_game_profile(key, detection.game_name, updated)

    def learn_pending(self, display_name: str) -> bool:
        pending = self.pending_unknown; clean = re.sub(r"\s+", " ", display_name).strip(" .!?")[:120]
        if not pending or not pending.executable or not clean or not self.memory: return False
        pending.game_name = clean; key = pending.executable.lower(); self._learn_detection(pending)
        profile = self.memory.get_game_profile(key) or {}
        profile["identity_source"] = "user_correction"
        self.memory.update_game_profile(key, clean, profile); self.pending_unknown = None
        self._last_process_signature = (); return True

    def debug_report(self) -> dict[str, Any]:
        self.detect(force=True)
        foreground = next((item for item in self._last_candidates if item.foreground), None)
        rejected = [item.as_dict() for item in self._last_candidates if item.rejection_reason][:4]
        return {"active": self._last_detection.as_dict() if self._last_detection else None,
                "foreground": foreground.as_dict() if foreground else None,
                "candidates": [item.as_dict() for item in self._last_candidates if not item.rejection_reason][:5],
                "rejected": rejected,
                "manifest_entries": len(self.installed), "activation_threshold": 75}

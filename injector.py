"""
FrostInject — Roblox FFlag Injector
Single-file, no external UI deps (tkinter only).
PyInstaller onefile target.
"""

import sys
import os
import re
import json
import time
import stat
import base64
import hashlib
import ctypes
import ctypes.wintypes
import threading
import webbrowser
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

# ── tkinter ────────────────────────────────────────────────────────────────
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# ── optional deps (pymem) ──────────────────────────────────────────────────
try:
    import pymem
    import pymem.process
    _PYMEM = True
except ImportError:
    _PYMEM = False

# ══════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ══════════════════════════════════════════════════════════════════════════
APP_NAME    = "FrostInject"
APP_VERSION = "1.0.0"

OFFSET_URL  = "https://offsets.imtheo.lol/fflags.hpp"
HTTP_TIMEOUT = 10

FPREFIX = ("DFString","SFString","FString","DFFlag","SFFlag","FFlag",
           "DFInt","SFInt","FInt","DFLog","SFLog","FLog")

_PREFIX_TYPE = {}
for p in ("FFlag","DFFlag","SFFlag"): _PREFIX_TYPE[p] = "Bool"
for p in ("FInt","DFInt","SFInt"):    _PREFIX_TYPE[p] = "Int"
for p in ("FLog","DFLog","SFLog"):    _PREFIX_TYPE[p] = "Log"
for p in ("FString","DFString","SFString"): _PREFIX_TYPE[p] = "String"

# ── app data dir ───────────────────────────────────────────────────────────
def _appdir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base

APPDIR          = _appdir()
CACHE_FILE      = APPDIR / "offsets.dat"
FLAGS_FILE      = APPDIR / "flags.json"
SETTINGS_FILE   = APPDIR / "settings.json"
CACHE_MAX_AGE_H = 24

# ══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════
def clean_name(n: str) -> str:
    for p in FPREFIX:
        if n.startswith(p): return n[len(p):]
    return n

def flag_type(n: str) -> str:
    for p in FPREFIX:
        if n.startswith(p): return _PREFIX_TYPE.get(p, "Bool")
    return "Bool"

def _safe_write(path: Path, text: str) -> bool:
    try:
        try: ctypes.windll.kernel32.SetFileAttributesW(str(path), 128)
        except Exception: pass
        path.write_text(text, encoding="utf-8")
        return True
    except Exception: return False

def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text("utf-8"))
    except Exception: pass
    return default

def save_json(path: Path, obj) -> bool:
    return _safe_write(path, json.dumps(obj, indent=2, ensure_ascii=False))

# ══════════════════════════════════════════════════════════════════════════
#  OFFSET MANAGER
# ══════════════════════════════════════════════════════════════════════════
class OffsetManager:
    def __init__(self):
        self.offsets: dict[str,int] = {}
        self.version: str | None    = None
        self._lock                  = threading.RLock()
        self._load_cache()

    # ── parse ──────────────────────────────────────────────────────────────
    @staticmethod
    def _extract_ns(src: str, ns: str) -> str | None:
        m = re.search(rf"\bnamespace\s+{re.escape(ns)}\s*\{{", src)
        if not m: return None
        ob = src.find("{", m.start()); depth = 0
        for i in range(ob, len(src)):
            if src[i] == "{": depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0: return src[ob+1:i]
        return None

    @classmethod
    def _parse(cls, src: str) -> tuple[dict,str|None]:
        sec = cls._extract_ns(src, "FFlags")
        if not sec: return {}, None
        offs = {n: int(v,16) for n,v in
                re.findall(r"\b(?:std::)?uintptr_t\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+)\s*;", sec)}
        vm = re.search(r'ClientVersion\s*=\s*"([^"]+)"', src)
        return offs, (vm.group(1) if vm else None)

    # ── cache ──────────────────────────────────────────────────────────────
    def _load_cache(self):
        d = load_json(CACHE_FILE, {})
        if not d: return
        with self._lock:
            self.offsets = {k: int(v) for k,v in d.get("offsets",{}).items()}
            self.version = d.get("version")
        ts = d.get("ts")
        if ts: self._ts = datetime.fromisoformat(ts)
        else:  self._ts = datetime.min

    def _save_cache(self):
        with self._lock:
            d = {"offsets": self.offsets, "version": self.version,
                 "ts": datetime.now().isoformat()}
        save_json(CACHE_FILE, d)
        self._ts = datetime.now()

    def is_fresh(self) -> bool:
        try: return datetime.now() - self._ts < timedelta(hours=CACHE_MAX_AGE_H)
        except Exception: return False

    # ── fetch ──────────────────────────────────────────────────────────────
    def sync(self, log) -> bool:
        try:
            log(f"Fetching offsets from {OFFSET_URL}…")
            req = urllib.request.Request(
                OFFSET_URL,
                headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                src = r.read().decode("utf-8")
            offs, ver = self._parse(src)
            if not offs:
                log("FAIL: no FFlags namespace in response"); return False
            with self._lock:
                self.offsets = offs; self.version = ver
            self._save_cache()
            log(f"OK: loaded {len(offs)} offsets (ver {ver})")
            return True
        except Exception as e:
            log(f"FAIL: {e}"); return False

    # ── resolve ────────────────────────────────────────────────────────────
    def resolve(self, name: str) -> str | None:
        with self._lock:
            if name in self.offsets: return name
            c = clean_name(name)
            if c in self.offsets: return c
            for p in FPREFIX:
                for cand in (p+name, p+c):
                    if cand in self.offsets: return cand
        return None

    def get_offset(self, name: str) -> int | None:
        a = self.resolve(name)
        if a is None: return None
        with self._lock: return self.offsets.get(a)

    def all_names(self) -> list[str]:
        with self._lock: return sorted(self.offsets.keys())

# ══════════════════════════════════════════════════════════════════════════
#  ROBLOX PROCESS FINDER
# ══════════════════════════════════════════════════════════════════════════
def find_roblox_pids() -> list[int]:
    if not _PYMEM: return []
    out = []
    try:
        for p in pymem.process.list_processes():
            if p.th32ProcessID and \
               "robloxplayerbeta.exe" in p.szExeFile.decode("utf-8","ignore").lower():
                out.append(p.th32ProcessID)
    except Exception: pass
    return out

def get_module_base(pid: int) -> int | None:
    k  = ctypes.WinDLL("kernel32", use_last_error=True)
    ps = ctypes.WinDLL("psapi",    use_last_error=True)
    k.OpenProcess.argtypes  = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
    k.OpenProcess.restype   = ctypes.wintypes.HANDLE
    h = k.OpenProcess(1040, False, pid)
    if not h: return None
    try:
        mods   = (ctypes.c_void_p * 1024)()
        needed = ctypes.wintypes.DWORD()
        if ps.EnumProcessModules(h, mods, ctypes.sizeof(mods), ctypes.byref(needed)) \
           and needed.value >= ctypes.sizeof(ctypes.c_void_p):
            return int(mods[0])
    finally: k.CloseHandle(h)
    return None

def get_exe_path(pid: int) -> str | None:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes             = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
    k.OpenProcess.restype              = ctypes.wintypes.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD,
                                              ctypes.wintypes.LPWSTR,
                                              ctypes.POINTER(ctypes.wintypes.DWORD)]
    k.QueryFullProcessImageNameW.restype  = ctypes.wintypes.BOOL
    h = k.OpenProcess(0x1000, False, pid)
    if not h: return None
    try:
        sz  = ctypes.wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(sz.value)
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(sz)):
            return buf.value
    finally: k.CloseHandle(h)
    return None

# ══════════════════════════════════════════════════════════════════════════
#  MEMORY INJECTOR
# ══════════════════════════════════════════════════════════════════════════
class MemInjector:
    def __init__(self, pid: int, pm, base: int, offmgr: OffsetManager):
        self.pid  = pid
        self.pm   = pm
        self.base = base
        self.omgr = offmgr
        self.k32  = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.WriteProcessMemory.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.k32.WriteProcessMemory.restype  = ctypes.wintypes.BOOL
        self._hproc = pm.process_handle
        self.defaults: dict[str,bytes] = {}
        self.last_err = ""

    def _write(self, addr: int, data: bytes) -> bool:
        buf = ctypes.create_string_buffer(data, len(data))
        written = ctypes.c_size_t(0)
        ok = self.k32.WriteProcessMemory(
            self._hproc, ctypes.c_void_p(addr),
            buf, len(data), ctypes.byref(written))
        if not ok or written.value != len(data):
            self.last_err = f"WPM failed at 0x{addr:X}"; return False
        return True

    def _read(self, addr: int, n: int) -> bytes:
        return bytes(self.pm.read_bytes(addr, n))

    def _addr(self, name: str) -> int | None:
        off = self.omgr.get_offset(name)
        if off is None: self.last_err = f"no offset: {name}"; return None
        return self.base + off

    # std::string read/write (MSVC ABI: sso cap=15, heap if cap>=16)
    def _read_str(self, addr: int) -> str:
        raw  = self._read(addr, 32)
        size = int.from_bytes(raw[16:24], "little")
        cap  = int.from_bytes(raw[24:32], "little")
        if size > cap or cap > 16*1024*1024: raise ValueError("bad std::string")
        data = raw[:size] if cap < 16 else self._read(int.from_bytes(raw[:8],"little"), size)
        return data.decode("utf-8")

    def _write_str(self, addr: int, value: str) -> bool:
        enc = value.encode("utf-8")
        raw = self._read(addr, 32)
        cap = int.from_bytes(raw[24:32], "little")
        if cap > 16*1024*1024: self.last_err="bad string"; return False
        if len(enc) > cap: self.last_err=f"string too long ({len(enc)}>{cap})"; return False
        tgt = addr if cap < 16 else int.from_bytes(raw[:8], "little")
        if not tgt: self.last_err="null buf"; return False
        if not self._write(tgt, enc + b"\x00"): return False
        return self._write(addr+16, len(enc).to_bytes(8,"little"))

    def _encodings(self, value: str, primary: str):
        v = value.strip(); vl = v.lower(); out = []
        def add(t, payload):
            if primary == t: out.insert(0, (t, payload))
            else:            out.append((t, payload))
        if vl in ("true","1","yes","on"):
            add("Bool", (1).to_bytes(4,"little",signed=True))
        elif vl in ("false","0","no","off"):
            add("Bool", (0).to_bytes(4,"little",signed=True))
        try:
            iv = int(v)
            if -(2**31) <= iv < 2**31:
                add("Int", iv.to_bytes(4,"little",signed=True))
        except Exception: pass
        if any(c in v for c in ".eE"):
            try:
                fv = ctypes.c_float(float(v))
                import struct
                add("Float", struct.pack("<f", fv.value))
            except Exception: pass
        try:
            lv = int(v)
            if 0 <= lv <= 255: add("Log", bytes([lv]))
        except Exception: pass
        out.append(("String", None))
        return out

    def set_flag(self, name: str, value: str) -> bool:
        self.last_err = ""
        addr = self._addr(name)
        if addr is None: return False
        ptype = flag_type(self.omgr.resolve(name) or name)
        # save default once
        if name not in self.defaults:
            try: self.defaults[name] = self._read(addr, 32)
            except Exception: pass
        for tname, payload in self._encodings(value, ptype):
            try:
                if tname == "String":
                    if self._write_str(addr, value) and self._read_str(addr) == value:
                        return True
                else:
                    if self._write(addr, payload) and self._read(addr, len(payload)) == payload:
                        return True
            except Exception as e:
                self.last_err = str(e)
        return False

    def verify_flag(self, name: str, value: str) -> bool:
        addr = self._addr(name)
        if addr is None: return False
        ptype = flag_type(self.omgr.resolve(name) or name)
        for tname, payload in self._encodings(value, ptype):
            try:
                if tname == "String":
                    if self._read_str(addr) == value: return True
                elif self._read(addr, len(payload)) == payload: return True
            except Exception: pass
        return False

    def restore_all(self) -> int:
        n = 0
        for name, snap in self.defaults.items():
            addr = self._addr(name)
            if addr and self._write(addr, snap[:min(len(snap),4)]): n += 1
        return n

# ══════════════════════════════════════════════════════════════════════════
#  STARTUP FILE PERSIST
# ══════════════════════════════════════════════════════════════════════════
def _startup_targets(exe_path: str | None) -> list[Path]:
    cands = []
    lad   = os.environ.get("LOCALAPPDATA","")
    if exe_path:
        vd = Path(exe_path).parent
        cands.append(vd / "ClientSettings" / "ClientAppSettings.json")
    if lad:
        for bs in ("Froststrap","Bloxstrap"):
            p = Path(lad) / bs / "Modifications" / "ClientSettings" / "ClientAppSettings.json"
            if p.exists() or p.parent.is_dir(): cands.append(p)
        vr = Path(lad) / "Roblox" / "Versions"
        if vr.is_dir():
            best = max(
                ((d.stat().st_mtime, d) for d in vr.iterdir()
                 if (d/"RobloxPlayerBeta.exe").is_file()),
                default=(0, None))[1]
            if best:
                cands.append(best / "ClientSettings" / "ClientAppSettings.json")
    seen, out = set(), []
    for c in cands:
        k = c.resolve()
        if k not in seen:
            seen.add(k); out.append(c)
    return out

def write_startup(flags: dict[str,str], exe_path: str | None) -> list[str]:
    targets = _startup_targets(exe_path)
    written = []
    for t in targets:
        try:
            t.parent.mkdir(parents=True, exist_ok=True)
            existing = {}
            if t.exists():
                try: existing = json.loads(t.read_text("utf-8"))
                except Exception: pass
            existing.update(flags)
            _safe_write(t, json.dumps(existing, indent=2))
            written.append(str(t))
        except Exception: pass
    return written

def clear_startup(names: list[str], exe_path: str | None):
    for t in _startup_targets(exe_path):
        if not t.exists(): continue
        try:
            d = json.loads(t.read_text("utf-8"))
            for n in names: d.pop(n, None)
            _safe_write(t, json.dumps(d, indent=2))
        except Exception: pass

# ══════════════════════════════════════════════════════════════════════════
#  FPS UNLOCK
# ══════════════════════════════════════════════════════════════════════════
_CAP_RE = re.compile(r'(<int name="FramerateCap">)\d+(</int>)')

def _fps_path() -> Path | None:
    lad = os.environ.get("LOCALAPPDATA","")
    if not lad: return None
    return Path(lad) / "Roblox" / "GlobalBasicSettings_13.xml"

def unlock_fps(cap: int = 9999) -> tuple[bool,str]:
    p = _fps_path()
    if not p or not p.is_file(): return False, "GlobalBasicSettings not found"
    try:
        was_ro = not os.access(p, os.W_OK)
        text   = p.read_text("utf-8")
        if was_ro: os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        if _CAP_RE.search(text):
            new = _CAP_RE.sub(rf"\g<1>{cap}\g<2>", text)
        else:
            new = text.replace("</Properties>",
                               f'\t\t\t<int name="FramerateCap">{cap}</int>\n</Properties>',1)
        if new != text: _safe_write(p, new)
        os.chmod(p, stat.S_IREAD)
        return True, f"FramerateCap → {cap}"
    except Exception as e: return False, str(e)

def restore_fps() -> tuple[bool,str]:
    p = _fps_path()
    if not p or not p.is_file(): return False, "file not found"
    try:
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        return True, "FPS lock released"
    except Exception as e: return False, str(e)

# ══════════════════════════════════════════════════════════════════════════
#  CORE INJECTOR CONTROLLER
# ══════════════════════════════════════════════════════════════════════════
class InjectorCore:
    def __init__(self, log):
        self._log   = log
        self.omgr   = OffsetManager()
        self._procs : dict[int, dict] = {}
        self._lock  = threading.RLock()
        self.saved  : dict[str,str] = load_json(FLAGS_FILE, {})
        self._exe   : str | None = None

        if not self.omgr.is_fresh():
            threading.Thread(target=self.omgr.sync, args=(log,), daemon=True).start()
        else:
            log(f"Offsets cached ({len(self.omgr.offsets)} entries, ver {self.omgr.version})")

        threading.Thread(target=self._watch, daemon=True).start()

    # ── process watcher ────────────────────────────────────────────────────
    def _watch(self):
        while True:
            if _PYMEM:
                current = set(find_roblox_pids())
                with self._lock: known = set(self._procs)
                # detach closed
                for pid in known - current:
                    with self._lock: t = self._procs.pop(pid, None)
                    if t:
                        try: t["inj"].restore_all()
                        except Exception: pass
                        try: t["pm"].close_process()
                        except Exception: pass
                    self._log(f"Roblox PID {pid} closed")
                # attach new
                for pid in sorted(current - known):
                    self._attach(pid)
            time.sleep(2)

    def _attach(self, pid: int):
        try:
            pm   = pymem.Pymem(pid)
            base = get_module_base(pid)
            if not base: raise RuntimeError("module base not found")
            exe  = get_exe_path(pid)
            if exe: self._exe = exe
            inj  = MemInjector(pid, pm, base, self.omgr)
            with self._lock:
                self._procs[pid] = {"pm": pm, "base": base, "inj": inj}
            self._log(f"Attached → PID {pid}")
            if self.saved:
                ok = fail = 0
                for n, v in self.saved.items():
                    if inj.set_flag(n, v): ok += 1
                    else: fail += 1; self._log(f"  FAIL {n}: {inj.last_err}")
                self._log(f"Reapplied {ok}/{ok+fail} saved flags to PID {pid}")
        except Exception as e:
            self._log(f"Attach PID {pid} failed: {e}")
            with self._lock: self._procs.pop(pid, None)

    # ── public API ─────────────────────────────────────────────────────────
    def is_attached(self) -> bool:
        with self._lock: return bool(self._procs)

    def process_count(self) -> int:
        with self._lock: return len(self._procs)

    def _injectors(self) -> list:
        with self._lock: return [t["inj"] for t in self._procs.values()]

    def apply(self, flags: dict[str,str]) -> tuple[int,int]:
        """Returns (success, fail)."""
        ok = fail = 0
        if not flags: return 0, 0
        injs = self._injectors()
        if not injs:
            # no process: persist only
            self.saved.update(flags)
            save_json(FLAGS_FILE, self.saved)
            write_startup(flags, self._exe)
            self._log(f"Roblox not running — saved {len(flags)} flags for next launch")
            return 0, 0
        for n, v in flags.items():
            flag_ok = False
            for inj in injs:
                if inj.set_flag(n, v) and inj.verify_flag(n, v):
                    flag_ok = True
                else:
                    self._log(f"  FAIL {n}: {inj.last_err}")
            if flag_ok: ok += 1
            else:       fail += 1
        self.saved.update(flags)
        save_json(FLAGS_FILE, self.saved)
        write_startup(flags, self._exe)
        return ok, fail

    def uninject(self, names: list[str] | None = None) -> int:
        names = names or list(self.saved.keys())
        restored = 0
        for inj in self._injectors():
            restored += inj.restore_all()
        clear_startup(names, self._exe)
        for n in names: self.saved.pop(n, None)
        save_json(FLAGS_FILE, self.saved)
        return restored

    def sync_offsets(self):
        threading.Thread(target=self.omgr.sync, args=(self._log,), daemon=True).start()

# ══════════════════════════════════════════════════════════════════════════
#  DARK THEME PALETTE
# ══════════════════════════════════════════════════════════════════════════
BG     = "#0d0d14"
BG2    = "#13131e"
BG3    = "#1a1a2e"
ACCENT = "#7c6af7"
ACCENT2= "#a89af9"
FG     = "#e2deff"
FG2    = "#9b94cc"
BORDER = "#2a2845"
RED    = "#e05c5c"
GREEN  = "#5ce08a"
FONT   = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")
MONO   = ("Consolas", 9)

# ══════════════════════════════════════════════════════════════════════════
#  STYLED WIDGETS
# ══════════════════════════════════════════════════════════════════════════
class Btn(tk.Button):
    def __init__(self, parent, text, cmd=None, accent=False, danger=False, **kw):
        bg  = ACCENT if accent else (RED if danger else BG3)
        fg  = "#0a0a14" if accent else (FG if danger else FG)
        abg = ACCENT2 if accent else (RED+"dd" if danger else "#22223a")
        super().__init__(parent, text=text, command=cmd,
                         bg=bg, fg=fg, activebackground=abg, activeforeground=fg,
                         relief="flat", borderwidth=0, padx=12, pady=5,
                         font=FONT_B if accent else FONT,
                         cursor="hand2", **kw)
        self.bind("<Enter>", lambda _: self.config(bg=abg))
        self.bind("<Leave>", lambda _: self.config(bg=bg))

class Entry(tk.Entry):
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=BG3, fg=FG, insertbackground=FG,
                         relief="flat", bd=0, font=MONO, **kw)

# ══════════════════════════════════════════════════════════════════════════
#  MAIN APP WINDOW
# ══════════════════════════════════════════════════════════════════════════
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("860x620")
        self.minsize(720, 500)
        self.configure(bg=BG)
        self.resizable(True, True)

        # console buffer
        self._log_lines: list[str] = []
        self._log_lock  = threading.Lock()

        # core
        self.core = InjectorCore(self._log)

        self._build_ui()
        self._load_saved_flags()
        self._poll_status()

    # ── logging ────────────────────────────────────────────────────────────
    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        with self._log_lock: self._log_lines.append(line)
        # schedule UI update from main thread
        try: self.after(0, self._flush_log)
        except Exception: pass

    def _flush_log(self):
        with self._log_lock:
            lines, self._log_lines = self._log_lines, []
        if not hasattr(self, "_console"): return
        self._console.config(state="normal")
        for l in lines:
            tag = "ok" if "ok" in l.lower() or "OK" in l else \
                  "fail" if "fail" in l.lower() or "FAIL" in l else \
                  "warn" if "warn" in l.lower() or "WARN" in l else "info"
            self._console.insert("end", l+"\n", tag)
        self._console.see("end")
        self._console.config(state="disabled")

    # ── UI BUILD ───────────────────────────────────────────────────────────
    def _build_ui(self):
        # titlebar
        tb = tk.Frame(self, bg=BG2, height=42)
        tb.pack(fill="x"); tb.pack_propagate(False)
        tk.Label(tb, text=f"❄  {APP_NAME}", bg=BG2, fg=ACCENT2,
                 font=("Segoe UI", 12, "bold")).pack(side="left", padx=16)
        tk.Label(tb, text=f"v{APP_VERSION}", bg=BG2, fg=FG2,
                 font=("Segoe UI", 9)).pack(side="left")
        self._status_lbl = tk.Label(tb, text="● not attached", bg=BG2, fg=RED,
                                    font=("Segoe UI", 9))
        self._status_lbl.pack(side="right", padx=16)

        # tabs
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(4,8))

        style = ttk.Style()
        style.theme_use("default")
        style.configure("TNotebook",           background=BG,  borderwidth=0)
        style.configure("TNotebook.Tab",       background=BG3, foreground=FG2,
                        padding=[14,6], font=FONT)
        style.map("TNotebook.Tab",
                  background=[("selected", BG2)],
                  foreground=[("selected", ACCENT2)])

        self._tab_inject  = tk.Frame(nb, bg=BG)
        self._tab_presets = tk.Frame(nb, bg=BG)
        self._tab_mods    = tk.Frame(nb, bg=BG)
        self._tab_console = tk.Frame(nb, bg=BG)
        nb.add(self._tab_inject,  text="Inject")
        nb.add(self._tab_presets, text="Presets")
        nb.add(self._tab_mods,    text="Mods")
        nb.add(self._tab_console, text="Console")

        self._build_inject_tab()
        self._build_presets_tab()
        self._build_mods_tab()
        self._build_console_tab()

    # ── INJECT TAB ─────────────────────────────────────────────────────────
    def _build_inject_tab(self):
        p = self._tab_inject

        # toolbar
        bar = tk.Frame(p, bg=BG); bar.pack(fill="x", padx=10, pady=(8,4))
        Btn(bar, "+ Add Flag",    self._add_flag,    accent=True).pack(side="left", padx=(0,4))
        Btn(bar, "Import JSON",   self._import_json).pack(side="left", padx=4)
        Btn(bar, "Export JSON",   self._export_json).pack(side="left", padx=4)
        Btn(bar, "Sync Offsets",  lambda: self.core.sync_offsets()).pack(side="left", padx=4)
        Btn(bar, "Uninject All",  self._uninject, danger=True).pack(side="right")
        Btn(bar, "Apply Flags ▶", self._apply_flags, accent=True).pack(side="right", padx=4)

        # search
        sf = tk.Frame(p, bg=BG); sf.pack(fill="x", padx=10, pady=(0,4))
        tk.Label(sf, text="Search:", bg=BG, fg=FG2, font=FONT).pack(side="left")
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._render_flags())
        Entry(sf, textvariable=self._search_var, width=30).pack(side="left", padx=6)

        # flag list frame + scrollbar
        listframe = tk.Frame(p, bg=BG); listframe.pack(fill="both", expand=True, padx=10, pady=(0,8))
        # header
        hdr = tk.Frame(listframe, bg=BG3, height=28); hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="Flag Name",  bg=BG3, fg=FG2, font=FONT_B, anchor="w",
                 width=38).pack(side="left", padx=8)
        tk.Label(hdr, text="Value",      bg=BG3, fg=FG2, font=FONT_B, anchor="w"
                 ).pack(side="left", padx=8, fill="x", expand=True)
        tk.Label(hdr, text="",           bg=BG3, fg=FG2, font=FONT_B, width=8).pack(side="right", padx=4)

        # canvas scroll
        canvas = tk.Canvas(listframe, bg=BG, highlightthickness=0)
        scroll = tk.Scrollbar(listframe, orient="vertical", command=canvas.yview)
        canvas.config(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self._inner = tk.Frame(canvas, bg=BG)
        self._canvas_win = canvas.create_window((0,0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>", lambda e: canvas.config(
            scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(
            self._canvas_win, width=e.width))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(
            int(-1*(e.delta/120)), "units"))
        self._canvas = canvas

        # data: list of (name, value) rows
        self._rows: list[dict] = []   # {name_var, val_var, frame}

    def _render_flags(self):
        q = self._search_var.get().lower()
        for widget in self._inner.winfo_children():
            widget.destroy()
        self._rows = []
        flags = self.core.saved
        names = [n for n in flags if q in n.lower() or q in str(flags[n]).lower()]
        for n in names:
            self._add_row(n, flags[n])

    def _add_row(self, name: str = "", value: str = ""):
        row = tk.Frame(self._inner, bg=BG, pady=1)
        row.pack(fill="x")
        sep = tk.Frame(row, bg=BORDER, height=1); sep.pack(fill="x")
        inner = tk.Frame(row, bg=BG); inner.pack(fill="x")

        nv = tk.StringVar(value=name)
        vv = tk.StringVar(value=value)

        ne = Entry(inner, textvariable=nv, width=38)
        ne.pack(side="left", padx=(4,4), pady=3)
        ve = Entry(inner, textvariable=vv)
        ve.pack(side="left", padx=4, fill="x", expand=True)

        def del_row():
            n = nv.get()
            self.core.saved.pop(n, None)
            save_json(FLAGS_FILE, self.core.saved)
            row.destroy()

        Btn(inner, "✕", del_row, danger=True, width=3, pady=2).pack(side="right", padx=4)

        # track changes
        def on_change(*_):
            old = getattr(row, "_last_name", name)
            new = nv.get(); val = vv.get()
            if old and old in self.core.saved:
                del self.core.saved[old]
            if new:
                self.core.saved[new] = val
                save_json(FLAGS_FILE, self.core.saved)
            row._last_name = new

        nv.trace_add("write", on_change)
        vv.trace_add("write", on_change)
        row._last_name = name

        self._rows.append({"name_var": nv, "val_var": vv, "frame": row})

    def _add_flag(self):
        self._add_row()

    def _load_saved_flags(self):
        self._render_flags()

    def _apply_flags(self):
        flags = {r["name_var"].get(): r["val_var"].get()
                 for r in self._rows
                 if r["name_var"].get().strip()}
        if not flags:
            messagebox.showinfo(APP_NAME, "No flags to apply.")
            return
        ok, fail = self.core.apply(flags)
        if not self.core.is_attached():
            self._log(f"Saved {len(flags)} flags — will apply on next Roblox launch")
        else:
            self._log(f"Applied {ok}/{ok+fail} flags")

    def _uninject(self):
        n = self.core.uninject()
        self._render_flags()
        self._log(f"Uninjected — restored {n} memory values")

    def _import_json(self):
        path = filedialog.askopenfilename(
            title="Import flags", filetypes=[("JSON","*.json"),("All","*.*")])
        if not path: return
        try:
            raw = json.loads(Path(path).read_text("utf-8"))
            if isinstance(raw, dict) and "flags" in raw:
                raw = raw["flags"]
            if isinstance(raw, dict):
                items = raw.items()
            elif isinstance(raw, list):
                items = [(i["name"], i["value"]) for i in raw if "name" in i]
            else: raise ValueError("unrecognised format")
            for n, v in items:
                self.core.saved[str(n)] = str(v)
            save_json(FLAGS_FILE, self.core.saved)
            self._render_flags()
            self._log(f"Imported {len(list(items))} flags from {Path(path).name}")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Import failed: {e}")

    def _export_json(self):
        path = filedialog.asksaveasfilename(
            title="Export flags", defaultextension=".json",
            filetypes=[("JSON","*.json"),("All","*.*")],
            initialfile="fflags.json")
        if not path: return
        try:
            _safe_write(Path(path), json.dumps(self.core.saved, indent=2))
            self._log(f"Exported to {Path(path).name}")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Export failed: {e}")

    # ── PRESETS TAB ────────────────────────────────────────────────────────
    _PRESETS = {
        "Max FPS (9999)":    [("DFIntDebugFRMQualityLevelOverride","0"),
                               ("FFlagGameBasicSettingsFramerateCap2","false")],
        "1080p resolution":  [("DFIntDebugDynamicRenderKiloPixels","2074")],
        "720p resolution":   [("DFIntDebugDynamicRenderKiloPixels","922")],
        "480p resolution":   [("DFIntDebugDynamicRenderKiloPixels","410")],
        "Far render dist":   [("FIntCameraFarZPlane","1000")],
        "Max sharpness":     [("FIntDebugForceMSAASamples","4"),
                               ("FIntDebugFRMOptionalMSAALevelOverride","4")],
        "Hitbox sync 300":   [("S2PhysicsSenderRate","300")],
        "Disable shadows":   [("DFIntCSGLevelOfDetailSwitchingDistance","0"),
                               ("DFIntCSGLevelOfDetailSwitchingDistanceLow","0")],
        "Max physics FPS":   [("DFIntPhysicsReceiveRate","240"),
                               ("DFIntS2PhysicsSenderRate","240")],
    }

    def _build_presets_tab(self):
        p = self._tab_presets
        tk.Label(p, text="Quick Presets", bg=BG, fg=ACCENT2,
                 font=("Segoe UI",12,"bold")).pack(anchor="w", padx=14, pady=(12,4))
        tk.Label(p, text="Click a preset to merge those flags into your current set.",
                 bg=BG, fg=FG2, font=FONT).pack(anchor="w", padx=14, pady=(0,8))

        frame = tk.Frame(p, bg=BG); frame.pack(fill="both", expand=True, padx=10)
        for name, flags in self._PRESETS.items():
            row = tk.Frame(frame, bg=BG2, pady=6)
            row.pack(fill="x", pady=3)
            lf = tk.Frame(row, bg=BG2); lf.pack(side="left", fill="both", expand=True, padx=12)
            tk.Label(lf, text=name, bg=BG2, fg=FG, font=FONT_B, anchor="w").pack(anchor="w")
            tk.Label(lf, text=", ".join(f"{n}={v}" for n,v in flags),
                     bg=BG2, fg=FG2, font=("Segoe UI",8), anchor="w").pack(anchor="w")
            def _apply(fl=flags):
                d = dict(fl)
                for n,v in d.items(): self.core.saved[n] = v
                save_json(FLAGS_FILE, self.core.saved)
                self._render_flags()
                ok, fail = self.core.apply(d)
                self._log(f"Preset applied — {ok} ok, {fail} fail")
            Btn(row, "Apply →", _apply, accent=True).pack(side="right", padx=12)

    # ── MODS TAB ───────────────────────────────────────────────────────────
    def _build_mods_tab(self):
        p = self._tab_mods

        def section(title):
            tk.Label(p, text=title, bg=BG, fg=ACCENT2,
                     font=("Segoe UI",11,"bold")).pack(anchor="w", padx=14, pady=(12,4))

        section("FPS Unlocker")
        fps_frame = tk.Frame(p, bg=BG2, pady=10); fps_frame.pack(fill="x", padx=10, pady=4)
        tk.Label(fps_frame, text="Frame Cap:", bg=BG2, fg=FG, font=FONT).pack(side="left", padx=12)
        self._fps_var = tk.StringVar(value="9999")
        Entry(fps_frame, textvariable=self._fps_var, width=10).pack(side="left", padx=6)
        def do_fps():
            try:
                v = int(self._fps_var.get())
                ok, msg = unlock_fps(v)
                self._log(f"FPS: {msg}" if ok else f"FPS FAIL: {msg}")
            except ValueError:
                self._log("FPS: invalid value")
        def do_fps_restore():
            ok, msg = restore_fps()
            self._log(msg)
        Btn(fps_frame, "Unlock",  do_fps,         accent=True).pack(side="left", padx=4)
        Btn(fps_frame, "Restore", do_fps_restore, danger=True).pack(side="left", padx=4)

        section("Roblox Process")
        rb_frame = tk.Frame(p, bg=BG2, pady=10); rb_frame.pack(fill="x", padx=10, pady=4)
        def close_roblox():
            os.system("taskkill /F /IM RobloxPlayerBeta.exe /T 2>nul")
            self._log("Terminated RobloxPlayerBeta.exe")
        def launch_roblox():
            webbrowser.open("roblox://"); self._log("Launched Roblox")
        Btn(rb_frame, "Launch Roblox", launch_roblox, accent=True).pack(side="left", padx=12)
        Btn(rb_frame, "Close Roblox",  close_roblox,  danger=True).pack(side="left", padx=4)

    # ── CONSOLE TAB ────────────────────────────────────────────────────────
    def _build_console_tab(self):
        p = self._tab_console
        bar = tk.Frame(p, bg=BG); bar.pack(fill="x", padx=10, pady=(8,4))
        Btn(bar, "Clear", lambda: (self._console.config(state="normal"),
                                    self._console.delete("1.0","end"),
                                    self._console.config(state="disabled"))).pack(side="left")
        self._console = scrolledtext.ScrolledText(
            p, bg=BG2, fg=FG, font=MONO, relief="flat",
            insertbackground=FG, state="disabled", wrap="word")
        self._console.pack(fill="both", expand=True, padx=10, pady=(0,8))
        self._console.tag_config("ok",   foreground=GREEN)
        self._console.tag_config("fail", foreground=RED)
        self._console.tag_config("warn", foreground="#d4a574")
        self._console.tag_config("info", foreground=FG2)

    # ── STATUS POLL ────────────────────────────────────────────────────────
    def _poll_status(self):
        if self.core.is_attached():
            n = self.core.process_count()
            self._status_lbl.config(
                text=f"● attached ({n} process{'es' if n>1 else ''})", fg=GREEN)
        else:
            self._status_lbl.config(text="● not attached", fg=RED)
        self.after(1500, self._poll_status)


# ══════════════════════════════════════════════════════════════════════════
#  ENTRY
# ══════════════════════════════════════════════════════════════════════════
def main():
    if not _PYMEM:
        import subprocess, sys
        # attempt self-install pymem
        subprocess.call([sys.executable, "-m", "pip", "install",
                         "pymem", "--quiet"], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()

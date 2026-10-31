#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discord Rich Presence for the Horizon launcher and the Horizon game.

It runs invisibly in the background, looks for Horizon.exe (launcher) and
horizon-client.exe (game), and shows an activity on the user's Discord profile
through Discord's local IPC pipe. No third-party modules are needed.

    python horizon_presence.py            run forever (this is what autostart does)
    python horizon_presence.py --status   print what is detected right now
    python horizon_presence.py --verbose  also print the log to the console
"""

from __future__ import annotations

import argparse
import ctypes
import json
import msvcrt
import os
import re
import socket
import struct
import sys
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "presence.json"

# Horizon's public Discord application: it is what makes Discord say "Playing Horizon"
# and lets us reuse its "horizon" logo asset.
DEFAULT_CONFIG = {
    "client_id": "1563857609738690560",
    "large_image": "horizon",
    "large_text": "Horizon",
    "buttons": [
        {
            "label": "Horizon Discord RPC",
            "url": "https://github.com/qu1ckmeow/horizon-discord-rpc",
        },
        {
            "label": "Horizon",
            "url": "https://github.com/horizon-rust/horizon",
        },
    ],
    # The small picture in the corner of the large one comes from the selected
    # instance's icon. Discord accepts a direct image URL for it.
    "instance_icon": {
        "enabled": True,
        "texture_url": "https://raw.githubusercontent.com/InventivetalentDev/"
        "minecraft-assets/1.21.1/assets/minecraft/textures/{kind}/{name}.png",
        "colour_url": "https://singlecolorimage.com/get/{hex}/64x64.png",
        # Optional per-instance URLs, by instance id or display name:
        # "testrpc": "https://example.com/my-icon.png"
        "overrides": {},
    },
    "launcher": {
        "details": "",
        "state": "{username} looking at the main menu",
    },
    "game": {
        "details": "Minecraft: Rust Edition",
        "state": "{username} in the menus",
    },
    "game_single": {
        "details": "Minecraft: Rust Edition",
        "state": "Playing single world",
    },
    "game_server": {
        "details": "Minecraft: Rust Edition",
        "state": "Playing {server}",
    },
    "processes": {
        "launcher": ["Horizon.exe", "horizon-launcher.exe"],
        "game": ["horizon-client.exe"],
    },
    "poll_seconds": 2,
    "refresh_seconds": 60,
    "retry_seconds": 15,
    "show_elapsed": False,
}

# Discord's RPC opcodes.
OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4


# --------------------------------------------------------------------------- logging


class Log:
    """Appends to presence.log next to this script (rotated at 512 KiB)."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.path = SCRIPT_DIR / "presence.log"
        try:
            if self.path.is_file() and self.path.stat().st_size > 512 * 1024:
                os.replace(self.path, self.path.with_suffix(".log.1"))
        except OSError:
            pass

    def __call__(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
        if self.verbose:
            print(line, flush=True)


# --------------------------------------------------------------------------- config


def deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(log: Log | None) -> dict:
    if not CONFIG_FILE.is_file():
        try:
            CONFIG_FILE.write_text(
                json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            if log:
                log(f"created {CONFIG_FILE}")
        except OSError as error:
            if log:
                log(f"could not create {CONFIG_FILE}: {error}")
        return deep_merge({}, DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("the top level is not an object")
    except (OSError, ValueError) as error:
        if log:
            log(f"bad {CONFIG_FILE} ({error}); using the built-in defaults")
        return deep_merge({}, DEFAULT_CONFIG)
    return deep_merge(DEFAULT_CONFIG, data)


# --------------------------------------------------------------------------- Windows paths


def roaming() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")


def game_home() -> Path:
    override = os.environ.get("HORIZON_HOME")
    return Path(override) if override else roaming() / "horizon"


def launcher_data() -> Path:
    override = os.environ.get("HORIZON_LAUNCHER_HOME")
    return Path(override) if override else roaming() / "horizon-launcher"


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def selected_username(home: Path) -> str:
    """The signed-in account's name, else the game's offline name, else the Windows user."""
    accounts = read_json(launcher_data() / "accounts.json")
    selected = accounts.get("selected")
    entries = [a for a in accounts.get("accounts") or [] if isinstance(a, dict)]
    for entry in entries:
        if selected and entry.get("uuid") == selected and entry.get("name"):
            return str(entry["name"]).strip()
    for entry in entries:
        if entry.get("name"):
            return str(entry["name"]).strip()

    try:
        for line in (home / "horizon-player.txt").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            if line.lower().startswith("name:"):
                name = line.split(":", 1)[1].strip()
                if name:
                    return name
    except OSError:
        pass

    return os.environ.get("USERNAME") or os.environ.get("USER") or "Player"


def _instance_home(instance_id: str) -> Path:
    home = game_home()
    if instance_id.startswith("port:"):
        slug = instance_id.split(":", 1)[1]
        candidate = home / "ports" / slug / "home"
        if candidate.is_dir():
            home = candidate
    elif instance_id.startswith("pack:"):
        pack = instance_id.split(":", 1)[1]
        candidate = home / "horizon-packs" / pack / "home"
        if candidate.is_dir():
            home = candidate
    elif instance_id != "horizon":
        home = home / "instances" / instance_id
    return home


def instance_entry(instance_id: str, meta: dict | None = None) -> dict:
    """Everything the presence needs about one instance."""
    meta = meta if isinstance(meta, dict) else {}
    name = str(meta.get("name") or "").strip()
    if not name:
        if instance_id == "horizon":
            name = "Horizon"
        elif ":" in instance_id:
            name = instance_id.split(":", 1)[1]
        else:
            name = instance_id
    return {
        "id": instance_id,
        "name": name,
        "home": _instance_home(instance_id),
        "meta": meta,
    }


def selected_instance() -> dict:
    """The instance the launcher's Play button will start."""
    settings = read_json(launcher_data() / "settings.json")
    instance_id = str(settings.get("instance") or "horizon")
    metas = read_json(launcher_data() / "instances.json").get("instances") or {}
    meta = metas.get(instance_id) if isinstance(metas, dict) else None
    return instance_entry(instance_id, meta if isinstance(meta, dict) else {})


def all_instances() -> dict[str, dict]:
    metas = read_json(launcher_data() / "instances.json").get("instances") or {}
    if not isinstance(metas, dict):
        metas = {}
    selected = selected_instance()
    ids = {str(key) for key in metas} | {"horizon", selected["id"]}
    return {
        instance_id: instance_entry(instance_id, metas.get(instance_id))
        for instance_id in ids
    }


def _log_mtime(home: Path) -> float:
    try:
        return (home / "logs" / "latest.log").stat().st_mtime
    except OSError:
        return 0.0


def active_instance(game_running: bool) -> dict:
    """The instance whose game is running (its log is the newest), else the selected one."""
    selected = selected_instance()
    if not game_running:
        return selected
    candidates = list(all_instances().values())
    if not candidates:
        return selected
    newest = max(candidates, key=lambda entry: _log_mtime(entry["home"]))
    return newest if _log_mtime(newest["home"]) > 0 else selected


# --------------------------------------------------------------------------- process scan


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
_iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)

_EnumProcesses = _psapi.EnumProcesses
_EnumProcesses.argtypes = [
    ctypes.POINTER(wintypes.DWORD),
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_EnumProcesses.restype = wintypes.BOOL

_OpenProcess = _kernel32.OpenProcess
_OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_OpenProcess.restype = wintypes.HANDLE

_QueryFullProcessImageNameW = _kernel32.QueryFullProcessImageNameW
_QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
_QueryFullProcessImageNameW.restype = wintypes.BOOL

_CloseHandle = _kernel32.CloseHandle
_CloseHandle.argtypes = [wintypes.HANDLE]
_CloseHandle.restype = wintypes.BOOL

_PeekNamedPipe = _kernel32.PeekNamedPipe
_PeekNamedPipe.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPDWORD,
    wintypes.LPDWORD,
    wintypes.LPDWORD,
]
_PeekNamedPipe.restype = wintypes.BOOL

_NtQueryInformationProcess = _ntdll.NtQueryInformationProcess
_NtQueryInformationProcess.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.ULONG,
    ctypes.POINTER(wintypes.ULONG),
]
_NtQueryInformationProcess.restype = ctypes.c_long

_GetExtendedTcpTable = _iphlpapi.GetExtendedTcpTable
_GetExtendedTcpTable.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.BOOL,
    wintypes.ULONG,
    ctypes.c_int,
    wintypes.ULONG,
]
_GetExtendedTcpTable.restype = wintypes.DWORD

_CreateMutexW = _kernel32.CreateMutexW
_CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_CreateMutexW.restype = wintypes.HANDLE

_instance_mutex = None


def claim_single_instance() -> bool:
    """False when another copy is already watching (the mutex is per Windows session)."""
    global _instance_mutex
    _instance_mutex = _CreateMutexW(None, False, "Local\\HorizonDiscordPresence")
    return bool(_instance_mutex) and ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def process_table() -> dict[int, str]:
    """PID -> lower-case image name, for every process we may inspect."""
    capacity = 8192
    pids = (wintypes.DWORD * capacity)()
    needed = wintypes.DWORD()
    pid_pointer = ctypes.cast(pids, ctypes.POINTER(wintypes.DWORD))
    if not _EnumProcesses(pid_pointer, ctypes.sizeof(pids), ctypes.byref(needed)):
        return {}
    count = needed.value // ctypes.sizeof(wintypes.DWORD)
    table: dict[int, str] = {}
    for pid in pids[:count]:
        if not pid:
            continue
        handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if _QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                table[int(pid)] = Path(buffer.value).name.lower()
        finally:
            _CloseHandle(handle)
    return table


def matching_pids(table: dict[int, str], wanted: list[str]) -> list[int]:
    wanted_lower = {str(name).lower() for name in wanted}
    found = []
    for pid, name in table.items():
        if name in wanted_lower or Path(name).stem in wanted_lower:
            found.append(pid)
    return found


def current_state(config: dict, table: dict[int, str]) -> tuple[str | None, list[int]]:
    processes = config.get("processes") or {}
    game = matching_pids(table, processes.get("game") or [])
    if game:
        return "game", game
    launcher = matching_pids(table, processes.get("launcher") or [])
    if launcher:
        return "launcher", launcher
    return None, []


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", ctypes.c_void_p),
    ]


def process_command_line(pid: int) -> str:
    """The command line of a process, through ProcessCommandLineInformation (no PEB walking)."""
    handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.ULONG(0)
        status = _NtQueryInformationProcess(
            handle, 60, None, 0, ctypes.byref(size)
        )
        if (status & 0xFFFFFFFF) != 0xC0000004 or not size.value:  # STATUS_INFO_LENGTH_MISMATCH
            return ""
        buffer = ctypes.create_string_buffer(size.value)
        status = _NtQueryInformationProcess(
            handle, 60, buffer, size.value, ctypes.byref(size)
        )
        if (status & 0xFFFFFFFF) != 0:
            return ""
        text = _UnicodeString.from_buffer(buffer)
        if text.Buffer:
            return ctypes.string_at(text.Buffer, text.Length).decode("utf-16-le", "replace")
        return buffer.raw[16 : 16 + text.Length].decode("utf-16-le", "replace")
    except (OSError, ValueError):
        return ""
    finally:
        _CloseHandle(handle)


class _TcpRowOwnerPid(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


TCP_TABLE_OWNER_PID_ALL = 5
MIB_TCP_STATE_ESTAB = 5
AF_INET = 2


def tcp_remotes(pids: list[int]) -> list[tuple[str, int]]:
    """Established outgoing IPv4 connections made by these processes."""
    if not pids:
        return []
    size = wintypes.DWORD(0)
    _GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
    if size.value == 0:
        return []
    buffer = ctypes.create_string_buffer(size.value)
    result = _GetExtendedTcpTable(
        buffer, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0
    )
    if result != 0:
        return []
    # GetExtendedTcpTable's table starts with a DWORD count, then the rows.
    if size.value < 4:
        return []
    count = min(
        struct.unpack_from("<I", buffer.raw, 0)[0],
        (size.value - 4) // ctypes.sizeof(_TcpRowOwnerPid),
    )
    wanted = set(pids)
    remotes: list[tuple[str, int]] = []
    for index in range(count):
        offset = 4 + index * ctypes.sizeof(_TcpRowOwnerPid)
        row = _TcpRowOwnerPid.from_buffer(buffer, offset)
        if int(row.dwOwningPid) not in wanted or int(row.dwState) != MIB_TCP_STATE_ESTAB:
            continue
        address = socket.inet_ntoa(int(row.dwRemoteAddr).to_bytes(4, "little"))
        port = socket.ntohs(int(row.dwRemotePort) & 0xFFFF)
        if address.startswith("127.") or address == "0.0.0.0":
            continue
        # The game also talks HTTPS (telemetry); a Minecraft server is not 80/443.
        if port in (80, 443):
            continue
        remotes.append((address, port))
    remotes.sort(key=lambda item: (item[1] != 25565, item[1]))
    return remotes


# --------------------------------------------------------------------------- world/server detection


def _lock_conflicts(path: Path) -> bool:
    """True while the game holds the world's session.lock (DirectoryLock)."""
    try:
        handle = open(path, "r+b")
    except OSError as error:
        return getattr(error, "winerror", None) == 32
    try:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return True
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return False
    finally:
        handle.close()


def world_is_open(home: Path) -> bool:
    saves = home / "saves"
    if not saves.is_dir():
        return False
    try:
        worlds = list(saves.iterdir())
    except OSError:
        return False
    for world in worlds:
        try:
            if world.is_dir() and _lock_conflicts(world / "session.lock"):
                return True
        except OSError:
            continue
    return False


class _NbtReader:
    """Just enough NBT for servers.dat (compounds, lists, strings and numbers)."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def _take(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data) or size > 64 << 20:
            raise ValueError("bad NBT")
        chunk = self.data[self.pos : self.pos + size]
        self.pos += size
        return chunk

    def u8(self) -> int:
        return self._take(1)[0]

    def i16(self) -> int:
        return struct.unpack(">h", self._take(2))[0]

    def i32(self) -> int:
        return struct.unpack(">i", self._take(4))[0]

    def i64(self) -> int:
        return struct.unpack(">q", self._take(8))[0]

    def string(self) -> str:
        length = struct.unpack(">H", self._take(2))[0]
        return self._take(length).decode("utf-8", "replace")

    def payload(self, tag: int):
        if tag == 1:
            return struct.unpack(">b", self._take(1))[0]
        if tag == 2:
            return self.i16()
        if tag == 3:
            return self.i32()
        if tag == 4:
            return self.i64()
        if tag == 5:
            return struct.unpack(">f", self._take(4))[0]
        if tag == 6:
            return struct.unpack(">d", self._take(8))[0]
        if tag == 7:
            return self._take(self.i32())
        if tag == 8:
            return self.string()
        if tag == 9:
            kind = self.u8()
            length = max(0, self.i32())
            if length > 1 << 20:
                raise ValueError("bad NBT list")
            return [self.payload(kind) for _ in range(length)]
        if tag == 10:
            result = {}
            while True:
                kind = self.u8()
                if kind == 0:
                    return result
                name = self.string()
                result[name] = self.payload(kind)
        if tag == 11:
            length = max(0, self.i32())
            return list(struct.unpack(f">{length}i", self._take(4 * length)))
        if tag == 12:
            length = max(0, self.i32())
            return list(struct.unpack(f">{length}q", self._take(8 * length)))
        return None

    def root(self):
        kind = self.u8()
        if kind == 0:
            return None
        self.string()  # root name
        return self.payload(kind)


def saved_servers(home: Path) -> list[tuple[str, str]]:
    """(address, name) pairs from the instance's servers.dat, else the shared one."""
    for path in (home / "servers.dat", game_home() / "servers.dat"):
        try:
            root = _NbtReader(path.read_bytes()).root()
        except (OSError, ValueError, struct.error):
            continue
        if not isinstance(root, dict):
            continue
        entries = root.get("servers")
        if not isinstance(entries, list):
            continue
        servers = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            address = str(entry.get("ip") or "").strip()
            name = str(entry.get("name") or "").strip()
            if address:
                servers.append((address, name))
        if servers:
            return servers
    return []


_resolved: dict[str, tuple[float, set[str]]] = {}
_resolve_lock = threading.Lock()


def resolve_host(host: str) -> set[str]:
    now = time.time()
    with _resolve_lock:
        cached = _resolved.get(host)
        if cached and now - cached[0] < 600:
            return cached[1]
    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM):
            addresses.add(info[4][0])
    except OSError:
        pass
    with _resolve_lock:
        _resolved[host] = (now, addresses)
    return addresses


_reverse: dict[str, str] = {}
_reverse_pending: set[str] = set()
_reverse_lock = threading.Lock()


def _reverse_lookup(address: str) -> None:
    name = ""
    try:
        name = socket.gethostbyaddr(address)[0].strip()
    except OSError:
        pass
    with _reverse_lock:
        _reverse[address] = name
        _reverse_pending.discard(address)


def reverse_name(address: str) -> str:
    with _reverse_lock:
        if address in _reverse:
            return _reverse[address] or address
        if address not in _reverse_pending:
            _reverse_pending.add(address)
            threading.Thread(target=_reverse_lookup, args=(address,), daemon=True).start()
    return address


def host_only(address: str) -> str:
    address = address.strip().strip('"')
    if address.startswith("["):
        end = address.find("]")
        return address[1:end] if end > 0 else address
    if address.count(":") == 1:
        return address.rsplit(":", 1)[0]
    return address


def _is_ip(text: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", text))


def command_line_server(pids: list[int]) -> str:
    pattern = re.compile(
        r"--(?:server|quickPlayMultiplayer)(?:=|\s+)(?:\"([^\"]+)\"|(\S+))",
        re.IGNORECASE,
    )
    for pid in pids:
        match = pattern.search(process_command_line(pid))
        if match:
            return match.group(1) or match.group(2)
    return ""


def server_display_name(address: str, hint: str, home: Path) -> str:
    hint_host = host_only(hint) if hint else ""
    if hint_host and not _is_ip(hint_host):
        return hint_host
    for saved, _name in saved_servers(home):
        host = host_only(saved)
        if not host or _is_ip(host):
            continue
        if address in resolve_host(host):
            return host
    name = reverse_name(address)
    if name != address:
        return name
    return hint_host or address


def game_mode(pids: list[int], home: Path) -> dict:
    remotes = tcp_remotes(pids)
    if remotes:
        address, _port = remotes[0]
        return {
            "play": "server",
            "server": server_display_name(address, command_line_server(pids), home),
        }
    if world_is_open(home):
        return {"play": "single", "server": ""}
    return {"play": "menu", "server": ""}


# --------------------------------------------------------------------------- instance icon


_texture_kind_cache: dict[str, str] = {}


def texture_kind(name: str) -> str:
    """Whether prepared resources hold the texture as block/ or item/."""
    if name in _texture_kind_cache:
        return _texture_kind_cache[name]
    selected = str(read_json(launcher_data() / "settings.json").get("selected") or "")
    kind = "block"
    if selected:
        base = launcher_data() / "versions" / selected / ".local" / "horizon-client"
        try:
            for textures in base.glob("*/assets/minecraft/textures"):
                if (textures / "block" / f"{name}.png").is_file():
                    kind = "block"
                    break
                if (textures / "item" / f"{name}.png").is_file():
                    kind = "item"
                    break
        except OSError:
            pass
    _texture_kind_cache[name] = kind
    return kind


def instance_icon(entry: dict, config: dict) -> tuple[str, str]:
    """(small image URL, tooltip) for an instance, or ("", "")."""
    settings = config.get("instance_icon") or {}
    if not settings.get("enabled", True):
        return "", ""
    overrides = settings.get("overrides") or {}
    if isinstance(overrides, dict):
        for key in (entry["id"], entry["name"]):
            url = overrides.get(key)
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url, entry["name"]
    icon = entry["meta"].get("icon")
    if not isinstance(icon, dict):
        return "", ""
    texture = icon.get("texture")
    colour = icon.get("colour")
    if isinstance(texture, str) and texture:
        name = texture.split(":", 1)[-1].split("/")[-1]
        template = str(settings.get("texture_url") or "")
        if template:
            return template.format(kind=texture_kind(name), name=name), entry["name"]
    if isinstance(colour, str) and colour:
        template = str(settings.get("colour_url") or "")
        if template:
            return template.format(hex=colour.lstrip("#").lower()), entry["name"]
    return "", ""


# --------------------------------------------------------------------------- Discord IPC


class DiscordIPC:
    """One connection to the local Discord client (discord-ipc-0 .. discord-ipc-9).

    Windows named pipes here are synchronous: a read waiting on one thread can hold
    up a write on another, so this class never reads and writes at the same time.
    Incoming frames are read only when PeekNamedPipe says they are there.
    """

    def __init__(self, client_id: str, log: Log) -> None:
        self.client_id = client_id
        self.log = log
        self.pipe = None
        self.ready = False
        self.alive = False

    def connect(self, timeout: float = 5.0) -> bool:
        for index in range(10):
            path = rf"\\.\pipe\discord-ipc-{index}"
            try:
                pipe = open(path, "r+b", buffering=0)
            except OSError:
                continue

            self.pipe = pipe
            self.alive = True
            self.ready = False
            try:
                self._send(OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
            except OSError:
                self.close()
                return False

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and self.alive and not self.ready:
                try:
                    if self._available() >= 8:
                        self._read_frame()
                    else:
                        time.sleep(0.02)
                except (OSError, ValueError):
                    self.alive = False

            if self.ready and self.alive:
                self.log(f"connected to Discord on {path}")
                return True
            self.log(f"no READY from Discord on {path}")
            self.close()
            return False
        return False

    def _send(self, opcode: int, payload: dict | None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        frame = struct.pack("<II", opcode, len(body)) + body
        if self.pipe is None:
            raise ConnectionError("not connected")
        self.pipe.write(frame)
        self.pipe.flush()

    def _available(self) -> int:
        if self.pipe is None:
            return 0
        try:
            handle = msvcrt.get_osfhandle(self.pipe.fileno())
        except OSError:
            self.alive = False
            return 0
        available = wintypes.DWORD(0)
        if not _PeekNamedPipe(
            wintypes.HANDLE(handle), None, 0, None, ctypes.byref(available), None
        ):
            self.alive = False
            return 0
        return int(available.value)

    def _read_exactly(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self.pipe.read(size - len(data))
            if not chunk:
                raise OSError("Discord closed the pipe")
            data += chunk
        return data

    def _read_frame(self) -> None:
        header = self._read_exactly(8)
        opcode, length = struct.unpack("<II", header)
        if length > 1 << 20:
            raise OSError("frame too large")
        body = self._read_exactly(length) if length else b""
        message = json.loads(body.decode("utf-8")) if body else {}
        if opcode == OP_PING:
            self._send(OP_PONG, message)
        elif opcode == OP_CLOSE:
            self.log(f"Discord closed the presence connection: {message}")
            self.alive = False
        elif message.get("cmd") == "DISPATCH" and message.get("evt") == "READY":
            self.ready = True
        elif message.get("evt") == "ERROR":
            self.log(f"Discord error: {message.get('data')}")

    def poll(self) -> None:
        """Read whatever Discord sent since the last call (replies, PINGs)."""
        if not self.alive:
            return
        try:
            while self.alive and self._available() >= 8:
                self._read_frame()
        except (OSError, ValueError):
            self.alive = False

    def set_activity(self, activity: dict | None) -> None:
        if not self.alive:
            raise ConnectionError("not connected")
        self._send(
            OP_FRAME,
            {
                "cmd": "SET_ACTIVITY",
                "nonce": str(uuid.uuid4()),
                "args": {"pid": os.getpid(), "activity": activity},
            },
        )

    def close(self) -> None:
        self.alive = False
        pipe, self.pipe = self.pipe, None
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- activity


class SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(template: str | None, values: dict) -> str | None:
    if not template:
        return None
    text = str(template).format_map(SafeDict(values))
    return text if text.strip() else None


def clean_buttons(value) -> list[dict] | None:
    if not isinstance(value, list):
        return None
    buttons = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        url = str(item.get("url") or "").strip()
        if label and url.startswith(("http://", "https://")):
            buttons.append({"label": label[:32], "url": url})
        if len(buttons) == 2:
            break
    return buttons or None


def build_activity(config: dict, state: str, values: dict, since: float | None) -> dict | None:
    if state == "game":
        play = values.get("play") or "menu"
        section = config.get(f"game_{play}") or config.get("game") or {}
    else:
        section = config.get(state) or {}

    activity: dict = {}
    details = render(section.get("details"), values)
    status = render(section.get("state"), values)
    if details:
        activity["details"] = details
    if status:
        activity["state"] = status
    if not activity:
        return None

    assets = {}
    large_image = section.get("large_image") or config.get("large_image")
    large_text = section.get("large_text") or config.get("large_text")
    if large_image:
        assets["large_image"] = large_image
    if large_text:
        assets["large_text"] = large_text
    small_image = section.get("small_image") or values.get("small_image")
    small_text = section.get("small_text") or values.get("small_text")
    if small_image:
        assets["small_image"] = small_image
    if small_text:
        assets["small_text"] = small_text
    if assets:
        activity["assets"] = assets

    buttons = clean_buttons(section.get("buttons") or config.get("buttons"))
    if buttons:
        activity["buttons"] = buttons
    if since and config.get("show_elapsed"):
        activity["timestamps"] = {"start": int(since)}
    return activity


# --------------------------------------------------------------------------- main loop


def context(config: dict, state: str | None, pids: list[int]) -> dict:
    entry = active_instance(state == "game")
    values = {
        "username": selected_username(entry["home"]),
        "instance_name": entry["name"],
        "instance_id": entry["id"],
        "play": "menu",
        "server": "",
    }
    icon, icon_text = instance_icon(entry, config)
    if icon:
        values["small_image"] = icon
        values["small_text"] = icon_text
    if state == "game":
        values.update(game_mode(pids, entry["home"]))
    return values


def run(log: Log) -> None:
    config = load_config(log)
    log(f"started; config {CONFIG_FILE}")

    rpc: DiscordIPC | None = None
    current_key: str | None = None
    last_state: str | None = None
    since: float | None = None
    last_sent = 0.0
    next_try = 0.0

    while True:
        try:
            if rpc is not None:
                rpc.poll()
            table = process_table()
            state, pids = current_state(config, table)
            now = time.time()

            if state != last_state:
                since = now
                if state:
                    log(f"detected {state}")
                elif last_state:
                    log("Horizon is closed")
                last_state = state

            activity: dict | None = None
            key: str | None = None
            if state:
                values = context(config, state, pids)
                activity = build_activity(
                    config, state, values, since if config.get("show_elapsed") else None
                )
                if activity:
                    key = json.dumps(activity, sort_keys=True, ensure_ascii=False)

            if activity is None:
                if rpc is not None:
                    try:
                        rpc.set_activity(None)
                    except (OSError, ConnectionError):
                        pass
                    rpc.close()
                    rpc = None
                    if current_key is not None:
                        log("cleared the Discord activity")
                current_key = None
                last_sent = 0.0
            else:
                changed = key != current_key
                stale = rpc is None or not rpc.alive
                due = now - last_sent >= max(15, int(config.get("refresh_seconds") or 60))
                if (changed or stale or due) and now >= next_try:
                    if rpc is None or not rpc.alive:
                        if rpc is not None:
                            rpc.close()
                        rpc = DiscordIPC(str(config["client_id"]), log)
                        if not rpc.connect():
                            rpc = None
                            next_try = now + max(5, int(config.get("retry_seconds") or 15))
                    if rpc is not None:
                        try:
                            rpc.set_activity(activity)
                            rpc.poll()
                            current_key = key
                            last_sent = now
                            next_try = 0.0
                            log(
                                "presence: "
                                + (activity.get("details") or "-")
                                + " / "
                                + (activity.get("state") or "-")
                            )
                        except (OSError, ConnectionError) as error:
                            log(f"could not send the activity: {error}")
                            rpc.close()
                            rpc = None
                            next_try = now + 2

        except Exception as error:  # keep the watcher alive whatever happens
            log(f"error: {type(error).__name__}: {error}")

        time.sleep(max(1, int(config.get("poll_seconds") or 2)))


def status() -> int:
    config = load_config(None)
    table = process_table()
    state, pids = current_state(config, table)
    values = context(config, state, pids)
    activity = build_activity(config, state, values, None) if state else None
    print(
        json.dumps(
            {
                "detected": state or "nothing",
                "horizon_processes": sorted(
                    f"{name} ({pid})"
                    for pid, name in table.items()
                    if "horizon" in name
                ),
                "username": values["username"],
                "instance_id": values["instance_id"],
                "instance_name": values["instance_name"],
                "play": values["play"],
                "server": values["server"],
                "small_image": values.get("small_image", ""),
                "activity": activity,
                "discord_pipe": next(
                    (
                        rf"\\.\pipe\discord-ipc-{i}"
                        for i in range(10)
                        if os.path.exists(rf"\\.\pipe\discord-ipc-{i}")
                    ),
                    None,
                ),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Discord Rich Presence for Horizon")
    parser.add_argument("--status", action="store_true", help="print what is detected and exit")
    parser.add_argument("--verbose", action="store_true", help="also print the log")
    args = parser.parse_args()

    if args.status:
        return status()

    log = Log(verbose=args.verbose)
    if not claim_single_instance():
        log("another copy is already running; exiting")
        if args.verbose:
            print("another copy is already running; exiting")
        return 0
    try:
        run(log)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

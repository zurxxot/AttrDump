"""Read-only probe of Roblox's in-memory layout.

Attaches to the running client, walks the instance tree with the offsets for that exact build, and can chase pointers
outward from an instance to find where a known string (an attribute name) is stored. It only reads; the one
caller that opens the process for writing is attrset.py.

    python -I attrprobe.py tree [path]
    python -I attrprobe.py hexdump <path> [offset] [size]
    python -I attrprobe.py find <path> <text> [depth]
    python -I attrprobe.py at <address> [size]
"""

import collections
import ctypes
import ctypes.wintypes as wt
import json
import os
import struct
import sys
import urllib.request

OFFSETS_URL = "https://offsets.imtheo.lol/{build}/offsets.json"
OFFSETS_CACHE = os.path.join(os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)), "offsets_cache.json")
USAGE = __doc__


class Fatal(Exception):
    pass


# --------------------------------------------------------------------------- Win32

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x2
TH32CS_SNAPMODULE = 0x8
TH32CS_SNAPMODULE32 = 0x10
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD),
        ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wt.DWORD),
        ("hModule", wt.HMODULE), ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p), ("AllocationProtect", wt.DWORD),
        ("PartitionId", wt.WORD), ("RegionSize", ctypes.c_size_t), ("State", wt.DWORD), ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def _api(dll, name, argtypes, restype=wt.BOOL):
    function = getattr(dll, name)
    function.argtypes = argtypes
    function.restype = restype
    return function


CreateToolhelp32Snapshot = _api(kernel32, "CreateToolhelp32Snapshot", [wt.DWORD, wt.DWORD], wt.HANDLE)
Process32FirstW = _api(kernel32, "Process32FirstW", [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)])
Process32NextW = _api(kernel32, "Process32NextW", [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)])
Module32FirstW = _api(kernel32, "Module32FirstW", [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)])
Module32NextW = _api(kernel32, "Module32NextW", [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)])
OpenProcess = _api(kernel32, "OpenProcess", [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE)
CloseHandle = _api(kernel32, "CloseHandle", [wt.HANDLE])
ReadProcessMemory = _api(kernel32, "ReadProcessMemory",
                         [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                          ctypes.POINTER(ctypes.c_size_t)])
VirtualQueryEx = _api(kernel32, "VirtualQueryEx",
                      [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t],
                      ctypes.c_size_t)
WriteProcessMemory = _api(kernel32, "WriteProcessMemory",
                          [wt.HANDLE, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
                           ctypes.POINTER(ctypes.c_size_t)])
QueryFullProcessImageNameW = _api(kernel32, "QueryFullProcessImageNameW",
                                  [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)])
EnumWindows = _api(user32, "EnumWindows", [WNDENUMPROC, wt.LPARAM])
IsWindowVisible = _api(user32, "IsWindowVisible", [wt.HWND])
GetWindowThreadProcessId = _api(user32, "GetWindowThreadProcessId",
                                [wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD)


def roblox_pids():
    """Every running Roblox client, the ones with a visible window first."""
    pids = []
    snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, INVALID_HANDLE_VALUE):
        return []
    entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
    ok = Process32FirstW(snapshot, ctypes.byref(entry))
    while ok:
        if entry.szExeFile.lower() == "robloxplayerbeta.exe":
            pids.append(entry.th32ProcessID)
        ok = Process32NextW(snapshot, ctypes.byref(entry))
    CloseHandle(snapshot)

    windowed = set()

    def visit(hwnd, _):
        if IsWindowVisible(hwnd):
            pid = wt.DWORD()
            GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            windowed.add(pid.value)
        return True

    EnumWindows(WNDENUMPROC(visit), 0)
    pids.sort(key=lambda pid: pid not in windowed)
    return pids


def roblox_pid():
    pids = roblox_pids()
    return pids[0] if pids else None


def module_range(pid):
    """(base, size) of RobloxPlayerBeta.exe in the process, or (0, 0)."""
    snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snapshot in (None, INVALID_HANDLE_VALUE):
        return 0, 0
    found = (0, 0)
    entry = MODULEENTRY32W(dwSize=ctypes.sizeof(MODULEENTRY32W))
    ok = Module32FirstW(snapshot, ctypes.byref(entry))
    while ok:
        if entry.szModule.lower() == "robloxplayerbeta.exe":
            found = (entry.modBaseAddr or 0, entry.modBaseSize)
            break
        ok = Module32NextW(snapshot, ctypes.byref(entry))
    CloseHandle(snapshot)
    return found


class Process:
    """A process opened for reading, and for writing only if write=True."""

    def __init__(self, pid, write=False):
        self.pid = pid
        access = PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION
        if write:
            access |= PROCESS_VM_WRITE | PROCESS_VM_OPERATION
        self.handle = OpenProcess(access, False, pid)
        if not self.handle:
            raise Fatal(f"couldn't open Roblox (pid {pid}), Windows error {ctypes.get_last_error()}")

    def read(self, address, size):
        if size <= 0 or not 0x10000 <= address <= 0x7FFFFFFFFFFF - size:
            return None
        buffer = ctypes.create_string_buffer(size)
        done = ctypes.c_size_t()
        if not ReadProcessMemory(self.handle, address, buffer, size, ctypes.byref(done)) or done.value != size:
            return None
        return buffer.raw

    def write(self, address, data):
        if not 0x10000 <= address <= 0x7FFFFFFFFFFF - len(data):
            return False
        done = ctypes.c_size_t()
        return bool(WriteProcessMemory(self.handle, address, data, len(data), ctypes.byref(done))) \
            and done.value == len(data)

    def regions(self):
        """(base, size) of every committed, private, writable region: where heap objects live."""
        found = []
        info = MEMORY_BASIC_INFORMATION()
        address = 0x10000
        while address < 0x7FFFFFFFFFFF:
            if not VirtualQueryEx(self.handle, address, ctypes.byref(info), ctypes.sizeof(info)):
                break
            if info.State == 0x1000 and info.Type == 0x20000 and info.Protect & 0x04 and not info.Protect & 0x100:
                found.append((info.BaseAddress or 0, info.RegionSize))
            address = (info.BaseAddress or 0) + info.RegionSize
        return found

    def build(self):
        path = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(path))
        if not QueryFullProcessImageNameW(self.handle, 0, path, ctypes.byref(size)):
            return None
        return next((folder for folder in path.value.split("\\") if folder.startswith("version-")), None)


# --------------------------------------------------------------------------- memory

STRING = struct.Struct("<16sQQ")  # MSVC x64 std::string: inline text or heap pointer, size, capacity


def read_u64(memory, address):
    raw = memory.read(address, 8)
    return struct.unpack("<Q", raw)[0] if raw else 0


def read_str(memory, at):
    raw = memory.read(at, STRING.size)
    if raw is None:
        return None
    head, size, capacity = STRING.unpack(raw)
    if not (15 <= capacity < 1024 and size <= capacity):
        return None
    text_at = at if capacity == 15 else struct.unpack_from("<Q", head)[0]
    data = memory.read(text_at, size) if size else b""
    return None if data is None else data.decode("utf-8", "replace")


def read_block(memory, address, size):
    """size bytes at address, or whatever is readable up to the end of its page."""
    data = memory.read(address, size)
    if data is None:
        data = memory.read(address, min(size, 0x1000 - (address & 0xFFF)))
    return data


def is_pointer(value):
    return 0x10000 <= value <= 0x7FFFFFFFFFFF


def load_offsets(build):
    """The imtheo.lol dump for this exact build, or the copy saved last time."""
    url = OFFSETS_URL.format(build=build)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "attrprobe"})
        with urllib.request.urlopen(request, timeout=15) as response:
            dump = json.load(response)
    except (OSError, ValueError) as error:
        try:
            with open(OFFSETS_CACHE, encoding="utf-8") as file:
                dump = json.load(file)
        except (OSError, ValueError):
            raise Fatal(f"couldn't download {url}: {error}") from None
    if dump.get("Roblox Version") != build:
        raise Fatal(f"the offsets are for {dump.get('Roblox Version')}, but Roblox is running {build}")
    try:
        with open(OFFSETS_CACHE, "w", encoding="utf-8") as file:
            json.dump(dump, file)
    except OSError:
        pass
    return dump["Offsets"]


# --------------------------------------------------------------------------- instance tree

class Tree:
    def __init__(self, memory, base, offsets):
        self.memory = memory
        self.base = base
        self.instance = offsets["Instance"]
        self.fake_datamodel = offsets["FakeDataModel"]

    def datamodel(self):
        fake = read_u64(self.memory, self.base + self.fake_datamodel["Pointer"])
        return read_u64(self.memory, fake + self.fake_datamodel["RealDataModel"]) if fake else 0

    def name(self, instance):
        container = read_u64(self.memory, instance + self.instance["NameContainer"])
        return read_str(self.memory, container + self.instance["Name"]) if container else None

    def class_name(self, instance):
        descriptor = read_u64(self.memory, instance + self.instance["ClassDescriptor"])
        string = read_u64(self.memory, descriptor + self.instance["ClassName"]) if descriptor else 0
        return read_str(self.memory, string) if string else None

    def children(self, instance):
        vector = read_u64(self.memory, instance + self.instance["ChildrenStart"])
        begin = read_u64(self.memory, vector) if vector else 0
        end = read_u64(self.memory, vector + self.instance["ChildrenEnd"]) if vector else 0
        count = (end - begin) // 16 if begin and end > begin else 0
        raw = self.memory.read(begin, count * 16) if 0 < count <= 10000 else None
        if raw is None:
            return []
        return [pointer for pointer in struct.unpack(f"<{count * 2}Q", raw)[::2] if pointer]

    def resolve(self, path):
        """The instance at a path like game/Workspace/Folder (game is optional; / or . both split)."""
        node = self.datamodel()
        if not node:
            raise Fatal("no DataModel yet: join a game first")
        parts = [part for part in path.replace(".", "/").split("/") if part]
        if parts and parts[0] == "game":
            parts = parts[1:]
        for part in parts:
            node = next((child for child in self.children(node) if self.name(child) == part), 0)
            if not node:
                raise Fatal(f"no child called {part!r} in {path!r}")
        return node


# --------------------------------------------------------------------------- probing

def describe(memory, value, image):
    """A short note on what an 8-byte value looks like."""
    if image[0] <= value < image[0] + image[1]:
        return f"-> exe +{value - image[0]:#x}"
    if not is_pointer(value):
        return ""
    head = memory.read(value, 24)
    if head is None:
        return "-> unreadable"
    text = head.split(b"\0")[0]
    if len(text) >= 3 and all(32 <= byte < 127 for byte in text):
        return f"-> text {text.decode()!r}"
    return "-> mapped"


def hexdump(memory, address, size, image):
    data = read_block(memory, address, size)
    if data is None:
        raise Fatal(f"can't read {address:#x}")
    for offset in range(0, len(data) - 7, 8):
        value = struct.unpack_from("<Q", data, offset)[0]
        print(f"+{offset:#05x}  {value:#018x}  {describe(memory, value, image)}")


def chase(memory, start, needle, depth, skip, image, limit=400000):
    """Breadth-first search for needle through the pointers reachable from start, printing each chain of offsets."""
    seen = {start}
    queue = collections.deque()
    root = read_block(memory, start, 0x200)
    if root is None:
        raise Fatal(f"can't read {start:#x}")
    for offset in range(0, len(root) - 7, 8):
        value = struct.unpack_from("<Q", root, offset)[0]
        if offset not in skip and is_pointer(value) and not image[0] <= value < image[0] + image[1]:
            queue.append((value, (offset,)))
    visited = hits = 0
    while queue and visited < limit and hits < 25:
        address, chain = queue.popleft()
        if address in seen:
            continue
        seen.add(address)
        block = read_block(memory, address, 0x100)
        if block is None:
            continue
        visited += 1
        at = block.find(needle)
        if at >= 0:
            hits += 1
            path = " -> ".join(f"+{offset:#x}" for offset in chain)
            print(f"FOUND {path}   text at +{at:#x} of {address:#x}")
        if len(chain) < depth:
            for offset in range(0, len(block) - 7, 8):
                value = struct.unpack_from("<Q", block, offset)[0]
                if is_pointer(value) and value not in seen and not image[0] <= value < image[0] + image[1]:
                    queue.append((value, chain + (offset,)))
    print(f"searched {visited} blocks, {hits} hits")


def main(argv):
    if len(argv) < 2 or argv[1] not in ("tree", "hexdump", "find", "at"):
        print(USAGE)
        return 2
    pid = roblox_pid()
    if not pid:
        raise Fatal("Roblox isn't running")
    memory = Process(pid)
    build = memory.build()
    base, size = module_range(pid)
    offsets = load_offsets(build)
    tree = Tree(memory, base, offsets)
    image = (base, size)
    print(f"Roblox pid {pid}, {build}, base {base:#x}")
    command, args = argv[1], argv[2:]

    if command == "at":
        hexdump(memory, int(args[0], 0), int(args[1], 0) if len(args) > 1 else 0x100, image)
    elif command == "tree":
        node = tree.resolve(args[0] if args else "game")
        for child in tree.children(node):
            print(f"{child:#x}  {tree.class_name(child) or '?':<22} {tree.name(child)}")
    elif command == "hexdump":
        node = tree.resolve(args[0])
        start = int(args[1], 0) if len(args) > 1 else 0
        hexdump(memory, node + start, int(args[2], 0) if len(args) > 2 else 0x100, image)
    else:
        node = tree.resolve(args[0])
        instance = offsets["Instance"]
        skip = {instance["This"], instance["Parent"], instance["ChildrenStart"], instance["ClassDescriptor"],
                instance["NameContainer"]}
        print(f"{args[0]} is at {node:#x}")
        chase(memory, node, args[1].encode(), int(args[2]) if len(args) > 2 else 4, skip, image)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Fatal as error:
        print(f"error: {error}")
        sys.exit(1)

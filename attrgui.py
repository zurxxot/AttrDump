"""Window for browsing and changing the attributes of a running Roblox client.

Everything is read straight out of Roblox's memory the way attrdump.py does it. A value that sits inside the attribute
entry itself (booleans, numbers, vectors, colours, UDims) can be overwritten, once or kept in place while the game
runs, the way attrset.py does it. Strings and the other types are shown but not changed, and attributes can't be added
or removed: both need Roblox's own allocator and hash table.

    python attrgui.py
"""

import ctypes
import json
import math
import os
import re
import struct
import sys
import threading
import time

from attrdump import Attributes, MAX_DEPTH, find_layout
from attrprobe import Fatal, Process, module_range, roblox_pids
from attrset import OFF, ON, local_player
from layout import u64

HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
RESOURCES = getattr(sys, "_MEIPASS", HERE)
PAGE = os.path.join(RESOURCES, "attrgui.html")

MAX_CHILDREN = 4000
MAX_TEXT = 4000
SHAPES = {"float": "<f", "double": "<d", "Vector2": "<2f", "Vector3": "<3f", "Color3": "<3f", "NumberRange": "<2f",
          "Rect2D": "<4f", "UDim": "<fi", "UDim2": "<fifi", "BrickColor": "<I"}


def codes(kind):
    """One struct code per number in a value of this type: Vector3 is f, f, f."""
    return [code for count, code in re.findall(r"(\d*)([fdiI])", SHAPES[kind]) for _ in range(int(count or 1))]


def encode(kind, value):
    """The bytes Roblox keeps in the entry for a value of an attribute type that lives inline there."""
    if kind == "bool":
        word = str(value).strip().lower()
        if word not in ON | OFF:
            raise Fatal(f"{value!r} isn't a boolean")
        return b"\x01" if word in ON else b"\x00"
    if kind not in SHAPES:
        raise Fatal(f"{kind} attributes can't be changed from outside the game")
    wanted = codes(kind)
    parts = value if isinstance(value, list) else [value]
    if len(parts) != len(wanted):
        raise Fatal(f"a {kind} needs {len(wanted)} number{'s' if len(wanted) > 1 else ''}")
    try:
        numbers = []
        for part, code in zip(parts, wanted):
            number = float(part)
            if not math.isfinite(number):
                raise ValueError
            if code in "iI":
                if not number.is_integer():
                    raise ValueError
                number = int(number)
            numbers.append(number)
        return struct.pack(SHAPES[kind], *numbers)
    except (ValueError, TypeError, OverflowError, struct.error):
        raise Fatal(f"{value!r} isn't a valid {kind}") from None


def shortest(number):
    """The shortest text that reads back as the same 32-bit float, as a float."""
    try:
        single = struct.unpack("<f", struct.pack("<f", number))[0]
    except (OverflowError, struct.error):
        return number
    for digits in range(1, 10):
        text = f"{single:.{digits}g}"
        if struct.unpack("<f", struct.pack("<f", float(text)))[0] == single:
            return float(text)
    return number


def flat(kind, text):
    """The decoded value as a number, a list of numbers (the shape encode() takes), or whatever JSON it was."""
    try:
        value = json.loads(text)
        if kind == "UDim":
            value = [value["scale"], value["offset"]]
        elif kind == "UDim2":
            value = [value["x"]["scale"], value["x"]["offset"], value["y"]["scale"], value["y"]["offset"]]
        elif kind == "BrickColor":
            value = value["number"]
    except (ValueError, KeyError, TypeError):
        return text
    if kind in SHAPES and kind != "double":
        parts = value if isinstance(value, list) else [value]
        parts = [shortest(part) if code == "f" else part for part, code in zip(parts, codes(kind))]
        value = parts if isinstance(value, list) else parts[0]
    if isinstance(value, str) and len(value) > MAX_TEXT:
        value = value[:MAX_TEXT]
    return value


class Hold:
    def __init__(self, chain, name, value, address, parent):
        self.chain, self.name, self.value, self.address, self.parent = chain, name, value, address, parent
        self.state, self.writes, self.looked = "starting", 0, 0.0

    @property
    def key(self):
        return key_of(self.chain, self.name)


def key_of(chain, name):
    return "/".join(chain) + "\u0001" + name


class Session:
    """One connection to a running client: the tree, the attributes, what's being held, and the scan."""

    def __init__(self, memory, tree, layout, how, build, pid, writer=None):
        self.memory, self.tree, self.layout = memory, tree, layout
        self.how, self.build, self.pid = how, build, pid
        self.attributes = Attributes(memory, layout)
        self.writer_for = writer or (lambda: memory)
        self.writer = None
        self.holds = {}
        self.lock = threading.Lock()
        self.keeper = None
        self.closed = False
        self.records, self.parents, self.branches = [], {}, set()
        self.scan_info = {"state": "idle", "instances": 0, "attributes": 0, "seconds": 0.0, "message": ""}
        self.stop_scan = False

    # ----------------------------------------------------------------------------------------------- tree

    def alive(self):
        return not self.closed and self.memory.read(self.tree.root, 8) is not None

    def named(self, address, leaf):
        return bool(address) and self.tree.name(address) == leaf

    def resolve(self, chain):
        """(instance, its parent) for a path of names from the root, or (0, 0)."""
        node = parent = self.tree.root
        for part in chain:
            parent = node
            node = next((child for child in self.tree.children(node) if self.tree.name(child) == part), 0)
            if not node:
                return 0, 0
        return node, parent

    def current(self, chain, address, parent):
        """Whether `address` is still the child of `parent` with the last name in `chain`: a destroyed instance can
        keep its memory (and its name) for a while, so the name alone doesn't say."""
        return bool(chain and address and parent and self.named(address, chain[-1])
                    and address in self.tree.children(parent))

    def child_count(self, instance):
        layout = self.layout
        vector = u64(self.memory, instance + layout.children)
        begin = u64(self.memory, vector) if vector else 0
        end = u64(self.memory, vector + layout.children_end) if vector else 0
        return (end - begin) // layout.child_stride if begin and end > begin else 0

    def attribute_count(self, instance):
        for value, component in self.attributes.components(instance):
            if component == self.layout.attribute_id:
                found = self.attributes.slots(value)
                return len(found[1]) if found else 0
        return 0

    def node(self, instance, name):
        return {"a": instance, "n": name, "k": self.child_count(instance) > 0,
                "c": self.attribute_count(instance), "b": instance in self.branches}

    def children(self, address):
        named = []
        for child in self.tree.children(address):
            name = self.tree.name(child)
            if name is not None:
                named.append((name, child))
        named.sort(key=lambda pair: pair[0].lower())
        return {"nodes": [self.node(child, name) for name, child in named[:MAX_CHILDREN]],
                "truncated": len(named) > MAX_CHILDREN}

    def chain_nodes(self, chain):
        """A node for the root and for each step down `chain`."""
        node = self.tree.root
        found = [self.node(node, "game")]
        for part in chain:
            node = next((child for child in self.tree.children(node) if self.tree.name(child) == part), 0)
            if not node:
                raise Fatal(f"there's no {part!r} there (yet)")
            found.append(self.node(node, part))
        return found

    def jump(self, which):
        if which in ("Workspace", "Players"):
            return self.chain_nodes([which])
        me = local_player(self.tree)
        if not me:
            raise Fatal("couldn't tell which player you are (the game may still be loading)")
        if which == "backpack":
            return self.chain_nodes(["Players", me, "Backpack"])
        for chain in (["Workspace", "Characters", me], ["Workspace", me]):
            if self.resolve(chain)[0]:
                return self.chain_nodes(chain)
        raise Fatal(f"{me}'s character isn't in the workspace right now")

    def find(self, ref):
        """(instance, parent) for a reference {c: names from the root, a: last known address, p: its parent}: the
        known address while it's still there, else the path looked up again."""
        chain = ref["c"]
        if not chain:
            return self.tree.root, 0
        if self.current(chain, ref.get("a", 0), ref.get("p", 0)):
            return ref["a"], ref["p"]
        return self.resolve(chain)

    # ------------------------------------------------------------------------------------------ attributes

    def inspect(self, ref):
        chain = ref["c"]
        instance, parent = self.find(ref)
        if not instance:
            return {"gone": True}
        found = []
        for name, kind, decoded, text in sorted(self.attributes.of(instance), key=lambda item: item[0].lower()):
            found.append({"name": name, "kind": kind, "decoded": decoded, "editable": decoded and (kind in SHAPES or kind == "bool"),
                          "value": flat(kind, text) if decoded else None})
        return {"address": instance, "parent": parent, "name": chain[-1] if chain else "game",
                "children": self.child_count(instance), "attributes": found}

    def locate(self, ref, name):
        instance, parent = self.find(ref)
        if not instance:
            raise Fatal("that instance isn't there any more")
        located = self.attributes.locate(instance, name)
        if located is None:
            raise Fatal(f"{name} isn't on that instance (any more); attributes can't be added from outside the game")
        return instance, parent, located

    def write(self, entry, kind, value):
        """Writes the value into the entry. Returns whether it differed."""
        data = encode(kind, value)
        slot = entry + self.layout.entry_value
        current = self.memory.read(slot, len(data))
        if current == data:
            return False
        if self.writer is None:
            self.writer = self.writer_for()
        if not self.writer.write(slot, data) or self.memory.read(slot, len(data)) != data:
            raise Fatal(f"couldn't write to Roblox (Windows error {ctypes.get_last_error()})")
        return True

    def set_value(self, ref, name, value):
        instance, parent, (entry, kind) = self.locate(ref, name)
        return {"changed": self.write(entry, kind, value), "address": instance, "parent": parent}

    # ------------------------------------------------------------------------------------------------ holds

    def hold(self, ref, name, value):
        instance, parent, (entry, kind) = self.locate(ref, name)
        self.write(entry, kind, value)
        item = Hold(list(ref["c"]), name, value, instance, parent)
        item.state = "holding"
        with self.lock:
            self.holds[item.key] = item
            if self.keeper is None:
                self.keeper = threading.Thread(target=self.keep, daemon=True)
                self.keeper.start()
        return {"key": item.key}

    def release(self, key):
        with self.lock:
            self.holds.pop(key, None)

    def release_all(self):
        with self.lock:
            self.holds.clear()

    def hold_list(self):
        with self.lock:
            return [{"key": item.key, "chain": item.chain, "name": item.name, "value": item.value,
                     "state": item.state, "writes": item.writes} for item in self.holds.values()]

    def keep(self):
        while True:
            with self.lock:
                items = list(self.holds.values())
                if not items or self.closed:
                    self.keeper = None
                    return
            for item in items:
                self.enforce(item)
            time.sleep(0.05)

    def enforce(self, item):
        try:
            instance = item.address if self.current(item.chain, item.address, item.parent) else 0
            if not item.chain:
                instance = self.tree.root
            if not instance:
                if time.monotonic() - item.looked < 0.5:
                    return
                item.looked = time.monotonic()
                instance, item.parent = self.resolve(item.chain)
                item.address = instance
            if not instance:
                item.state = "waiting for the instance"
                return
            located = self.attributes.locate(instance, item.name)
            if located is None:
                item.state = "waiting for the attribute"
                return
            if self.write(located[0], located[1], item.value):
                item.writes += 1
            item.state = "holding"
        except Exception as error:
            item.state = str(error) if isinstance(error, Fatal) else f"{type(error).__name__}: {error}"

    # --------------------------------------------------------------------------------------------- scan/search

    def names_of(self, instance, parents=None):
        """Names from the root down to the instance, as seen by the last scan (or by the scan in progress)."""
        parents = self.parents if parents is None else parents
        names = []
        while instance and instance in parents:
            instance, name = parents[instance]
            names.append(name)
        return names[::-1]

    def scan(self):
        """Walks the whole tree for attributes (run on a thread); fills records, parents and branches."""
        started = time.monotonic()
        attributes = Attributes(self.memory, self.layout)
        info = self.scan_info
        info.update(state="running", instances=0, attributes=0, seconds=0.0, message="")
        self.stop_scan = False
        records, parents, branches, seen = [], {self.tree.root: (0, "game")}, set(), set()
        stack = [(self.tree.root, 0, 0)]
        try:
            while stack and not self.stop_scan and not self.closed:
                instance, parent, depth = stack.pop()
                if instance in seen or depth > MAX_DEPTH:
                    continue
                seen.add(instance)
                info["instances"] += 1
                info["seconds"] = time.monotonic() - started
                found = attributes.of(instance)
                if found:
                    path = "/".join(self.names_of(instance, parents))
                    lowered = path.lower()
                    for name, kind, decoded, text in found:
                        records.append((instance, path, name, kind, decoded, text, name.lower(), lowered))
                    info["attributes"] += len(found)
                    above = instance
                    while above in parents and above not in branches:
                        branches.add(above)
                        above = parents[above][0]
                for child in reversed(self.tree.children(instance)):
                    name = self.tree.name(child)
                    if name is not None and child not in seen:
                        parents.setdefault(child, (instance, name))
                        stack.append((child, instance, depth + 1))
        except Exception as error:   # the process going away mid-walk is the usual cause
            info.update(state="error", message=f"{type(error).__name__}: {error}")
            return
        self.records, self.parents, self.branches = records, parents, branches
        info.update(state="stopped" if self.stop_scan else "done", seconds=time.monotonic() - started)

    def start_scan(self):
        if self.scan_info["state"] == "running":
            return
        self.scan_info["state"] = "running"
        threading.Thread(target=self.scan, daemon=True).start()

    def search(self, text, field, kind, limit=300):
        needle = text.strip().lower()
        hits, total = [], 0
        for record in self.records:
            instance, path, name, record_kind, decoded, value, lname, lpath = record
            if kind and record_kind != kind:
                continue
            if needle:
                by_name, by_path = needle in lname, needle in lpath
                by_value = (field in ("any", "value")) and needle in value.lower()
                if not ((field in ("any", "name") and by_name) or (field in ("any", "path") and by_path) or by_value):
                    continue
            total += 1
            if len(hits) < limit:
                hits.append({"a": instance, "path": path, "name": name, "kind": record_kind, "decoded": decoded,
                             "value": value if len(value) <= 160 else value[:160] + "…"})
        return {"hits": hits, "total": total, "scanned": len(self.records)}

    def chain_of(self, instance):
        return self.chain_nodes(self.names_of(instance)[1:])

    def export(self, path):
        payload = {"build": self.build, "scope": "game", "attributes": [
            {"instance_address": instance, "instance_path": where, "name": name, "type": kind, "decoded": decoded,
             "value_json": text} for instance, where, name, kind, decoded, text, _, _ in self.records]}
        with open(path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False)
        return len(self.records)

    def close(self):
        self.closed = True
        self.stop_scan = True
        self.release_all()


def safe(method):
    def wrapper(self, *args):
        try:
            return method(self, *args)
        except Fatal as error:
            return {"error": str(error)}
        except Exception as error:   # keep one bad read from taking the window down
            return {"error": f"{type(error).__name__}: {error}"}
    wrapper.__name__ = method.__name__
    wrapper.__doc__ = method.__doc__
    return wrapper


class Api:
    """What the page can call. Everything with a leading underscore stays private to Python."""

    def __init__(self):
        self._session = None
        self._window = None
        self._connecting = False
        self._message = ""
        self._lock = threading.Lock()

    def _need(self):
        if self._session is None or self._session.closed:
            raise Fatal("not connected to Roblox")
        return self._session

    def status(self):
        session = self._session
        if session is not None and not session.alive():
            session.close()
            self._message = "Roblox closed"
            session = self._session = None
        return {"connected": session is not None, "connecting": self._connecting, "message": self._message,
                "pids": roblox_pids(), "pid": session.pid if session else 0, "build": session.build if session else "",
                "how": session.how if session else "", "scan": dict(session.scan_info) if session else None,
                "holds": session.hold_list() if session else []}

    @safe
    def connect(self, pid=0, rediscover=False):
        with self._lock:
            if self._connecting:
                return {"busy": True}
            self._connecting = True
        try:
            if self._session is not None:
                self._session.close()
                self._session = None
            pid = pid or (roblox_pids() or [0])[0]
            if not pid:
                raise Fatal("Roblox isn't running")
            memory = Process(pid)
            build = memory.build()
            base, size = module_range(pid)
            tree, layout, how = find_layout(memory, base, size, build, bool(rediscover))
            self._session = Session(memory, tree, layout, how, build, pid, writer=lambda: Process(pid, write=True))
            self._message = ""
            return {"ok": True}
        finally:
            self._connecting = False

    def disconnect(self):
        if self._session is not None:
            self._session.close()
            self._session = None
        self._message = ""
        return {"ok": True}

    @safe
    def root(self):
        session = self._need()
        return session.chain_nodes([])[0]

    @safe
    def children(self, address):
        return self._need().children(address)

    @safe
    def nodes(self, chain):
        return {"nodes": self._need().chain_nodes(chain)}

    @safe
    def jump(self, which):
        return {"nodes": self._need().jump(which)}

    @safe
    def inspect(self, ref):
        return self._need().inspect(ref)

    @safe
    def set_value(self, ref, name, value):
        return self._need().set_value(ref, name, value)

    @safe
    def hold(self, ref, name, value):
        return self._need().hold(ref, name, value)

    @safe
    def release(self, key):
        self._need().release(key)
        return {"ok": True}

    @safe
    def release_all(self):
        self._need().release_all()
        return {"ok": True}

    @safe
    def scan(self):
        self._need().start_scan()
        return {"ok": True}

    @safe
    def stop_scan(self):
        self._need().stop_scan = True
        return {"ok": True}

    @safe
    def search(self, text, field, kind):
        return self._need().search(text, field, kind)

    @safe
    def locate(self, address):
        return {"nodes": self._need().chain_of(address)}

    @safe
    def export_json(self):
        session = self._need()
        if not session.records:
            raise Fatal("scan the game first")
        import webview
        picked = self._window.create_file_dialog(webview.FileDialog.SAVE, directory=HERE, save_filename="attributes.json",
                                                 file_types=("JSON (*.json)",))
        if not picked:
            return {"cancelled": True}
        path = picked if isinstance(picked, str) else picked[0]
        return {"count": session.export(path), "path": path}


def dark_titlebar(window):
    """Dark caption and border on Windows 11 (the page is dark; a white title bar looks wrong). Best effort."""
    try:
        handle = window.native.Handle.ToInt64()
        dwm = ctypes.windll.dwmapi
        for attribute, value in ((20, 1), (35, 0x00130F0D), (34, 0x00261F1B), (36, 0x00F0E7E9)):
            dwm.DwmSetWindowAttribute(ctypes.c_void_p(handle), attribute, ctypes.byref(ctypes.c_int(value)), 4)
    except Exception:
        pass


def selftest(report):
    """Opens the page in a hidden window, calls the API through it, writes what happened to `report`, and exits."""
    import webview
    api = Api()
    window = webview.create_window("AttrDump", PAGE, js_api=api, hidden=True)
    result = {}

    def run():
        try:
            for _ in range(100):
                if window.evaluate_js("typeof window.pywebview !== 'undefined' && !!window.pywebview.api"):
                    break
                time.sleep(0.1)
            window.evaluate_js("window.__probe = null; pywebview.api.status().then(function (r) { window.__probe = r; });")
            for _ in range(100):
                probe = window.evaluate_js("window.__probe")
                if probe:
                    result["status"] = probe
                    break
                time.sleep(0.1)
            result["title"] = window.evaluate_js("document.title")
            result["rows"] = window.evaluate_js("document.querySelectorAll('button').length")
        except Exception as error:
            result["error"] = f"{type(error).__name__}: {error}"
        finally:
            with open(report, "w", encoding="utf-8") as file:
                json.dump(result, file, indent=1)
            window.destroy()

    webview.start(run, gui="edgechromium")


def main(argv):
    import webview
    if "--selftest" in argv:
        selftest(argv[argv.index("--selftest") + 1])
        return 0
    api = Api()
    window = webview.create_window("AttrDump", PAGE, js_api=api, width=1360, height=860, min_size=(1000, 620),
                                   background_color="#0d0f13")
    api._window = window
    window.events.shown += lambda: dark_titlebar(window)
    webview.start(gui="edgechromium")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

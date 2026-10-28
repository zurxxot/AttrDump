"""Read-only dump of every attribute in the running Roblox client.

Finds the instance tree and attribute storage layout of whatever Roblox build is running (see layout.py), reads each
instance's attributes straight out of memory and writes attributes.json beside this script. It never writes to Roblox
(attrset.py is the separate tool that does).

    python attrdump.py [root path] [--out file.json] [--rediscover]
"""

import json
import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from attrprobe import Fatal, Process, module_range, roblox_pid  # noqa: E402
import layout as lay  # noqa: E402
from layout import Layout, Tree, TYPES, pointer, q, std_string, string_from, u64  # noqa: E402

MAX_ENTRIES = 100000
MAX_DEPTH = 64
SAMPLE = 8000          # instances checked to see whether a saved layout still fits


def number(value, digits=9):
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.{digits}g}"


class Attributes:
    def __init__(self, memory, layout):
        self.memory = memory
        self.layout = layout
        self.types = {}          # type descriptor -> name; descriptors are static
        self.storages = 0        # attribute storages seen
        self.unreadable = 0      # ... that couldn't be parsed
        self.attributes = 0
        self.undecoded = 0

    def components(self, instance):
        layout = self.layout
        block = u64(self.memory, instance + layout.components)
        if not block:
            return []
        begin, end = u64(self.memory, block), u64(self.memory, block + 8)
        stride = layout.component_stride
        count = (end - begin) // stride if end > begin else 0
        if not 0 < count <= lay.MAX_COMPONENTS:
            return []
        raw = self.memory.read(begin, count * stride)
        if raw is None:
            return []
        return [(q(raw, i * stride), q(raw, i * stride + layout.component_id) & 0xFFFF) for i in range(count)]

    def slots(self, storage):
        """(address of the entry array, [slot numbers in use]) for an attribute storage object, or None."""
        layout = self.layout
        raw = self.memory.read(storage, 0x80)
        if raw is None:
            return None
        kind = layout.kinds.get(str(struct.unpack_from("<I", raw, layout.kind)[0]))
        if kind is None:
            return None
        count, = struct.unpack_from("<I", raw, kind["count"])
        data = q(raw, kind["data"])
        if count > MAX_ENTRIES or (count and not pointer(data)):
            return None
        if not count:
            return data, []
        slots = range(count)
        if kind.get("table"):
            slots = lay.table_slots(self.memory, raw, kind["table"])
            if not slots or len(slots) != count:
                return None
        return data, slots

    def entries(self, storage):
        """The raw entries behind an attribute storage object, or None if it can't be read."""
        found = self.slots(storage)
        if found is None:
            return None
        data, slots = found
        if not slots:
            return []
        stride = self.layout.entry_stride
        block = self.memory.read(data, (slots[-1] + 1) * stride)
        if block is None:
            return None
        return [block[i * stride:(i + 1) * stride] for i in slots]

    def locate(self, instance, name):
        """(address of the entry, type name) of the live attribute called `name` on the instance, or None."""
        layout = self.layout
        for value, component in self.components(instance):
            if component != layout.attribute_id:
                continue
            found = self.slots(value)
            if found is None:
                return None
            data, slots = found
            for slot in slots:
                address = data + slot * layout.entry_stride
                block = self.memory.read(address, layout.entry_stride)
                node = q(block, layout.entry_name) if block else 0
                if pointer(node) and std_string(self.memory, node + layout.entry_name_string) == name:
                    return address, self.type_name(q(block, layout.entry_type))
            return None
        return None

    def type_name(self, descriptor):
        name = self.types.get(descriptor)
        if name is None:
            layout = self.layout
            string = u64(self.memory, descriptor + layout.type_name) if pointer(descriptor) else 0
            name = std_string(self.memory, string + layout.type_string) if pointer(string) else None
            if name is not None:
                self.types[descriptor] = name
        return name

    def of(self, instance):
        """[(name, type, decoded, json)] for the instance's attributes."""
        layout = self.layout
        found = []
        for value, component in self.components(instance):
            if component != layout.attribute_id:
                continue
            self.storages += 1
            entries = self.entries(value)
            if entries is None:
                self.unreadable += 1
                continue
            for entry in entries:
                node = q(entry, layout.entry_name)
                kind = self.type_name(q(entry, layout.entry_type))
                name = std_string(self.memory, node + layout.entry_name_string) if pointer(node) else None
                self.attributes += 1
                if name is None or kind is None:
                    self.undecoded += 1
                    continue
                decoded, text = self.decode(kind, entry[layout.entry_value:])
                self.undecoded += not decoded
                found.append((name, kind, decoded, text))
            break
        return found

    def decode(self, kind, value):
        layout = self.layout
        try:
            if kind == "bool":
                return value[0] <= 1, "true" if value[0] else "false"
            if kind == "double":
                number_ = struct.unpack_from("<d", value)[0]
                return math.isfinite(number_), number(number_, 15) if math.isfinite(number_) else "null"
            if kind == "float":
                return True, number(struct.unpack_from("<f", value)[0])
            if kind == "string":
                text = string_from(self.memory, value)
                return text is not None, json.dumps(text, ensure_ascii=False) if text is not None else "null"
            if kind in ("Vector3", "Color3"):
                return True, "[" + ", ".join(number(v) for v in struct.unpack_from("<3f", value)) + "]"
            if kind in ("Vector2", "NumberRange"):
                return True, "[" + ", ".join(number(v) for v in struct.unpack_from("<2f", value)) + "]"
            if kind == "Rect2D":
                return True, "[" + ", ".join(number(v) for v in struct.unpack_from("<4f", value)) + "]"
            if kind == "UDim":
                scale, offset = struct.unpack_from("<fi", value)
                return True, f'{{"scale": {number(scale)}, "offset": {offset}}}'
            if kind == "UDim2":
                xs, xo, ys, yo = struct.unpack_from("<fifi", value)
                return True, (f'{{"x": {{"scale": {number(xs)}, "offset": {xo}}}, '
                              f'"y": {{"scale": {number(ys)}, "offset": {yo}}}}}')
            if kind == "BrickColor":
                return True, f'{{"number": {struct.unpack_from("<I", value)[0]}}}'
            if kind == "CoordinateFrame":
                matrix = struct.unpack_from("<12f", value)
                return True, (f'{{"position": [{", ".join(number(v) for v in matrix[9:])}], '
                              f'"rotation": [{", ".join(number(v) for v in matrix[:9])}]}}')
            if kind == "Font":
                font, = struct.unpack_from("<Q", value)
                family = std_string(self.memory, font + layout.font_family)
                weight, style = struct.unpack("<II", self.memory.read(font + layout.font_weight, 8))
                if family is not None and weight in range(100, 1000, 100) and style in (0, 1, 2):
                    return True, f'{{"family": {json.dumps(family)}, "weight": {weight}, "style": {style}}}'
            if kind in ("NumberSequence", "ColorSequence"):
                points, count, capacity = struct.unpack_from("<QII", value)
                width = 3 if kind == "NumberSequence" else 5   # time, value, envelope / time, r, g, b, envelope
                raw = self.memory.read(points, count * width * 4) if 0 < count <= capacity <= 1024 else None
                if raw is not None:
                    keys = struct.unpack(f"<{count * width}f", raw)
                    if kind == "NumberSequence":
                        items = [f'{{"time": {number(keys[i])}, "value": {number(keys[i + 1])}, '
                                 f'"envelope": {number(keys[i + 2])}}}' for i in range(0, len(keys), 3)]
                    else:
                        items = [f'{{"time": {number(keys[i])}, "color": [{", ".join(number(v) for v in keys[i + 1:i + 4])}]}}'
                                 for i in range(0, len(keys), 5)]
                    return True, "[" + ", ".join(items) + "]"
        except (struct.error, TypeError):
            pass
        return False, json.dumps(value.hex())

    def fits(self):
        """Whether what was read so far looks like real attributes rather than garbage."""
        return (self.storages >= 20 and self.attributes >= 30 and self.unreadable <= 0.01 * self.storages
                and self.undecoded <= 0.01 * self.attributes)


def walk(tree, attributes, root, name):
    """Pre-order walk of the tree yielding (instance, path, attributes) for instances that have attributes."""
    seen = set()
    stack = [(root, name, 0)]
    while stack:
        instance, path, depth = stack.pop()
        if instance in seen or depth > MAX_DEPTH:
            continue
        seen.add(instance)
        found = attributes.of(instance)
        if found:
            yield instance, path, found
        for child in reversed(tree.children(instance)):
            child_name = tree.name(child)
            if child_name is not None:
                stack.append((child, f"{path}/{child_name}", depth + 1))


def fits(memory, tree, layout):
    attributes = Attributes(memory, layout)
    for instance in tree.sample(SAMPLE):
        attributes.of(instance)
    return attributes.fits()


def find_layout(memory, base, size, build, rediscover):
    """(tree, layout, how): the layout of the running build, reusing the saved one only while it still fits."""
    image = (base, base + size)
    saved = lay.load_cached(build) or {}
    found = None
    session = saved.get("session")
    if session and session.get("pid") == memory.pid:
        # Same process as last time, so the DataModel hasn't moved: only check that it's still there.
        again = lay.discover_tree(memory, session["object"], image)
        if again:
            found = (session["object"] + again[1], saved.get("vtable"), again[0], session["object"])
    if not found:
        found = lay.find_datamodel(memory, base, size, image, saved.get("vtable"))
    if not found:
        raise Fatal("couldn't find the DataModel (no RTTI for it, or the game hasn't loaded yet)")
    root, vtable, tree_fields, anchor = found
    remember = {"vtable": vtable, "session": {"pid": memory.pid, "object": anchor}}
    prior = {**lay.DEFAULT, **saved, **tree_fields}
    layout = Layout(prior)
    tree = Tree(memory, root, layout)
    if not rediscover and fits(memory, tree, layout):
        lay.save_cached(build, {**layout.values, **remember})
        return tree, layout, "saved" if saved else "built-in"

    fields = lay.discover_attributes(memory, tree, image, prior)
    if fields is None:
        raise Fatal("couldn't work out how this build stores attributes (the shape may have changed; "
                    "attrprobe.py can look at it by hand)")
    layout = Layout({**fields, **tree_fields})
    tree = Tree(memory, root, layout)
    if not fits(memory, tree, layout):
        raise Fatal("worked out a layout, but what it reads doesn't look like attributes")
    lay.save_cached(build, {**layout.values, **remember})
    return tree, layout, "discovered"


def main(argv):
    args = argv[1:]
    out = os.path.join(os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)), "attributes.json")
    if "--out" in args:
        at = args.index("--out")
        out = args[at + 1]
        del args[at:at + 2]
    rediscover = "--rediscover" in args
    args = [arg for arg in args if arg != "--rediscover"]
    parts = [part for part in (args[0] if args else "game").replace(".", "/").split("/") if part]
    root_path = "/".join(parts if parts[:1] == ["game"] else ["game"] + parts)

    pid = roblox_pid()
    if not pid:
        raise Fatal("Roblox isn't running")
    memory = Process(pid)
    build = memory.build()
    base, size = module_range(pid)
    tree, layout, how = find_layout(memory, base, size, build, rediscover)
    print(f"{build}: layout {how}")
    try:
        root = tree.resolve(root_path)
    except LookupError as error:
        raise Fatal(str(error)) from None

    attributes = Attributes(memory, layout)
    results = []
    instances = 0
    for instance, path, found in walk(tree, attributes, root, root_path):
        instances += 1
        for name, kind, decoded, text in found:
            results.append({"instance_address": instance, "instance_path": path, "name": name, "type": kind,
                            "decoded": decoded, "value_json": text})
    with open(out, "w", encoding="utf-8") as file:
        json.dump({"build": build, "scope": root_path, "attributes": results}, file, ensure_ascii=False)

    print(f"{len(results)} attributes on {instances} instances under {root_path}")
    if attributes.undecoded or attributes.unreadable:
        print(f"{attributes.undecoded} values not decoded, {attributes.unreadable} storages unreadable")
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Fatal as error:
        print(f"error: {error}")
        sys.exit(1)

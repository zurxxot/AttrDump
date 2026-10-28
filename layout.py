"""Finds how a Roblox build lays out its instance tree and attribute storage by looking at the running process, so a
Roblox update doesn't need new offsets.

The DataModel is found through its RTTI name, the tree offsets from the children it must have (Workspace, Players), and
the attribute offsets from the shapes the storage has to take (a vector of components, a block of entries that each
point at a name string and a type descriptor whose name is a known type). Whatever the last build used (DEFAULT, or
the saved copy) is only a tiebreaker. Everything here only reads.
"""

import collections
import json
import os
import struct
import sys

CACHE = os.path.join(os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)), "layout_cache.json")

TYPES = {"bool", "double", "float", "string", "Vector2", "Vector3", "Color3", "UDim", "UDim2", "Font", "BrickColor",
         "Rect2D", "NumberRange", "NumberSequence", "ColorSequence", "CoordinateFrame"}

# What 9ac8595712b447a7 uses.
DEFAULT = {
    "children": 0x70, "children_end": 0x8, "child_stride": 0x10,
    "name": 0x68, "name_mode": "ptr", "name_string": 0x0,
    "components": 0x38, "component_stride": 0x10, "component_id": 0x8,
    "attribute_id": 11,
    "kind": 0x0,
    "kinds": {"0": {"data": 0x8, "count": 0x10},
              "1": {"data": 0x18, "count": 0x8, "table": {"buckets": 0x10, "mask": 0x20, "exponent": 0x24}}},
    "entry_stride": 0x58, "entry_name": 0x0, "entry_name_string": 0x0, "entry_type": 0x8,
    "type_name": 0x8, "type_string": 0x0, "entry_value": 0x18,
    "font_family": 0x8, "font_weight": 0x48, "font_style": 0x4C,
}

MAX_COMPONENTS = 512
CANARIES = {"RBX_LightingTechnologyUnifiedMigration", "ChatMigratedToExpChat"}
MAX_ENTRIES = 100000


class Layout:
    def __init__(self, values=None):
        self.values = dict(DEFAULT)
        self.values.update(values or {})

    def __getattr__(self, name):
        try:
            return self.__dict__["values"][name]
        except KeyError:
            raise AttributeError(name) from None


def load_cached(build):
    try:
        with open(CACHE, encoding="utf-8") as file:
            return json.load(file).get(build)
    except (OSError, ValueError):
        return None


def save_cached(build, values):
    try:
        with open(CACHE, encoding="utf-8") as file:
            cache = json.load(file)
    except (OSError, ValueError):
        cache = {}
    cache[build] = values
    try:
        with open(CACHE, "w", encoding="utf-8") as file:
            json.dump(cache, file, indent=1)
    except OSError:
        pass


# --------------------------------------------------------------------------- reading

def u64(mem, address):
    raw = mem.read(address, 8)
    return struct.unpack("<Q", raw)[0] if raw else 0


def q(buffer, offset):
    return struct.unpack_from("<Q", buffer, offset)[0] if offset + 8 <= len(buffer) else 0


def pointer(value):
    return 0x10000 <= value <= 0x7FFFFFFFFFFF


def string_from(mem, raw):
    """The text of an MSVC std::string whose 32 bytes are `raw`, or None."""
    if len(raw) < 32:
        return None
    head, size, capacity = struct.unpack_from("<16sQQ", raw)
    if size > capacity or capacity < 15 or size > 1 << 22:
        return None
    if capacity == 15:
        return head[:size].decode("utf-8", "replace")
    data = mem.read(struct.unpack_from("<Q", head)[0], size) if size else b""
    return None if data is None else data.decode("utf-8", "replace")


def std_string(mem, at):
    raw = mem.read(at, 32) if pointer(at) else None
    return string_from(mem, raw) if raw else None


def plausible_text(text):
    return text is not None and 1 <= len(text) <= 256 and all(ch >= " " and ch != "\x7f" for ch in text)


def read_range(mem, address, size, piece=1 << 22):
    """size bytes at address; pages that can't be read come back as zeros."""
    out = bytearray()
    done = 0
    while done < size:
        count = min(piece, size - done)
        data = mem.read(address + done, count)
        if data is None:
            data = bytearray()
            for page in range(0, count, 0x1000):
                part = mem.read(address + done + page, min(0x1000, count - page))
                data += part if part else bytes(min(0x1000, count - page))
        out += data
        done += count
    return bytes(out)


def sections(mem, base):
    """[(name, rva, size, flags)] from the PE header of the module at base."""
    head = mem.read(base, 0x1000)
    if not head:
        return []
    nt = struct.unpack_from("<I", head, 0x3C)[0]
    count, = struct.unpack_from("<H", head, nt + 6)
    optional, = struct.unpack_from("<H", head, nt + 20)
    table = nt + 24 + optional
    found = []
    for index in range(count):
        name, size, rva, _, _, _, _, _, _, flags = struct.unpack_from("<8sIIIIIIHHI", head, table + 40 * index)
        found.append((name.rstrip(b"\0").decode("latin-1"), rva, size, flags))
    return found


def scan_qword(mem, value, regions, chunk=1 << 24):
    """Addresses (8-aligned) of every occurrence of `value` in the given (base, size) regions."""
    needle = struct.pack("<Q", value)
    hits = []
    for base, size in regions:
        for start in range(0, size, chunk):
            data = read_range(mem, base + start, min(chunk + 8, size - start))
            at = data.find(needle)
            while 0 <= at < chunk:
                if (base + start + at) % 8 == 0:
                    hits.append(base + start + at)
                at = data.find(needle, at + 1)
    return hits


EMPTY, DELETED = 0xFFFFFFFF, 0x7FFFFFFF


def table_slots(mem, wrapper, table):
    """The entry slots a wrapper's bucket table says are in use (each bucket word holds a slot number in its low bits;
    removing an attribute leaves the entry where it was and only marks its bucket), or None if it doesn't read as one."""
    mask, = struct.unpack_from("<I", wrapper, table["mask"])
    if not 0 < mask < 1 << 20 or mask & (mask + 1):
        return None
    if table["exponent"] is None:
        words = 2 * (mask + 1)
    else:
        exponent, = struct.unpack_from("<I", wrapper, table["exponent"])
        if not 1 <= exponent <= 21:
            return None
        words = 1 << exponent
    buckets = q(wrapper, table["buckets"])
    raw = mem.read(buckets, words * 4) if pointer(buckets) else None
    if raw is None:
        return None
    return sorted({word & mask for word in struct.unpack(f"<{words}I", raw) if word not in (EMPTY, DELETED)})


# --------------------------------------------------------------------------- the tree

class Tree:
    def __init__(self, mem, root, layout):
        self.mem = mem
        self.root = root
        self.layout = layout

    def name(self, instance):
        layout = self.layout
        if layout.name_mode == "inline":
            return std_string(self.mem, instance + layout.name)
        node = u64(self.mem, instance + layout.name)
        return std_string(self.mem, node + layout.name_string) if node else None

    def children(self, instance):
        layout = self.layout
        vector = u64(self.mem, instance + layout.children)
        begin = u64(self.mem, vector) if vector else 0
        end = u64(self.mem, vector + layout.children_end) if vector else 0
        stride = layout.child_stride
        count = (end - begin) // stride if begin and end > begin else 0
        raw = self.mem.read(begin, count * stride) if 0 < count <= 20000 else None
        if raw is None:
            return []
        return [value for value in struct.unpack(f"<{count * stride // 8}Q", raw)[::stride // 8] if value]

    def resolve(self, path):
        node = self.root
        for part in [part for part in path.replace(".", "/").split("/") if part and part != "game"]:
            node = next((child for child in self.children(node) if self.name(child) == part), 0)
            if not node:
                raise LookupError(f"no child called {part!r} in {path!r}")
        return node

    def sample(self, limit):
        """Up to `limit` instances, breadth first."""
        seen = {self.root}
        order = [self.root]
        at = 0
        while at < len(order) and len(order) < limit:
            for child in self.children(order[at]):
                if child not in seen:
                    seen.add(child)
                    order.append(child)
            at += 1
        return order[:limit]


def vector_of_pointers(mem, vector, end_at, stride, in_image, least=1):
    """The pointers in a {begin, end, ...} vector whose elements are `stride` bytes apart, or None."""
    head = mem.read(vector, 0x20) if pointer(vector) and not in_image(vector) else None
    if not head:
        return None
    begin, end = q(head, 0), q(head, end_at)
    if not (pointer(begin) and pointer(end) and end > begin) or (end - begin) % stride:
        return None
    count = (end - begin) // stride
    if not least <= count <= 20000:
        return None
    raw = mem.read(begin, count * stride)
    return [q(raw, i * stride) for i in range(count)] if raw else None


def discover_tree(mem, datamodel, image):
    """(tree layout, offset from `datamodel` to its Instance part), if Workspace and Players are its children."""
    low, high = image
    in_image = lambda value: low <= value < high
    block = mem.read(datamodel, 0x300)
    if not block:
        return None
    for children in range(0, 0x2F8, 8):
        for end_at in (0x8, 0x10):
            for stride in (0x10, 0x8):
                kids = vector_of_pointers(mem, q(block, children), end_at, stride, in_image, 3)
                if not kids or len(kids) > 2000:
                    continue
                blocks = {}
                for kid in kids:
                    kid_block = mem.read(kid, 0x300) if pointer(kid) and not in_image(kid) else None
                    if not kid_block or not in_image(q(kid_block, 0)):
                        break
                    blocks[kid] = kid_block
                else:
                    found = name_offsets(mem, blocks, in_image)
                    if not found:
                        continue
                    own = own_children_offset(mem, blocks, end_at, stride, found, in_image)
                    if own is not None:
                        return {"children": own, "children_end": end_at, "child_stride": stride, **found}, children - own
    return None


def own_children_offset(mem, blocks, end_at, stride, name_layout, in_image):
    """Where an ordinary instance keeps its children: the DataModel is a later part of a bigger object, so its own
    children vector sits at a different offset than its children's do."""
    tree = Tree(mem, 0, Layout({"children_end": end_at, "child_stride": stride, **name_layout}))
    votes = collections.Counter()
    for block in blocks.values():
        for offset in range(0, 0x2F8, 8):
            kids = vector_of_pointers(mem, q(block, offset), end_at, stride, in_image)
            if kids and all(pointer(kid) and in_image(u64(mem, kid)) for kid in kids[:8]):
                names = [tree.name(kid) for kid in kids[:8]]
                if all(plausible_text(name) for name in names):
                    votes[offset] += 1
    if not votes:
        return None
    best = max(votes.values())
    return min(offset for offset, count in votes.items() if count == best) if best >= 2 else None


def name_offsets(mem, blocks, in_image):
    for name in range(0, 0x2F8, 8):
        for mode, string in (("ptr", 0x0), ("ptr", 0x8), ("inline", 0x0)):
            names = set()
            for block in blocks.values():
                if mode == "inline":
                    names.add(string_from(mem, block[name:name + 32]))
                else:
                    node = q(block, name)
                    names.add(std_string(mem, node + string) if pointer(node) and not in_image(node) else None)
            if {"Workspace", "Players"} <= names:
                return {"name": name, "name_mode": mode, "name_string": string}
    return None


def find_datamodel(mem, base, size, image, known_vtable=None):
    """(datamodel, vtable rva, tree layout, RTTI object) found through the DataModel's RTTI name, or None."""
    data = []
    for _, rva, length, flags in sections(mem, base):
        if length and flags & 0x40000000 and not flags & 0x20000000 and not flags & 0x02000000:
            data.append((rva, read_range(mem, base + rva, length)))

    def vtables():
        if known_vtable:
            yield known_vtable
        name = b".?AVDataModel@RBX@@\0"
        descriptors = [rva + at - 16 for rva, buf in data for at in [buf.find(name)] if at >= 16]
        locators = []
        for descriptor in descriptors:
            pattern = struct.pack("<I", descriptor)
            for rva, buf in data:
                at = buf.find(pattern)
                while at >= 0:
                    start = at - 12
                    if at % 4 == 0 and start >= 0 and struct.unpack_from("<I", buf, start)[0] == 1 \
                            and struct.unpack_from("<I", buf, start + 20)[0] == rva + start:
                        locators.append((struct.unpack_from("<I", buf, start + 4)[0], rva + start))
                    at = buf.find(pattern, at + 1)
        for _, locator in sorted(locators):
            pattern = struct.pack("<Q", base + locator)
            for rva, buf in data:
                at = buf.find(pattern)
                while at >= 0:
                    if at % 8 == 0:
                        yield rva + at + 8
                    at = buf.find(pattern, at + 1)

    tried = set()
    for vtable in vtables():
        if vtable in tried:
            continue
        tried.add(vtable)
        for candidate in scan_qword(mem, base + vtable, mem.regions())[:64]:
            found = discover_tree(mem, candidate, image)
            if found:
                return candidate + found[1], vtable, found[0], candidate
    return None


# --------------------------------------------------------------------------- the attributes

def entry_ok(mem, address, fields, in_image):
    """Whether the entry at `address` has a readable name and a type descriptor naming a known type."""
    block = mem.read(address, max(fields["entry_name"], fields["entry_type"]) + 8)
    if not block:
        return False
    node, descriptor = q(block, fields["entry_name"]), q(block, fields["entry_type"])
    if not (pointer(node) and pointer(descriptor)):
        return False
    return (plausible_text(std_string(mem, node + fields["entry_name_string"]))
            and type_name(mem, descriptor, fields) in TYPES)


def type_name(mem, descriptor, fields):
    string = u64(mem, descriptor + fields["type_name"])
    return std_string(mem, string + fields["type_string"]) if pointer(string) else None


def pick(votes, total, prior=None, floor=0.9):
    """The key most hits agree on (at least `floor` of them), preferring `prior`, then the lowest."""
    if not votes or not total:
        return None
    best = max(votes.values())
    if best < floor * total:
        return None
    leaders = sorted(key for key, count in votes.items() if count == best)
    return prior if prior in leaders else leaders[0]


def discover_attributes(mem, tree, image, prior, limit=30000, wanted=400):
    """The attribute part of the layout, or None if the storage can't be found."""
    low, high = image
    in_image = lambda value: low <= value < high
    hits = []
    kinds_seen = set()
    for instance in tree.sample(limit):
        block = mem.read(instance, 0x200)
        if not block:
            continue
        for components in range(0, 0x1F8, 8):
            vector = q(block, components)
            if not pointer(vector) or in_image(vector):
                continue
            head = mem.read(vector, 0x10)
            begin, end = (q(head, 0), q(head, 8)) if head else (0, 0)
            span = end - begin
            if not (pointer(begin) and span > 0 and span % 0x10 == 0 and span <= 0x10 * MAX_COMPONENTS):
                continue
            raw = mem.read(begin, span)
            for at in range(0, span if raw else 0, 0x10):
                wrapper = q(raw, at)
                wrapper_block = mem.read(wrapper, 0x80) if pointer(wrapper) and not in_image(wrapper) else None
                if wrapper_block:
                    found = storage_hits(mem, in_image, wrapper_block)
                    for hit in found:
                        hit.update(instance=instance, components=components, ident=q(raw, at + 8) & 0xFFFF)
                    hits.extend(found)
        kinds_seen.update(hit["kind"] for hit in hits)
        if len(hits) >= wanted and len(kinds_seen) >= 2 or len(hits) >= wanted * 4:
            break
    return settle_attributes(mem, hits, in_image, prior)


def storage_hits(mem, in_image, wrapper_block):
    """For each pointer in a component's value that leads to something entry-shaped, which offsets could be the
    entry's name and type."""
    found = []
    kind = struct.unpack_from("<I", wrapper_block, 0)[0]
    for data in range(0, 0x78, 8):
        entries = q(wrapper_block, data)
        block = mem.read(entries, 0x100) if pointer(entries) and not in_image(entries) else None
        if not block:
            continue
        names, types = set(), set()
        for offset in range(0, 0x40, 8):
            value = q(block, offset)
            if not pointer(value):
                continue
            if in_image(value):
                descriptor = mem.read(value, 0x40)
                for string_at in range(0, 0x38, 8):
                    string = q(descriptor, string_at) if descriptor else 0
                    for inner in (0x0, 0x8):
                        if pointer(string) and std_string(mem, string + inner) in TYPES:
                            types.add((offset, string_at, inner))
            else:
                for inner in (0x0, 0x8):
                    if plausible_text(std_string(mem, value + inner)):
                        names.add((offset, inner))
        if names and types:
            found.append({"kind": kind, "data": data, "entries": entries, "wrapper": wrapper_block,
                          "names": names, "types": types})
    return found


def names_in_a_row(mem, entries, fields, in_image):
    """The names of the entries, from the start of a block, for as long as they keep reading as entries."""
    names = []
    stride = fields["entry_stride"]
    while len(names) < 600 and entry_ok(mem, entries + len(names) * stride, fields, in_image):
        node = u64(mem, entries + len(names) * stride + fields["entry_name"])
        names.append(std_string(mem, node + fields["entry_name_string"]))
    return names


def settle_attributes(mem, hits, in_image, prior):
    if len(hits) < 5:
        return None
    components = pick(collections.Counter(h["components"] for h in hits), len(hits), prior["components"], 0.5)
    hits = [h for h in hits if h["components"] == components]
    total = len(hits)
    name = pick(collections.Counter(key for h in hits for key in h["names"]), total,
                (prior["entry_name"], prior["entry_name_string"]), 0.9)
    kind_type = pick(collections.Counter(key for h in hits for key in h["types"]), total,
                     (prior["entry_type"], prior["type_name"], prior["type_string"]), 0.9)
    if name is None or kind_type is None:
        return None
    fields = dict(prior)
    fields.update(components=components, entry_name=name[0], entry_name_string=name[1], entry_type=kind_type[0],
                  type_name=kind_type[1], type_string=kind_type[2])
    hits = [h for h in hits if name in h["names"] and kind_type in h["types"]]

    # Entry stride: the smallest distance at which a second entry still reads as an entry.
    strides = collections.Counter()
    multi = 0
    for hit in hits:
        valid = [s for s in range(0x20, 0x100, 8) if entry_ok(mem, hit["entries"] + s, fields, in_image)]
        if valid:
            multi += 1
            strides.update(valid)
    if not strides:
        return None
    common = sorted(s for s, c in strides.items() if c == max(strides.values()))
    fields["entry_stride"] = prior["entry_stride"] if prior["entry_stride"] in common else common[0]
    for hit in hits:
        hit["names_read"] = names_in_a_row(mem, hit["entries"], fields, in_image)

    # Which component id is the attributes: the one engine-owned attributes turn up under, else the most common.
    anchored = collections.Counter(h["ident"] for h in hits if CANARIES & set(h["names_read"]))
    counted = collections.Counter(h["ident"] for h in hits if h["names_read"])
    if not (anchored or counted):
        return None
    fields["attribute_id"] = (anchored or counted).most_common(1)[0][0]
    hits = [h for h in hits if h["ident"] == fields["attribute_id"] and h["names_read"]]

    # Per kind of wrapper: where its data pointer is, and which field holds how many entries it has.
    layout_kinds = {}
    typed = collections.defaultdict(list)
    for kind in {h["kind"] for h in hits}:
        group = [h for h in hits if h["kind"] == kind]
        if len(group) < 3:
            continue
        data = pick(collections.Counter(h["data"] for h in group), len({h["instance"] for h in group}), None, 0.9)
        if data is None:
            continue
        group = [h for h in group if h["data"] == data]
        votes = collections.Counter()
        for hit in group:
            run = len(hit["names_read"])
            for field in range(0, 0x3C, 4):
                if struct.unpack_from("<I", hit["wrapper"], field)[0] == run:
                    votes[field] += 1
            for index in range(min(run, 8)):
                block = mem.read(hit["entries"] + index * fields["entry_stride"], fields["entry_stride"])
                if block:
                    typed[type_name(mem, q(block, fields["entry_type"]), fields)].append(block)
        count = pick(votes, len(group), prior["kinds"].get(str(kind), {}).get("count"), 0.7)
        if count is not None:
            info = {"data": data, "count": count}
            table = discover_table(mem, group, info, fields, in_image, prior["kinds"].get(str(kind), {}).get("table"))
            if table:
                info["table"] = table
            layout_kinds[str(kind)] = info
    if not layout_kinds:
        return None
    fields["kinds"] = layout_kinds

    # Value slot: the offset where entries of type string hold a readable std::string.
    string_entries = typed.get("string", [])
    if string_entries:
        votes = collections.Counter()
        for block in string_entries:
            for offset in range(0x10, fields["entry_stride"] - 0x1F, 8):
                if string_from(mem, block[offset:offset + 32]) is not None:
                    votes[offset] += 1
        value = pick(votes, len(string_entries), prior["entry_value"], 0.9)
        if value is not None:
            fields["entry_value"] = value
    fields.update(discover_font(mem, typed.get("Font", []), fields, prior))
    return fields


def discover_table(mem, group, info, fields, in_image, prior):
    """Where a kind of wrapper keeps the bucket table that says which entry slots are live, if it has one: the table
    that, over the wrapper hits, names exactly as many slots as the wrapper's count and every one reads as an entry."""
    sample = group[:60]
    if len(sample) < 3:
        return None
    first = sample[0]["wrapper"]
    pointers = [o for o in range(0, 0x80, 8) if o != info["data"] and pointer(q(first, o)) and not in_image(q(first, o))]
    masks = [o for o in range(0, 0x3C, 4) if struct.unpack_from("<I", first, o)[0] >= 1
             and not struct.unpack_from("<I", first, o)[0] & (struct.unpack_from("<I", first, o)[0] + 1)]
    best, best_score = None, 0
    for buckets in pointers:
        for mask in masks:
            for exponent in [None] + list(range(0, 0x3C, 4)):
                table = {"buckets": buckets, "mask": mask, "exponent": exponent}
                good = 0
                for hit in sample:
                    slots = table_slots(mem, hit["wrapper"], table)
                    count = struct.unpack_from("<I", hit["wrapper"], info["count"])[0]
                    if slots and len(slots) == count and all(
                            entry_ok(mem, hit["entries"] + slot * fields["entry_stride"], fields, in_image)
                            for slot in slots[:3]):
                        good += 1
                score = good / len(sample) + (0.001 if table == prior else 0)
                if score > best_score:
                    best, best_score = table, score
    return best if best_score >= 0.9 else None


def discover_font(mem, entries, fields, prior):
    """Where a Font value keeps its family, weight and style (the prior if there are no Fonts to look at)."""
    found = {}
    objects = [q(block, fields["entry_value"]) for block in entries]
    objects = [o for o in objects if pointer(o)]
    if not objects:
        return found
    family = collections.Counter()
    weight = collections.Counter()
    for font in objects:
        block = mem.read(font, 0x80)
        if not block:
            continue
        for offset in range(0, 0x40, 8):
            text = string_from(mem, block[offset:offset + 32])
            if text and ("://" in text or text.endswith(".json")):
                family[offset] += 1
        for offset in range(0x20, 0x78, 4):
            w, s = struct.unpack_from("<II", block, offset)
            if w in range(100, 1000, 100) and s in (0, 1, 2):
                weight[offset] += 1
    chosen = pick(family, len(objects), prior["font_family"], 0.9)
    if chosen is not None:
        found["font_family"] = chosen
    chosen = pick(weight, len(objects), prior["font_weight"], 0.9)
    if chosen is not None:
        found["font_weight"] = chosen
        found["font_style"] = chosen + 4
    return found

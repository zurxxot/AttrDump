"""Overwrites an existing boolean or number attribute on your character by writing its value straight into Roblox's
memory, once or kept in place while the game runs.

    python attrset.py <attribute> <value> [--hold] [--interval 0.05] [--player NAME] [--path game/...] [--tool NAME] [--dry-run]

    python attrset.py UnbreakableAll true --hold
    python attrset.py NumChargesClient 99 --tool Lightning-Lightning --hold

--tool NAME changes the attribute on that tool instead of on your character, wherever it is (equipped or in
the Backpack).

Only the value of an attribute that already exists is changed. Adding or removing one means editing Roblox's own hash
table and allocating with its allocator, which can't be done safely from outside, so a missing attribute is an error.
Strings and the other types are refused for the same kind of reason. Nothing signals the change (no
AttributeChanged), and this opens the process for writing, which attrdump.py and attrprobe.py do not.
"""

import ctypes
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from attrdump import Attributes, find_layout  # noqa: E402
from attrprobe import Fatal, Process, module_range, roblox_pid  # noqa: E402
from layout import q  # noqa: E402

ON = {"true", "1", "on", "yes"}
OFF = {"false", "0", "off", "no"}


def encode(kind, text):
    """The bytes to write for a value of the given attribute type."""
    if kind == "bool":
        word = text.lower()
        if word not in ON | OFF:
            raise Fatal(f"{text!r} isn't a boolean (use true or false)")
        return b"\x01" if word in ON else b"\x00"
    if kind in ("double", "float"):
        try:
            return struct.pack("<d" if kind == "double" else "<f", float(text))
        except ValueError:
            raise Fatal(f"{text!r} isn't a number") from None
    raise Fatal(f"this is a {kind} attribute; only boolean and number attributes can be changed this way")


def local_player(tree):
    """The name of the local player: the Players child that one of the Players service's own fields points at."""
    players = tree.resolve("Players")
    block = tree.mem.read(players, 0x400)
    kids = set(tree.children(players))
    hits = {q(block, offset) for offset in range(0, 0x3F8, 8)} & kids if block else set()
    return tree.name(next(iter(hits))) if len(hits) == 1 else None


class Target:
    """Finds the instance to change again each time, since a respawn replaces the character."""

    def __init__(self, tree, attributes, attribute, player, path, tool=None):
        self.tree, self.attributes, self.attribute, self.player, self.path = tree, attributes, attribute, player, path
        self.tool = tool
        self.containers = {}

    def named(self, parent, name):
        cached = self.containers.get(name)
        if cached and self.tree.name(cached) == name:
            return cached
        found = next((child for child in self.tree.children(parent) if self.tree.name(child) == name), 0)
        if found:
            self.containers[name] = found
        return found

    def characters(self):
        workspace = self.named(self.tree.root, "Workspace")
        if not workspace:
            return []
        return [container for container in (self.named(workspace, "Characters"), workspace) if container]

    def child(self, parent, name, needs_attribute=False):
        for child in self.tree.children(parent):
            if self.tree.name(child) == name and (not needs_attribute or self.attributes.locate(child, self.attribute)):
                return child
        return 0

    def find(self):
        if self.path:
            try:
                return self.tree.resolve(self.path)
            except LookupError:
                return 0
        if self.tool:
            players = self.named(self.tree.root, "Players")
            me = self.child(players, self.player) if players else 0
            backpack = self.child(me, "Backpack") if me else 0
            holders = [self.child(container, self.player) for container in self.characters()] + [backpack]
            return next((found for found in (self.child(holder, self.tool) for holder in holders if holder) if found), 0)
        for container in self.characters():
            found = self.child(container, self.player, needs_attribute=True)
            if found:
                return found
        return 0


def apply(memory, attributes, instance, attribute, text, dry_run):
    """Writes the value if it differs. Returns 'changed', 'same', or 'missing'."""
    located = attributes.locate(instance, attribute)
    if located is None:
        return "missing", None
    address, kind = located
    data = encode(kind, text)
    slot = address + attributes.layout.entry_value
    current = memory.read(slot, len(data))
    if current == data:
        return "same", kind
    if dry_run:
        return "would change", kind
    if not memory.write(slot, data) or memory.read(slot, len(data)) != data:
        raise Fatal(f"couldn't write to Roblox (Windows error {ctypes.get_last_error()}); "
                    "the process may not allow writes")
    return "changed", kind


def main(argv):
    args = argv[1:]
    options = {"--interval": "0.05", "--player": None, "--path": None, "--tool": None}
    for flag in options:
        if flag in args:
            at = args.index(flag)
            options[flag] = args[at + 1]
            del args[at:at + 2]
    hold = "--hold" in args
    dry_run = "--dry-run" in args
    args = [arg for arg in args if arg not in ("--hold", "--dry-run")]
    if len(args) != 2:
        print(__doc__)
        return 2
    attribute, text = args

    pid = roblox_pid()
    if not pid:
        raise Fatal("Roblox isn't running")
    memory = Process(pid, write=not dry_run)
    base, size = module_range(pid)
    tree, layout, how = find_layout(memory, base, size, memory.build(), False)
    attributes = Attributes(memory, layout)
    try:
        player = options["--player"] or (None if options["--path"] else local_player(tree))
    except LookupError:
        raise Fatal("the game hasn't loaded yet") from None
    if not player and not options["--path"]:
        raise Fatal("couldn't tell which player is you; pass --player NAME (or --path to name the instance)")
    target = Target(tree, attributes, attribute, player, options["--path"], options["--tool"])
    print(f"layout {how}; target {options['--path'] or options['--tool'] or player}")

    changes = 0
    announced = False
    try:
        while True:
            instance = target.find()
            if instance:
                outcome, kind = apply(memory, attributes, instance, attribute, text, dry_run)
                if outcome == "missing":
                    if not hold:
                        raise Fatal(f"{attribute} isn't on that instance right now, and this tool can't add attributes")
                    if not announced:
                        print(f"waiting: {attribute} isn't on it yet (this tool can't add attributes, only change them)")
                if outcome in ("changed", "would change"):
                    changes += 1
                    if changes <= 20:
                        print(f"{time.strftime('%H:%M:%S')} {attribute} ({kind}) -> {text}"
                              f"{' (dry run)' if dry_run else ''} on {instance:#x}")
                elif not announced and outcome == "same":
                    print(f"{attribute} ({kind}) is already {text}")
                announced = True
            elif not hold:
                raise Fatal("couldn't find your character (try --player NAME or --path)")
            if not hold or dry_run:
                return 0
            time.sleep(float(options["--interval"]))
    except KeyboardInterrupt:
        print(f"\nstopped; wrote {changes} time(s)")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Fatal as error:
        print(f"error: {error}")
        sys.exit(1)

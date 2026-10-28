# AttrDump

Browse, search and change the attributes of a running Roblox client from outside the game. It reads Roblox's memory
directly, works out how the current Roblox build lays out its instance tree and attribute storage by itself (no offsets
to update after a Roblox update), and shows everything in a dark desktop window.

Windows only. Python 3.10+ and the WebView2 runtime (already on Windows 11).

## Run it

```
pip install -r requirements.txt
python attrgui.py
```

Start Roblox and join a game first; the window connects on its own. Several clients running at once get a dropdown.

## What you can do

- **Explorer**: the whole instance tree, with shortcuts to your character, backpack, Workspace and Players. Every
  attribute on the open instance is shown with its type and updates live.
- **Edit**: booleans, numbers, vectors, colors (with a picker), UDims and BrickColors can be overwritten in place. Each
  edited row gets a revert button.
- **Hold**: pin a value and it is written back every 50 ms, so the game can't undo it. Holds follow an instance
  that respawns.
- **Search**: "Scan game" reads every attribute once in the background; then search by name, value, path or type and
  jump straight to the instance.
- **Export**: save the scan as `attributes.json` (`build`, `scope`, and a list of `instance_address`, `instance_path`,
  `name`, `type`, `decoded`, `value_json`).

Not possible from outside the game: adding or removing attributes, and changing strings, fonts, sequences or CFrames.
Those need Roblox's own allocator or hash table.

## Command line

The original scripts still work without the window:

| Script | Does |
| --- | --- |
| `attrdump.py [root] [--out file.json] [--rediscover]` | read-only dump of every attribute to JSON |
| `attrset.py <attribute> <value> [--hold] [--tool NAME] [--dry-run]` | set one existing bool/number attribute on your character or a tool |
| `attrprobe.py` | manual probing helper (win32 reads, hex dumps) |

## How it finds things

`RobloxPlayerBeta.exe` is encrypted on disk, so builds can't be compared statically. Instead each run:

1. finds the DataModel through its RTTI name and scans the process heap for the object with that vtable,
2. works out the child/name offsets from the fact that the root must have `Workspace` and `Players` as children,
3. works out the attribute storage layout (component id, entry shape, hash-table or vector storage) by voting over
   hundreds of real hits, using two known attribute names as canaries.

The result is cached per build in `layout_cache.json` next to the program, and is re-checked against a sample of real
instances before it is reused. If a Roblox update changes a shape the tool doesn't expect, it stops with an error
rather than guessing. See `layout.py`.

## Build a single exe

```
pip install pyinstaller
python -m PyInstaller --onefile --windowed --name attrdump --icon attrdump.ico --add-data "attrgui.html;." attrgui.py
```

## Status and limits

- Reading is tested against a live client (build `version-9ac8595712b447a7`) and against a fake-memory test world.
- Writing is tested only against the fake-memory world. It has not been confirmed against a live client.
- It has not been tried on a second Roblox build.
- Opening a process for writing, and editing another process's memory, can be detected by anti-cheat or break a game's
  assumptions. The process is only opened for writing at your first edit or pin. Use it at your own risk and only in
  places where that is allowed.

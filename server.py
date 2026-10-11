#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Project Zomboid B42 lazy chunk renderer + local Canvas viewer.

- Static: B42 .lotheader / world_*.lotpack
- Save: worldVersion 249 map/X/Y.bin via bundled pzdataspec Kaitai-mini interpreter
- Sprite images: pack_entries.csv + *.pack under media_root
- Cache: rendered per 8x8 chunk / z / mode
- Extra sprites: intentionally ignored for now

Run:
    pip install -r requirements.txt
    copy config.example.json -> config.json
    edit config.json
    python server.py

Open:
    http://localhost:8080/
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import struct
import sqlite3
import sys
import threading
import time
import urllib.parse
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from PIL import Image

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor"
sys.path.insert(0, str(VENDOR))

import kaitai_mini as k  # noqa: E402


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    p = HERE / "config.json"
    if not p.exists():
        raise SystemExit(
            "config.json がありません。\n"
            "config.example.json を config.json にコピーしてパスを編集してください。"
        )
    cfg = json.loads(p.read_text(encoding="utf-8"))

    defaults = {
        "host": "127.0.0.1",
        "port": 8080,
        "start_chunk_x": 1329,
        "start_chunk_y": 1300,
        "start_z": 0,
        "prefetch_radius": 2,
        "max_request_radius": 5,
        "save_world_version": 249,
        "start_at_player": True,
        "player_username": "",
    }
    for key, value in defaults.items():
        cfg.setdefault(key, value)

    required = ["media_root", "map_root", "save_root", "pack_index_csv"]
    missing = [x for x in required if not cfg.get(x)]
    if missing:
        raise SystemExit(f"config.json に不足: {', '.join(missing)}")

    for key in ["media_root", "map_root", "save_root", "pack_index_csv"]:
        cfg[key] = str(Path(cfg[key]).expanduser().resolve())

    return cfg


CFG = load_config()
CACHE_ROOT = HERE / "cache"
CACHE_ROOT.mkdir(exist_ok=True)

SPEC_ROOT = VENDOR / "pzdataspec-main" / "data_spec" / "spec"
SAVE_SPEC = SPEC_ROOT / str(CFG["save_world_version"]) / "chunk.ksy"

if not SAVE_SPEC.exists():
    raise SystemExit(f"save schema not found: {SAVE_SPEC}")


# ---------------------------------------------------------------------------
# File locator
# ---------------------------------------------------------------------------

class FileLocator:
    """
    Lazy file locator.

    v0.3:
    - 起動時に map_root を rglob("*") しない。
    - static cell が実際に要求された時だけ、そのcellの
      <cx>_<cy>.lotheader / world_<cx>_<cy>.lotpack を探す。
    - 一度見つけた結果（見つからない結果も）をメモリにキャッシュする。

    media側も最初から全ファイルを走査せず、必要なpack/.tilesを名前で遅延検索する。
    """
    def __init__(self, media_root: Path, map_root: Path):
        self.media_root = media_root
        self.map_root = map_root
        self.map_by_name = {}
        self.pack_by_name = {}
        self.tile_by_name = {}
        self._missing_map = set()
        self._missing_pack = set()
        self._missing_tile = set()
        self._lock = threading.RLock()

    def _find_named_file(self, root: Path, filename: str) -> Optional[Path]:
        # Fast path: directly under root.
        direct = root / filename
        if direct.exists():
            return direct

        # Common map layout: one subdirectory deep.
        try:
            for d in root.iterdir():
                if not d.is_dir():
                    continue
                p = d / filename
                if p.exists():
                    return p
        except OSError:
            pass

        # Fallback only for the requested filename.
        # This is still a recursive walk, but no longer happens for the entire map
        # at server startup and only runs for a cell/file actually requested.
        try:
            for p in root.rglob(filename):
                if p.is_file():
                    return p
        except OSError:
            pass
        return None

    def lotheader(self, cell_x: int, cell_y: int) -> Optional[Path]:
        name = f"{cell_x}_{cell_y}.lotheader"
        with self._lock:
            if name in self.map_by_name:
                return self.map_by_name[name]
            if name in self._missing_map:
                return None
        p = self._find_named_file(self.map_root, name)
        with self._lock:
            if p:
                self.map_by_name[name] = p
            else:
                self._missing_map.add(name)
        return p

    def lotpack(self, cell_x: int, cell_y: int) -> Optional[Path]:
        name = f"world_{cell_x}_{cell_y}.lotpack"
        with self._lock:
            if name in self.map_by_name:
                return self.map_by_name[name]
            if name in self._missing_map:
                return None
        p = self._find_named_file(self.map_root, name)
        with self._lock:
            if p:
                self.map_by_name[name] = p
            else:
                self._missing_map.add(name)
        return p

    def pack(self, pack_name: str, source_path: str = "") -> Optional[Path]:
        if source_path:
            p = Path(source_path)
            if p.exists():
                return p

        with self._lock:
            if pack_name in self.pack_by_name:
                return self.pack_by_name[pack_name]
            if pack_name in self._missing_pack:
                return None

        p = self._find_named_file(self.media_root, pack_name)
        with self._lock:
            if p:
                self.pack_by_name[pack_name] = p
            else:
                self._missing_pack.add(pack_name)
        return p

    def tile_file(self, name: str) -> Optional[Path]:
        key = name.lower()
        with self._lock:
            if key in self.tile_by_name:
                return self.tile_by_name[key]
            if key in self._missing_tile:
                return None

        # Preserve case supplied by caller first; fallback to case-insensitive scan
        # only for this requested filename.
        p = self._find_named_file(self.media_root, name)
        if not p:
            target = key
            try:
                for q in self.media_root.rglob("*.tiles"):
                    if q.name.lower() == target:
                        p = q
                        break
            except OSError:
                pass

        with self._lock:
            if p:
                self.tile_by_name[key] = p
            else:
                self._missing_tile.add(key)
        return p

LOCATOR = FileLocator(Path(CFG["media_root"]), Path(CFG["map_root"]))


# ---------------------------------------------------------------------------
# Tile definitions / save sprite resolver
# ---------------------------------------------------------------------------

def _read_line_bytes(data: bytes, pos: int):
    end = data.find(b"\n", pos)
    if end < 0:
        raise EOFError("newline not found")
    return data[pos:end].decode("utf-8", "replace").rstrip("\r"), end + 1


def parse_tiledef_names(path: Path):
    data = path.read_bytes()
    if data[:4] != b"tdef":
        raise ValueError(f"bad .tiles magic: {path}")
    version, count = struct.unpack_from("<II", data, 4)
    pos = 12
    sheets = []

    for _ in range(count):
        name, pos = _read_line_bytes(data, pos)
        image_name, pos = _read_line_bytes(data, pos)
        width, height, tileset_number, num_tiles = struct.unpack_from("<IIII", data, pos)
        pos += 16

        for _tile in range(num_tiles):
            nprops = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            for _p in range(nprops):
                _, pos = _read_line_bytes(data, pos)
                _, pos = _read_line_bytes(data, pos)

        sheets.append((tileset_number, name, num_tiles))

    return sheets


class SpriteIdResolver:
    """
    Main object rendering currently prefers the legacy/file-0 interpretation:
        id = (tileset_number - 1) * 1024 + tile_index
    This matches all 116 unique main sprite IDs in the investigated B42.20 chunk.

    Numbered TileDef maps are retained as fallback for IDs not found in file-0.
    Extra sprites are not rendered by this viewer yet.
    """
    NUMBERED = {
        "newtiledefinitions.tiles": 1,
        "tiledefinitions_erosion.tiles": 2,
        "tiledefinitions_overlays.tiles": 4,
        "tiledefinitions_b42chunkcaching.tiles": 5,
        "tiledefinitions_noiseworks.patch.tiles": 6,
        "jumbo_trees.tiles": 8,
        "jumbo_trees_big.tiles": 8,
    }

    def __init__(self):
        self.legacy = {}
        self.numbered = defaultdict(set)
        self._load()

    def _load(self):
        newtiles = LOCATOR.tile_file("newtiledefinitions.tiles")
        if not newtiles:
            raise RuntimeError("media_root から newtiledefinitions.tiles が見つかりません")

        sheets = parse_tiledef_names(newtiles)
        for tileset_number, name, num_tiles in sheets:
            for tile_index in range(num_tiles):
                sid = (tileset_number - 1) * 1024 + tile_index
                self.legacy[sid] = f"{name}_{tile_index}"

        for filename, file_no in self.NUMBERED.items():
            p = LOCATOR.tile_file(filename)
            if not p:
                continue
            for sheet_no, name, num_tiles in parse_tiledef_names(p):
                for tile_index in range(num_tiles):
                    if file_no == 1:
                        sid = 110000 + sheet_no * 1000 + tile_index
                    else:
                        sid = file_no * 262144 + sheet_no * 512 + tile_index
                    self.numbered[sid].add(f"{name}_{tile_index}")

        print(f"[index] legacy sprite ids: {len(self.legacy):,}")

    def main_name(self, sprite_id: int) -> Optional[str]:
        # Main save objects: prefer the legacy/file-0 space.
        if sprite_id in self.legacy:
            return self.legacy[sprite_id]

        candidates = self.numbered.get(sprite_id)
        if candidates and len(candidates) == 1:
            return next(iter(candidates))
        return None


SPRITE_IDS = SpriteIdResolver()


# ---------------------------------------------------------------------------
# Pack index and atlas loader
# ---------------------------------------------------------------------------

def _rank_pack(name: str):
    low = name.lower()
    if low == "tiles2x.floor.pack":
        return 0
    if low == "tiles2x.pack":
        return 1
    if "2x.floor.pack" in low:
        return 2
    if "2x.pack" in low:
        return 3
    if "2x" in low:
        return 4
    return 20


class PackIndex:
    def __init__(self, csv_path: Path):
        self.by_sprite = defaultdict(list)
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    row["page_index"] = int(row["page_index"])
                    row["entry_index"] = int(row["entry_index"])
                    for key in ["x", "y", "w", "h", "offset_x", "offset_y", "frame_w", "frame_h"]:
                        row[key] = int(row[key])
                except Exception:
                    continue
                self.by_sprite[row["sprite_name"]].append(row)

        for rows in self.by_sprite.values():
            rows.sort(key=lambda r: (_rank_pack(r["pack_name"]), r["page_index"], r["entry_index"]))
        print(f"[index] pack sprite names: {len(self.by_sprite):,}")

    def best(self, sprite_name: str):
        rows = self.by_sprite.get(sprite_name)
        return rows[0] if rows else None


PACK_INDEX = PackIndex(Path(CFG["pack_index_csv"]))


def _u32(f):
    b = f.read(4)
    if len(b) != 4:
        raise EOFError()
    return struct.unpack("<I", b)[0]


def _lenstr(f):
    n = _u32(f)
    if n > 2_000_000:
        raise ValueError(f"bad string length {n}")
    return f.read(n).decode("utf-8", "replace")


class PackFile:
    """
    Incrementally scans a PZPK file until requested page indexes are known.
    PNG bytes are skipped during metadata scan, then loaded by seek on demand.
    """
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.version = None
        self.page_count = None
        self.next_page = 0
        self.scan_pos = None
        self.page_png = {}  # page_index -> (offset, length)
        self.page_image = {}  # page_index -> PIL RGBA
        self._init()

    def _init(self):
        with self.path.open("rb") as f:
            if f.read(4) != b"PZPK":
                raise ValueError(f"bad pack: {self.path}")
            self.version = _u32(f)
            self.page_count = _u32(f)
            self.scan_pos = f.tell()

    def _scan_until(self, target: int):
        if target in self.page_png:
            return
        if target >= self.page_count:
            raise IndexError(target)

        with self.path.open("rb") as f:
            f.seek(self.scan_pos)
            pi = self.next_page

            while pi <= target:
                page_name = _lenstr(f)
                entry_count = _u32(f)
                _unknown = _u32(f)

                for _ in range(entry_count):
                    _ = _lenstr(f)
                    f.seek(32, os.SEEK_CUR)

                png_len = _u32(f)
                png_off = f.tell()
                self.page_png[pi] = (png_off, png_len)
                f.seek(png_len, os.SEEK_CUR)

                pi += 1
                self.next_page = pi
                self.scan_pos = f.tell()

    def image(self, page_index: int):
        with self.lock:
            if page_index in self.page_image:
                return self.page_image[page_index]

            self._scan_until(page_index)
            off, length = self.page_png[page_index]

            with self.path.open("rb") as f:
                f.seek(off)
                raw = f.read(length)

            img = Image.open(io.BytesIO(raw)).convert("RGBA")
            # Simple memory guard.
            if len(self.page_image) > 80:
                self.page_image.clear()
            self.page_image[page_index] = img
            return img


class SpriteStore:
    def __init__(self):
        self.pack_files = {}
        self.sprite_cache = {}
        self.lock = threading.RLock()

    def _pack(self, row):
        name = row["pack_name"]
        if name in self.pack_files:
            return self.pack_files[name]

        path = LOCATOR.pack(name, row.get("source_pack", ""))
        if not path:
            raise FileNotFoundError(f"pack not found: {name}")

        obj = PackFile(path)
        self.pack_files[name] = obj
        return obj

    def sprite(self, name: str):
        with self.lock:
            if name in self.sprite_cache:
                return self.sprite_cache[name]

            row = PACK_INDEX.best(name)
            if not row:
                return None

            atlas = self._pack(row).image(row["page_index"])
            x, y, w, h = row["x"], row["y"], row["w"], row["h"]
            crop = atlas.crop((x, y, x + w, y + h))

            item = (
                crop,
                {
                    "ox": row["offset_x"],
                    "oy": row["offset_y"],
                    "fw": row["frame_w"],
                    "fh": row["frame_h"],
                },
            )

            if len(self.sprite_cache) > 3000:
                self.sprite_cache.clear()
            self.sprite_cache[name] = item
            return item


SPRITES = SpriteStore()


# ---------------------------------------------------------------------------
# Static B42 cell parser
# ---------------------------------------------------------------------------

_HEADER_CACHE = {}
_LOTPACK_TABLE_CACHE = {}
_STATIC_LOCK = threading.RLock()


def parse_lotheader(path: Path):
    key = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
    if key in _HEADER_CACHE:
        return _HEADER_CACHE[key]

    b = path.read_bytes()
    p = 0

    if b[:4] == b"LOTH":
        version = struct.unpack_from("<I", b, 4)[0]
        p = 8
    else:
        version = struct.unpack_from("<I", b, 0)[0]
        p = 4

    if version != 1:
        raise ValueError(f"このviewerはB42 lotheader version 1用です: {path} version={version}")

    tile_count = struct.unpack_from("<I", b, p)[0]
    p += 4
    tiles = []

    for _ in range(tile_count):
        e = b.find(b"\n", p)
        if e < 0:
            raise EOFError("lotheader tile string")
        tiles.append(b[p:e].decode("utf-8", "replace").strip())
        p = e + 1

    width, height = struct.unpack_from("<II", b, p)
    p += 8
    min_z, max_z = struct.unpack_from("<ii", b, p)
    p += 8

    out = {
        "version": version,
        "tiles": tiles,
        "width": width,
        "height": height,
        "min_z": min_z,
        "max_z": max_z,
    }
    _HEADER_CACHE.clear()
    _HEADER_CACHE[key] = out
    return out


def lotpack_table(path: Path):
    key = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
    if key in _LOTPACK_TABLE_CACHE:
        return _LOTPACK_TABLE_CACHE[key]

    with path.open("rb") as f:
        magic = f.read(4)
        if magic == b"LOTP":
            _version = _u32(f)
            count = _u32(f)
        else:
            f.seek(0)
            count = _u32(f)

        if count != 32 * 32:
            raise ValueError(f"unexpected lotpack chunk count: {count}")

        poses = []
        sizes = []
        for _ in range(count):
            poses.append(struct.unpack("<i", f.read(4))[0])
            sizes.append(struct.unpack("<i", f.read(4))[0])

    # Find the next positive file offset after each chunk.
    ends = [path.stat().st_size] * count
    next_pos = path.stat().st_size
    for i in range(count - 1, -1, -1):
        if poses[i] > 0:
            ends[i] = next_pos
            next_pos = poses[i]

    out = (poses, sizes, ends)
    _LOTPACK_TABLE_CACHE.clear()
    _LOTPACK_TABLE_CACHE[key] = out
    return out


def static_chunk_data(chunk_x: int, chunk_y: int):
    cell_x = math.floor(chunk_x / 32)
    cell_y = math.floor(chunk_y / 32)
    cx = chunk_x - cell_x * 32
    cy = chunk_y - cell_y * 32

    lh = LOCATOR.lotheader(cell_x, cell_y)
    lp = LOCATOR.lotpack(cell_x, cell_y)
    if not lh or not lp:
        return None, None

    header = parse_lotheader(lh)
    poses, sizes, ends = lotpack_table(lp)

    idx = cx * 32 + cy
    cp = poses[idx]
    if cp <= 0:
        return [], {
            "lotheader": str(lh),
            "lotpack": str(lp),
            "cell_x": cell_x,
            "cell_y": cell_y,
            "chunk_in_cell_x": cx,
            "chunk_in_cell_y": cy,
        }

    end = ends[idx]
    body = lp.read_bytes()[cp:end]
    p = 0
    skip = 0
    squares = []

    for z in range(header["min_z"], header["max_z"] + 1):
        for lx in range(8):
            for ly in range(8):
                if skip > 0:
                    skip -= 1
                    continue

                if p + 4 > len(body):
                    break

                count = struct.unpack_from("<i", body, p)[0]
                p += 4

                if count == -1:
                    if p + 4 > len(body):
                        break
                    s = struct.unpack_from("<i", body, p)[0]
                    p += 4
                    skip = max(0, s - 1)
                    continue

                if count <= 1:
                    continue

                if p + 4 > len(body):
                    break
                room_id = struct.unpack_from("<i", body, p)[0]
                p += 4

                names = []
                refs = []

                for _ in range(count - 1):
                    if p + 4 > len(body):
                        break
                    r = struct.unpack_from("<i", body, p)[0]
                    p += 4
                    refs.append(r)
                    if 0 <= r < len(header["tiles"]):
                        names.append(header["tiles"][r])

                if names:
                    squares.append(
                        {
                            "x": lx,
                            "y": ly,
                            "z": z,
                            "room_id": room_id,
                            "tiles": names,
                            "tile_refs": refs,
                        }
                    )

    return squares, {
        "lotheader": str(lh),
        "lotpack": str(lp),
        "cell_x": cell_x,
        "cell_y": cell_y,
        "chunk_in_cell_x": cx,
        "chunk_in_cell_y": cy,
    }


# ---------------------------------------------------------------------------
# Save parser
# ---------------------------------------------------------------------------

_SAVE_SPEC_OBJ = None
_SAVE_PARSE_CACHE = {}
_SAVE_LOCK = threading.RLock()


CLASS_NAMES = {
    0: "IsoObject",
    9: "IsoRadio",
    17: "IsoDoor",
    18: "IsoThumpable",
    26: "IsoWindow",
    29: "IsoLightSwitch",
}


def save_chunk_path(chunk_x: int, chunk_y: int):
    root = Path(CFG["save_root"])
    candidates = [
        root / "map" / str(chunk_x) / f"{chunk_y}.bin",
        root / str(chunk_x) / f"{chunk_y}.bin",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def parse_save_chunk(chunk_x: int, chunk_y: int):
    global _SAVE_SPEC_OBJ

    path = save_chunk_path(chunk_x, chunk_y)
    if not path:
        return None, None

    st = path.stat()
    key = (chunk_x, chunk_y, st.st_mtime_ns, st.st_size)

    with _SAVE_LOCK:
        if key in _SAVE_PARSE_CACHE:
            return _SAVE_PARSE_CACHE[key]

        if _SAVE_SPEC_OBJ is None:
            _SAVE_SPEC_OBJ = k.load_spec(str(SAVE_SPEC))

        data = path.read_bytes()
        c = k.parse(_SAVE_SPEC_OBJ, data, {})

        squares = []
        for slot_index, slot in enumerate(c.squares):
            bits = [b for b in range(64) if (slot.layer_flags >> b) & 1]
            lx = slot_index // 8
            ly = slot_index % 8

            for zi, gs in enumerate(slot.squares):
                z = bits[zi] - 32
                objects = []

                for ow in gs.objects:
                    o = ow.object
                    base = o.base_object
                    if base is None:
                        continue

                    sid = int(base.sprite_id)
                    try:
                        sid = int(base.sprite_id)
                    except (AttributeError, TypeError, ValueError):
                        continue

                    name = SPRITE_IDS.main_name(sid)
                    objects.append(
                        {
                            "class_id": int(o.class_id),
                            "class_name": CLASS_NAMES.get(int(o.class_id), f"class_{int(o.class_id)}"),
                            "base_type": getattr(o, "base_type", None),
                            "sprite_id": sid,
                            "sprite_name": name,
                        }
                    )

                squares.append(
                    {
                        "x": lx,
                        "y": ly,
                        "z": z,
                        "objects": objects,
                    }
                )

        meta = {
            "path": str(path),
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "world_version": int(c.world_version),
            "crc": int(c.crc),
        }

        # Keep only current version for each chunk.
        for old in list(_SAVE_PARSE_CACHE):
            if old[:2] == (chunk_x, chunk_y):
                del _SAVE_PARSE_CACHE[old]
        _SAVE_PARSE_CACHE[key] = (squares, meta)
        return squares, meta


# ---------------------------------------------------------------------------
# Renderer and disk cache
# ---------------------------------------------------------------------------

HALF_W = 64
HALF_H = 32
RENDER_LOCKS = defaultdict(threading.Lock)


def iso_base(x: int, y: int):
    return (x - y) * HALF_W, (x + y) * HALF_H


def source_signature(paths):
    h = hashlib.sha1()
    for p in paths:
        if not p:
            continue
        p = Path(p)
        if not p.exists():
            continue
        st = p.stat()
        h.update(str(p).encode("utf-8", "replace"))
        h.update(str(st.st_mtime_ns).encode())
        h.update(str(st.st_size).encode())
    # Rendering code version.
    h.update(b"renderer-v1-main-sprite-no-extra")
    return h.hexdigest()[:14]


def chunk_model(chunk_x: int, chunk_y: int, z: int, mode: str):
    if mode == "static":
        squares, meta = static_chunk_data(chunk_x, chunk_y)
        if squares is None:
            return [], {"exists": False}, []

        drawables = []
        square_info = {}
        for sq in squares:
            if sq["z"] != z:
                continue
            square_info[(sq["x"], sq["y"])] = {
                "static_tiles": sq["tiles"],
                "save_objects": [],
            }
            for order, name in enumerate(sq["tiles"]):
                drawables.append((sq["x"], sq["y"], order, name))

        sig_paths = [meta["lotheader"], meta["lotpack"]]
        meta = dict(meta)
        meta["exists"] = True
        return drawables, meta, sig_paths

    if mode == "save":
        squares, meta = parse_save_chunk(chunk_x, chunk_y)
        if squares is None:
            return [], {"exists": False}, []

        drawables = []
        square_info = {}
        for sq in squares:
            if sq["z"] != z:
                continue

            square_info[(sq["x"], sq["y"])] = {
                "static_tiles": [],
                "save_objects": sq["objects"],
            }

            for order, obj in enumerate(sq["objects"]):
                if obj["sprite_name"]:
                    drawables.append((sq["x"], sq["y"], order, obj["sprite_name"]))

        meta = dict(meta)
        meta["exists"] = True
        return drawables, meta, [meta["path"]]

    raise ValueError(mode)


def render_chunk(chunk_x: int, chunk_y: int, z: int, mode: str):
    drawables, src_meta, source_paths = chunk_model(chunk_x, chunk_y, z, mode)

    if not src_meta.get("exists"):
        return {
            "exists": False,
            "x": chunk_x,
            "y": chunk_y,
            "z": z,
            "mode": mode,
        }

    sig = source_signature(source_paths)
    cache_dir = CACHE_ROOT / mode / f"z{z}"
    cache_dir.mkdir(parents=True, exist_ok=True)

    stem = f"{chunk_x}_{chunk_y}_{sig}"
    png_path = cache_dir / f"{stem}.png"
    meta_path = cache_dir / f"{stem}.json"

    lock = RENDER_LOCKS[(chunk_x, chunk_y, z, mode)]
    with lock:
        if png_path.exists() and meta_path.exists():
            out = json.loads(meta_path.read_text(encoding="utf-8"))
            return out

        drawables.sort(key=lambda t: (t[0] + t[1], t[0], t[1], t[2]))

        # Always include the logical 8x8 diamond in bounds.
        refs = []
        for x in range(8):
            for y in range(8):
                bx, by = iso_base(x, y)
                refs.extend([(bx - 64, by - 32), (bx + 64, by + 32)])

        min_x = min(x for x, _ in refs)
        max_x = max(x for x, _ in refs)
        min_y = min(y for _, y in refs)
        max_y = max(y for _, y in refs)

        placements = []
        missing = []

        for x, y, order, name in drawables:
            spr = SPRITES.sprite(name)
            if not spr:
                missing.append(name)
                continue

            img, sm = spr
            bx, by = iso_base(x, y)
            left = bx - sm["fw"] // 2 + sm["ox"]
            top = by - sm["fh"] + sm["oy"]

            placements.append((left, top, img))
            min_x = min(min_x, left)
            min_y = min(min_y, top)
            max_x = max(max_x, left + img.width)
            max_y = max(max_y, top + img.height)

        margin = 24
        min_x -= margin
        min_y -= margin
        max_x += margin
        max_y += margin

        width = max(1, int(math.ceil(max_x - min_x)))
        height = max(1, int(math.ceil(max_y - min_y)))

        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))

        for left, top, img in placements:
            dx = int(round(left - min_x))
            dy = int(round(top - min_y))
            canvas.alpha_composite(img, (dx, dy))

        # Write cache atomically so clients never see a partially written PNG.
        tmp_png = png_path.with_name(
            png_path.name + f".tmp-{os.getpid()}-{threading.get_ident()}"
        )
        try:
            canvas.save(tmp_png, format="PNG", optimize=True)
            os.replace(tmp_png, png_path)
        finally:
            try:
                if tmp_png.exists():
                    tmp_png.unlink()
            except OSError:
                pass

        out = {
            "exists": True,
            "x": chunk_x,
            "y": chunk_y,
            "z": z,
            "mode": mode,
            "min_x": min_x,
            "min_y": min_y,
            "width": width,
            "height": height,
            "drawables": len(drawables),
            "drawn": len(placements),
            "missing_sprite_names": sorted(set(missing)),
            "image_url": f"/cache/{mode}/z{z}/{png_path.name}",
            "source": src_meta,
            "cache_signature": sig,
        }
        tmp_meta = meta_path.with_name(
            meta_path.name + f".tmp-{os.getpid()}-{threading.get_ident()}"
        )
        try:
            tmp_meta.write_text(
                json.dumps(out, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_meta, meta_path)
        finally:
            try:
                if tmp_meta.exists():
                    tmp_meta.unlink()
            except OSError:
                pass
        return out


def inspect_square(chunk_x: int, chunk_y: int, local_x: int, local_y: int, z: int):
    result = {
        "chunk_x": chunk_x,
        "chunk_y": chunk_y,
        "local_x": local_x,
        "local_y": local_y,
        "world_x": chunk_x * 8 + local_x,
        "world_y": chunk_y * 8 + local_y,
        "z": z,
        "static_tiles": [],
        "save_objects": [],
    }

    static_squares, _ = static_chunk_data(chunk_x, chunk_y)
    if static_squares:
        for sq in static_squares:
            if sq["x"] == local_x and sq["y"] == local_y and sq["z"] == z:
                result["static_tiles"] = sq["tiles"]
                break

    save_squares, _ = parse_save_chunk(chunk_x, chunk_y)
    if save_squares:
        for sq in save_squares:
            if sq["x"] == local_x and sq["y"] == local_y and sq["z"] == z:
                result["save_objects"] = sq["objects"]
                break

    return result


# ---------------------------------------------------------------------------
# Player position (players.db)
# ---------------------------------------------------------------------------

_PLAYER_CACHE = {"sig": None, "value": None}
_PLAYER_LOCK = threading.RLock()

def _players_db_path():
    p = Path(CFG["save_root"]) / "players.db"
    return p if p.exists() else None

def _quote_ident(name: str):
    return '"' + name.replace('"', '""') + '"'

def _find_player_position():
    if not CFG.get("start_at_player", True):
        return None
    db = _players_db_path()
    if not db:
        return None
    st = db.stat(); sig = (st.st_mtime_ns, st.st_size, str(db))
    with _PLAYER_LOCK:
        if _PLAYER_CACHE["sig"] == sig:
            return _PLAYER_CACHE["value"]
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables=[r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            order=[n for n in ("localPlayers","networkPlayers") if n in tables] + [n for n in tables if n not in ("localPlayers","networkPlayers")]
            candidates=[]
            for table in order:
                info=conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
                cols={str(r["name"]).lower():str(r["name"]) for r in info}
                if not all(k in cols for k in ("x","y","z")): continue
                for row in conn.execute(f"SELECT * FROM {_quote_ident(table)}").fetchall():
                    try:
                        x=float(row[cols["x"]]); y=float(row[cols["y"]]); z=float(row[cols["z"]])
                    except Exception: continue
                    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)): continue
                    def val(*names):
                        for n in names:
                            c=cols.get(n.lower())
                            if c is not None: return row[c]
                        return None
                    candidates.append({"table":table,"x":x,"y":y,"z":z,"username":None if val("username") is None else str(val("username")),"name":None if val("name") is None else str(val("name")),"steamid":None if val("steamid") is None else str(val("steamid")),"player_index":val("playerIndex","player_index"),"is_dead":val("isDead","is_dead","dead")})
            result=None
            if candidates:
                wanted=str(CFG.get("player_username","") or "").strip()
                def score(c):
                    um=0 if (wanted and c.get("username")==wanted) else (1 if wanted else 0)
                    try: dead=1 if int(c.get("is_dead") or 0)!=0 else 0
                    except Exception: dead=0
                    try: idx=0 if int(c.get("player_index"))==0 else 1
                    except Exception: idx=1
                    ts=0 if c["table"]=="localPlayers" else (1 if c["table"]=="networkPlayers" else 2)
                    return (um,dead,idx,ts)
                result=dict(min(candidates,key=score))
                result["chunk_x"]=math.floor(result["x"]/8.0); result["chunk_y"]=math.floor(result["y"]/8.0)
                result["local_x"]=result["x"]-result["chunk_x"]*8.0; result["local_y"]=result["y"]-result["chunk_y"]*8.0
                result["db_path"]=str(db)
            _PLAYER_CACHE["sig"]=sig; _PLAYER_CACHE["value"]=result
            return result
        finally:
            conn.close()

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
}


def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "PZLazyViewer/0.1"

    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def send_bytes(self, data: bytes, content_type: str, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path: Path, content_type: str):
        """Stream a stable opened file with an exact Content-Length."""
        try:
            f = path.open("rb")
        except FileNotFoundError:
            return self.send_json({"error": "not found"}, 404)

        with f:
            st = os.fstat(f.fileno())
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(st.st_size))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            remaining = st.st_size
            while remaining > 0:
                chunk = f.read(min(256 * 1024, remaining))
                if not chunk:
                    raise IOError(
                        f"unexpected EOF while serving {path}: "
                        f"{remaining} bytes remaining"
                    )
                self.wfile.write(chunk)
                remaining -= len(chunk)
            self.wfile.flush()

    def send_json(self, obj, status=200):
        self.send_bytes(json_bytes(obj), "application/json; charset=utf-8", status)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)

        try:
            if path == "/":
                data = (HERE / "viewer.html").read_bytes()
                return self.send_bytes(data, "text/html; charset=utf-8")

            if path == "/api/config":
                return self.send_json(
                    {
                        "start_chunk_x": CFG["start_chunk_x"],
                        "start_chunk_y": CFG["start_chunk_y"],
                        "start_z": CFG["start_z"],
                        "prefetch_radius": CFG["prefetch_radius"],
                        "max_request_radius": CFG["max_request_radius"],
                        "extra_sprites": False,
                        "lazy_map_lookup": True,
                        "player": _find_player_position(),
                    }
                )

            if path == "/api/player":
                return self.send_json({"player": _find_player_position()})

            if path == "/api/chunk":
                x = int(q["x"][0])
                y = int(q["y"][0])
                z = int(q.get("z", [0])[0])
                mode = q.get("mode", ["static"])[0]
                if mode not in ("static", "save"):
                    raise ValueError("mode")
                return self.send_json(render_chunk(x, y, z, mode))

            if path == "/api/square":
                return self.send_json(
                    inspect_square(
                        int(q["x"][0]),
                        int(q["y"][0]),
                        int(q["lx"][0]),
                        int(q["ly"][0]),
                        int(q.get("z", [0])[0]),
                    )
                )

            if path.startswith("/cache/"):
                rel = path[len("/cache/"):]
                target = (CACHE_ROOT / rel).resolve()
                if CACHE_ROOT.resolve() not in target.parents:
                    return self.send_json({"error": "bad path"}, 403)
                if not target.exists():
                    return self.send_json({"error": "not found"}, 404)
                return self.send_file(
                    target,
                    MIME.get(target.suffix.lower(), "application/octet-stream"),
                )

            if path == "/favicon.ico":
                return self.send_bytes(b"", "image/x-icon", 204)

            return self.send_json({"error": "not found"}, 404)

        except Exception as e:
            import traceback
            traceback.print_exc()
            return self.send_json(
                {"error": type(e).__name__, "message": str(e)},
                500,
            )


def main():
    host = CFG["host"]
    port = int(CFG["port"])
    server = ThreadingHTTPServer((host, port), Handler)
    print()
    print(f"PZ lazy viewer: http://{host}:{port}/")
    print(f"start chunk: {CFG['start_chunk_x']}/{CFG['start_chunk_y']} z={CFG['start_z']}")
    print("extra sprites: OFF")
    print("Ctrl+C で終了")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

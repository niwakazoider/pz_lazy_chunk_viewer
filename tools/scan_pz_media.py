#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan_pz_media.py

Project Zomboid の media フォルダ全体を走査し、
sprite / tileset / texture pack の索引を作るスクリプト。

主な出力:
  media_index.sqlite
  sprite_index.csv
  tilesets.csv
  pack_entries.csv
  loose_images.csv
  text_refs.csv            (--scan-text 指定時)
  summary.json
  errors.log

標準ライブラリのみ使用。

使い方:
  python scan_pz_media.py "C:\\Program Files (x86)\\Steam\\steamapps\\common\\ProjectZomboid\\media"

出力先指定:
  python scan_pz_media.py "C:\\Program Files (x86)\\Steam\\steamapps\\common\\ProjectZomboid\\media" -o ".\\output"

テキスト中のsprite名らしき参照も検索:
  python scan_pz_media.py "C:\\Program Files (x86)\\Steam\\steamapps\\common\\ProjectZomboid\\media" --scan-text

.pack は画像を展開せず、atlas entry名と座標メタデータだけを読みます。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import struct
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import BinaryIO, Iterable, Optional


# ---------------------------------------------------------------------------
# B42で既知のTileDef file番号
# ---------------------------------------------------------------------------

KNOWN_TILEDEF_FILE_NO = {
    "tiledefinitions.tiles": 0,  # legacy / B41系。B42で存在する場合も記録
    "newtiledefinitions.tiles": 1,
    "tiledefinitions_erosion.tiles": 2,
    "tiledefinitions_apcom.tiles": 3,
    "tiledefinitions_overlays.tiles": 4,
    "tiledefinitions_b42chunkcaching.tiles": 5,
    "tiledefinitions_noiseworks.patch.tiles": 6,
    "jumbo_trees.tiles": 8,
    "jumbo_trees_big.tiles": 8,
}


def calc_known_sprite_id(file_no: Optional[int], tileset_number: int, tile_index: int):
    """
    既知のPZ/pzdataspec系番号規則。
    file 0 は legacy 512刻みとして候補を出す。
    file 1 は特殊な 110000 + sheet*1000 + index。
    その他の番号付きTileDefは 512*512 ブロック。

    あくまで candidate として保存する。
    """
    if file_no is None:
        return None, None

    if file_no == 0:
        return tileset_number * 512 + tile_index, "legacy:file0 sheet*512+tile"
    if file_no == 1:
        return 110000 + tileset_number * 1000 + tile_index, "file1:110000+sheet*1000+tile"
    return (
        file_no * 512 * 512 + tileset_number * 512 + tile_index,
        "numbered:file*262144+sheet*512+tile",
    )


# ---------------------------------------------------------------------------
# Binary helpers
# ---------------------------------------------------------------------------

def read_exact(f: BinaryIO, n: int) -> bytes:
    b = f.read(n)
    if len(b) != n:
        raise EOFError(f"wanted {n} bytes, got {len(b)}")
    return b


def read_u32le(f: BinaryIO) -> int:
    return struct.unpack("<I", read_exact(f, 4))[0]


def read_i32le(f: BinaryIO) -> int:
    return struct.unpack("<i", read_exact(f, 4))[0]


def read_line_bytes(data: bytes, pos: int):
    end = data.find(b"\n", pos)
    if end < 0:
        raise EOFError("newline not found")
    s = data[pos:end].decode("utf-8", "replace").rstrip("\r")
    return s, end + 1


def read_len_string(f: BinaryIO, max_len=1_000_000) -> str:
    n = read_u32le(f)
    if n > max_len:
        raise ValueError(f"string length too large: {n}")
    return read_exact(f, n).decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# .tiles parser
# ---------------------------------------------------------------------------

def parse_tiles_file(path: Path):
    data = path.read_bytes()
    if len(data) < 12 or data[:4] != b"tdef":
        raise ValueError("not a tdef file")

    version, nsets = struct.unpack_from("<II", data, 4)
    pos = 12

    file_no = KNOWN_TILEDEF_FILE_NO.get(path.name.lower())
    out = []

    for _ in range(nsets):
        tileset_name, pos = read_line_bytes(data, pos)
        image_name, pos = read_line_bytes(data, pos)

        if pos + 16 > len(data):
            raise EOFError("truncated tileset header")

        width, height, tileset_number, num_tiles = struct.unpack_from("<IIII", data, pos)
        pos += 16

        # propertiesを全部読み飛ばしつつtileごとの行を作る
        for tile_index in range(num_tiles):
            if pos + 4 > len(data):
                raise EOFError("truncated property count")

            nprops = struct.unpack_from("<I", data, pos)[0]
            pos += 4

            props = {}
            for _p in range(nprops):
                key, pos = read_line_bytes(data, pos)
                value, pos = read_line_bytes(data, pos)
                props[key] = value

            sprite_name = f"{tileset_name}_{tile_index}"
            sprite_id_candidate, id_formula = calc_known_sprite_id(
                file_no, tileset_number, tile_index
            )

            out.append({
                "source_file": str(path),
                "source_name": path.name,
                "tiledef_version": version,
                "file_no": file_no,
                "tileset_number": tileset_number,
                "tileset_name": tileset_name,
                "image_name": image_name,
                "sheet_width": width,
                "sheet_height": height,
                "tile_index": tile_index,
                "sprite_name": sprite_name,
                "sprite_id_candidate": sprite_id_candidate,
                "id_formula": id_formula,
                "property_count": nprops,
                "properties_json": json.dumps(props, ensure_ascii=False, separators=(",", ":")),
            })

    return out, {
        "version": version,
        "tilesets": nsets,
        "tiles": len(out),
        "bytes_consumed": pos,
        "file_size": len(data),
        "complete": pos == len(data),
    }


# ---------------------------------------------------------------------------
# .pack parser
# ---------------------------------------------------------------------------

PNG_SIG = b"\x89PNG\r\n\x1a\n"

def parse_pack_file(path: Path):
    """
    PZPK v1 の一般的な構造:
      'PZPK'
      u32 version
      u32 page_count
      repeat page:
        lenstr page_name
        u32 entry_count
        u32 unknown
        repeat entry:
          lenstr name
          8 * i32: x,y,w,h,ox,oy,fw,fh
        u32 png_length
        png bytes

    壊れた末尾/異なる派生形式があっても、それまで読めたentryは返す。
    """
    entries = []
    pages = []
    error = None

    with path.open("rb") as f:
        magic = read_exact(f, 4)
        if magic != b"PZPK":
            raise ValueError(f"bad pack magic: {magic!r}")

        version = read_u32le(f)
        page_count = read_u32le(f)

        if page_count > 100_000:
            raise ValueError(f"implausible page_count={page_count}")

        for page_index in range(page_count):
            page_start = f.tell()
            try:
                page_name = read_len_string(f)
                entry_count = read_u32le(f)
                unknown = read_u32le(f)

                if entry_count > 1_000_000:
                    raise ValueError(f"implausible entry_count={entry_count}")

                for entry_index in range(entry_count):
                    sprite_name = read_len_string(f)
                    vals = struct.unpack("<8i", read_exact(f, 32))
                    x, y, w, h, ox, oy, fw, fh = vals

                    entries.append({
                        "source_pack": str(path),
                        "pack_name": path.name,
                        "pack_version": version,
                        "page_index": page_index,
                        "page_name": page_name,
                        "entry_index": entry_index,
                        "sprite_name": sprite_name,
                        "x": x, "y": y, "w": w, "h": h,
                        "offset_x": ox, "offset_y": oy,
                        "frame_w": fw, "frame_h": fh,
                    })

                png_length = read_u32le(f)
                png_offset = f.tell()

                if png_length > path.stat().st_size - png_offset:
                    raise ValueError(
                        f"png length beyond EOF: {png_length} at {png_offset}"
                    )

                # PNG本体は読まずseek
                f.seek(png_length, os.SEEK_CUR)

                pages.append({
                    "source_pack": str(path),
                    "pack_name": path.name,
                    "pack_version": version,
                    "page_index": page_index,
                    "page_name": page_name,
                    "entry_count": entry_count,
                    "unknown": unknown,
                    "png_offset": png_offset,
                    "png_length": png_length,
                    "page_start": page_start,
                })

            except Exception as e:
                error = (
                    f"page {page_index}/{page_count} at offset {page_start}: "
                    f"{type(e).__name__}: {e}"
                )
                break

    return entries, pages, {
        "version": version,
        "declared_pages": page_count,
        "parsed_pages": len(pages),
        "entries": len(entries),
        "partial_error": error,
    }


# ---------------------------------------------------------------------------
# loose images / text refs
# ---------------------------------------------------------------------------

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tga"}
TEXT_EXTS = {
    ".txt", ".lua", ".xml", ".json", ".ini", ".cfg", ".properties",
    ".js", ".ts", ".csv", ".md",
}

# 例:
# floors_interior_tilesandwood_01_18
# lighting_indoor_02_8
SPRITE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_\-]*_[0-9]+)(?![A-Za-z0-9_])"
)


def scan_text_file(path: Path, root: Path):
    refs = []
    try:
        # 巨大ファイルを丸ごと抱えない
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, 1):
                for m in SPRITE_TOKEN_RE.finditer(line):
                    refs.append({
                        "source_file": str(path),
                        "relative_path": str(path.relative_to(root)),
                        "line": line_no,
                        "sprite_name": m.group(1),
                        "context": line.strip()[:500],
                    })
    except Exception:
        raise
    return refs


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

SCHEMA = r"""
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS tile_entries (
    source_file TEXT,
    source_name TEXT,
    tiledef_version INTEGER,
    file_no INTEGER,
    tileset_number INTEGER,
    tileset_name TEXT,
    image_name TEXT,
    sheet_width INTEGER,
    sheet_height INTEGER,
    tile_index INTEGER,
    sprite_name TEXT,
    sprite_id_candidate INTEGER,
    id_formula TEXT,
    property_count INTEGER,
    properties_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_tile_sprite_name
ON tile_entries(sprite_name);

CREATE INDEX IF NOT EXISTS idx_tile_sprite_id
ON tile_entries(sprite_id_candidate);

CREATE INDEX IF NOT EXISTS idx_tile_sheet
ON tile_entries(file_no, tileset_number, tile_index);

CREATE TABLE IF NOT EXISTS pack_entries (
    source_pack TEXT,
    pack_name TEXT,
    pack_version INTEGER,
    page_index INTEGER,
    page_name TEXT,
    entry_index INTEGER,
    sprite_name TEXT,
    x INTEGER,
    y INTEGER,
    w INTEGER,
    h INTEGER,
    offset_x INTEGER,
    offset_y INTEGER,
    frame_w INTEGER,
    frame_h INTEGER
);

CREATE INDEX IF NOT EXISTS idx_pack_sprite_name
ON pack_entries(sprite_name);

CREATE TABLE IF NOT EXISTS pack_pages (
    source_pack TEXT,
    pack_name TEXT,
    pack_version INTEGER,
    page_index INTEGER,
    page_name TEXT,
    entry_count INTEGER,
    unknown INTEGER,
    png_offset INTEGER,
    png_length INTEGER,
    page_start INTEGER
);

CREATE TABLE IF NOT EXISTS loose_images (
    source_file TEXT,
    relative_path TEXT,
    file_name TEXT,
    stem TEXT,
    extension TEXT,
    file_size INTEGER
);

CREATE INDEX IF NOT EXISTS idx_loose_image_stem
ON loose_images(stem);

CREATE TABLE IF NOT EXISTS text_refs (
    source_file TEXT,
    relative_path TEXT,
    line INTEGER,
    sprite_name TEXT,
    context TEXT
);

CREATE INDEX IF NOT EXISTS idx_text_sprite_name
ON text_refs(sprite_name);
"""


# ---------------------------------------------------------------------------
# CSV helper
# ---------------------------------------------------------------------------

def write_csv(path: Path, rows, fieldnames):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Scan Project Zomboid media folder and build sprite index."
    )
    ap.add_argument("media", type=Path, help="Project Zomboid/media folder")
    ap.add_argument(
        "-o", "--output", type=Path, default=Path("pz_media_index"),
        help="output directory (default: ./pz_media_index)"
    )
    ap.add_argument(
        "--scan-text", action="store_true",
        help="also scan text/Lua/XML/etc for sprite-like name references"
    )
    ap.add_argument(
        "--no-pack", action="store_true",
        help="skip .pack parsing"
    )
    ap.add_argument(
        "--max-text-mb", type=float, default=16.0,
        help="skip individual text files larger than this MB (default: 16)"
    )
    args = ap.parse_args()

    root = args.media.resolve()
    outdir = args.output.resolve()

    if not root.is_dir():
        print(f"ERROR: media folder not found: {root}", file=sys.stderr)
        return 2

    outdir.mkdir(parents=True, exist_ok=True)

    db_path = outdir / "media_index.sqlite"
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    all_files = [p for p in root.rglob("*") if p.is_file()]

    tile_files = [p for p in all_files if p.suffix.lower() == ".tiles"]
    pack_files = [p for p in all_files if p.suffix.lower() == ".pack"]
    image_files = [p for p in all_files if p.suffix.lower() in IMAGE_EXTS]
    text_files = [p for p in all_files if p.suffix.lower() in TEXT_EXTS]

    errors = []
    tile_rows = []
    pack_rows = []
    page_rows = []
    loose_rows = []
    text_rows = []
    tile_summaries = {}
    pack_summaries = {}

    print(f"media: {root}")
    print(f"files: {len(all_files):,}")
    print(f".tiles: {len(tile_files):,}")
    print(f".pack : {len(pack_files):,}")
    print(f"images: {len(image_files):,}")
    if args.scan_text:
        print(f"text candidates: {len(text_files):,}")

    # ---- .tiles
    for i, path in enumerate(tile_files, 1):
        try:
            rows, summary = parse_tiles_file(path)
            tile_rows.extend(rows)
            tile_summaries[str(path.relative_to(root))] = summary

            conn.executemany(
                """INSERT INTO tile_entries VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(
                    r["source_file"], r["source_name"], r["tiledef_version"],
                    r["file_no"], r["tileset_number"], r["tileset_name"],
                    r["image_name"], r["sheet_width"], r["sheet_height"],
                    r["tile_index"], r["sprite_name"], r["sprite_id_candidate"],
                    r["id_formula"], r["property_count"], r["properties_json"]
                ) for r in rows]
            )
            conn.commit()
            print(f"[tiles {i}/{len(tile_files)}] {path.name}: {len(rows):,} entries")
        except Exception as e:
            errors.append(f"TILES\t{path}\t{type(e).__name__}: {e}")

    # ---- packs
    if not args.no_pack:
        for i, path in enumerate(pack_files, 1):
            try:
                rows, pages, summary = parse_pack_file(path)
                pack_rows.extend(rows)
                page_rows.extend(pages)
                pack_summaries[str(path.relative_to(root))] = summary

                conn.executemany(
                    """INSERT INTO pack_entries VALUES
                       (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [(
                        r["source_pack"], r["pack_name"], r["pack_version"],
                        r["page_index"], r["page_name"], r["entry_index"],
                        r["sprite_name"], r["x"], r["y"], r["w"], r["h"],
                        r["offset_x"], r["offset_y"], r["frame_w"], r["frame_h"]
                    ) for r in rows]
                )
                conn.executemany(
                    """INSERT INTO pack_pages VALUES
                       (?,?,?,?,?,?,?,?,?,?)""",
                    [(
                        r["source_pack"], r["pack_name"], r["pack_version"],
                        r["page_index"], r["page_name"], r["entry_count"],
                        r["unknown"], r["png_offset"], r["png_length"], r["page_start"]
                    ) for r in pages]
                )
                conn.commit()

                partial = f" [PARTIAL: {summary['partial_error']}]" if summary["partial_error"] else ""
                print(
                    f"[pack {i}/{len(pack_files)}] {path.name}: "
                    f"{len(rows):,} entries, {len(pages):,} pages{partial}"
                )
            except Exception as e:
                errors.append(f"PACK\t{path}\t{type(e).__name__}: {e}")

    # ---- loose images
    for path in image_files:
        try:
            r = {
                "source_file": str(path),
                "relative_path": str(path.relative_to(root)),
                "file_name": path.name,
                "stem": path.stem,
                "extension": path.suffix.lower(),
                "file_size": path.stat().st_size,
            }
            loose_rows.append(r)
        except Exception as e:
            errors.append(f"IMAGE\t{path}\t{type(e).__name__}: {e}")

    conn.executemany(
        "INSERT INTO loose_images VALUES (?,?,?,?,?,?)",
        [(
            r["source_file"], r["relative_path"], r["file_name"],
            r["stem"], r["extension"], r["file_size"]
        ) for r in loose_rows]
    )
    conn.commit()

    # ---- text refs
    if args.scan_text:
        max_bytes = int(args.max_text_mb * 1024 * 1024)
        for i, path in enumerate(text_files, 1):
            try:
                if path.stat().st_size > max_bytes:
                    continue
                rows = scan_text_file(path, root)
                text_rows.extend(rows)
                if rows:
                    conn.executemany(
                        "INSERT INTO text_refs VALUES (?,?,?,?,?)",
                        [(
                            r["source_file"], r["relative_path"], r["line"],
                            r["sprite_name"], r["context"]
                        ) for r in rows]
                    )
                    conn.commit()
                if i % 500 == 0:
                    print(f"[text {i}/{len(text_files)}] refs={len(text_rows):,}")
            except Exception as e:
                errors.append(f"TEXT\t{path}\t{type(e).__name__}: {e}")

    # ---- unified sprite index
    #
    # nameごとに:
    #   .tilesに存在するか
    #   .packに存在するか
    #   loose image stemに存在するか
    #   text参照数
    # を集約
    tile_by_name = defaultdict(list)
    for r in tile_rows:
        tile_by_name[r["sprite_name"]].append(r)

    pack_by_name = defaultdict(list)
    for r in pack_rows:
        pack_by_name[r["sprite_name"]].append(r)

    loose_by_name = defaultdict(list)
    for r in loose_rows:
        loose_by_name[r["stem"]].append(r)

    text_count = Counter(r["sprite_name"] for r in text_rows)

    all_names = set(tile_by_name) | set(pack_by_name) | set(loose_by_name) | set(text_count)

    sprite_index = []
    for name in sorted(all_names):
        trs = tile_by_name.get(name, [])
        prs = pack_by_name.get(name, [])
        lrs = loose_by_name.get(name, [])

        candidate_ids = sorted({
            r["sprite_id_candidate"] for r in trs
            if r["sprite_id_candidate"] is not None
        })

        file_nos = sorted({
            r["file_no"] for r in trs if r["file_no"] is not None
        })
        tileset_numbers = sorted({
            r["tileset_number"] for r in trs
        })
        tileset_names = sorted({
            r["tileset_name"] for r in trs
        })
        tile_sources = sorted({
            r["source_name"] for r in trs
        })
        pack_sources = sorted({
            r["pack_name"] for r in prs
        })

        sprite_index.append({
            "sprite_name": name,
            "in_tiles": bool(trs),
            "in_pack": bool(prs),
            "in_loose_image": bool(lrs),
            "text_ref_count": text_count.get(name, 0),
            "sprite_id_candidates": ";".join(map(str, candidate_ids)),
            "file_nos": ";".join(map(str, file_nos)),
            "tileset_numbers": ";".join(map(str, tileset_numbers)),
            "tileset_names": ";".join(tileset_names),
            "tile_sources": ";".join(tile_sources),
            "pack_sources": ";".join(pack_sources),
            "pack_entry_count": len(prs),
            "loose_image_count": len(lrs),
        })

    # ---- CSV outputs
    write_csv(
        outdir / "sprite_index.csv",
        sprite_index,
        [
            "sprite_name", "in_tiles", "in_pack", "in_loose_image",
            "text_ref_count", "sprite_id_candidates", "file_nos",
            "tileset_numbers", "tileset_names", "tile_sources",
            "pack_sources", "pack_entry_count", "loose_image_count",
        ],
    )

    write_csv(
        outdir / "tilesets.csv",
        tile_rows,
        [
            "source_file", "source_name", "tiledef_version", "file_no",
            "tileset_number", "tileset_name", "image_name",
            "sheet_width", "sheet_height", "tile_index", "sprite_name",
            "sprite_id_candidate", "id_formula", "property_count",
            "properties_json",
        ],
    )

    write_csv(
        outdir / "pack_entries.csv",
        pack_rows,
        [
            "source_pack", "pack_name", "pack_version", "page_index",
            "page_name", "entry_index", "sprite_name", "x", "y", "w", "h",
            "offset_x", "offset_y", "frame_w", "frame_h",
        ],
    )

    write_csv(
        outdir / "loose_images.csv",
        loose_rows,
        [
            "source_file", "relative_path", "file_name",
            "stem", "extension", "file_size",
        ],
    )

    if args.scan_text:
        write_csv(
            outdir / "text_refs.csv",
            text_rows,
            ["source_file", "relative_path", "line", "sprite_name", "context"],
        )

    # ---- Summary
    resolved_tile_names_in_pack = sum(
        1 for name in tile_by_name if name in pack_by_name
    )

    summary = {
        "media_root": str(root),
        "output_dir": str(outdir),
        "file_counts": {
            "all_files": len(all_files),
            "tiles_files": len(tile_files),
            "pack_files": len(pack_files),
            "loose_images": len(image_files),
            "text_candidates": len(text_files),
        },
        "index_counts": {
            "tile_entries": len(tile_rows),
            "pack_entries": len(pack_rows),
            "pack_pages": len(page_rows),
            "unique_sprite_names": len(all_names),
            "text_refs": len(text_rows),
            "errors": len(errors),
        },
        "cross_checks": {
            "unique_names_from_tiles": len(tile_by_name),
            "unique_names_from_packs": len(pack_by_name),
            "tile_names_also_present_in_pack": resolved_tile_names_in_pack,
            "tile_names_missing_from_pack": len(tile_by_name) - resolved_tile_names_in_pack,
        },
        "tile_files": tile_summaries,
        "pack_files": pack_summaries,
        "notes": [
            "sprite_id_candidate は既知のTileDef file番号規則から計算した候補。",
            "同じ数値IDが別名前空間と衝突する可能性があるため、候補は絶対正解として扱わない。",
            ".pack は画像そのものを展開せず、atlas entryメタデータだけ索引化する。",
            "file-0/legacy の完全な sheet番号→tileset名 は media の構成次第で別途復元が必要な場合がある。",
        ],
    }

    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    (outdir / "errors.log").write_text(
        "\n".join(errors),
        encoding="utf-8"
    )

    # SQLite convenience views
    conn.executescript("""
    DROP VIEW IF EXISTS v_sprite_lookup;
    CREATE VIEW v_sprite_lookup AS
    SELECT
        t.sprite_id_candidate AS sprite_id,
        t.sprite_name,
        t.file_no,
        t.tileset_number,
        t.tile_index,
        t.source_name AS tile_source,
        p.pack_name,
        p.page_name,
        p.x, p.y, p.w, p.h,
        p.offset_x, p.offset_y, p.frame_w, p.frame_h
    FROM tile_entries t
    LEFT JOIN pack_entries p
      ON p.sprite_name = t.sprite_name;

    DROP VIEW IF EXISTS v_pack_names_without_tiledef;
    CREATE VIEW v_pack_names_without_tiledef AS
    SELECT DISTINCT p.sprite_name, p.pack_name, p.page_name
    FROM pack_entries p
    LEFT JOIN tile_entries t
      ON t.sprite_name = p.sprite_name
    WHERE t.sprite_name IS NULL;

    DROP VIEW IF EXISTS v_tile_names_without_pack;
    CREATE VIEW v_tile_names_without_pack AS
    SELECT DISTINCT
        t.sprite_name,
        t.sprite_id_candidate,
        t.file_no,
        t.tileset_number,
        t.tile_index,
        t.source_name
    FROM tile_entries t
    LEFT JOIN pack_entries p
      ON p.sprite_name = t.sprite_name
    WHERE p.sprite_name IS NULL;
    """)
    conn.commit()
    conn.close()

    print()
    print("DONE")
    print(f"SQLite : {db_path}")
    print(f"CSV    : {outdir / 'sprite_index.csv'}")
    print(f"Summary: {outdir / 'summary.json'}")
    print(f"Errors : {outdir / 'errors.log'}")
    print()
    print(
        f"tile names -> pack matched: "
        f"{resolved_tile_names_in_pack:,}/{len(tile_by_name):,}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

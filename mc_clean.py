#!/usr/bin/env python3
"""
mc_clean.py - Minecraft world chunk cleaner
Fast binary scan to find deletable chunks, anvil-parser only for safe writes.

Usage:
    python mc_clean.py --world ./world --dry-run
    python mc_clean.py --world ./world --inhabited 10
    python mc_clean.py --world ./world --dims overworld nether end
"""

import sys
import struct
import zlib
import argparse
import multiprocessing
from pathlib import Path
from time import time

import anvil

# ─── ANSI colors ─────────────────────────────────────────────────────────────
G    = "\033[92m"
Y    = "\033[93m"
R    = "\033[91m"
B    = "\033[94m"
W    = "\033[0m"
BOLD = "\033[1m"

def fmt_size(b):
    for unit in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

# ─── Fast binary scan ─────────────────────────────────────────────────────────
# Reads raw .mca bytes, decompresses each chunk just enough to find
# InhabitedTime — never does full NBT parsing.

SECTOR = 4096
MARKER = b"InhabitedTime"

def fast_get_inhabited(data: bytes, cx: int, cz: int) -> int:
    """
    Returns InhabitedTime for chunk at local cx,cz.
    -1 = chunk doesn't exist
    -2 = old format, can't determine (keep it)
    """
    idx = 4 * (cx + cz * 32)
    raw = struct.unpack_from(">I", data, idx)[0]
    offset = (raw >> 8) & 0xFFFFFF
    if offset == 0:
        return -1  # chunk doesn't exist

    byte_off = offset * SECTOR
    if byte_off + 5 > len(data):
        return -2

    length      = struct.unpack_from(">I", data, byte_off)[0]
    compression = data[byte_off + 4]
    compressed  = data[byte_off + 5 : byte_off + 4 + length]

    try:
        if compression == 2:
            raw_nbt = zlib.decompress(compressed)
        elif compression == 1:
            import gzip
            raw_nbt = gzip.decompress(compressed)
        elif compression == 3:
            raw_nbt = compressed
        else:
            return -2
    except Exception:
        return -2

    # Binary search for tag name — way faster than full NBT parse
    pos = raw_nbt.find(MARKER)
    if pos == -1:
        return -2  # old chunk, no InhabitedTime tag

    val_pos = pos + len(MARKER)
    if val_pos + 8 > len(raw_nbt):
        return -2

    return struct.unpack_from(">q", raw_nbt, val_pos)[0]

# ─── Per-file worker ──────────────────────────────────────────────────────────

def process_region_file(args):
    path_str, min_inhabited, dry_run = args
    path = Path(path_str)
    original_size = path.stat().st_size

    try:
        data = path.read_bytes()
    except Exception as e:
        return path_str, 0, 0, 0, f"read error: {e}"

    if len(data) < SECTOR * 2:
        return path_str, 0, 0, 0, "file too small, skipping"

    total      = 0
    to_delete  = set()  # (cx, cz) pairs to remove

    # Fast pass — binary scan only
    for cz in range(32):
        for cx in range(32):
            it = fast_get_inhabited(data, cx, cz)
            if it == -1:
                continue  # chunk doesn't exist
            total += 1
            if it == -2:
                continue  # old format, keep it
            if it <= min_inhabited:
                to_delete.add((cx, cz))

    deleted  = len(to_delete)
    new_size = original_size

    if deleted > 0 and not dry_run:
        # Only now do we use anvil-parser — only for files that need changes
        try:
            region = anvil.Region.from_file(str(path))
        except Exception as e:
            return path_str, total, 0, 0, f"anvil open error: {e}"

        kept = []
        for cz in range(32):
            for cx in range(32):
                if (cx, cz) in to_delete:
                    continue
                try:
                    chunk = region.get_chunk(cx, cz)
                    kept.append((cx, cz, chunk))
                except anvil.errors.ChunkNotFound:
                    continue
                except Exception:
                    continue

        if not kept:
            path.unlink()
            new_size = 0
        else:
            new_region = anvil.EmptyRegion(0, 0)
            for cx, cz, chunk in kept:
                new_region.add_chunk(chunk)
            new_region.save(str(path))
            new_size = path.stat().st_size

    saved = max(0, original_size - new_size)
    return path_str, total, deleted, saved, None

# ─── Dimension paths ──────────────────────────────────────────────────────────

DIM_PATHS = {
    "overworld": "region",
    "nether":    "DIM-1/region",
    "end":       "DIM1/region",
}

def collect_region_files(world: Path, dims: list) -> list:
    files = []
    for dim in dims:
        region_dir = world / DIM_PATHS[dim]
        if region_dir.exists():
            files.extend(region_dir.glob("*.mca"))
    return files

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Minecraft chunk cleaner — fast binary scan + safe anvil writes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run first — see what would be deleted without touching anything
  python mc_clean.py --world "E:/my server/world" --dry-run

  # Delete chunks never visited (overworld only)
  python mc_clean.py --world ./world

  # Delete chunks visited under 1 second (20 ticks), all dimensions
  python mc_clean.py --world ./world --inhabited 20 --dims overworld nether end

  # Use 8 CPU cores instead of auto
  python mc_clean.py --world ./world --workers 8
        """
    )
    parser.add_argument("--world",     required=True,
                        help="Path to world folder (containing level.dat)")
    parser.add_argument("--inhabited", type=int, default=0,
                        help="Delete chunks with InhabitedTime <= N ticks (default: 0)")
    parser.add_argument("--dims",      nargs="+", default=["overworld"],
                        choices=["overworld", "nether", "end"],
                        help="Dimensions to process (default: overworld)")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Don't write anything, just show what would be deleted")
    parser.add_argument("--workers",   type=int, default=0,
                        help="CPU cores to use (default: all)")
    args = parser.parse_args()

    world = Path(args.world)
    if not world.exists():
        print(f"{R}Error: world folder not found: {world}{W}")
        sys.exit(1)
    if not (world / "level.dat").exists():
        print(f"{Y}Warning: level.dat not found — are you sure this is a world folder?{W}")

    workers = args.workers or multiprocessing.cpu_count()

    print(f"\n{BOLD}mc_clean.py{W} — Minecraft chunk cleaner")
    print(f"  World     : {B}{world}{W}")
    print(f"  Dimensions: {', '.join(args.dims)}")
    print(f"  Threshold : InhabitedTime <= {args.inhabited} ticks")
    print(f"  Dry run   : {'YES — nothing will be written' if args.dry_run else 'no'}")
    print(f"  Workers   : {workers}\n")

    files = collect_region_files(world, args.dims)
    if not files:
        print(f"{R}No .mca files found.{W}")
        sys.exit(1)

    print(f"Found {len(files)} region files — processing with {workers} workers...\n")

    task_args = [(str(f), args.inhabited, args.dry_run) for f in files]

    start         = time()
    total_chunks  = 0
    total_deleted = 0
    total_saved   = 0
    errors        = 0

    with multiprocessing.Pool(workers) as pool:
        for i, (path_str, chunks, deleted, saved, err) in enumerate(
            pool.imap_unordered(process_region_file, task_args), 1
        ):
            fname = Path(path_str).name
            if err:
                print(f"  {R}[{i:4d}/{len(files)}] {fname} — {err}{W}")
                errors += 1
            else:
                total_chunks  += chunks
                total_deleted += deleted
                total_saved   += saved
                hi  = G if deleted > 0 else ""
                rst = W if deleted > 0 else ""
                print(f"  [{i:4d}/{len(files)}] {fname:30s}  "
                      f"chunks: {chunks:4d}  {hi}deleted: {deleted:4d}{rst}  "
                      f"saved: {fmt_size(saved)}")

    elapsed = time() - start
    print(f"\n{'─'*60}")
    print(f"{BOLD}Done in {elapsed:.1f}s{W}")
    print(f"  Region files : {len(files)}")
    print(f"  Total chunks : {total_chunks}")
    print(f"  Deleted      : {G}{total_deleted}{W}")
    print(f"  Space freed  : {G}{fmt_size(total_saved)}{W}")
    if errors:
        print(f"  Errors       : {R}{errors}{W}")
    if args.dry_run:
        print(f"\n  {Y}Dry run — nothing written. Remove --dry-run to apply.{W}")
    print()

if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
mc_clean.py - Minecraft world chunk cleaner
Deletes unvisited chunks using anvil-parser and nbtlib.

Usage:
    python mc_clean.py --world ./world
    python mc_clean.py --world ./world --inhabited 10 --dry-run
    python mc_clean.py --world ./world --dims overworld nether end
"""

import sys
import argparse
import multiprocessing
from pathlib import Path
from time import time

import anvil
from nbtlib import nbt

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

# ─── Per-file worker ──────────────────────────────────────────────────────────

def process_region_file(args):
    path_str, min_inhabited, dry_run = args
    path = Path(path_str)
    original_size = path.stat().st_size

    total   = 0
    deleted = 0
    kept    = []

    try:
        region = anvil.Region.from_file(str(path))
    except Exception as e:
        return path_str, 0, 0, 0, f"open error: {e}"

    for cz in range(32):
        for cx in range(32):
            try:
                chunk = region.get_chunk(cx, cz)
            except anvil.errors.ChunkNotFound:
                continue
            except Exception:
                continue

            total += 1

            try:
                it = int(chunk.data["InhabitedTime"])
            except (KeyError, TypeError):
                # Old chunk format — keep it to be safe
                kept.append((cx, cz, chunk))
                continue

            if it <= min_inhabited:
                deleted += 1
            else:
                kept.append((cx, cz, chunk))

    new_size = original_size

    if deleted > 0 and not dry_run:
        if not kept:
            # Whole region is empty after deletion — remove the file
            path.unlink()
            new_size = 0
        else:
            # Rebuild region with only kept chunks
            new_region = anvil.EmptyRegion(0, 0)
            for cx, cz, chunk in kept:
                new_region.add_chunk(chunk)
            new_region.save(str(path))
            new_size = path.stat().st_size

    saved = max(0, original_size - new_size)
    return path_str, total, deleted, saved, None

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Minecraft chunk cleaner — deletes unvisited chunks fast",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run first — see what would be deleted without touching anything
  python mc_clean.py --world ./world --dry-run

  # Delete chunks never visited (overworld only)
  python mc_clean.py --world ./world

  # Delete chunks visited under 1 second (20 ticks), all dimensions
  python mc_clean.py --world ./world --inhabited 20 --dims overworld nether end

  # Use 8 CPU cores instead of auto
  python mc_clean.py --world ./world --workers 8
        """
    )
    parser.add_argument("--world",     required=True,
                        help="Path to world folder")
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
#!/usr/bin/env python3
"""
sbbi_convert.py — universal buildings_index converter for Subway Builder.

Subway Builder 1.3.3+ reads the collision/building index from a binary
`buildings_index.bin` (SBBI) instead of `buildings_index.json`. This script
generates that binary from whatever index a map already ships, and leaves the
original JSON in place so the map keeps working on older game versions too.

    "Add the .bin, keep the .json" — that is what Railyard's compatibility
    badge checks for, and it is what makes a map load on both 1.3.x and 1.4.x.

--------------------------------------------------------------------------------
WHAT IT HANDLES
--------------------------------------------------------------------------------
  * buildings_index.json  and  buildings_index.json.gz
  * proper index objects:  {"cs":..,"bbox":[..],"grid":[..],"buildings":[{b,f,p}]}
  * bare building arrays:  [{b,f,p}, ...]        -> cs/bbox/grid computed
  * raw GeoJSON footprints: GeometryCollection / FeatureCollection
    (Polygon + MultiPolygon)                     -> wrapped into an index
  * MultiPolygon-nested rings inside "p"         -> flattened
  * truncated / "spliced" JSON corruption        -> damaged buildings dropped
  * very large indexes                           -> streamed if ijson installed

--------------------------------------------------------------------------------
USAGE
--------------------------------------------------------------------------------
  # one map, in place (writes buildings_index.bin.gz next to the json)
  python3 sbbi_convert.py path/to/buildings_index.json.gz

  # explicit output
  python3 sbbi_convert.py in.json.gz out.bin.gz

  # a whole city folder (finds the index itself)
  python3 sbbi_convert.py --city-dir path/to/cities/data/OKC

  # every city under a data dir; safe to re-run, skips maps already converted
  python3 sbbi_convert.py --all "~/Library/Application Support/metro-maker4/cities/data"
  python3 sbbi_convert.py --all <dir> --force        # redo even if a bin exists

  # just validate a bin someone sent you
  python3 sbbi_convert.py --check path/to/buildings_index.bin.gz

Useful flags:
  --floors N     floors to assume for raw GeoJSON with no height data (default 3)
  --cs F         cell size when the index has none (default 0.0009 deg latitude)
  --dry-run      report what would happen, write nothing
  --no-backup    skip .bak files (backups are on by default)

Optional dependency:  pip install ijson   (only needed for >340 MB indexes)

--------------------------------------------------------------------------------
SBBI BINARY FORMAT  (reverse-engineered; little-endian throughout)
--------------------------------------------------------------------------------
Header, 88 bytes:
  0   4B   magic b'SBBI'
  4   4B   uint32  version = 1
  8   4B   uint32  f1      = building count
  12  4B   uint32  grid_w - 1
  16  4B   uint32  grid_h - 1
  20  4B   uint32  f4      = total rings
  24  4B   uint32  f5      = total polygon points
  28  4B   uint32  f6      = occupied grid cells
  32  4B   uint32  f_refs  = total building refs in the spatial index
  36  4B   uint32  0 (padding)
  40  8B   float64 cs      = cell size in degrees latitude
  48  8B   float64 1.0     (reserved)
  56  32B  float64 x4      bbox = min_lon, min_lat, max_lon, max_lat

Body, in order:
  building bboxes     f1      x 32B   (4 x float64)
  building floors     f1      x  4B   (float32)
  ring offset table   (f1+2)  x  4B   uint32   (leading 0, then per building)
  point offset table  (f4+1)  x  4B   uint32
  polygon points      f5      x 16B   (lon, lat float64)
  row CSR             grid_h  x  4B   uint32
  cell x-array        f6      x  4B   uint32
  cell CSR            (f6+1)  x  4B   uint32
  flat refs           f_refs  x  4B   uint32

Longitude cells are stretched by latitude:  cs_x = cs / cos(lat_mid).
Buildings are bucketed into every cell their bbox touches.

MIT-ish: do whatever you want with this. Made for the Subway Builder modding
community. Bug reports welcome.
"""

import argparse
import array
import gzip
import json
import math
import os
import re
import shutil
import struct
import sys
import tempfile
import time
from collections import defaultdict

VERSION = "1.0"

# Indexes with more raw JSON than this are streamed with ijson when available.
STREAM_THRESHOLD = 340_000_000

DEFAULT_CS = 0.0009          # degrees latitude; matches vanilla maps
DEFAULT_FLOORS = 3

# Sanity guards: refuse to write something that will OOM the game.
MAX_UNCOMPRESSED_MB = 1500.0
MAX_BUILDINGS = 4_000_000

INDEX_NAMES = ("buildings_index.json.gz", "buildings_index.json")


# ──────────────────────────────────────────────────────────────────────────────
# small helpers
# ──────────────────────────────────────────────────────────────────────────────
def _open(path, mode="rb"):
    return gzip.open(path, mode) if path.endswith(".gz") else open(path, mode)


def raw_size(path):
    """Uncompressed size of a (possibly gzipped) file, cheaply."""
    if not path.endswith(".gz"):
        return os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(-4, 2)
        val = struct.unpack("<I", f.read(4))[0]
    # ISIZE is mod 2^32; for >4 GB it wraps, so fall back to an estimate
    return val if val > 0 else os.path.getsize(path) * 5


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def backup(path, tag, enabled=True):
    if not enabled or not os.path.exists(path):
        return None
    dst = f"{path}.bak_{tag}_{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, dst)
    return dst


# ──────────────────────────────────────────────────────────────────────────────
# JSON loading, repair, and shape normalisation
# ──────────────────────────────────────────────────────────────────────────────
def repair_splices(raw):
    """
    Some published indexes have a mangled building record mid-file (a truncated
    entry spliced onto the next one), which makes the whole JSON unparseable.
    Drop the damaged record(s) and keep going. Returns (obj, n_dropped).
    """
    # start of a building record, tolerating pretty-printed whitespace
    nxt_re = re.compile(r',\s*\{\s*"b"\s*:')
    dropped = 0
    while True:
        try:
            return json.loads(raw), dropped
        except json.JSONDecodeError as e:
            # case 1: garbage spliced mid-array — skip to the next building
            m = nxt_re.search(raw, e.pos)
            if m:
                raw = raw[:e.pos] + raw[m.start():]
                dropped += 1
                continue
            # case 2: the file is truncated (download cut short). Cut back to
            # the last complete building and close the JSON by hand.
            starts = list(nxt_re.finditer(raw, 0, e.pos))
            if not starts:
                raise
            cut = starts[-1].start()
            tail = "]}" if raw.lstrip()[:1] == "{" else "]"
            candidate = raw[:cut] + tail
            try:
                obj = json.loads(candidate)
            except json.JSONDecodeError:
                raise e
            # count how many records the truncation cost (at least one)
            return obj, dropped + 1


def load_json(path):
    """Load an index JSON, repairing splice corruption if needed."""
    with _open(path, "rb") as f:
        data = f.read()
    try:
        return json.loads(data), 0
    except json.JSONDecodeError:
        obj, n = repair_splices(data.decode("utf-8", "replace"))
        return obj, n


def geojson_polygons(gj):
    """Yield lists-of-rings from a GeometryCollection / FeatureCollection."""
    for g in (gj.get("geometries") or gj.get("features") or []):
        if "geometry" in g:
            g = g["geometry"]
        t, c = g.get("type"), g.get("coordinates")
        if not c:
            continue
        if t == "Polygon":
            yield c
        elif t == "MultiPolygon":
            for poly in c:
                yield poly


def flatten_ring(ring):
    """Rings are sometimes MultiPolygon-nested one level deeper. Flatten those."""
    if ring and isinstance(ring[0], (list, tuple)) and ring[0] \
            and isinstance(ring[0][0], (list, tuple)):
        return [pt for sub in ring for pt in sub]
    return ring


def compute_metadata(buildings, cs):
    """Derive bbox + grid from building bboxes."""
    min_lon = min_lat = 1e9
    max_lon = max_lat = -1e9
    for b in buildings:
        bb = b["b"]
        min_lon = min(min_lon, bb[0]); min_lat = min(min_lat, bb[1])
        max_lon = max(max_lon, bb[2]); max_lat = max(max_lat, bb[3])
    lat_mid = (min_lat + max_lat) / 2.0
    cs_x = cs / math.cos(math.radians(lat_mid))
    return {
        "cs": cs,
        "bbox": [min_lon, min_lat, max_lon, max_lat],
        "grid": [int(math.ceil((max_lon - min_lon) / cs_x)) + 2,
                 int(math.ceil((max_lat - min_lat) / cs)) + 2],
    }


def normalise(obj, cs, floors):
    """
    Turn any supported input shape into a proper index dict, and report which
    shape it was so the caller can tell the user.
    """
    # raw GeoJSON footprints -> synthesise building records
    if isinstance(obj, dict) and obj.get("type") in ("GeometryCollection", "FeatureCollection"):
        buildings = []
        for rings in geojson_polygons(obj):
            pts = [pt for r in rings for pt in flatten_ring(r)]
            if len(pts) < 3:
                continue
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            buildings.append({"b": [min(xs), min(ys), max(xs), max(ys)],
                              "f": floors, "p": rings})
        if not buildings:
            raise ValueError("GeoJSON contained no usable polygons")
        idx = compute_metadata(buildings, cs)
        idx["buildings"] = buildings
        return idx, f"raw GeoJSON ({len(buildings)} polygons, floors={floors})"

    # bare list of building records
    if isinstance(obj, list):
        idx = compute_metadata(obj, cs)
        idx["buildings"] = obj
        return idx, "bare building array (metadata computed)"

    if not isinstance(obj, dict) or "buildings" not in obj:
        raise ValueError("unrecognised index shape: no 'buildings' key")

    # proper index; fill in anything missing
    note = "index object"
    missing = [k for k in ("cs", "bbox", "grid") if k not in obj]
    if missing:
        meta = compute_metadata(obj["buildings"], float(obj.get("cs", cs)))
        for k in missing:
            obj[k] = meta[k]
        note += f" (computed {'/'.join(missing)})"
    return obj, note


# ──────────────────────────────────────────────────────────────────────────────
# streaming readers (large indexes)
# ──────────────────────────────────────────────────────────────────────────────
def stream_metadata(path):
    import ijson
    out = {}
    for key in ("cs", "bbox", "grid"):
        with _open(path, "rb") as f:
            try:
                val = next(ijson.items(f, key))
            except StopIteration:
                return None
        out[key] = [float(v) for v in val] if isinstance(val, (list, tuple)) else float(val)
    return out


def stream_buildings(path):
    import ijson
    with _open(path, "rb") as f:
        for b in ijson.items(f, "buildings.item"):
            yield b


# ──────────────────────────────────────────────────────────────────────────────
# the actual encoder
# ──────────────────────────────────────────────────────────────────────────────
def encode(cs, bbox, grid, buildings):
    """buildings: any iterable of {b, f, p}. Returns the SBBI byte string."""
    origin_lon, origin_lat = float(bbox[0]), float(bbox[1])
    grid_w, grid_h = int(grid[0]), int(grid[1])
    lat_mid = (float(bbox[1]) + float(bbox[3])) / 2.0
    cs_x = cs / math.cos(math.radians(lat_mid))
    max_x, max_y = grid_w - 2, grid_h - 2

    pt_offsets = array.array("I", [0])
    ring_offsets = array.array("I", [0])
    poly_buf = bytearray()
    bbox_buf = bytearray()
    floors_buf = bytearray()
    cells = defaultdict(list)
    f1 = 0
    skipped = 0

    for bldg in buildings:
        rings = bldg.get("p") or []
        flat = [flatten_ring(r) for r in rings]
        flat = [r for r in flat if len(r) >= 3]
        if not flat:
            skipped += 1
            continue
        k = f1
        f1 += 1

        b = [float(v) for v in bldg["b"]]
        bbox_buf += struct.pack("<4d", b[0], b[1], b[2], b[3])
        floors_buf += struct.pack("<f", float(bldg.get("f") or 1))

        for ring in flat:
            pt_offsets.append(pt_offsets[-1] + len(ring))
            for pt in ring:
                poly_buf += struct.pack("<2d", float(pt[0]), float(pt[1]))
        ring_offsets.append(len(pt_offsets) - 1)

        xs = max(0, math.floor((b[0] - origin_lon) / cs_x))
        xe = min(max_x, math.floor((b[2] - origin_lon) / cs_x))
        ys = max(0, math.floor((b[1] - origin_lat) / cs))
        ye = min(max_y, math.floor((b[3] - origin_lat) / cs))
        for y in range(ys, ye + 1):
            for x in range(xs, xe + 1):
                cells[(y, x)].append(k)

    f4 = int(ring_offsets[-1])
    f5 = int(pt_offsets[-1])
    sec_a = array.array("I", [0])
    sec_a.extend(ring_offsets)

    by_y = defaultdict(list)
    for (y, x) in cells:
        by_y[y].append(x)

    row_csr = array.array("I", [0] * grid_h)
    x_arr = array.array("I")
    cell_csr = array.array("I")
    refs_arr = array.array("I")
    ref_pos = cell_pos = 0

    for y in range(grid_h - 1):
        row_csr[y] = cell_pos
        for x in sorted(by_y.get(y, [])):
            x_arr.append(x)
            cell_csr.append(ref_pos)
            refs = cells[(y, x)]
            refs_arr.extend(refs)
            ref_pos += len(refs)
            cell_pos += 1
    row_csr[grid_h - 1] = cell_pos
    cell_csr.append(ref_pos)
    f6, f_refs = cell_pos, ref_pos

    if sys.byteorder != "little":
        for a in (sec_a, pt_offsets, row_csr, x_arr, cell_csr, refs_arr):
            a.byteswap()

    hdr = struct.pack("<4sIIIIIIIIIdd4d",
                      b"SBBI", 1, f1, grid_w - 1, grid_h - 1,
                      f4, f5, f6, f_refs, 0,
                      cs, 1.0,
                      float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    assert len(hdr) == 88, "header must be exactly 88 bytes"

    blob = (hdr + bytes(bbox_buf) + bytes(floors_buf) +
            sec_a.tobytes() + pt_offsets.tobytes() + bytes(poly_buf) +
            row_csr.tobytes() + x_arr.tobytes() +
            cell_csr.tobytes() + refs_arr.tobytes())
    stats = {"buildings": f1, "rings": f4, "points": f5,
             "cells": f6, "refs": f_refs, "skipped": skipped}
    return blob, stats


# ──────────────────────────────────────────────────────────────────────────────
# verification — read a bin back and check it is internally consistent
# ──────────────────────────────────────────────────────────────────────────────
def verify_bytes(d):
    if len(d) < 88:
        return False, f"too short ({len(d)} bytes)"
    if d[:4] != b"SBBI":
        return False, f"bad magic {d[:4]!r} (not an SBBI file)"
    ver = struct.unpack("<I", d[4:8])[0]
    f1, gwm1, ghm1, f4, f5, f6, f_refs = struct.unpack("<7I", d[8:36])
    if f1 == 0:
        return False, "0 buildings — this is a stub, the game will show nothing"
    grid_h = ghm1 + 1
    off = (88 + f1 * 32 + f1 * 4 + (f1 + 2) * 4 + (f4 + 1) * 4 +
           f5 * 16 + grid_h * 4 + f6 * 4 + (f6 + 1) * 4)
    if off + f_refs * 4 > len(d):
        return False, f"truncated: refs need {off + f_refs*4} bytes, file has {len(d)}"
    if f_refs:
        refs = struct.unpack(f"<{f_refs}I", d[off:off + f_refs * 4])
        if max(refs) >= f1:
            return False, f"corrupt spatial index: ref {max(refs)} >= {f1} buildings"
    cs, = struct.unpack("<d", d[40:48])
    bb = struct.unpack("<4d", d[56:88])
    return True, (f"v{ver}, {f1:,} buildings, {f4:,} rings, {f5:,} points, "
                  f"{f6:,} cells, {f_refs:,} refs, cs={cs:g}, "
                  f"bbox=[{bb[0]:.4f},{bb[1]:.4f},{bb[2]:.4f},{bb[3]:.4f}]")


def verify_file(path):
    with _open(path, "rb") as f:
        return verify_bytes(f.read())


# ──────────────────────────────────────────────────────────────────────────────
# top-level conversion of one index file
# ──────────────────────────────────────────────────────────────────────────────
def convert(src, dst, cs=DEFAULT_CS, floors=DEFAULT_FLOORS,
            do_backup=True, dry_run=False, quiet=False):
    """Convert one buildings_index JSON to an SBBI bin. Returns a stats dict."""
    def say(msg):
        if not quiet:
            print(msg)

    size = raw_size(src)
    say(f"  source: {os.path.basename(src)}  ({human(os.path.getsize(src))} on disk, "
        f"~{human(size)} raw)")

    if size / 1_048_576 > MAX_UNCOMPRESSED_MB:
        raise ValueError(f"index is ~{size/1_048_576:.0f}MB — above the "
                         f"{MAX_UNCOMPRESSED_MB:.0f}MB guard; trim the map bbox first")

    repaired = 0
    if size > STREAM_THRESHOLD:
        try:
            meta = stream_metadata(src)
        except ImportError:
            say("  note: index is large and ijson is not installed "
                "(pip install ijson) — loading it all into memory")
            meta = None
        if meta:
            say("  shape: index object (streamed)")
            blob, stats = encode(meta["cs"], meta["bbox"], meta["grid"],
                                 stream_buildings(src))
        else:
            obj, repaired = load_json(src)
            idx, note = normalise(obj, cs, floors)
            say(f"  shape: {note}")
            blob, stats = encode(float(idx["cs"]), idx["bbox"], idx["grid"],
                                 idx["buildings"])
    else:
        obj, repaired = load_json(src)
        idx, note = normalise(obj, cs, floors)
        say(f"  shape: {note}")
        blob, stats = encode(float(idx["cs"]), idx["bbox"], idx["grid"],
                             idx["buildings"])

    if repaired:
        say(f"  repaired: {repaired} JSON corruption site(s) patched around")
    if stats["skipped"]:
        say(f"  skipped: {stats['skipped']} building(s) with degenerate geometry")

    ok, msg = verify_bytes(blob)
    if not ok:
        raise ValueError(f"generated bin failed verification: {msg}")
    say(f"  encoded: {msg}")

    if stats["buildings"] > MAX_BUILDINGS:
        raise ValueError(f"{stats['buildings']:,} buildings exceeds the "
                         f"{MAX_BUILDINGS:,} guard — the game may run out of memory")

    if dry_run:
        say(f"  dry-run: would write {dst}")
        return stats

    # write to a temp file first, then move into place; never leave a half file
    bak = backup(dst, "prev", do_backup)
    if bak:
        say(f"  backup: {os.path.basename(bak)}")
    tmp_dir = os.path.dirname(os.path.abspath(dst)) or "."
    fd, tmp = tempfile.mkstemp(dir=tmp_dir, suffix=".sbbi.tmp")
    os.close(fd)
    try:
        if dst.endswith(".gz"):
            with gzip.open(tmp, "wb", compresslevel=1) as f:
                f.write(blob)
        else:
            with open(tmp, "wb") as f:
                f.write(blob)
        shutil.move(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    ok, msg = verify_file(dst)
    if not ok:
        raise ValueError(f"written file failed re-verification: {msg}")
    say(f"  wrote:   {os.path.basename(dst)}  ({human(os.path.getsize(dst))})")
    return stats


def find_index(city_dir):
    for name in INDEX_NAMES:
        p = os.path.join(city_dir, name)
        if os.path.exists(p):
            return p
    return None


def bin_path_for(src):
    """buildings_index.json[.gz] -> buildings_index.bin[.gz] beside it."""
    d = os.path.dirname(os.path.abspath(src))
    return os.path.join(d, "buildings_index.bin.gz" if src.endswith(".gz")
                        else "buildings_index.bin")


def existing_bin_ok(path):
    if not os.path.exists(path) or os.path.getsize(path) < 200:
        return False
    try:
        ok, _ = verify_file(path)
        return ok
    except Exception:
        return False


def do_city(city_dir, args):
    code = os.path.basename(os.path.normpath(city_dir))
    src = find_index(city_dir)
    if not src:
        return ("skip", code, "no buildings_index.json")
    dst = bin_path_for(src)
    if not args.force and existing_bin_ok(dst):
        return ("already", code, "real bin present")
    print(f"[{code}]")
    try:
        stats = convert(src, dst, cs=args.cs, floors=args.floors,
                        do_backup=not args.no_backup, dry_run=args.dry_run)
        return ("ok", code, f"{stats['buildings']:,} buildings")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        return ("fail", code, f"{type(e).__name__}: {e}")


# ──────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="Convert Subway Builder buildings_index JSON to the "
                    "1.3.3+ SBBI binary, keeping the JSON for back-compat.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  sbbi_convert.py buildings_index.json.gz\n"
               "  sbbi_convert.py --city-dir cities/data/OKC\n"
               "  sbbi_convert.py --all cities/data\n"
               "  sbbi_convert.py --check buildings_index.bin.gz\n")
    ap.add_argument("input", nargs="?", help="index json, or omit and use --all/--city-dir")
    ap.add_argument("output", nargs="?", help="output bin (default: beside the input)")
    ap.add_argument("--city-dir", help="a single city folder containing buildings_index.json")
    ap.add_argument("--all", metavar="DATA_DIR", help="convert every city folder under DATA_DIR")
    ap.add_argument("--check", metavar="BIN", help="verify an existing .bin and exit")
    ap.add_argument("--floors", type=int, default=DEFAULT_FLOORS,
                    help=f"floors for raw GeoJSON with no heights (default {DEFAULT_FLOORS})")
    ap.add_argument("--cs", type=float, default=DEFAULT_CS,
                    help=f"cell size in degrees latitude if absent (default {DEFAULT_CS})")
    ap.add_argument("--force", action="store_true", help="reconvert even if a good bin exists")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--no-backup", action="store_true", help="do not write .bak files")
    ap.add_argument("--version", action="version", version=f"sbbi_convert {VERSION}")
    args = ap.parse_args()

    if args.check:
        path = os.path.expanduser(args.check)
        ok, msg = verify_file(path)
        print(("VALID   " if ok else "INVALID ") + os.path.basename(path) + " — " + msg)
        sys.exit(0 if ok else 1)

    if args.all:
        root = os.path.expanduser(args.all)
        if not os.path.isdir(root):
            sys.exit(f"not a directory: {root}")
        dirs = sorted(d for d in (os.path.join(root, x) for x in os.listdir(root))
                      if os.path.isdir(d))
        results = defaultdict(list)
        for d in dirs:
            status, code, note = do_city(d, args)
            results[status].append(f"{code} ({note})" if status in ("ok", "fail") else code)
        print("\n==== SUMMARY ====")
        for key, label in (("ok", "converted"), ("already", "already had a bin"),
                           ("skip", "skipped"), ("fail", "FAILED")):
            if results[key]:
                print(f"{label}: {len(results[key])}")
                for item in results[key]:
                    print(f"  - {item}")
        if results["ok"] and not args.dry_run:
            print("\nShip BOTH buildings_index.json.gz and buildings_index.bin.gz "
                  "in the map zip so it loads on old and new game versions.")
        sys.exit(1 if results["fail"] else 0)

    if args.city_dir:
        status, code, note = do_city(os.path.expanduser(args.city_dir), args)
        sys.exit(1 if status == "fail" else 0)

    if not args.input:
        ap.print_help()
        sys.exit(2)

    src = os.path.expanduser(args.input)
    if os.path.isdir(src):
        status, code, note = do_city(src, args)
        sys.exit(1 if status == "fail" else 0)
    if not os.path.exists(src):
        sys.exit(f"no such file: {src}")
    dst = os.path.expanduser(args.output) if args.output else bin_path_for(src)
    print(f"[{os.path.basename(os.path.dirname(os.path.abspath(src))) or 'index'}]")
    try:
        convert(src, dst, cs=args.cs, floors=args.floors,
                do_backup=not args.no_backup, dry_run=args.dry_run)
    except Exception as e:
        sys.exit(f"  ERROR: {type(e).__name__}: {e}")
    if not args.dry_run:
        print("\nKeep the .json alongside the new .bin in your map zip — that is "
              "what makes it load on both old and new game versions.")


if __name__ == "__main__":
    main()

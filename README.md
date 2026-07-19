# sbbi-convert

Converts a Subway Builder map's `buildings_index.json` into the `buildings_index.bin`
(SBBI) format the game expects from **1.3.3 onward**, while leaving the original JSON
in place so the map still loads on older versions.

Pure Python 3, standard library only. No dependencies, no install step; just run the
file.

```bash
git clone https://github.com/ryandicicco/sbbi-convert.git
cd sbbi-convert
python3 sbbi_convert.py path/to/buildings_index.json.gz
```

## Why

Subway Builder 1.3.3+ reads the building/collision index from a binary file. Maps that
ship only `buildings_index.json` get flagged incompatible in Railyard. The fix is not to
replace the JSON but to **ship both files** — that is what Railyard's compatibility check
looks for, and it keeps the map working on old and new game builds.

A word of warning from experience: a `.bin` that merely *exists* is not enough. An empty
or stub binary passes the badge check and then renders nothing in game. This script
verifies every file it writes and refuses to install one with zero buildings.

## Usage

```bash
# one map — writes buildings_index.bin.gz next to the json
python3 sbbi_convert.py path/to/buildings_index.json.gz

# a city folder (it finds the index itself)
python3 sbbi_convert.py --city-dir cities/data/OKC

# every city under a data dir; safe to re-run, skips maps already done
python3 sbbi_convert.py --all "~/Library/Application Support/metro-maker4/cities/data"
python3 sbbi_convert.py --all <dir> --force     # redo even if a bin exists

# validate a bin someone sent you
python3 sbbi_convert.py --check buildings_index.bin.gz
```

Flags: `--floors N` (assumed floors for raw GeoJSON with no heights, default 3),
`--cs F` (cell size in degrees latitude when the index lacks one, default 0.0009),
`--dry-run`, `--no-backup`, `--force`.

## What it accepts

| Input | Handling |
| --- | --- |
| `buildings_index.json` / `.json.gz` | either, auto-detected |
| `{"cs","bbox","grid","buildings":[{b,f,p}]}` | the normal case |
| index missing `cs` / `bbox` / `grid` | computed from the footprints |
| bare `[{b,f,p}, ...]` array | wrapped into an index |
| raw GeoJSON `GeometryCollection` / `FeatureCollection` | Polygon + MultiPolygon wrapped into an index |
| MultiPolygon-nested rings inside `p` | flattened |
| spliced or truncated JSON | damaged records dropped, rest kept |
| indexes over ~340 MB | streamed if `ijson` is installed (`pip install ijson`) |

Backups are written before anything is overwritten, output goes through a temp file so a
crash cannot leave a half-written `.bin`, and every result is re-verified after writing.

## Format notes

Little-endian throughout. 88-byte header:

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 4 | magic `SBBI` |
| 4 | 4 | uint32 version = 1 |
| 8 | 4 | uint32 building count |
| 12 | 4 | uint32 grid_w − 1 |
| 16 | 4 | uint32 grid_h − 1 |
| 20 | 4 | uint32 total rings |
| 24 | 4 | uint32 total polygon points |
| 28 | 4 | uint32 occupied grid cells |
| 32 | 4 | uint32 total building refs |
| 36 | 4 | uint32 padding = 0 |
| 40 | 8 | float64 `cs` (cell size, degrees latitude) |
| 48 | 8 | float64 1.0 (reserved) |
| 56 | 32 | float64 ×4 bbox: min_lon, min_lat, max_lon, max_lat |

Body, in order: building bboxes (`n` × 32 B), floors (`n` × float32), ring offset table
(`n+2` × uint32), point offset table (`rings+1` × uint32), polygon points (`points` ×
16 B as lon/lat float64), then the spatial index as CSR — row offsets (`grid_h` ×
uint32), cell x-array (`cells` × uint32), cell offsets (`cells+1` × uint32), flat refs
(`refs` × uint32).

Longitude cells are stretched by latitude: `cs_x = cs / cos(lat_mid)`. Each building is
bucketed into every cell its bbox touches.

## Verified against

Output is **byte-identical** to the binaries shipped by maps known to work in game
(checked against a 43,822-building index). If your converted map loads correctly, the
format above is right; if you find a map it mishandles, the input shape is probably one
not in the table — open an issue with a sample.

## Packaging your map

Put **both** files in the zip:

```
buildings_index.json.gz
buildings_index.bin.gz
```

Then make sure your Railyard `manifest.json` `file_sizes` lists `buildings_index.bin`,
or the badge will still read as incompatible.

## Contributing

Found a map this mishandles? It is almost always an input shape not in the table above.
Open an issue with a small sample of the `buildings_index.json` (a few buildings is
plenty) and what the game or Railyard did with the output, and it can be added.

## License

MIT — see [LICENSE](LICENSE). Made for the Subway Builder modding community; do whatever
you want with it.

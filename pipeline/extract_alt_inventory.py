"""
extract_alt_inventory.py
========================

Read the accessibility metadata back out of tagged images and write it to a CSV
inventory keyed by WordPress attachment id.

This is the readback stage. Stage two (pipeline/alt_text_tagger.py) embeds alt
text into each file's IPTC/XMP fields; this script recovers it into a flat table
for review, hand-off, or a future upload step. It reads the files themselves
rather than the tagger's run report, so it reflects what is actually embedded --
including edits a human made in Lightroom, Bridge, or exiftool after the fact.

Reads these fields, in order of preference for each column:

    alt_text   XMP-iptcCore:AltTextAccessibility, falling back to XMP-dc:description
    ext_text   XMP-iptcCore:ExtDescrAccessibility

The id comes from the filename stem, which pipeline/wp_media_pull.py sets to the
WordPress attachment id (that is why it downloads as `<id>.<ext>` in the first
place). Files whose stem is not a plain integer are still reported, with an empty
id and a note in the `status` column, so nothing silently vanishes from the count.

Usage
-----
    # Whole tagged folder -> alt_inventory.csv
    python pipeline/extract_alt_inventory.py tagged

    # Somewhere else, and only rows that actually have alt text
    python pipeline/extract_alt_inventory.py tagged -o out.csv --only-tagged

    # Include the source_url / title columns from stage one's inventory
    python pipeline/extract_alt_inventory.py tagged --inventory library_images/inventory.csv

Requirements
------------
    exiftool must be on PATH (it may be installed as exiftool.bat on Windows;
    Python's shutil.which finds it either way).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ALT_FIELD = "XMP-iptcCore:AltTextAccessibility"
EXT_FIELD = "XMP-iptcCore:ExtDescrAccessibility"
LEGACY_FIELD = "XMP-dc:description"

# The exiftool JSON keys corresponding to the fields above. exiftool strips the
# group prefix in its output unless -G is passed.
ALT_KEY = "AltTextAccessibility"
EXT_KEY = "ExtDescrAccessibility"
LEGACY_KEY = "Description"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".gif", ".bmp"}

BASE_FIELDS = ["id", "file", "alt_text", "ext_text", "alt_chars", "status"]

# Columns worth carrying over from stage one's inventory.csv when it is supplied.
INVENTORY_EXTRAS = ["source_url", "mime_type", "title"]


def flatten(value) -> str:
    """Normalise one exiftool value to a string.

    XMP lang-alt fields (dc:description among them) can come back as a dict of
    language -> text, or as a list when a tag repeats. Take the first usable
    entry rather than str()-ing a dict into the CSV.
    """
    if value is None:
        return ""
    if isinstance(value, dict):
        for item in value.values():
            text = flatten(item)
            if text:
                return text
        return ""
    if isinstance(value, list):
        for item in value:
            text = flatten(item)
            if text:
                return text
        return ""
    return str(value).strip()


def read_metadata(files: list[Path]) -> dict[str, dict]:
    """Bulk-read the accessibility fields for `files`, keyed by SourceFile.

    One exiftool call for the whole batch: process startup dominates its cost,
    so per-file invocations would be far slower. Paths go through a temp argfile
    to stay clear of command-line length limits on a thousand-image collection.
    """
    exe = shutil.which("exiftool")
    if not exe:
        raise RuntimeError("exiftool not found on PATH")
    if not files:
        return {}

    argfile = Path(f".exiftool_read_{os.getpid()}.txt")
    try:
        with open(argfile, "w", encoding="utf-8") as fh:
            fh.write("-j\n-charset\nfilename=utf8\n")
            for field in (ALT_FIELD, EXT_FIELD, LEGACY_FIELD):
                fh.write(f"-{field}\n")
            for f in files:
                fh.write(f"{f}\n")

        result = subprocess.run(
            [exe, "-@", str(argfile)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    finally:
        argfile.unlink(missing_ok=True)

    if not result.stdout.strip():
        detail = (result.stderr or "").strip()[:200]
        raise RuntimeError(f"exiftool returned no output ({detail})")

    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"could not parse exiftool output: {e}") from e

    return {entry["SourceFile"]: entry for entry in entries if entry.get("SourceFile")}


def load_inventory(path: Path) -> dict[str, dict]:
    """Load stage one's inventory.csv, keyed by id, for the extra columns."""
    out: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (row.get("id") or "").strip()
            if key:
                out[key] = row
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Extract embedded alt text from tagged images into a CSV "
                    "keyed by WordPress attachment id.",
    )
    p.add_argument("input_dir", nargs="?", default=os.getenv("OUT_DIR") or "tagged",
                   help="folder of tagged images, searched recursively "
                        "(default: OUT_DIR from .env, else ./tagged)")
    p.add_argument("-o", "--output", default="alt_inventory.csv",
                   help="CSV to write (default: alt_inventory.csv)")
    p.add_argument("--inventory", type=Path,
                   help="stage one's inventory.csv; adds source_url/mime_type/"
                        "title columns for rows whose id matches")
    p.add_argument("--only-tagged", action="store_true",
                   help="omit rows that have no alt text (default: list every "
                        "image, so gaps in coverage stay visible)")
    p.add_argument("--no-legacy-fallback", action="store_true",
                   help="don't fall back to XMP-dc:description when the "
                        "accessibility field is empty")
    return p


def main() -> int:
    args = build_parser().parse_args()

    in_dir = Path(args.input_dir)
    if not in_dir.is_dir():
        return err(f"not a directory: {in_dir}\n"
                   f"       run pipeline/alt_text_tagger.py first, or pass the "
                   f"folder holding the tagged images")

    files = sorted(p for p in in_dir.rglob("*")
                   if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS)
    if not files:
        return err(f"no images found under {in_dir}")

    inventory: dict[str, dict] = {}
    if args.inventory:
        if not args.inventory.is_file():
            return err(f"inventory not found: {args.inventory}")
        inventory = load_inventory(args.inventory)

    print(f"reading metadata from {len(files)} image(s) in {in_dir} ...", flush=True)
    try:
        meta = read_metadata(files)
    except RuntimeError as e:
        return err(str(e))

    fieldnames = list(BASE_FIELDS)
    if inventory:
        fieldnames += INVENTORY_EXTRAS

    rows: list[dict] = []
    skipped = 0

    for path in files:
        entry = meta.get(str(path)) or meta.get(path.as_posix()) or {}

        alt = flatten(entry.get(ALT_KEY))
        ext = flatten(entry.get(EXT_KEY))
        legacy = flatten(entry.get(LEGACY_KEY))

        status = "tagged"
        if not alt and legacy and not args.no_legacy_fallback:
            alt = legacy
            status = "legacy-only"
        elif not alt:
            status = "no-alt"

        # wp_media_pull.py names downloads <attachment id>.<ext>, so the stem is
        # the id. Anything else is still reported, with an empty id, rather than
        # dropped -- a silent gap here would look like a coverage gap later.
        stem = path.stem
        file_id = stem if stem.isdigit() else ""
        if not file_id:
            status = f"{status};bad-id"

        if args.only_tagged and not alt:
            skipped += 1
            continue

        row = {
            "id": file_id,
            "file": str(path),
            "alt_text": alt,
            "ext_text": ext,
            "alt_chars": len(alt),
            "status": status,
        }
        if inventory:
            source = inventory.get(file_id, {})
            for col in INVENTORY_EXTRAS:
                row[col] = source.get(col, "")
        rows.append(row)

    summary: dict[str, int] = {}
    for row in rows:
        for part in row["status"].split(";"):
            summary[part] = summary.get(part, 0) + 1

    # Sort numerically by id so the CSV reads in attachment order; unparseable
    # ids sort last rather than throwing.
    rows.sort(key=lambda r: (r["id"] == "", int(r["id"]) if r["id"] else 0))

    out_path = Path(args.output)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n{len(rows)} row(s) -> {out_path}")
    for key in sorted(summary):
        print(f"  {key:14} {summary[key]}")
    if skipped:
        print(f"  {'(omitted)':14} {skipped}  --only-tagged")

    missing = summary.get("no-alt", 0)
    if missing and not args.only_tagged:
        print(f"\n{missing} image(s) carry no alt text -- run "
              f"pipeline/alt_text_tagger.py over them, or re-run this with "
              f"--only-tagged to exclude them")
    if summary.get("bad-id"):
        print(f"{summary['bad-id']} file(s) have a non-numeric name and got an "
              f"empty id; they are listed but cannot be matched to WordPress")
    return 0


def err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)

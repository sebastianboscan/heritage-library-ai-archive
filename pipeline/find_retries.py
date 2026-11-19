"""
find_retries.py
===============

Pick out the images from a tagger run report that are worth re-running, and
write their ids to a list for alt_text_tagger.py --only.

Two kinds of row are selected:

    empty      the model returned no extended text. These are almost all dense
               newspaper pages and maps, where the reply was cut off before the
               transcription closed and only alt_text could be salvaged.

    clippings  newspaper clippings and articles, identified from their alt text.
               Runs made before the prompt asked for handwritten annotations
               skipped the source citation written beside a clipping (newspaper
               name, date, page) -- the most useful string for a researcher.

Rows with confidence "unknown" but non-empty extended text are NOT selected:
their reply was cut off after the transcription had closed, so the text is
complete and only the visible_text / confidence keys were lost.

Usage
-----
    python pipeline/find_retries.py results.csv -o retry.txt
    python pipeline/alt_text_tagger.py --only retry.txt --overwrite ...
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

CLIPPING = re.compile(r"newspaper|clipping|obituar|article|island packet|column",
                      re.IGNORECASE)


def main() -> int:
    p = argparse.ArgumentParser(
        description="List images from a tagger report that should be re-run.")
    p.add_argument("report", help="the tagger's CSV report (alt_text_report.csv)")
    p.add_argument("-o", "--output", help="write the id list here (default: stdout)")
    p.add_argument("--no-empty", action="store_true",
                   help="don't select rows with empty extended text")
    p.add_argument("--no-clippings", action="store_true",
                   help="don't select newspaper clippings")
    args = p.parse_args()

    csv.field_size_limit(10 ** 7)       # transcriptions can run to many KB
    try:
        with open(args.report, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # A report can hold several attempts at the same file; the last one wins.
    latest = {Path(r["file"]).stem: r for r in rows if r.get("file")}

    picked: dict[str, str] = {}
    for file_id, r in latest.items():
        if r.get("status") == "error":
            picked[file_id] = "error"
        elif not args.no_empty and not (r.get("extended") or "").strip():
            picked[file_id] = "empty"
        elif not args.no_clippings and CLIPPING.search(r.get("alt_text") or ""):
            picked[file_id] = "clipping"

    ordered = sorted(picked, key=lambda s: (not s.isdigit(), int(s) if s.isdigit() else 0, s))
    lines = [f"# {len(ordered)} image(s) from {args.report}"] + ordered
    text = "\n".join(lines) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)

    counts: dict[str, int] = {}
    for reason in picked.values():
        counts[reason] = counts.get(reason, 0) + 1
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing"
    print(f"{len(picked)} of {len(latest)} selected ({summary})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

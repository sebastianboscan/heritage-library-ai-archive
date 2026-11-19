"""
wp_upload_alt.py
================

Stage four: publish reviewed alt text and transcriptions back to WordPress.

Reads a CSV keyed by WordPress attachment id -- normally the output of
pipeline/extract_alt_inventory.py, which reflects what is actually embedded in
the tagged files, including any corrections made during review -- and writes:

    alt_text      -> the attachment's alt text
    ext_text      -> the attachment's description (the transcription)

The caption is never touched: many themes print it publicly under the image.

DRY RUN BY DEFAULT. Nothing is written to the site unless --apply is passed.

Safety
------
  * An attachment that already has alt text on the site is skipped (someone may
    have written it by hand since the pull) unless --overwrite-alt is given.
  * The description is only written when it is currently empty, unless
    --description overwrite is given. --description skip leaves it alone.
  * Every change is appended to a log CSV with the previous values, and
    --restore LOG puts them back. A field is only restored if it still holds
    what this script wrote, so later hand edits are never clobbered.

Usage
-----
    python pipeline/extract_alt_inventory.py tagged -o alt_inventory.csv
    python pipeline/wp_upload_alt.py alt_inventory.csv --limit 5          # dry run
    python pipeline/wp_upload_alt.py alt_inventory.csv --limit 5 --apply  # write 5
    python pipeline/wp_upload_alt.py alt_inventory.csv --apply            # write all

    python pipeline/wp_upload_alt.py --restore upload_log.csv --apply     # undo

The tagger's own report (alt_text_report.csv) is also accepted as input, but it
records what the model produced, not what the files carry after review -- prefer
the extract_alt_inventory.py output.
"""

from __future__ import annotations

import argparse
import csv
import html
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wp_config

DELAY = 0.3                     # seconds between write requests
LOG_FIELDS = ["time", "id", "action", "old_alt", "new_alt",
              "old_description", "new_description", "detail"]


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------

def load_rows(path: str) -> list[dict]:
    """Load (id, alt, ext) rows from either supported CSV format."""
    csv.field_size_limit(10 ** 7)
    with open(path, newline="", encoding="utf-8") as fh:
        raw = list(csv.DictReader(fh))

    rows: dict[str, dict] = {}
    for r in raw:
        # extract_alt_inventory.py: id / alt_text / ext_text
        # alt_text_tagger.py report: file / alt_text / extended
        file_id = (r.get("id") or Path(r.get("file") or "").stem or "").strip()
        if not file_id.isdigit():
            continue
        if r.get("status") == "error":
            continue
        rows[file_id] = {           # later rows win, so re-runs override
            "id": file_id,
            "alt": (r.get("alt_text") or "").strip(),
            "ext": (r.get("ext_text") or r.get("extended") or "").strip(),
        }
    return sorted(rows.values(), key=lambda r: int(r["id"]))


def load_id_filter(path: str) -> set[str]:
    with open(path, encoding="utf-8") as fh:
        return {Path(line.strip()).stem for line in fh
                if line.strip() and not line.startswith("#")}


# --------------------------------------------------------------------------
# wordpress
# --------------------------------------------------------------------------

def to_description_html(text: str) -> str:
    """Render a transcription as safe HTML that keeps its line structure.

    WordPress stores the description as HTML. Escape everything (a transcribed
    "<" must not become markup), then keep blank-line paragraphs and single
    line breaks, since the layout of a form or clipping carries meaning.
    """
    paras = [p for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    return "\n".join(
        "<p>" + "<br>\n".join(html.escape(line) for line in p.split("\n")) + "</p>"
        for p in paras
    )


def raw_field(value) -> str:
    """context=edit returns {"raw": ..., "rendered": ...}; we want raw."""
    if isinstance(value, dict):
        return value.get("raw") or ""
    return value or ""


def get_media(base: str, auth, media_id: str) -> dict | None:
    r = requests.get(f"{base}/media/{media_id}", auth=auth, timeout=30,
                     params={"context": "edit",
                             "_fields": "id,alt_text,description"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def update_media(base: str, auth, media_id: str, fields: dict) -> None:
    r = requests.post(f"{base}/media/{media_id}", auth=auth, timeout=60,
                      json=fields)
    if r.status_code >= 400:
        try:
            msg = r.json().get("message", r.text)
        except ValueError:
            msg = r.text
        raise RuntimeError(f"HTTP {r.status_code}: {str(msg)[:200]}")


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

class Log:
    def __init__(self, path: Path, enabled: bool):
        self.path, self.enabled = path, enabled
        self.fh = self.writer = None

    def __enter__(self):
        if self.enabled:
            new = not self.path.exists()
            self.fh = open(self.path, "a", newline="", encoding="utf-8")
            self.writer = csv.DictWriter(self.fh, fieldnames=LOG_FIELDS)
            if new:
                self.writer.writeheader()
        return self

    def write(self, **row) -> None:
        if self.writer:
            row["time"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.writer.writerow({k: row.get(k, "") for k in LOG_FIELDS})
            self.fh.flush()         # survive a Ctrl-C mid-run

    def __exit__(self, *exc):
        if self.fh:
            self.fh.close()


# --------------------------------------------------------------------------
# upload
# --------------------------------------------------------------------------

def upload(args) -> int:
    rows = load_rows(args.input)
    if args.only:
        wanted = load_id_filter(args.only)
        rows = [r for r in rows if r["id"] in wanted]
    rows = [r for r in rows if r["alt"]]
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        print("nothing to upload -- no rows with an id and alt text")
        return 0

    base, auth = wp_config.base_url(), wp_config.auth()
    mode = "APPLY -- writing to the site" if args.apply else "dry run (pass --apply to write)"
    print(f"{len(rows)} attachment(s) | description: {args.description} | {mode}\n")

    counts: dict[str, int] = {}
    with Log(Path(args.log), enabled=args.apply) as log:
        for i, row in enumerate(rows, 1):
            media_id = row["id"]
            try:
                current = get_media(base, auth, media_id)
            except requests.RequestException as e:
                action, detail, fields = "error", f"read failed: {e}", {}
                current = {}
            else:
                if current is None:
                    action, detail, fields = "not-found", "no such attachment", {}
                else:
                    action, detail, fields = plan(row, current, args)

            old_alt = (current or {}).get("alt_text") or ""
            old_desc = raw_field((current or {}).get("description"))

            if fields and args.apply:
                try:
                    update_media(base, auth, media_id, fields)
                    time.sleep(DELAY)
                except (requests.RequestException, RuntimeError) as e:
                    action, detail = "error", f"write failed: {e}"

            if fields or action == "error":
                log.write(id=media_id, action=action,
                          old_alt=old_alt, new_alt=fields.get("alt_text", old_alt),
                          old_description=old_desc,
                          new_description=fields.get("description", old_desc),
                          detail=detail)

            counts[action] = counts.get(action, 0) + 1
            verb = action if args.apply or not fields else "would " + {"updated": "update", "restored": "restore"}.get(action, action)
            print(f"[{i}/{len(rows)}] {media_id:>6}  {verb:<22} {detail}")

    print("\n" + "  ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    if args.apply:
        print(f"log: {args.log}   (undo with --restore {args.log} --apply)")
    else:
        print("dry run -- nothing was written. Re-run with --apply to publish.")
    return 1 if counts.get("error") else 0


def plan(row: dict, current: dict, args) -> tuple[str, str, dict]:
    """Decide what to write for one attachment. Returns (action, detail, fields)."""
    fields: dict = {}
    notes: list[str] = []

    old_alt = (current.get("alt_text") or "").strip()
    if old_alt and old_alt != row["alt"] and not args.overwrite_alt:
        notes.append("alt kept (already set on site)")
    elif old_alt != row["alt"]:
        fields["alt_text"] = row["alt"]
        notes.append(f"alt {len(row['alt'])}ch")

    if args.description != "skip" and row["ext"]:
        new_desc = to_description_html(row["ext"])
        old_desc = raw_field(current.get("description")).strip()
        if old_desc and old_desc != new_desc and args.description != "overwrite":
            notes.append("description kept (already set)")
        elif old_desc != new_desc:
            fields["description"] = new_desc
            notes.append(f"description {len(row['ext'])}ch")

    if not fields:
        return "unchanged", "; ".join(notes) or "already up to date", {}
    return "updated", "; ".join(notes), fields


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------

def restore(args) -> int:
    csv.field_size_limit(10 ** 7)
    with open(args.restore, newline="", encoding="utf-8") as fh:
        entries = [r for r in csv.DictReader(fh) if r.get("action") == "updated"]
    if not entries:
        print("nothing to restore -- the log has no 'updated' rows")
        return 0

    # Restore in reverse so an attachment updated twice ends at its original.
    entries.reverse()
    base, auth = wp_config.base_url(), wp_config.auth()
    mode = "APPLY -- writing to the site" if args.apply else "dry run (pass --apply to write)"
    print(f"{len(entries)} change(s) to undo | {mode}\n")

    counts: dict[str, int] = {}
    for i, e in enumerate(entries, 1):
        media_id = e["id"]
        try:
            current = get_media(base, auth, media_id)
        except requests.RequestException as ex:
            current, action, detail = None, "error", f"read failed: {ex}"
        else:
            action, detail = "", ""
        fields: dict = {}

        if current is not None:
            cur_alt = current.get("alt_text") or ""
            cur_desc = raw_field(current.get("description"))
            skipped = []
            if e["old_alt"] != e["new_alt"]:
                if cur_alt == e["new_alt"]:
                    fields["alt_text"] = e["old_alt"]
                else:
                    skipped.append("alt edited since upload")
            if e["old_description"] != e["new_description"]:
                if cur_desc.strip() == e["new_description"].strip():
                    fields["description"] = e["old_description"]
                else:
                    skipped.append("description edited since upload")
            action = "restored" if fields else "skipped"
            detail = "; ".join(skipped) or ", ".join(fields)
        elif not action:
            action, detail = "not-found", "no such attachment"

        if fields and args.apply:
            try:
                update_media(base, auth, media_id, fields)
                time.sleep(DELAY)
            except (requests.RequestException, RuntimeError) as ex:
                action, detail = "error", f"write failed: {ex}"

        counts[action] = counts.get(action, 0) + 1
        verb = action if args.apply or not fields else "would " + {"updated": "update", "restored": "restore"}.get(action, action)
        print(f"[{i}/{len(entries)}] {media_id:>6}  {verb:<20} {detail}")

    print("\n" + "  ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    if not args.apply:
        print("dry run -- nothing was written. Re-run with --apply to restore.")
    return 1 if counts.get("error") else 0


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Publish alt text and transcriptions to WordPress "
                    "(dry run unless --apply).")
    p.add_argument("input", nargs="?", default="alt_inventory.csv",
                   help="CSV with id, alt_text, ext_text -- the output of "
                        "extract_alt_inventory.py (default: alt_inventory.csv)")
    p.add_argument("--apply", action="store_true",
                   help="actually write to the site; without it nothing changes")
    p.add_argument("--description", choices=["if-empty", "overwrite", "skip"],
                   default="if-empty",
                   help="what to do with the transcription: write it only where "
                        "the description is empty (default), always, or never")
    p.add_argument("--overwrite-alt", action="store_true",
                   help="replace alt text that is already set on the site")
    p.add_argument("--only", metavar="LIST",
                   help="upload only the ids in this file, one per line")
    p.add_argument("--limit", type=int, help="stop after N attachments")
    p.add_argument("--log", default="upload_log.csv",
                   help="where to record changes for --restore "
                        "(default: upload_log.csv)")
    p.add_argument("--restore", metavar="LOG",
                   help="undo the changes recorded in LOG instead of uploading")
    args = p.parse_args()

    if args.restore:
        if not Path(args.restore).is_file():
            print(f"error: log not found: {args.restore}", file=sys.stderr)
            return 1
        return restore(args)

    if not Path(args.input).is_file():
        print(f"error: input not found: {args.input}\n"
              f"       run pipeline/extract_alt_inventory.py first", file=sys.stderr)
        return 1
    return upload(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Everything written so far is in the log.")
        sys.exit(130)

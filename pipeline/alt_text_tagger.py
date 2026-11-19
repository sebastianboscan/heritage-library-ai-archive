"""
alt_text_tagger.py
==================

Generate accessibility alt text for a folder of images using a local Ollama
vision model and embed it directly into each file's IPTC/XMP metadata.

This is the second stage of the pipeline. Stage one (pipeline/wp_media_pull.py)
downloads the WordPress attachments that are missing alt text into
library_images/ along with an inventory.csv manifest.

Writes to the IPTC Photo Metadata Standard accessibility fields:

    XMP-iptcCore:AltTextAccessibility   short alt text (the important one)
    XMP-iptcCore:ExtDescrAccessibility  longer extended description
    XMP-dc:description                  legacy caption field, for older DAMS
                                        (disable with --no-legacy-fields)

Requirements
------------
    pip install pillow python-dotenv requests
    exiftool must be on PATH
    ollama must be running, with a vision model pulled:
        ollama pull qwen3-vl:2b

    Settings can live in a .env file beside the project:

        OLLAMA_HOST=http://localhost:11434
        OLLAMA_MODEL=qwen3-vl:2b
        INPUT_DIR=./library_images
        OUT_DIR=./tagged

    Real environment variables take precedence over .env, and anything passed on
    the command line wins over both.

Usage
-----
    # Safe default: originals untouched, tagged copies written to ./tagged
    python pipeline/alt_text_tagger.py ./library_images --out-dir ./tagged

    # Try 10 files first and eyeball the CSV before committing
    python pipeline/alt_text_tagger.py ./library_images --out-dir ./tagged --limit 10

    # See what would happen without writing anything
    python pipeline/alt_text_tagger.py ./library_images --dry-run --limit 10

    # Modify the originals in place (a copy is kept in the output folder only
    # when not using --in-place)
    python pipeline/alt_text_tagger.py ./library_images --in-place

Context massively improves output quality. Pass what you know about the
collection:

    python pipeline/alt_text_tagger.py ./library_images --out-dir ./tagged \
        --collection-context "Local history collection for a South Carolina
        heritage library: town street scenes, portraits, maps and event flyers."

Every result is also appended to a CSV report for human review. Auto-generated
alt text for a heritage collection should be reviewed before publication -- the
confidence column exists to tell reviewers where to look first.
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.exit("Pillow is required:  pip install pillow")

try:
    import requests
except ImportError:
    sys.exit("requests is required:  pip install requests")

try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit("python-dotenv is required:  pip install python-dotenv")

load_dotenv()

# Pillow refuses very large files by default as a decompression-bomb guard.
# Scanned heritage material legitimately trips this, so raise the ceiling.
Image.MAX_IMAGE_PIXELS = 500_000_000

# Downscale before sending. Vision models tile large images into many tokens,
# and a 1024px edge is plenty for describing a scene while keeping small local
# models fast.
MAX_EDGE = 1024
JPEG_QUALITY = 85

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".gif", ".bmp"}

ALT_FIELD = "XMP-iptcCore:AltTextAccessibility"
EXT_FIELD = "XMP-iptcCore:ExtDescrAccessibility"
LEGACY_FIELD = "XMP-dc:description"

DEFAULT_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "qwen3-vl:2b")

SYSTEM_PROMPT = """You write alt text for a digitised heritage library collection. The alt text is embedded in image metadata so the collection becomes searchable and accessible to screen reader users.

Describe only what is visibly present. This is the single most important rule: these are historical materials and a confident-sounding invention becomes a catalogue error that is very hard to undo later. Do not infer dates, places, events, occupations or relationships that you cannot actually see. Do not attempt to name any individual. If something is ambiguous, describe its appearance rather than asserting what it is -- "a man in a dark uniform with metal buttons", not "a Union soldier".

Transcribe visible text exactly: signage, shop names, banners, placards, inscriptions, handwriting, headlines. Researchers search on these strings more than anything else, so they carry real indexing weight.

Return a JSON object with exactly these keys:

  "alt_text"     A single sentence for the HTML alt attribute, describing what the image shows. Aim for under {max_chars} characters. Lead with the subject. Do not begin with "Image of", "Photograph of", "A picture showing" or similar -- screen readers already announce that it is an image. This field is a description, not a transcription: name the kind of document or scene and its most salient visible features, and leave the full text to "extended".

  "extended"     This field is primarily a TRANSCRIPTION. What goes in it depends on the image:

                 If the image contains a significant amount of text -- a document, certificate, record, form, letter, headstone, plaque, poster, flyer, newspaper page, or map with labels -- transcribe every legible word, verbatim. Preserve the original spelling, capitalisation, punctuation and abbreviations exactly as written, including archaic or non-standard forms; do not modernise, correct or normalise anything. Follow the reading order of the page, and use newline characters to separate lines and blocks as they appear. Mark anything you genuinely cannot read as [illegible], and an uncertain reading as [word?]. Do not paraphrase, summarise, or skip repetitive parts such as printed form boilerplate.

                 Annotations ADDED to the item are part of it and must be transcribed too: handwritten notes in the margins or below a clipping, a newspaper name and date written in pen beside an article (for example "Island Packet Mar 16, 2014, p 4A"), stamps, accession or catalogue numbers, and labels or captions written on a mount or the back of a print. These record where the item came from and are often the most important text for a researcher. Put them on their own line at the end of the transcription, prefixed "Annotation: ", so they are distinguishable from the printed text. After the transcription, add one short sentence describing the artefact's physical form and condition (for example: a single-sided printed form completed in brown ink, with staining along the lower edge and a torn upper-right corner).

                 If the image contains little or no text -- a photograph, portrait, street scene, landscape, or artwork -- write a detailed visual description instead: setting, composition, number and arrangement of people, clothing, buildings, vehicles, objects, and the condition of the artefact. Favour concrete searchable nouns over interpretation. Transcribe any text that is present, such as shop signage or a caption written on the print.

  "visible_text" A plain string of the text in the image, with no transcription markup, newlines collapsed to single spaces, and no physical description. This is a simple keyword-search field, so it will often duplicate part of "extended". Use "" if the image has no legible text at all.

  "confidence"   "high", "medium" or "low" -- your confidence that the description is accurate and useful. Use "low" for images that are badly degraded, near-illegible, or that you found genuinely hard to read, so a human reviewer knows where to look first.

Return only the JSON object, with no preamble and no markdown fences."""

USER_PROMPT = "Write the alt text metadata for this image."

REPORT_FIELDS = [
    "file", "status", "alt_text", "extended", "visible_text",
    "confidence", "chars", "seconds", "error",
]

_print_lock = Lock()
_csv_lock = Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# --------------------------------------------------------------------------
# image preparation
# --------------------------------------------------------------------------

def prepare_image(path: Path, max_edge: int = MAX_EDGE) -> str:
    """Return a downscaled JPEG of `path` as base64, ready for the Ollama API."""
    with Image.open(path) as im:
        # Honour the EXIF orientation flag, otherwise phone/scanner images get
        # described sideways.
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            im = im.convert("RGB")
        im.thumbnail((max_edge, max_edge), Image.LANCZOS)

        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=JPEG_QUALITY)
        return base64.b64encode(buf.getvalue()).decode("ascii")


# --------------------------------------------------------------------------
# exiftool
# --------------------------------------------------------------------------

def exiftool_cmd() -> list[str] | None:
    exe = shutil.which("exiftool")
    return [exe] if exe else None


def exiftool_available() -> bool:
    return exiftool_cmd() is not None


def read_existing_alt(files: list[Path]) -> dict[Path, str]:
    """Bulk-read the existing accessibility alt field for `files`.

    One exiftool invocation for the whole batch -- process startup dominates
    exiftool's cost, so per-file calls here would be slower than the model.
    Arguments go through a temp -@ argfile to stay clear of command-line
    length limits on a thousand-image collection.
    """
    cmd = exiftool_cmd()
    if not cmd or not files:
        return {}

    argfile = Path(f".exiftool_args_{os.getpid()}.txt")
    try:
        with open(argfile, "w", encoding="utf-8") as fh:
            fh.write("-j\n-charset\nfilename=utf8\n")
            fh.write(f"-{ALT_FIELD}\n")
            for f in files:
                fh.write(f"{f}\n")

        result = subprocess.run(
            cmd + ["-@", str(argfile)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return {}
    finally:
        argfile.unlink(missing_ok=True)

    if not result.stdout.strip():
        return {}

    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}

    out: dict[Path, str] = {}
    for entry in entries:
        src = entry.get("SourceFile")
        if not src:
            continue
        value = entry.get("AltTextAccessibility") or ""
        if isinstance(value, dict):        # lang-alt fields come back as dicts
            value = next(iter(value.values()), "") if value else ""
        out[Path(src)] = str(value).strip()
    return out


def write_metadata(path: Path, alt: str, extended: str, legacy: bool) -> None:
    """Embed the accessibility fields into `path` via exiftool."""
    cmd = exiftool_cmd()
    if not cmd:
        raise RuntimeError("exiftool not found on PATH")

    # Values go in through -TAG<=FILE and everything else through a -@ argfile,
    # never on the command line. On Windows exiftool is often exiftool.bat, and
    # cmd.exe cuts arguments at the first newline -- a multi-line transcription
    # swallowed the filename ("No file specified") -- and mangles % & ^ besides.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        values = {ALT_FIELD: alt, EXT_FIELD: extended}
        if legacy:
            values[LEGACY_FIELD] = alt
        lines = ["-charset", "filename=utf8", "-overwrite_original"]
        for i, (tag, value) in enumerate(values.items()):
            vf = tmp / f"value{i}.txt"
            vf.write_bytes(value.encode("utf-8"))   # bytes: no \r\n on Windows
            lines.append(f"-{tag}<={vf}")
        lines.append(str(path))
        argfile = tmp / "args.txt"
        argfile.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = subprocess.run(cmd + ["-@", str(argfile)], capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:300])


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------

def _extract_payload(data: dict) -> str:
    """Pull the model's text out of an Ollama response.

    Reasoning-capable models (qwen3-vl among them) put their output in
    `thinking` and leave `message.content` empty, and they do this even when
    think=False is requested. Reading only the documented field would silently
    produce blank alt text for every image, so accept either.
    """
    message = data.get("message") or {}
    for candidate in (
        message.get("content"),
        data.get("response"),
        message.get("thinking"),
        data.get("thinking"),
    ):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def _salvage_truncated(text: str) -> dict | None:
    """Recover the complete leading keys from a JSON object cut off mid-write.

    Small models routinely run out of num_predict partway through `extended`.
    The `alt_text` that precedes it is usually complete and perfectly good, and
    discarding it would fail an image that we actually captioned successfully.
    """
    out: dict[str, str] = {}
    for key in ("alt_text", "extended", "visible_text", "confidence"):
        # Match "key": "...", tolerating escaped quotes inside the value.
        marker = f'"{key}"'
        i = text.find(marker)
        if i == -1:
            continue
        j = text.find('"', i + len(marker) + text[i + len(marker):].find(":") + 1)
        if j == -1:
            continue
        buf, k, closed = [], j + 1, False
        while k < len(text):
            ch = text[k]
            if ch == "\\" and k + 1 < len(text):
                buf.append(text[k:k + 2])
                k += 2
                continue
            if ch == '"':
                closed = True
                break
            buf.append(ch)
            k += 1
        if closed:
            try:
                out[key] = json.loads('"' + "".join(buf) + '"')
            except json.JSONDecodeError:
                continue
    return out or None


def _coerce_json(text: str) -> dict:
    """Parse the model's reply, tolerating fences, prose, and truncation."""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) > 1:
            text = parts[1]
        text = text.strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                parsed = _salvage_truncated(text)
        else:
            parsed = _salvage_truncated(text)
        if parsed is None:
            raise ValueError(f"no JSON object in model reply: {text[:200]}")

    if not isinstance(parsed, dict):
        raise ValueError(f"model returned {type(parsed).__name__}, expected an object")
    return parsed


def _first_sentence(text: str, max_chars: int) -> str:
    """Trim alt text to roughly one sentence within `max_chars`.

    The short field is what a screen reader announces, so an over-long one is a
    real accessibility problem; the full detail is preserved in `extended`.
    """
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text

    cut = text[:max_chars]
    for end in (". ", "? ", "! "):
        i = cut.rfind(end)
        if i > max_chars * 0.4:
            return cut[:i + 1].strip()
    # No sentence break to fall back on: trim to a word boundary, leaving room
    # for the ellipsis so max_chars stays a hard bound.
    cut = text[:max_chars - 3]
    i = cut.rfind(" ")
    return (cut[:i] if i > 0 else cut).rstrip(" ,;:") + "..."


def generate_alt_text(host: str, model: str, b64: str, max_chars: int,
                      context: str, timeout: int, num_predict: int = 4096,
                      max_retries: int = 3) -> dict:
    """Ask the model for alt text. Returns the parsed JSON dict."""
    system = SYSTEM_PROMPT.format(max_chars=max_chars)
    if context:
        system += f"\n\nContext about this collection: {context}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": USER_PROMPT, "images": [b64]},
        ],
        "stream": False,
        "format": "json",
        "think": False,
        # A dense page of handwriting transcribed verbatim runs long; too low a
        # cap here truncates mid-transcription and lands in _salvage_truncated.
        "options": {"temperature": 0.2, "num_predict": num_predict},
    }

    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            r = requests.post(f"{host}/api/chat", json=payload, timeout=timeout)
            r.raise_for_status()
            raw = _extract_payload(r.json())
            if not raw:
                raise ValueError("model returned an empty reply")
            data = _coerce_json(raw)
            if not str(data.get("alt_text", "") or "").strip():
                raise ValueError("model returned no alt_text")
            return data
        except Exception as e:                      # noqa: BLE001 - retry anything
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)            # 1s, then 2s

    raise RuntimeError(f"after {max_retries} attempts: {last_err}")


# --------------------------------------------------------------------------
# per-file work
# --------------------------------------------------------------------------

def process_file(src: Path, target: Path, args, existing: str) -> dict:
    """Caption one image and write its metadata. Returns a report row."""
    row = {k: "" for k in REPORT_FIELDS}
    row["file"] = str(src)
    started = time.time()

    if existing and not args.overwrite:
        row["status"] = "skipped-has-alt"
        row["alt_text"] = existing
        return row

    try:
        b64 = prepare_image(src, args.max_edge)
    except Exception as e:                          # noqa: BLE001
        row["status"] = "error"
        row["error"] = f"image read failed: {e}"[:300]
        return row

    try:
        data = generate_alt_text(
            args.host, args.model, b64, args.max_chars,
            args.collection_context, args.timeout, args.num_predict,
        )
    except Exception as e:                          # noqa: BLE001
        row["status"] = "error"
        row["error"] = str(e)[:300]
        return row

    raw_alt = str(data.get("alt_text", "") or "").strip()
    extended = str(data.get("extended", "") or "").strip()
    visible = str(data.get("visible_text", "") or "").strip()
    confidence = str(data.get("confidence", "") or "").strip().lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "unknown"

    # visible_text is the flat keyword-search field: no line structure.
    visible = " ".join(visible.split())

    # Models overshoot the length target routinely, so the announced field gets
    # trimmed. Do NOT fold the overflow into `extended` -- that field is now a
    # verbatim transcription, and prepending a caption to it would corrupt the
    # first line of the transcribed document.
    alt = _first_sentence(raw_alt, args.max_chars)

    row.update({
        "alt_text": alt, "extended": extended, "visible_text": visible,
        "confidence": confidence, "chars": len(alt),
        "seconds": f"{time.time() - started:.1f}",
    })

    if args.dry_run:
        row["status"] = "dry-run"
        return row

    try:
        if not args.in_place:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        write_metadata(target, alt, extended, not args.no_legacy_fields)
        row["status"] = "tagged"
        row["file"] = str(target)
    except Exception as e:                          # noqa: BLE001
        row["status"] = "error"
        row["error"] = f"metadata write failed: {e}"[:300]
        # Don't leave a copy that has no metadata -- it would look done on the
        # next resume pass and get skipped forever.
        if not args.in_place and target.exists():
            target.unlink(missing_ok=True)

    return row


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def _has_vision(host: str, model: str) -> bool:
    """Whether `model` reports the vision capability. Best effort."""
    try:
        r = requests.post(f"{host}/api/show", json={"model": model}, timeout=30)
        return "vision" in (r.json().get("capabilities") or [])
    except Exception:                               # noqa: BLE001
        return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate alt text with a local Ollama vision model and "
                    "embed it in IPTC/XMP metadata.",
    )
    p.add_argument("input_dir", nargs="?", default=os.getenv("INPUT_DIR"),
                   help="folder of images (searched recursively); "
                        "defaults to INPUT_DIR from .env")
    p.add_argument("--out-dir", default=os.getenv("OUT_DIR"),
                   help="write tagged copies here, leaving originals untouched; "
                        "defaults to OUT_DIR from .env")
    p.add_argument("--in-place", action="store_true",
                   help="modify the originals instead of copying")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"Ollama server URL (default: {DEFAULT_HOST})")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"vision model id (default: {DEFAULT_MODEL})")
    p.add_argument("--workers", type=int, default=2,
                   help="concurrent requests (default: 2; local models are "
                        "GPU-bound, so more is not always faster)")
    p.add_argument("--max-chars", type=int, default=250,
                   help="target max length for the short alt text (default: 250)")
    p.add_argument("--max-edge", type=int, default=MAX_EDGE,
                   help=f"downscale images to this longest edge before sending "
                        f"(default: {MAX_EDGE}). Raising it to 1568 or 2048 "
                        f"materially improves handwriting transcription, at the "
                        f"cost of speed and VRAM")
    p.add_argument("--num-predict", type=int, default=4096,
                   help="max tokens the model may generate per image "
                        "(default: 4096; a dense verbatim transcription needs "
                        "far more than a short caption)")
    p.add_argument("--collection-context", default="",
                   help="what you know about the collection -- improves accuracy a lot")
    p.add_argument("--timeout", type=int, default=300,
                   help="per-request timeout in seconds (default: 300)")
    p.add_argument("--overwrite", action="store_true",
                   help="re-caption files that already carry alt text")
    p.add_argument("--no-legacy-fields", action="store_true",
                   help="don't also write XMP-dc:description")
    p.add_argument("--dry-run", action="store_true",
                   help="generate and report, but write no metadata")
    p.add_argument("--only", metavar="LIST",
                   help="process only the images named in this file, one id or "
                        "filename per line (see pipeline/find_retries.py). "
                        "Combine with --overwrite to redo already-tagged files")
    p.add_argument("--limit", type=int,
                   help="stop after N files (use this first)")
    p.add_argument("--report", default="alt_text_report.csv",
                   help="CSV review log (default: alt_text_report.csv)")
    return p


def main() -> int:
    args = build_parser().parse_args()

    if not args.input_dir:
        return err("no input directory given, and INPUT_DIR is not set in .env")
    in_dir = Path(args.input_dir)
    if not in_dir.is_dir():
        return err(f"not a directory: {in_dir}")

    if not args.in_place and not args.out_dir and not args.dry_run:
        return err("pass --out-dir to write tagged copies, or --in-place to "
                   "modify the originals")

    if not args.dry_run and not exiftool_available():
        return err("exiftool not found on PATH -- it is required to write metadata")

    # Fail fast on a bad host or a model that was never pulled, rather than
    # after a long run of identical errors.
    try:
        tags = requests.get(f"{args.host}/api/tags", timeout=10).json()
        names = {m["name"] for m in tags.get("models", [])}
    except Exception as e:                          # noqa: BLE001
        return err(f"cannot reach Ollama at {args.host}: {e}\n"
                   f"       is it running?  try:  ollama serve")
    if args.model not in names:
        return err(f"model {args.model!r} is not pulled on {args.host}\n"
                   f"       available: {', '.join(sorted(names)) or '(none)'}\n"
                   f"       pull it with:  ollama pull {args.model}")

    # Being pulled is not enough -- a text-only model accepts the request and
    # then fails per-image with a 400, which would otherwise mean a thousand
    # identical errors before anyone noticed.
    try:
        shown = requests.post(f"{args.host}/api/show",
                              json={"model": args.model}, timeout=30).json()
        caps = shown.get("capabilities") or []
    except Exception:                               # noqa: BLE001
        caps = []                                   # older servers omit it
    if caps and "vision" not in caps:
        vision = sorted(n for n in names if _has_vision(args.host, n))
        return err(f"model {args.model!r} has no vision capability "
                   f"(it reports: {', '.join(caps)})\n"
                   f"       vision models pulled here: {', '.join(vision) or '(none)'}\n"
                   f"       pull one with:  ollama pull qwen3-vl:2b")

    files = sorted(p for p in in_dir.rglob("*")
                   if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS)
    if not files:
        return err(f"no images found under {in_dir}")

    if args.only:
        try:
            wanted = {line.strip() for line in open(args.only, encoding="utf-8")
                      if line.strip() and not line.startswith("#")}
        except OSError as e:
            return err(f"cannot read --only list: {e}")
        # Accept bare ids ("6013") or filenames ("6013.jpg") in the list.
        files = [p for p in files if p.stem in wanted or p.name in wanted]
        if not files:
            return err(f"none of the {len(wanted)} entries in {args.only} "
                       f"matched an image under {in_dir}")

    existing = {} if args.overwrite else read_existing_alt(files)

    # Resume: in copy mode a file already present in out-dir is done, so a
    # re-run after an interruption picks up where it left off.
    out_dir = Path(args.out_dir) if args.out_dir else None
    targets: list[tuple[Path, Path]] = []
    for src in files:
        if args.in_place or not out_dir:
            target = src
        else:
            target = out_dir / src.relative_to(in_dir)
            if not args.dry_run and target.exists() and not args.overwrite:
                continue
        targets.append((src, target))
        if args.limit and len(targets) >= args.limit:
            break

    if not targets:
        log("nothing to do -- every file is already tagged in the output folder")
        return 0

    mode = ("dry run" if args.dry_run
            else "in place" if args.in_place
            else f"copies -> {out_dir}")
    log(f"{len(targets)} image(s) | model {args.model} | "
        f"{args.workers} worker(s) | {mode}")

    report_path = Path(args.report)
    new_report = not report_path.exists()
    counts: dict[str, int] = {}
    done = 0

    with open(report_path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_FIELDS)
        if new_report:
            writer.writeheader()

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(process_file, src, target, args,
                            existing.get(src, "")): src
                for src, target in targets
            }
            try:
                for future in as_completed(futures):
                    row = future.result()
                    counts[row["status"]] = counts.get(row["status"], 0) + 1
                    done += 1

                    with _csv_lock:
                        writer.writerow(row)
                        fh.flush()          # survive a Ctrl-C mid-run

                    name = Path(futures[future]).name
                    if row["status"] == "error":
                        log(f"[{done}/{len(targets)}] !! {name}: {row['error']}")
                    elif row["status"] == "skipped-has-alt":
                        log(f"[{done}/{len(targets)}] -- {name} (already has alt text)")
                    else:
                        log(f"[{done}/{len(targets)}] {name} "
                            f"[{row['confidence']}] {row['alt_text'][:70]}")
            except KeyboardInterrupt:
                log("\nInterrupted. Re-run the same command to resume.")
                pool.shutdown(wait=False, cancel_futures=True)
                return 130

    summary = "  ".join(f"{k}: {v}" for k, v in sorted(counts.items()) if v)
    log(f"\n{summary}\nreport: {report_path}")
    if counts.get("error"):
        log("review the error rows in the report; re-running retries only the "
            "files that were not written")
    if not args.dry_run:
        log("auto-generated alt text should be reviewed before publication -- "
            "sort the report by confidence and start at 'low'")
    return 0


def err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)

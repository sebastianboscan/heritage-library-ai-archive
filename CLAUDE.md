# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A four-stage batch pipeline that generates accessibility alt text for a digitised
heritage image collection hosted in WordPress:

1. **Pull** (`pipeline/wp_media_pull.py`) — download images missing alt text from
   the WordPress REST API into `library_images/`, plus an `inventory.csv` manifest.
2. **Tag** (`pipeline/alt_text_tagger.py`) — caption each image with a local
   Ollama vision model and embed the result into the file's IPTC/XMP accessibility
   metadata via `exiftool`.
3. **Extract** (`pipeline/extract_alt_inventory.py`) — read that metadata back out
   of the tagged files into a flat CSV keyed by WordPress attachment id.
4. **Upload** (`pipeline/wp_upload_alt.py`) — publish that CSV back to the site:
   alt text to the attachment's alt, transcription to its description.

Between 2 and 3 a human reviews the output, using `review_app.py` (a Flask
viewer on port 60606). `pipeline/find_retries.py` picks rows from a tagger report
worth re-running.

Git repo on GitHub, intended to go public. No test suite, linter, or build step —
these are scripts run directly against a live staging site and a local model.
The tagger in practice runs on a separate Linux GPU server (two RTX 4000 Ada,
`qwen2.5vl:7b`); the Windows machine is for development and has only an 8GB GPU.

## Environment

Python 3.10 venv at `.venv/` (3.9+ required: the code uses `list[...]` annotations
and `str.removeprefix`).

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Two things are not pip packages and must be installed separately:

- **exiftool** on PATH — required to write metadata. Note it may be installed as
  `exiftool.bat`, which Python's `shutil.which` finds but a bash shell on PATH may
  not; check with `where exiftool` rather than `which`.
- **ollama** running, with a vision model pulled: `ollama pull qwen3-vl:2b`.

## Running

```powershell
python pipeline\wp_media_pull.py     # stage 1: download + inventory.csv
python misc\wp_media_count.py        # how many files of each type exist on the site
python misc\wp_access_test.py        # verify credentials and edit_posts capability

# stage 2 — always start with a small --limit and review the CSV
python pipeline\alt_text_tagger.py library_images --out-dir tagged --limit 10

# re-run only problem rows from a previous report
python pipeline\find_retries.py alt_text_report.csv -o retry.txt
python pipeline\alt_text_tagger.py --only retry.txt --overwrite ...

# stage 3 — read the embedded metadata back into a flat CSV
python pipeline\extract_alt_inventory.py tagged -o alt_inventory.csv

# stage 4 — publish; dry run unless --apply, undo with --restore
python pipeline\wp_upload_alt.py alt_inventory.csv --limit 5
python pipeline\wp_upload_alt.py alt_inventory.csv --apply
python pipeline\wp_upload_alt.py --restore upload_log.csv --apply

# review UI
python review_app.py tagged --host 0.0.0.0
```

`wp_media_pull.py` has no CLI; behaviour is set by module constants at the top
(`LIMIT`, `MISSING_ALT_ONLY`, `TARGET_WIDTH`, `DELAY`). Rate limiting is a bare
`time.sleep(DELAY)` between requests.

`TARGET_WIDTH = None` (the default) downloads the uploaded original. Set it to an
integer to fetch the smallest WordPress rendition at least that wide instead.
This matters for transcription: ~73% of this collection's originals are 2000px or
wider (up to 4000px), and the earlier `TARGET_WIDTH = 1024` meant the model saw a
1024px copy of a handwritten form. Note that `--max-edge` in stage 2 can only
downscale, so it cannot recover detail that was never downloaded.

Both stages are **resumable**, and this is the intended recovery path after an
interruption — just re-run the same command:

- Stage 1 skips images already on disk.
- Stage 2 skips files already present in `--out-dir`, and (via a bulk exiftool
  read) any file that already carries alt text. `--limit N` therefore means "do N
  *more* files", not "do the first N" — repeated runs with `--limit 50` walk
  through the collection in batches.

## Stage 2 details

Writes three fields, following the IPTC Photo Metadata Standard:

| Field | Purpose |
| --- | --- |
| `XMP-iptcCore:AltTextAccessibility` | short alt text — the important one |
| `XMP-iptcCore:ExtDescrAccessibility` | longer extended description |
| `XMP-dc:description` | legacy caption for older DAMS (`--no-legacy-fields` disables) |

Defaults to `qwen3-vl:2b` — the only vision model currently pulled. Override with
`--model` / `OLLAMA_MODEL`, host with `--host` / `OLLAMA_HOST`.

Preflight checks run before any work and fail fast: server reachable, model
pulled, and **model has the `vision` capability**. That last check matters —
`llama3.1:8b` is pulled and passes a naive "is it installed" test, but then returns
a 400 on every single image. The capability comes from `POST /api/show`.

### Two model quirks the code works around

Both were found by testing against the real model; don't "simplify" them away:

- **Output arrives in `thinking`, not `content`.** `qwen3-vl` is a reasoning model
  and puts its JSON in the `thinking` field, leaving `message.content` empty — and
  it does this even when `think: false` is set. `_extract_payload` reads both.
  Reading only the documented field silently writes blank alt text for every image.
- **Replies get truncated mid-JSON** when the model exhausts `num_predict`, which
  happens on dense documents. `_salvage_truncated` recovers the complete leading
  keys, so an image with a perfectly good `alt_text` isn't failed because
  `extended` was cut off.

### What each field is for

The two text fields have deliberately different jobs, and the prompt enforces the
split:

- **`alt_text`** is a *description* — what the image shows, one sentence, for a
  screen reader. It is trimmed to `--max-chars` (default 250) at a sentence
  boundary; models overshoot the target routinely.
- **`extended`** is primarily a *transcription*. On a document, certificate,
  form, poster or labelled map it is every legible word verbatim, preserving
  original spelling and line structure, with `[illegible]` / `[word?]` markers,
  followed by one sentence on physical form and condition. On a photograph with
  little text it falls back to detailed visual description.
- **`visible_text`** is a flat keyword-search field: same text, newlines
  collapsed, no markup. It duplicates part of `extended` by design.

Do not fold overflowed `alt_text` into `extended` — that field is a verbatim
transcription and prepending a caption corrupts the document's first line.

`--num-predict` (default 4096) must stay generous: a dense handwritten page
transcribed verbatim needs far more tokens than a caption, and running out lands
in `_salvage_truncated` mid-transcription.

`--max-edge` (default 1024) is the single biggest lever on transcription
accuracy. 1568 or 2048 materially improves handwriting at the cost of speed and
VRAM; small models also tend to ignore the line-break and `[illegible]`
instructions, which larger ones honour better.

Throughput is roughly 20–75s per image on `qwen3-vl:2b`; `--workers` defaults to 2
because local models are GPU-bound and more workers is not reliably faster. A full
run over ~1050 images is measured in hours, not minutes.

## Stage 3 details

Emits `id, file, alt_text, ext_text, alt_chars, status`, sorted by numeric id.
`--inventory library_images/inventory.csv` joins on id to add `source_url`,
`mime_type`, and `title`.

It reads **the image files, not the tagger's run report** — so it reflects what is
actually embedded, including any human edits made in Lightroom/Bridge/exiftool
after tagging. That is the point of the split: the report says what the model
produced, this says what the collection currently carries.

The id is the filename stem, because `wp_media_pull.py` downloads as
`<attachment id>.<ext>` specifically so ids survive the round trip. A non-numeric
name still gets a row (empty id, `bad-id` in status) rather than being dropped —
a silently missing file would read as a coverage gap later.

`status` is one of `tagged`, `legacy-only` (alt found only in `XMP-dc:description`;
disable that fallback with `--no-legacy-fallback`), or `no-alt`, and may carry a
`;bad-id` suffix. By default every image is listed so gaps stay visible;
`--only-tagged` drops the empty ones.

One bulk exiftool call via a temp argfile handles the whole collection — 1046
images in about 5 seconds. Don't refactor this into per-file calls: process
startup dominates exiftool's cost.

## Stage 4 details

Dry run by default; `--apply` is the only thing that writes. Its guard rails are
deliberate — the target is a live site whose existing content may be hand-written:

- Alt text already set on the site is left alone unless `--overwrite-alt`
  (someone may have written it after the pull).
- The transcription goes to the attachment's **description**, and only where that
  is empty, unless `--description overwrite`. The **caption** is never written:
  many themes print it publicly under the image.
- The description is escaped HTML with `<p>`/`<br>` preserving line structure —
  a transcribed `<` must not become markup.
- Every write appends the previous values to `upload_log.csv`. `--restore LOG`
  undoes them, but only for fields that still hold what was written, so later
  hand edits survive.

Input is normally stage 3's output (what the files carry after review). The
tagger's report is also accepted (`file`/`extended` columns), but reflects only
what the model produced.

## Re-running problem rows

`find_retries.py` selects from a report: error rows, rows with empty `extended`
(dense pages whose reply was cut off before the transcription closed), and
newspaper clippings. It deliberately does **not** select `confidence=unknown`
rows that have text: their reply was cut after `extended` closed, so the
transcription is complete and only `visible_text`/`confidence` were lost.

Clippings are selected because runs before the annotation prompt change skipped
the handwritten source citation beside a clipping ("Island Packet Mar 16, 2014,
p 4A"). The prompt now asks for these on a trailing `Annotation:` line.

## Domain constraints for alt text

These live in `SYSTEM_PROMPT` and matter more than anything mechanical:

- **Describe only what is visibly present.** These are historical materials; a
  confident-sounding invention becomes a catalogue error that is hard to undo.
  Never infer dates, places, events, or occupations that aren't visible, and never
  name individuals. Describe appearance instead — "a man in a dark uniform with
  metal buttons", not "a Union soldier".
- **Transcribe annotations added to the item** — pen notes naming the newspaper
  and date beside a clipping, stamps, accession numbers. They record provenance.
- **Transcribe visible text exactly** (signage, banners, inscriptions, handwriting).
  Researchers search these strings more than anything else. This is where most of
  the collection's research value is: a caption like "Refugees' Quarters, Marion
  Head, S.C. Oct. 27th 1864" is worth more than any description of the scene.
- Don't open alt text with "Image of" / "Photograph of" — screen readers already
  announce it.
- Every run appends to a CSV report because auto-generated alt text for a heritage
  collection is meant to be **human-reviewed before publication**. The `confidence`
  column tells reviewers where to start; `unknown` means the model's own confidence
  value was missing or unparseable, which usually accompanies a salvaged reply.
- `--collection-context` substantially improves output quality; pass what is known
  about the collection.

## Credentials

`.env` (gitignored) holds `WP_USER`, `WP_KEY`, `WP_BASE_URL`, and optionally
`OLLAMA_HOST`, `OLLAMA_MODEL`, `INPUT_DIR`, `OUT_DIR`. `.env.example` documents
the format and is committed.

**Nothing in tracked source names the site.** `wp_config.py` at the project root
is the single place WordPress settings are read; every script that talks to the
site imports it rather than calling `os.getenv` itself:

```python
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wp_config
BASE = wp_config.base_url()      # exits with a clear message if unset
auth = wp_config.auth()
```

The `sys.path` line is needed because scripts live in `misc/` and `pipeline/`;
Python puts the *script's* directory on the path, not the project root. Keep this
intact — the repo is intended to go public, so no hostname, username, or key
should reappear in source, not even as a fallback default to `os.getenv`.

`wp_config` loads `.env` by absolute path from the project root, so scripts
behave the same regardless of the working directory they are run from.

Treat anything that lands in git history as permanently public.

## Safety when working against the site and the originals

- Stage 2 defaults to copy mode; prefer `--out-dir` over `--in-place`. On a
  metadata-write failure the partial copy is deleted, so a failed file doesn't look
  finished to the next resume pass.
- `--in-place` uses `-overwrite_original`, so it does **not** leave exiftool's
  `_original` backups. `library_images/` is re-downloadable from WordPress, but
  never point `--in-place` at the only copy of anything else.
- Use `--dry-run` and a small `--limit` before committing to a long run.

## Directories

- `pipeline/` — the real pipeline stages.
- `misc/` — scratch and diagnostic scripts, including unfinished ones
  (`sebastian_pull.py` is a stub with TODO comments). Don't treat these as
  reference implementations.
- `library_images/` — gitignored; ~1050 downloaded images plus `inventory.csv`,
  keyed by WordPress attachment ID so filenames can't collide. These are the
  untagged originals; running stage 3 against this folder correctly reports
  everything as `no-alt`.
- `tagged/` — gitignored; default output of stage 2 and default input of stage 3.
- `sitegpt/` — builds knowledge files for a SiteGPT chatbot. Only the two scripts
  are tracked: `fetch_certificates.py` pulls the land-certificate posts to `src/`,
  `build_export.py` writes `out/*.txt` from `retry22/results_merged.csv` +
  `inventory.csv`. Everything else there (exports, checklists, link lists) holds
  collection data or the hostname and stays ignored via `sitegpt/.gitignore`.

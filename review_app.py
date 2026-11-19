"""
review_app.py
=============

A small Flask app for eyeballing tagged images next to their alt text and
transcription. Auto-generated alt text for a heritage collection is meant to be
reviewed before publication, and reading transcriptions in a CSV cell is
miserable -- this puts the image and the text side by side.

Read-only: it never writes to the images. Edit metadata with exiftool, or
re-run the tagger with --overwrite.

Usage
-----
    python review_app.py                    # serves ./tagged (or OUT_DIR from .env)
    python review_app.py tagged --port 60606
    python review_app.py --host 0.0.0.0     # reachable from another machine

Then open http://localhost:60606
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from flask import Flask, Response, abort, request, send_file
except ImportError:
    sys.exit("Flask is required:  pip install flask")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ALT_KEY = "AltTextAccessibility"
EXT_KEY = "ExtDescrAccessibility"
LEGACY_KEY = "Description"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".gif", ".bmp"}

PER_PAGE = 25

app = Flask(__name__)
IMAGE_DIR = Path("tagged")

# exiftool is slow to start, so read the whole folder once and cache it.
_cache: dict | None = None


def flatten(value) -> str:
    """Normalise one exiftool value to a string (lang-alt fields can be dicts)."""
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


def scan() -> list[dict]:
    """Read metadata for every image in IMAGE_DIR, via one bulk exiftool call."""
    files = sorted(
        (p for p in IMAGE_DIR.rglob("*")
         if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS),
        key=lambda p: (not p.stem.isdigit(), int(p.stem) if p.stem.isdigit() else 0),
    )
    if not files:
        return []

    exe = shutil.which("exiftool")
    if not exe:
        # Still show the images, just without any text.
        return [{"id": p.stem, "rel": str(p.relative_to(IMAGE_DIR)).replace("\\", "/"),
                 "alt": "", "ext": "", "err": "exiftool not found on PATH"}
                for p in files]

    argfile = Path(f".exiftool_review_{os.getpid()}.txt")
    try:
        with open(argfile, "w", encoding="utf-8") as fh:
            fh.write("-j\n-charset\nfilename=utf8\n")
            for field in ("XMP-iptcCore:AltTextAccessibility",
                          "XMP-iptcCore:ExtDescrAccessibility",
                          "XMP-dc:description"):
                fh.write(f"-{field}\n")
            for f in files:
                fh.write(f"{f}\n")
        result = subprocess.run([exe, "-@", str(argfile)], capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
        entries = json.loads(result.stdout or "[]")
    except (OSError, json.JSONDecodeError):
        entries = []
    finally:
        argfile.unlink(missing_ok=True)

    meta = {e["SourceFile"]: e for e in entries if e.get("SourceFile")}

    rows = []
    for p in files:
        entry = meta.get(str(p)) or meta.get(p.as_posix()) or {}
        alt = flatten(entry.get(ALT_KEY)) or flatten(entry.get(LEGACY_KEY))
        rows.append({
            "id": p.stem,
            "rel": str(p.relative_to(IMAGE_DIR)).replace("\\", "/"),
            "alt": alt,
            "ext": flatten(entry.get(EXT_KEY)),
            "err": "",
        })
    return rows


def rows(refresh: bool = False) -> list[dict]:
    global _cache
    if _cache is None or refresh:
        _cache = {"rows": scan()}
    return _cache["rows"]


PAGE_CSS = """
:root { color-scheme: light dark; --bg:#fbfaf7; --fg:#1c1a17; --mut:#6b6459;
        --line:#e2ddd3; --card:#fff; --warn:#8a5a00; --warnbg:#fff6e5; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161513; --fg:#eceae6; --mut:#9a9389; --line:#302d28;
          --card:#1e1c19; --warn:#e5b55c; --warnbg:#2a2213; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:15px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
header { position:sticky; top:0; z-index:5; background:var(--bg);
         border-bottom:1px solid var(--line); padding:14px 20px;
         display:flex; gap:16px; align-items:baseline; flex-wrap:wrap; }
h1 { font-size:16px; margin:0; font-weight:650; letter-spacing:-.01em; }
.meta { color:var(--mut); font-size:13px; }
a { color:inherit; }
main { padding:20px; max-width:1500px; margin:0 auto; }
.row { display:grid; grid-template-columns:minmax(220px,340px) 1fr; gap:22px;
       background:var(--card); border:1px solid var(--line); border-radius:10px;
       padding:16px; margin-bottom:16px; }
@media (max-width:820px){ .row { grid-template-columns:1fr; } }
.row img { width:100%; height:auto; border-radius:6px; background:#0001;
           display:block; cursor:zoom-in; }
.row img:hover { outline:2px solid var(--mut); outline-offset:2px; }

/* lightbox */
#lb[hidden] { display:none !important; }
#lb { position:fixed; inset:0; z-index:50; background:#000000e8;
      display:flex; align-items:center; justify-content:center; cursor:zoom-out; }
/* Fill the viewport rather than just capping it: these are WordPress 1024px
   renditions and some are far smaller, so max-width alone would leave a
   300px scan floating in the middle of a 4K screen. object-fit:contain keeps
   the aspect ratio while scaling up. */
#lb img { width:96vw; height:92vh; object-fit:contain;
          border-radius:4px; box-shadow:0 8px 40px #0008;
          image-rendering:auto; }
/* click-to-magnify inside the lightbox, for reading handwriting */
#lb.mag { cursor:zoom-out; overflow:auto; display:block; }
#lb.mag img { max-width:none; max-height:none; width:auto; height:auto;
              object-fit:none; transform-origin:0 0; border-radius:0;
              /* these are upscaled past native resolution, so keep the pixels
                 crisp rather than letting the browser smear them */
              image-rendering:pixelated; }
#lbbar { position:fixed; top:0; left:0; right:0; padding:10px 16px;
         display:flex; gap:14px; align-items:center; justify-content:space-between;
         background:linear-gradient(#000000cc,#0000); color:#fff;
         font:13px ui-monospace,SFMono-Regular,Menlo,monospace; z-index:51;
         pointer-events:none; }
#lbbar b { font-weight:500; }
#lbbar .hint { opacity:.72; font-family:inherit; }
.idline { font:12px ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--mut);
          margin-top:8px; word-break:break-all; }
.lab { font-size:11px; text-transform:uppercase; letter-spacing:.08em;
       color:var(--mut); margin:0 0 4px; font-weight:600; }
.alt { margin:0 0 16px; }
.ext { margin:0; white-space:pre-wrap; font-size:14px; }
.empty { color:var(--warn); background:var(--warnbg); padding:6px 9px;
         border-radius:5px; display:inline-block; font-size:13px; }
.chars { color:var(--mut); font-size:12px; font-weight:400;
         text-transform:none; letter-spacing:0; }
nav { display:flex; gap:10px; align-items:center; padding:8px 0 30px; }
nav a, nav span { padding:6px 12px; border:1px solid var(--line);
                  border-radius:6px; text-decoration:none; font-size:14px; }
nav span { color:var(--mut); border-color:transparent; }
.note { color:var(--mut); font-size:13px; padding:40px 0; }
"""


LIGHTBOX_HTML = """
<div id=lb hidden>
  <div id=lbbar><b id=lbname></b>
    <span class=hint>click to magnify · ← → to move · Esc to close</span></div>
  <img id=lbimg alt=''>
</div>
<script>
(function () {
  var lb = document.getElementById('lb'),
      img = document.getElementById('lbimg'),
      name = document.getElementById('lbname'),
      thumbs = [].slice.call(document.querySelectorAll('.row img[data-full]')),
      at = -1;

  function show(i) {
    if (i < 0 || i >= thumbs.length) return;
    at = i;
    var t = thumbs[i];
    demag();
    img.src = t.getAttribute('data-full');
    name.textContent = t.getAttribute('data-name') + '  (' + (i + 1) + '/' + thumbs.length + ')';
    lb.hidden = false;
    document.body.style.overflow = 'hidden';
  }
  function close() {
    lb.hidden = true;
    demag();
    img.removeAttribute('src');       // stop decoding a large image once hidden
    document.body.style.overflow = '';
  }
  function demag() {
    lb.classList.remove('mag');
    img.style.width = img.style.height = img.style.transform = '';
  }

  // Second click magnifies around the clicked point so handwriting can be read
  // without leaving the page. These are 1024px (and often much smaller) WP
  // renditions, so 1:1 native would frequently be *smaller* than the filled
  // view -- scale relative to what is on screen instead.
  var ZOOM = 3;
  function magnify(e) {
    if (lb.classList.contains('mag')) { demag(); return; }
    var r = img.getBoundingClientRect(),
        fx = (e.clientX - r.left) / r.width,
        fy = (e.clientY - r.top) / r.height;
    // Clicks land on the letterboxed area for non-matching aspect ratios.
    if (fx < 0 || fx > 1 || fy < 0 || fy > 1) { close(); return; }

    var w = r.width * ZOOM, h = r.height * ZOOM;
    lb.classList.add('mag');
    img.style.width = w + 'px';
    img.style.height = h + 'px';
    lb.scrollLeft = Math.max(0, fx * w - lb.clientWidth / 2);
    lb.scrollTop = Math.max(0, fy * h - lb.clientHeight / 2);
  }

  thumbs.forEach(function (t, i) {
    t.addEventListener('click', function () { show(i); });
  });
  img.addEventListener('click', function (e) { e.stopPropagation(); magnify(e); });
  lb.addEventListener('click', close);
  document.addEventListener('keydown', function (e) {
    if (lb.hidden) return;
    if (e.key === 'Escape') close();
    else if (e.key === 'ArrowRight') show(at + 1);
    else if (e.key === 'ArrowLeft') show(at - 1);
  });
})();
</script>
"""


def esc(text: str) -> str:
    return html.escape(text or "")


@app.route("/")
def index():
    data = rows(refresh=request.args.get("refresh") == "1")
    only = request.args.get("only")
    if only == "untagged":
        data = [r for r in data if not r["alt"]]
    elif only == "tagged":
        data = [r for r in data if r["alt"]]

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    pages = max(1, (len(data) + PER_PAGE - 1) // PER_PAGE)
    page = min(page, pages)
    chunk = data[(page - 1) * PER_PAGE: page * PER_PAGE]

    total = len(rows())
    tagged = sum(1 for r in rows() if r["alt"])

    parts = [
        "<!doctype html><meta charset=utf-8>",
        "<meta name=viewport content='width=device-width,initial-scale=1'>",
        f"<title>Review — {esc(IMAGE_DIR.name)}</title><style>{PAGE_CSS}</style>",
        "<header><h1>Alt text review</h1>",
        f"<span class=meta>{esc(str(IMAGE_DIR))} · {tagged}/{total} tagged</span>",
        "<span class=meta>",
        "<a href='/'>all</a> · <a href='/?only=tagged'>tagged</a> · ",
        "<a href='/?only=untagged'>untagged</a> · <a href='/?refresh=1'>refresh</a>",
        "</span></header><main>",
    ]

    if not chunk:
        parts.append("<p class=note>No images to show. Point this at the tagger's "
                     "--out-dir, or run the tagger first.</p>")

    for r in chunk:
        alt_html = (f"<p class=alt>{esc(r['alt'])}</p>" if r["alt"]
                    else "<p class=alt><span class=empty>no alt text</span></p>")
        ext_html = (f"<p class=ext>{esc(r['ext'])}</p>" if r["ext"]
                    else "<p class=ext><span class=empty>no extended text</span></p>")
        chars = f"<span class=chars>{len(r['alt'])} chars</span>" if r["alt"] else ""
        parts.append(
            "<div class=row>"
            f"<div><img loading=lazy src='/img/{esc(r['rel'])}' alt='' "
            f"data-full='/img/{esc(r['rel'])}' data-name='{esc(r['rel'])}'>"
            f"<div class=idline>{esc(r['rel'])}</div></div>"
            f"<div><p class=lab>Alt text {chars}</p>{alt_html}"
            f"<p class=lab>Extended / transcription</p>{ext_html}</div>"
            "</div>"
        )

    if pages > 1:
        q = f"&only={only}" if only else ""
        parts.append("<nav>")
        parts.append(f"<a href='/?page={page-1}{q}'>← prev</a>" if page > 1
                     else "<span>← prev</span>")
        parts.append(f"<span>page {page} of {pages}</span>")
        parts.append(f"<a href='/?page={page+1}{q}'>next →</a>" if page < pages
                     else "<span>next →</span>")
        parts.append("</nav>")

    parts.append("</main>")
    parts.append(LIGHTBOX_HTML)
    return Response("".join(parts), mimetype="text/html")


@app.route("/img/<path:rel>")
def image(rel: str):
    # Resolve and confirm the result is still inside IMAGE_DIR, so a crafted
    # path can't walk out of the folder being served.
    root = IMAGE_DIR.resolve()
    target = (root / rel).resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
        abort(404)
    return send_file(target)


def main() -> int:
    global IMAGE_DIR

    p = argparse.ArgumentParser(
        description="Browse tagged images alongside their alt text and transcription.")
    p.add_argument("image_dir", nargs="?",
                   default=os.getenv("OUT_DIR") or "tagged",
                   help="folder of tagged images (default: OUT_DIR from .env, "
                        "else ./tagged)")
    p.add_argument("--port", type=int, default=60606, help="port (default: 60606)")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default: 127.0.0.1; use 0.0.0.0 to allow "
                        "connections from other machines)")
    args = p.parse_args()

    IMAGE_DIR = Path(args.image_dir)
    if not IMAGE_DIR.is_dir():
        print(f"error: not a directory: {IMAGE_DIR}", file=sys.stderr)
        return 1

    n = len(rows())
    print(f"serving {n} image(s) from {IMAGE_DIR}")
    print(f"  http://localhost:{args.port}")
    if args.host == "0.0.0.0":
        print("  (bound to 0.0.0.0 -- reachable from other machines on the network)")
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

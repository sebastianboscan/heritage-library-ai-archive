import csv, os, requests, sys, time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wp_config

# Credentials (from .env via wp_config)
BASE = wp_config.base_url()

OUT_DIR          = "library_images"   # folder for downloads + inventory CSV
LIMIT            = None               # how many to pull; None = everything
MISSING_ALT_ONLY = True               # skip attachments that already have alt text
DELAY            = 0.3                # seconds between requests

# Which rendition to download.
#   None  -> the uploaded original (largest available). Best for transcription:
#            most of this collection is 2000-4000px, and handing the model a
#            1024px copy of a handwritten form throws away the detail that
#            makes it legible.
#   <int> -> smallest WordPress rendition at least this wide, else the largest
#            one available. Use this to keep the download smaller.
TARGET_WIDTH     = None

auth = wp_config.auth()
FIELDS = "id,source_url,mime_type,alt_text,title,caption,description,media_details"

def raw(v):
    return v.get("raw", "") if isinstance(v, dict) else (v or "")

def all_media():
    page = 1
    while True:
        r = requests.get(f"{BASE}/media", auth=auth, timeout=30, params={
            "per_page": 100, "page": page, "media_type": "image",
            "orderby": "id", "order": "asc", "context": "edit", "_fields": FIELDS,
        })
        if r.status_code == 400:
            return
        r.raise_for_status()
        items = r.json()
        if not items:
            return
        yield from items
        if page >= int(r.headers.get("X-WP-TotalPages", 1)):
            return
        page += 1
        time.sleep(DELAY)

def render_url(item, target=TARGET_WIDTH):
    # target=None means the uploaded original. WordPress reports it as the
    # "full" size when one exists; source_url is the same file and is the
    # reliable fallback for attachments with no generated sizes at all.
    if target is None:
        return item["source_url"]

    sizes = item.get("media_details", {}).get("sizes", {})
    opts = [(s["width"], s["source_url"]) for s in sizes.values() if s.get("width")]
    if not opts:
        return item["source_url"]
    at_least = [o for o in opts if o[0] >= target]
    return min(at_least)[1] if at_least else max(opts)[1]

os.makedirs(OUT_DIR, exist_ok=True)
rows, pulled = [], 0

for item in all_media():
    if LIMIT is not None and pulled >= LIMIT:
        break
    alt = item.get("alt_text") or ""
    if MISSING_ALT_ONLY and alt.strip():
        continue

    url = render_url(item)
    ext = os.path.splitext(urlparse(url).path)[1] or ".jpg"
    path = os.path.join(OUT_DIR, f"{item['id']}{ext}")   # id prefix = no collisions

    if not os.path.exists(path):                          # resume: skip existing
        try:
            resp = requests.get(url, auth=auth, timeout=60)
            resp.raise_for_status()
            with open(path, "wb") as f:
                f.write(resp.content)
            time.sleep(DELAY)
        except requests.RequestException as e:
            print(f"  !! {item['id']} download failed: {e}")
            continue

    rows.append({
        "id": item["id"], "file": path, "render_url": url,
        "source_url": item["source_url"], "mime_type": item.get("mime_type", ""),
        "alt_text": alt, "title": raw(item.get("title")),
        "caption": raw(item.get("caption")), "description": raw(item.get("description")),
    })
    pulled += 1
    print(f"[{pulled}] {item['id']}  {raw(item.get('title'))[:60]}")

csv_path = os.path.join(OUT_DIR, "inventory.csv")
with open(csv_path, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)

print(f"\n{pulled} files -> {OUT_DIR}/  |  inventory: {csv_path}")
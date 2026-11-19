'''
This program counts how many file are at the base URL's /media.

Honestly I just wanna know for stats purposes
'''
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wp_config

BASE = wp_config.base_url()
auth = wp_config.auth()

def count(**params):
    """Total items reported by the API for a /media query (headers only, no paging)."""
    r = requests.get(f"{BASE}/media", auth=auth, timeout=30, params={
        "per_page": 1, "context": "edit", "_fields": "id", **params,
    })
    r.raise_for_status()
    return int(r.headers.get("X-WP-Total", 0))

total = count()
print(f"total files in {BASE}/media: {total}")

for mtype in ("image", "video", "audio", "application", "text"):
    n = count(media_type=mtype)
    if n:
        print(f"  {mtype:12} {n}")

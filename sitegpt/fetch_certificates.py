"""Pull the site's land-certificate posts from the REST API into sitegpt/src/land_certificates.json.
Read-only. Usage: python sitegpt/fetch_certificates.py"""
import json, os, sys, requests

sys.path.insert(0, os.getcwd())
import wp_config

recs, page = [], 1
while True:
    r = requests.get(f"{wp_config.base_url()}/land-certificate", timeout=60,
                     params={"per_page": 100, "page": page, "_fields": "id,slug,link,title,content"})
    if r.status_code != 200 or not r.json():
        break
    recs += r.json()
    page += 1

os.makedirs("sitegpt/src", exist_ok=True)
with open("sitegpt/src/land_certificates.json", "w", encoding="utf-8") as fh:
    json.dump(recs, fh)
print(len(recs), "certificate records -> sitegpt/src/land_certificates.json")

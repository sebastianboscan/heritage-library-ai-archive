"""Build SiteGPT knowledge files from the merged tagger report + inventory.
Usage: python sitegpt/build_export.py  (writes sitegpt/out/*.txt)"""
import collections, csv, html, json, os, re, sys

sys.path.insert(0, os.getcwd())
import wp_config
ROOT = wp_config.base_url().split("/wp-json")[0]
inv = {r["id"]: r for r in csv.DictReader(open("library_images/inventory.csv", encoding="utf-8"))}
rows = list(csv.DictReader(open("retry22/results_merged.csv", encoding="utf-8")))

# site graphics and event promotion: not collection items, and dated event info goes stale
ALT_EXCLUDE = re.compile(r"\b(logo|poster|flyer|masthead|promotional|sticker|calendar|"
                         r"class(es)? (advertisement|on|for)|winter classes|genealogy class|"
                         r"family tree maker|blank white|heritage observer)\b", re.I)
TITLE_EXCLUDE = re.compile(r"logo|masthead|background|icon", re.I)   # file names of site graphics
KIND = [("Land or tax certificate", re.compile(r"certificate", re.I)),
        ("Newspaper clipping", re.compile(r"newspaper|clipping|obituar|article|deaths", re.I)),
        ("Map or plan", re.compile(r"\bmap\b|plat|chart|diagram|blueprint|survey", re.I)),
        ("Document or record", re.compile(r"document|record|letter|form\b|register|ledger|census|deed", re.I))]

# The site's land-certificate pages (pulled from the REST API into sitegpt/src by
# fetch_certificates.py). Most embed the scan, which ties a page to our attachment id;
# a few carry a catalogue line (locality, lot, acreage) and some have no scan at all.
by_file = {}
for v in inv.values():
    f = os.path.basename(v.get("source_url", ""))
    by_file[f] = by_file[f.replace("-scaled", "")] = v["id"]

def text_of(fragment):
    t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()
    return "" if t in ("", "nan") else t

# scan file names carry the certificate number (Surname_Given_cert_0626_...), as do page slugs
# (surname-given-certificate-626): the fallback join for catalogue pages that don't embed the scan
by_num = collections.defaultdict(set)
for v in inv.values():
    m = re.search(r"_cert_0*(\d+)", os.path.basename(v.get("source_url", "")), re.I)
    if m:
        by_num[m.group(1)].add(v["id"])

cert_pages, cert_orphans = {}, []
for c in json.load(open("sitegpt/src/land_certificates.json", encoding="utf-8")):
    body = c["content"]["rendered"]
    entry = {"page": c["link"], "name": html.unescape(c["title"]["rendered"]).replace("_", " "),
             "catalogue": text_of(body)}
    ids = {by_file.get(os.path.basename(s)) or by_file.get(os.path.basename(s).replace("-scaled", ""))
           for s in re.findall(r'src="([^"]+)"', body)} - {None}
    m = re.search(r"certificate-0*(\d+)$", c["slug"])
    if not ids and m and len(by_num.get(m.group(1), ())) == 1:
        ids = set(by_num[m.group(1)])
    for i in ids:
        cert_pages[i] = entry
    if not ids:
        cert_orphans.append(entry)

groups, skipped, linked = collections.defaultdict(list), [], 0
for r in rows:
    i = os.path.splitext(os.path.basename(r["file"]))[0]
    v, alt, ext = inv.get(i, {}), r["alt_text"].strip(), r["extended"].strip()
    if not alt or ALT_EXCLUDE.search(alt) or TITLE_EXCLUDE.search(v.get("title", "")):
        skipped.append((i, alt[:70])); continue
    kind = next((k for k, rx in KIND if rx.search(alt)), "Photograph or artwork")
    cert = cert_pages.get(i)
    linked += bool(cert)
    rec = [f"ITEM {i}", f"Title: {alt}", f"Type: {kind}"]
    if cert:
        rec += [f"Name in library catalogue: {cert['name']}", f"Page: {cert['page']}"]
        if cert["catalogue"]:
            rec.append(f"Catalogue entry: {cert['catalogue']}")
    rec.append(f"Image: {v.get('source_url', '')}")
    if ext and ext != alt:
        rec += ["Transcription and description (machine-generated, not yet reviewed):", ext]
    groups[kind].append("\n".join(rec))

# catalogue pages with no scan of their own still name a person and a certificate
for c in cert_orphans:
    rec = [f"CATALOGUE ENTRY: {c['name']}", "Type: Land or tax certificate (catalogue entry, no scan online)",
           f"Page: {c['page']}"]
    if c["catalogue"]:
        rec.append(f"Catalogue entry: {c['catalogue']}")
    groups["Land or tax certificate"].append("\n".join(rec))

os.makedirs("sitegpt/out", exist_ok=True)
for kind, recs in groups.items():
    name = re.sub(r"\W+", "_", kind.lower()).strip("_")
    with open(f"sitegpt/out/collection_{name}.txt", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"Heritage Library collection: {kind} ({len(recs)} items)\n"
                 "Each record below is one item. Transcriptions were produced by AI and may contain "
                 "misreadings; [word?] marks an uncertain reading and [illegible] text that could not be read.\n\n")
        fh.write("\n\n----------\n\n".join(recs) + "\n")
with open("sitegpt/out/_excluded.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(f"{i}\t{a}" for i, a in skipped))
for kind, recs in sorted(groups.items(), key=lambda x: -len(x[1])):
    print(f"{len(recs):5}  {kind}")
print(f"{len(skipped):5}  excluded as site graphics/blank; {linked} items linked to a certificate page; "
      f"{len(cert_orphans)} catalogue-only certificates added")

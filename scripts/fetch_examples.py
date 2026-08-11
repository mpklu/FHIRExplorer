#!/usr/bin/env python3
"""
Pick one published HL7 example instance per resource and cache it for the build.

Downloads the release's official examples archive (e.g.
https://hl7.org/fhir/R4/examples-json.zip), indexes every entry by its own
`resourceType`, chooses one per resource, and writes a single JSON cache.

The cache is committed, so this only needs re-running when the models are bumped
to a new FHIR release — `build.py` never touches the network.

Usage
    python3 scripts/fetch_examples.py --release r4 --data data/r4.json \
        --out data/r4.examples.json
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.request
import zipfile

ARCHIVES = {
    "dstu2": "https://hl7.org/fhir/DSTU2/examples-json.zip",
    "stu3":  "https://hl7.org/fhir/STU3/examples-json.zip",
    "r4":    "https://hl7.org/fhir/R4/examples-json.zip",
    "r4b":   "https://hl7.org/fhir/R4B/examples-json.zip",
    "r5":    "https://hl7.org/fhir/R5/examples-json.zip",
}
SPEC_BASE = {
    "dstu2": "https://hl7.org/fhir/DSTU2/",
    "stu3":  "https://hl7.org/fhir/STU3/",
    "r4":    "https://hl7.org/fhir/R4/",
    "r4b":   "https://hl7.org/fhir/R4B/",
    "r5":    "https://hl7.org/fhir/R5/",
}
# Big instances stop being readable examples and start being test fixtures.
PREFER_UNDER = 24_000


def download(url: str, cache: str) -> bytes:
    if cache and os.path.exists(cache):
        print(f"using cached archive {cache} ({os.path.getsize(cache) / 1e6:.1f} MB)")
        return open(cache, "rb").read()
    print(f"downloading {url} …")
    req = urllib.request.Request(url, headers={"User-Agent": "FHIRExplorer/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        blob = resp.read()
    print(f"  {len(blob) / 1e6:.1f} MB")
    if cache:
        os.makedirs(os.path.dirname(os.path.abspath(cache)), exist_ok=True)
        open(cache, "wb").write(blob)
    return blob


def index_archive(blob: bytes) -> dict[str, list[tuple[str, int, dict]]]:
    """Map resourceType → [(filename, size, parsed), …], sorted by filename."""
    out: dict[str, list] = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in sorted(z.namelist()):
            if not name.endswith(".json"):
                continue
            raw = z.read(name)
            try:
                doc = json.loads(raw)
            except Exception:
                continue
            rt = doc.get("resourceType")
            if isinstance(rt, str):
                out.setdefault(rt, []).append((os.path.basename(name), len(raw), doc))
    return out


def choose(resource: str, candidates: list[tuple[str, int, dict]]):
    """Canonical `<lower>-example.json` if it exists, else the richest readable one."""
    canonical = f"{resource.lower()}-example.json"
    for c in candidates:
        if c[0] == canonical:
            return c
    readable = [c for c in candidates if c[1] <= PREFER_UNDER]
    pool = readable or candidates
    # largest of the readable ones (richer instance); filename breaks ties
    return sorted(pool, key=lambda c: (-c[1], c[0]))[0] if readable else \
        sorted(pool, key=lambda c: (c[1], c[0]))[0]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release", default="r4", choices=sorted(ARCHIVES))
    ap.add_argument("--data", required=True, help="parsed structure payload (for the resource list)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--archive-cache", default="", help="keep/reuse the downloaded zip here")
    args = ap.parse_args(argv)

    resources = [r["name"] for r in json.load(open(args.data))["resources"]]
    index = index_archive(download(ARCHIVES[args.release], args.archive_cache))

    picked, missing = {}, []
    for name in resources:
        candidates = index.get(name)
        if not candidates:
            missing.append(name)
            continue
        fname, size, doc = choose(name, candidates)
        picked[name] = {
            "file": fname,
            "url": SPEC_BASE[args.release] + fname.replace(".json", ".json.html"),
            "of": len(candidates),
            "json": doc,
        }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump({"release": args.release,
                   "source": ARCHIVES[args.release],
                   "examples": picked}, fh, separators=(",", ":"))

    print(f"wrote {args.out} ({os.path.getsize(args.out) / 1e6:.2f} MB)")
    print(f"  {len(picked)}/{len(resources)} resources have a published example")
    if missing:
        print(f"  no published example: {', '.join(missing)}")


if __name__ == "__main__":
    main()

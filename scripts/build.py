#!/usr/bin/env python3
"""
Build dist/index.html — a single self-contained page, no external requests.

Reads src/template.html and injects three payloads:
  /*__DATA__*/      the structure payload for the built release
  /*__SOURCES__*/   verbatim Swift source per resource, for the read-only viewer
  /*__RELEASES__*/  the release picker's options

If data/<release>.json is missing (or --reparse is passed), the models are parsed
first, so a clean checkout builds with one command.

Usage
    python3 scripts/build.py                       # build R4 from ../FHIRModels
    python3 scripts/build.py --reparse             # re-parse the models first
    python3 scripts/build.py --fhir-repo /path/to/FHIRModels
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import parse_models  # noqa: E402  (local module)

# Every release Apple ships a module for. `available` releases are parsed into
# the page; the rest render the "coming soon" panel. To add one: parse its module
# (see the panel's own instructions) and flip `available` to True.
VERSIONS = [
    {"id": "ModelsDSTU2", "label": "DSTU2", "slug": "dstu2", "available": False},
    {"id": "ModelsSTU3",  "label": "STU3",  "slug": "stu3",  "available": False},
    {"id": "ModelsR4",    "label": "R4",    "slug": "r4",    "available": True},
    {"id": "ModelsR4B",   "label": "R4B",   "slug": "r4b",   "available": False},
    {"id": "ModelsR5",    "label": "R5",    "slug": "r5",    "available": False},
    {"id": "ModelsBuild", "label": "R6 ballot", "slug": "build", "available": False},
]

# A JSON payload lives inside <script type="application/json">, so the one
# sequence that could end the tag early has to be neutralised.
def embed(obj) -> str:
    return json.dumps(obj, separators=(",", ":")).replace("</", "<\\/")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fhir-repo", default=os.path.join(ROOT, "..", "FHIRModels"),
                    help="path to the FHIRModels checkout (default: ../FHIRModels)")
    ap.add_argument("--release", default="r4", help="slug of the release to build (default: r4)")
    ap.add_argument("--reparse", action="store_true", help="re-parse the models before building")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist", "index.html"))
    args = ap.parse_args(argv)

    version = next((v for v in VERSIONS if v["slug"] == args.release), None)
    if version is None:
        sys.exit(f"unknown release '{args.release}' — known: "
                 + ", ".join(v["slug"] for v in VERSIONS))
    if not version["available"]:
        sys.exit(f"release '{args.release}' is not marked available in VERSIONS")

    data_path = os.path.join(ROOT, "data", f"{version['slug']}.json")
    src_path = os.path.join(ROOT, "data", f"{version['slug']}.sources.json")
    models_dir = os.path.join(args.fhir_repo, "Sources", version["id"])

    if args.reparse or not os.path.exists(data_path) or not os.path.exists(src_path):
        if not os.path.isdir(models_dir):
            sys.exit(f"cannot parse: {models_dir} not found (pass --fhir-repo)")
        print(f"parsing {models_dir} …")
        parse_models.main(["--models-dir", models_dir,
                           "--out", data_path, "--sources-out", src_path])

    payload = json.load(open(data_path))
    sources = json.load(open(src_path))
    template = open(os.path.join(ROOT, "src", "template.html"), encoding="utf-8").read()

    for token in ("/*__DATA__*/", "/*__SOURCES__*/", "/*__RELEASES__*/"):
        if token not in template:
            sys.exit(f"template is missing {token}")

    releases = [{"id": v["id"], "label": v["label"], "available": v["available"]}
                for v in VERSIONS]

    html = (template
            .replace("/*__DATA__*/", embed(payload))
            .replace("/*__SOURCES__*/", embed(sources))
            .replace("/*__RELEASES__*/", json.dumps(releases, separators=(",", ":"))))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)

    print(f"built {args.out} ({os.path.getsize(args.out) / 1e6:.2f} MB)")
    print(f"  release   {version['label']} · Sources/{version['id']} "
          f"· FHIR {payload['meta']['fhir_version']} @ {payload['meta']['models_commit']}")
    print(f"  resources {len(payload['resources'])} "
          f"({len(sources)} with source bundled)")
    print(f"  selectable releases: " +
          ", ".join(v["label"] + ("" if v["available"] else " (soon)") for v in VERSIONS))


if __name__ == "__main__":
    main()

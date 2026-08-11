#!/usr/bin/env python3
"""
Build dist/index.html — a single self-contained page, no external requests.

Reads src/template.html and injects:
  <!--__BUNDLES__-->  one <script type="application/json"> trio per embedded release
                      (structure payload, Swift sources, HL7 examples)
  /*__RELEASES__*/    the release picker's options

If data/<release>.json is missing (or --reparse is passed), the models are parsed
first, so a clean checkout builds with one command. The examples cache is optional
and never fetched here — run scripts/fetch_examples.py once if it is missing.

Usage
    python3 scripts/build.py                       # every available release
    python3 scripts/build.py --reparse             # re-parse the models first
    python3 scripts/build.py --fhir-repo /path/to/FHIRModels

The FHIRModels checkout is located in this order: --fhir-repo, then
$FHIR_MODELS_REPO, then ../FHIRModels.
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

# FHIRModels is read-only input that lives wherever the user keeps it, so the
# path comes from the environment first and only falls back to a sibling checkout.
DEFAULT_FHIR_REPO = os.environ.get("FHIR_MODELS_REPO") or os.path.join(ROOT, "..", "FHIRModels")

# Every release Apple ships a module for. `available` releases are parsed into
# the page; the rest render the "coming soon" panel. To add one: parse its module
# (see the panel's own instructions) and flip `available` to True.
VERSIONS = [
    {"id": "ModelsDSTU2", "label": "DSTU2", "slug": "dstu2", "available": False},
    {"id": "ModelsSTU3",  "label": "STU3",  "slug": "stu3",  "available": False},
    {"id": "ModelsR4",    "label": "R4",    "slug": "r4",    "available": True},
    {"id": "ModelsR4B",   "label": "R4B",   "slug": "r4b",   "available": False},
    {"id": "ModelsR5",    "label": "R5",    "slug": "r5",    "available": True},
    {"id": "ModelsBuild", "label": "R6 ballot", "slug": "build", "available": False},
]

# A JSON payload lives inside <script type="application/json">, so the one
# sequence that could end the tag early has to be neutralised.
def embed(obj) -> str:
    return json.dumps(obj, separators=(",", ":")).replace("</", "<\\/")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fhir-repo", default=DEFAULT_FHIR_REPO,
                    help="path to the FHIRModels checkout "
                         "(default: $FHIR_MODELS_REPO, else ../FHIRModels)")
    ap.add_argument("--releases", default="",
                    help="comma-separated slugs to embed (default: every available release)")
    ap.add_argument("--reparse", action="store_true", help="re-parse the models before building")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist", "index.html"))
    args = ap.parse_args(argv)

    wanted = [s.strip() for s in args.releases.split(",") if s.strip()] or \
        [v["slug"] for v in VERSIONS if v["available"]]
    embedded = []
    for slug in wanted:
        version = next((v for v in VERSIONS if v["slug"] == slug), None)
        if version is None:
            sys.exit(f"unknown release '{slug}' — known: " + ", ".join(v["slug"] for v in VERSIONS))
        if not version["available"]:
            sys.exit(f"release '{slug}' is not marked available in VERSIONS")
        embedded.append(version)

    template = open(os.path.join(ROOT, "src", "template.html"), encoding="utf-8").read()
    for token in ("<!--__BUNDLES__-->", "/*__RELEASES__*/"):
        if token not in template:
            sys.exit(f"template is missing {token}")

    bundles, summary = [], []
    for version in embedded:
        slug = version["slug"]
        data_path = os.path.join(ROOT, "data", f"{slug}.json")
        src_path = os.path.join(ROOT, "data", f"{slug}.sources.json")
        ex_path = os.path.join(ROOT, "data", f"{slug}.examples.json")
        models_dir = os.path.join(args.fhir_repo, "Sources", version["id"])

        if args.reparse or not os.path.exists(data_path) or not os.path.exists(src_path):
            if not os.path.isdir(models_dir):
                sys.exit(f"cannot parse: {models_dir} not found.\n"
                         f"Point at your FHIRModels checkout with either:\n"
                         f"  export FHIR_MODELS_REPO=/path/to/FHIRModels\n"
                         f"  python3 scripts/build.py --fhir-repo /path/to/FHIRModels")
            print(f"parsing {models_dir} …")
            parse_models.main(["--models-dir", models_dir,
                               "--out", data_path, "--sources-out", src_path])

        payload = json.load(open(data_path))
        sources = json.load(open(src_path))
        if os.path.exists(ex_path):
            examples = json.load(open(ex_path))
        else:
            examples = {"release": slug, "source": "", "examples": {}, "fragments": {}}
            print(f"note: data/{slug}.examples.json not found — the JSON example tab will be "
                  f"empty for {version['label']}. Populate it with:\n"
                  f"      python3 scripts/fetch_examples.py --release {slug} "
                  f"--data data/{slug}.json --out data/{slug}.examples.json")

        for kind, obj in (("payload", payload), ("sources", sources), ("examples", examples)):
            bundles.append(f'<script id="{kind}-{slug}" type="application/json">'
                           f'{embed(obj)}</script>')
        summary.append((version, payload, sources, examples))

    releases = [{"id": v["id"], "label": v["label"], "slug": v["slug"],
                 "available": v["available"],
                 "embedded": any(e["slug"] == v["slug"] for e in embedded)}
                for v in VERSIONS]

    html = (template
            .replace("<!--__BUNDLES__-->", "\n".join(bundles))
            .replace("/*__RELEASES__*/", json.dumps(releases, separators=(",", ":"))))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)

    print(f"built {args.out} ({os.path.getsize(args.out) / 1e6:.2f} MB)")
    for version, payload, sources, examples in summary:
        frags = examples.get("fragments", {})
        print(f"  {version['label']:>9}  FHIR {payload['meta']['fhir_version']} · "
              f"Sources/{version['id']} @ {payload['meta']['models_commit']}")
        print(f"             {len(payload['resources'])} resources "
              f"({len(examples['examples'])} with an example) · "
              f"{len(payload['datatypes'])} datatypes ({len(frags)} with a fragment) · "
              f"{len(sources)} source files")
    print("  picker: " + ", ".join(
        v["label"] + ("" if any(e["slug"] == v["slug"] for e in embedded) else " (soon)")
        for v in VERSIONS))


if __name__ == "__main__":
    main()

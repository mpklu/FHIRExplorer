# FHIRExplorer

A single-page, offline map of [Apple's FHIRModels](https://github.com/apple/FHIRModels) — what
the generated Swift types are, how they relate, and for every resource *and* datatype its Swift
source and a real JSON instance, both read-only.

Built by parsing a local FHIRModels checkout, so nothing in it is hand-maintained: bump the
dependency, re-run the build, and the page matches the new models exactly.

**Live: [mpklu.github.io/FHIRExplorer](https://mpklu.github.io/FHIRExplorer/)** — rebuilt and
published by GitHub Actions on every push to `main`.

```
python3 scripts/build.py            # parses ../FHIRModels, writes dist/index.html
open dist/index.html                # ~5.1 MB (0.7 MB gzipped), self-contained, no network calls
```

## What's in it

**Overview** — the three relationships the generator actually expresses in Swift, since those
are all you need to read the other 126,000 lines:

| Relation | Shape in the code |
|---|---|
| Conformance | Two protocol lines off `FHIRType` — `Resource → DomainResource` and `ElementReadOnly → Element → BackboneElement`. No subclassing: 667 structs, 2 classes, 7 protocols. |
| Containment | A resource is one struct plus a private nest. 459 of 467 backbone structs serve exactly one owner. |
| A type-erased pointer | 658 `Reference` properties, none parameterised by target. `Reference(reference: "Banana/42")` compiles. |

Plus the shared datatype core (41 datatypes, the `Identifier ⇄ Reference` cycle that forces the
module's only two classes), datatype reuse counts, and the 186 nested `value[x]` choice enums.

**Resource explorer** — all 146 resources, filterable and sortable, each with:

- **Structure** — nested backbone structs, every `Reference` property with its doc comment,
  shared datatypes held, terminology enums bound, `value[x]` slots.
- **Swift source** — the generated file verbatim, with line numbers, syntax highlighting, and a
  folded license header. Read-only.
- **JSON example** — a published HL7 example instance of that resource, unmodified, with a link
  to its page on hl7.org. 141 of 146 resources have one; the five `Substance*` resources the
  spec publishes no example for show their minimal shape instead. Read-only.

**Data type explorer** — the 41 datatypes and 8 shared backbones, sorted by reach. Same three
tabs, plus the thing the Swift source cannot give you: a **reverse index** of every type that
holds this one, grouped into resources / backbone structs / datatypes. Its **JSON example** is a
real fragment of that datatype lifted out of a published resource instance, labelled with the
path it came from (`RelatedPerson.name[0]` in `relatedperson-example.json`) — 33 of 49 datatypes
have one. Chips cross-link between the two explorers, and clicking a bar on the Overview chart
opens that datatype.

**Release picker** — R4 is built. The other releases Apple ships modules for (DSTU2, STU3, R4B,
R5, R6 ballot) show a "coming soon" panel with the exact commands to add them.

## Layout

```
FHIRExplorer/
├── scripts/parse_models.py   # Swift module → structure JSON + source bundle
├── scripts/fetch_examples.py # hl7.org examples → one per resource + one fragment per datatype
├── scripts/build.py          # template + JSON → dist/index.html (self-contained)
├── src/template.html         # the page: markup, CSS, diagrams, client JS
├── data/r4.json              # parsed structure payload (committed, ~290 kB)
├── data/r4.examples.json     # HL7 examples + datatype fragments (committed, ~580 kB)
├── data/r4.sources.json      # verbatim Swift, keyed by file (generated, ~4.1 MB, ignored)
└── dist/index.html           # build output (generated, ~5.1 MB, ignored)
```

`data/*.sources.json` and `dist/` are generated from the FHIRModels checkout, so they stay out of
git. `data/r4.json` is committed because it is small and its diffs show exactly what changed when
FHIRModels is bumped. `data/r4.examples.json` is committed because it is the one payload that
needs the network to produce — with it in the repo, every build is offline.

## Build

Defaults assume FHIRModels is a sibling checkout (`../FHIRModels`).

```bash
python3 scripts/build.py                          # build R4
python3 scripts/build.py --reparse                # re-parse the models first
python3 scripts/build.py --fhir-repo /path/to/FHIRModels
```

Python 3.9+, standard library only. No npm, no bundler. `build.py` never touches the network;
only `fetch_examples.py` does, and its output is committed:

```bash
python3 scripts/fetch_examples.py --release r4 \
    --data data/r4.json --out data/r4.examples.json
```

That downloads `hl7.org/fhir/R4/examples-json.zip` (~20 MB), indexes all 2,912 instances by their
own `resourceType`, and keeps one per resource — the canonical `<resource>-example.json` where the
spec publishes one, otherwise the richest instance under 24 kB. It then lifts one fragment per
datatype out of those instances, using `dt_props` (which resource properties carry that datatype)
and, for datatypes that never appear at the top level, `dt_keys` — property names that can only
mean one datatype module-wide, so a deep hit is still an honest fragment. Re-run it only when
moving to a new FHIR release.

## Deploying

`.github/workflows/pages.yml` rebuilds the page on every push to `main` and publishes it to
GitHub Pages. Because `dist/` is generated rather than committed, CI reconstructs it from the
payloads that *are* committed plus a **pinned** sparse checkout of `apple/FHIRModels`:

```yaml
repository: apple/FHIRModels
ref: 3fdead0f6459d7d3480d12c514ae27da31974a41   # bump this to move releases
sparse-checkout: Sources/ModelsR4
```

Same inputs in, same page out — a clean clone plus that checkout reproduces `dist/index.html`
byte for byte. To build against a different models commit without editing the file, run the
workflow manually (**Actions → Build and deploy to GitHub Pages → Run workflow**) and give it a
ref; the input is validated as a plain SHA, tag or branch name before it reaches `checkout`.

One-time repo setting: **Settings → Pages → Build and deployment → Source: GitHub Actions**.

## Adding a FHIR release

The parser is release-agnostic — it reads whichever `Sources/Models*` directory it is pointed at.

```bash
python3 scripts/parse_models.py \
    --models-dir ../FHIRModels/Sources/ModelsR5 \
    --out data/r5.json --sources-out data/r5.sources.json
```

Then flip `available` to `True` for that entry in `VERSIONS` in `scripts/build.py` and rebuild.
Each build embeds one release; the picker offers the rest as "coming soon".

## How the parse works

`parse_models.py` reads the Swift text — no Swift toolchain involved:

- top-level declarations and their protocol conformances → which line a type sits on
- `public var` / `public let` and their types → containment edges, datatype reuse, code bindings
- nested `…X` enums and their cases (including `indirect case`) → `value[x]` choice slots
- the `///` doc comment above each property → the only in-code hint about reference targets
- `public typealias` → `Age`/`Count`/`Distance`/`Duration` resolve to `Quantity`
- every holder of every datatype → the reverse index behind the Data types tab

## What it cannot show

Reference targets, cardinality beyond optional-vs-array, and value-set bindings FHIR marks
*example* or *preferred* rather than *required* do not survive into Swift. No parse of these
modules can recover them — use the FHIR spec.

## Provenance

FHIRModels is Apache-2.0, © Apple Inc. The **Swift source** tab shows that code verbatim, and
`data/*.sources.json` embeds it; the license header is folded in the viewer, not stripped from
the data. This repo only reads FHIRModels — never edit that checkout.

The **JSON example** tab shows HL7's own published example instances, unmodified, each linked
back to its page on hl7.org under the [FHIR licence](https://hl7.org/fhir/R4/license.html).
HL7®, FHIR® and the FHIR flame mark are registered trademarks of Health Level Seven
International.

Current build: `Sources/ModelsR4` · FHIR `4.0.1-9346c8cc45` · FHIRModels @ `3fdead0`.

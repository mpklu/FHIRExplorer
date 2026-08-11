# FHIRExplorer

A single-page, offline map of [Apple's FHIRModels](https://github.com/apple/FHIRModels) —
what the generated Swift types are, how they relate, and the source of every resource,
read-only.

Built by parsing a local FHIRModels checkout, so nothing in it is hand-maintained: bump the
dependency, re-run the build, and the page matches the new models exactly.

```
python3 scripts/build.py            # parses ../FHIRModels, writes dist/index.html
open dist/index.html                # ~4 MB, self-contained, no network calls
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

**Release picker** — R4 is built. The other releases Apple ships modules for (DSTU2, STU3, R4B,
R5, R6 ballot) show a "coming soon" panel with the exact commands to add them.

## Layout

```
FHIRExplorer/
├── scripts/parse_models.py   # Swift module → structure JSON + source bundle
├── scripts/build.py          # template + JSON → dist/index.html (self-contained)
├── src/template.html         # the page: markup, CSS, diagrams, client JS
├── data/r4.json              # parsed structure payload (committed, ~170 kB)
├── data/r4.sources.json      # verbatim Swift per resource (generated, ~3.8 MB, ignored)
└── dist/index.html           # build output (generated, ignored)
```

`data/*.sources.json` and `dist/` are generated, so they stay out of git; `data/r4.json` is
committed because it is small and its diffs show exactly what changed when FHIRModels is bumped.

## Build

Defaults assume FHIRModels is a sibling checkout (`../FHIRModels`).

```bash
python3 scripts/build.py                          # build R4
python3 scripts/build.py --reparse                # re-parse the models first
python3 scripts/build.py --fhir-repo /path/to/FHIRModels
```

Python 3.9+, standard library only. No npm, no bundler, no network access at build or run time.

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

## What it cannot show

Reference targets, cardinality beyond optional-vs-array, and value-set bindings FHIR marks
*example* or *preferred* rather than *required* do not survive into Swift. No parse of these
modules can recover them — use the FHIR spec.

## Provenance

FHIRModels is Apache-2.0, © Apple Inc. The **Swift source** tab shows that code verbatim, and
`data/*.sources.json` embeds it; the license header is folded in the viewer, not stripped from
the data. This repo only reads FHIRModels — never edit that checkout.

Current build: `Sources/ModelsR4` · FHIR `4.0.1-9346c8cc45` · FHIRModels @ `3fdead0`.

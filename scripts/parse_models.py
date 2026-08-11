#!/usr/bin/env python3
"""
Parse an Apple FHIRModels Swift module (e.g. Sources/ModelsR4) into the JSON
payload FHIRExplorer renders.

Everything here is derived from the Swift source alone: declarations and their
protocol conformances, public stored properties and their types, nested choice
(`value[x]`) enums and their cases, and the `///` doc comment above each property.

Usage
    python3 scripts/parse_models.py --models-dir ../FHIRModels/Sources/ModelsR4 \
        --out data/r4.json --sources-out data/r4.sources.json
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import subprocess
import sys

# --- declaration grammar -------------------------------------------------
# Top-level declarations sit at column 0; nested ones are tab-indented.
TOP_RE = re.compile(
    r'^(?:public |open )?(?:indirect )?(?:final )?(struct|class|enum|protocol) '
    r'([A-Za-z0-9_]+)(?:<[^>]*>)?\s*(?::\s*([^{]+?))?\s*\{', re.M)
NESTED_ENUM_RE = re.compile(r'^\t(?:public )?(?:indirect )?enum ([A-Za-z0-9_]+)\s*(?::[^{]*)?\{', re.M)
# Complex payloads are written `indirect case` to keep the enum's inline size down.
NESTED_CASE_RE = re.compile(r'^\t\t(indirect )?case ([A-Za-z0-9_]+)\(([^)]*)\)', re.M)
PROP_RE = re.compile(r'^\tpublic (?:var|let) `?([A-Za-z0-9_]+)`?\s*:\s*(.+)', re.M)
DOC_PROP_RE = re.compile(r'/// (.+)\n\tpublic (?:var|let) `?([A-Za-z0-9_]+)`?\s*:\s*(.+)')
SUMMARY_RE = re.compile(r'/\*\*\n (.+?)\n')
FHIR_VERSION_RE = re.compile(r'Generated from FHIR ([0-9A-Za-z.\-]+)')
TYPEALIAS_RE = re.compile(r'^public typealias ([A-Za-z0-9_]+) = ([A-Za-z0-9_]+)', re.M)
# Terminology files: one `public enum X: String, FHIRPrimitiveType` per code system,
# with the system/value-set URLs in the doc block above it.
CODE_ENUM_RE = re.compile(r'^public enum ([A-Za-z0-9_]+)\s*:\s*String', re.M)
CODE_CASE_RE = re.compile(r'^\tcase ([A-Za-z0-9_]+)', re.M)
CODE_URL_RE = re.compile(r'^ URL: (\S+)', re.M)
CODE_VS_RE = re.compile(r'^ ValueSet: (\S+)', re.M)

IDENT_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
# Wrappers that are plumbing rather than a modelled relationship.
WRAPPERS = {"FHIRPrimitive", "Array", "Optional", "Self",
            "Codable", "Sendable", "Equatable", "Hashable"}


def idents(type_string: str) -> list[str]:
    return IDENT_RE.findall(type_string)


def is_terminology_file(base: str) -> bool:
    """CodeSystem*.swift / ValueSet*.swift hold generated code enums — except
    CodeSystem.swift and ValueSet.swift themselves, which are DomainResources."""
    return (base.startswith("CodeSystem") and base != "CodeSystem") or \
           (base.startswith("ValueSet") and base != "ValueSet")


def parse(models_dir: str) -> dict:
    files: dict[str, dict] = {}
    types: dict[str, dict] = {}
    nested: dict[str, list] = {}
    documented: dict[str, list] = collections.defaultdict(list)
    code_enums: set[str] = set()
    code_systems: dict[str, dict] = {}
    aliases: dict[str, str] = {}
    fhir_version = ""

    for fn in sorted(os.listdir(models_dir)):
        if not fn.endswith(".swift"):
            continue
        base = fn[:-6]
        src = open(os.path.join(models_dir, fn), encoding="utf-8").read()
        terminology = is_terminology_file(base)
        files[base] = {"terminology": terminology, "lines": src.count("\n") + 1}

        if not fhir_version:
            m = FHIR_VERSION_RE.search(src)
            if m:
                fhir_version = m.group(1)

        if terminology:
            decls = list(CODE_ENUM_RE.finditer(src))
            for i, m in enumerate(decls):
                name = m.group(1)
                code_enums.add(name)
                head = src[:m.start()]
                body = src[m.end(): decls[i + 1].start() if i + 1 < len(decls) else len(src)]
                doc = SUMMARY_RE.findall(head)
                url = CODE_URL_RE.findall(head)
                vs = CODE_VS_RE.findall(head)
                code_systems[name] = {
                    "name": name, "file": base + ".swift",
                    "lines": src.count("\n") + 1,
                    "doc": doc[-1].strip() if doc else "",
                    "url": url[-1] if url else "",
                    "valueSet": vs[-1] if vs else "",
                    "ncases": len(CODE_CASE_RE.findall(body)),
                }
            continue

        for name, target in TYPEALIAS_RE.findall(src):
            aliases[name] = target

        decls = list(TOP_RE.finditer(src))
        for i, m in enumerate(decls):
            kind, name, conf = m.group(1), m.group(2), (m.group(3) or "")
            end = decls[i + 1].start() if i + 1 < len(decls) else len(src)
            body = src[m.end():end]
            types[name] = {
                "name": name, "kind": kind, "file": base,
                "conforms": [c.strip() for c in conf.split(",") if c.strip()],
                "props": [{"name": p.group(1), "type": p.group(2).strip()}
                          for p in PROP_RE.finditer(body)],
            }
            for p in DOC_PROP_RE.finditer(body):
                documented[name].append({"n": p.group(2), "t": p.group(3).strip(),
                                         "d": p.group(1).strip()})
            for nm in NESTED_ENUM_RE.finditer(body):
                rest = body[nm.end():]
                stop = rest.find("\n\t}")          # enum body ends at the first tab-indented close
                cases = [{"name": c.group(2), "payload": c.group(3).strip(),
                          "indirect": bool(c.group(1))}
                         for c in NESTED_CASE_RE.finditer(rest[:stop] if stop >= 0 else rest)]
                if cases:
                    nested.setdefault(name, []).append({"enum": nm.group(1), "cases": cases})

    # --- classify every declared type by which protocol line it sits on ----
    # Conformance has to be followed transitively and through typealiases: R5
    # writes `Address: DataType` where DataType is a typealias for Element, and
    # `Timing: BackboneType` where `BackboneType: DataType`.
    def deref(proto: str) -> str:
        seen = set()
        while proto in aliases and proto not in seen:
            seen.add(proto)
            proto = aliases[proto]
        return proto

    protocol_parents = {n: [deref(p) for p in t["conforms"]]
                        for n, t in types.items() if t["kind"] == "protocol"}

    def closure(protos: list[str]) -> set[str]:
        out, stack = set(), [deref(p) for p in protos]
        while stack:
            p = stack.pop()
            if p in out:
                continue
            out.add(p)
            stack.extend(protocol_parents.get(p, []))
        return out

    # Order matters: in R5 both BackboneType and PrimitiveType conform to
    # DataType (= Element), so they have to be tested before the datatype line.
    ROLE_BY_PROTOCOL = [
        ("domain-resource", {"DomainResource"}),
        ("resource",        {"Resource"}),
        ("backbone",        {"BackboneElement", "BackboneType"}),
        ("primitive",       {"FHIRPrimitiveType", "PrimitiveType"}),
        ("datatype",        {"Element", "ElementReadOnly"}),
    ]

    def role(name: str, t: dict) -> str:
        if t["kind"] == "protocol":
            return "protocol"
        if name == "ResourceProxy":
            return "proxy"
        conforms = closure(t["conforms"])
        for label, markers in ROLE_BY_PROTOCOL:
            if conforms & markers:
                return label
        if t["kind"] == "enum":
            return "enum"
        return "primitive"

    for name, t in types.items():
        t["role"] = role(name, t)

    resource_names = sorted(n for n, t in types.items()
                            if t["role"] in ("domain-resource", "resource"))
    datatype_names = {n for n, t in types.items() if t["role"] == "datatype"}
    # A handful of shared complex datatypes conform to BackboneElement and live
    # in their own file rather than inside a resource.
    resource_files = {types[n]["file"] for n in resource_names}
    shared_backbones = {n for n, t in types.items()
                        if t["role"] == "backbone" and t["file"] not in resource_files}

    def normalise(tok: str) -> str:
        """Age/Count/Distance/Duration are typealiases for Quantity."""
        return aliases.get(tok, tok)

    def datatypes_in(type_string: str) -> set[str]:
        out = set()
        for tok in idents(type_string):
            n = normalise(tok)
            if n in datatype_names or tok in shared_backbones:
                out.add(n if n in datatype_names else tok)
        return out

    # --- type→type edges (properties + choice payloads) -------------------
    edges: set[tuple[str, str]] = set()
    for name, t in types.items():
        reached = set()
        for p in t["props"]:
            reached |= {x for x in idents(p["type"])
                        if x in types and x != name and x not in WRAPPERS}
        for grp in nested.get(name, []):
            for c in grp["cases"]:
                reached |= {x for x in idents(c["payload"])
                            if x in types and x != name and x not in WRAPPERS}
        for d in reached:
            edges.add((name, d))

    # --- who holds each datatype, and what each datatype holds -------------
    usage: collections.Counter = collections.Counter()
    holders: dict[str, list[str]] = collections.defaultdict(list)
    holds: dict[str, set[str]] = collections.defaultdict(set)
    # datatype → resource-level properties carrying it, for pulling a real JSON
    # fragment out of that resource's published example
    dt_props: dict[str, list] = collections.defaultdict(list)

    for name, t in sorted(types.items()):
        held = set()
        for p in t["props"]:
            found = datatypes_in(p["type"])
            held |= found
            if t["role"] in ("domain-resource", "resource"):
                for d in found:
                    dt_props[d].append([name, p["name"], p["type"].lstrip().startswith("[")])
        for grp in nested.get(name, []):
            for c in grp["cases"]:
                held |= datatypes_in(c["payload"])
        held -= {name}
        for d in held:
            usage[d] += 1
            holders[d].append(name)
        if name in datatype_names or name in shared_backbones:
            holds[name] = held

    # --- per-resource detail ---------------------------------------------
    resources = []
    ref_property_count = 0
    for name in resource_names:
        t = types[name]
        backbones = sorted(m for m, u in types.items()
                           if u["role"] == "backbone" and u["file"] == t["file"] and m != name)
        refs, held, codes, choices = [], set(), [], []
        for owner in [name] + backbones:
            for p in documented.get(owner, []):
                if re.search(r'\bReference\b', p["t"]):
                    ref_property_count += 1
                    refs.append({"o": "" if owner == name else owner, "n": p["n"],
                                 "d": p["d"], "a": p["t"].lstrip().startswith("[")})
                held |= datatypes_in(p["t"])
                for tok in idents(p["t"]):
                    if tok in code_enums:
                        codes.append({"o": "" if owner == name else owner,
                                      "n": p["n"], "e": tok})
            for grp in nested.get(owner, []):
                choices.append({"o": "" if owner == name else owner, "e": grp["enum"],
                                "n": len(grp["cases"]),
                                "indirect": sum(1 for c in grp["cases"] if c["indirect"]),
                                "types": [c["payload"] for c in grp["cases"]]})

        summary = ""
        m = SUMMARY_RE.search(open(os.path.join(models_dir, t["file"] + ".swift"),
                                   encoding="utf-8").read())
        if m:
            summary = m.group(1).strip()

        resources.append({
            "name": name,
            "kind": "DomainResource" if t["role"] == "domain-resource" else "Resource",
            "file": t["file"] + ".swift",
            "lines": files[t["file"]]["lines"],
            "props": len(t["props"]),
            "doc": summary,
            "bb": [{"n": b, "p": len(types[b]["props"])} for b in backbones],
            "refs": refs,
            "dt": sorted(held),
            "codes": codes,
            "ch": choices,
        })

    # --- JSON keys that can only ever mean one datatype --------------------
    # Used to lift a real fragment for datatypes that never appear as a
    # resource-level property. A key qualifies only if *every* declaration of
    # that property name anywhere in the module resolves to the same single
    # datatype — so "extension" and "slicing" qualify, "code" and "value" don't.
    name_meaning: dict[str, set[str]] = collections.defaultdict(set)
    for t in types.values():
        for p in t["props"]:
            found = datatypes_in(p["type"])
            name_meaning[p["name"]].add(next(iter(found)) if len(found) == 1 and
                                        len(idents(p["type"])) <= 2 else "?")
    dt_keys: dict[str, list[str]] = collections.defaultdict(list)
    for prop_name, meanings in sorted(name_meaning.items()):
        if len(meanings) == 1:
            only = next(iter(meanings))
            if only != "?":
                dt_keys[only].append(prop_name)

    # --- who binds each terminology enum ----------------------------------
    code_binders: dict[str, dict[str, list[str]]] = collections.defaultdict(
        lambda: collections.defaultdict(list))
    for name, t in sorted(types.items()):
        for p in t["props"]:
            for tok in idents(p["type"]):
                if tok in code_systems:
                    code_binders[tok][t["role"]].append(f'{name}.{p["name"]}')

    enums = sorted(
        ({**code_systems[n],
          "bound": sum(len(v) for v in code_binders.get(n, {}).values()),
          "binders": {role: sorted(names)
                      for role, names in sorted(code_binders.get(n, {}).items())}}
         for n in sorted(code_systems)),
        key=lambda e: (-e["bound"], e["name"]))

    alias_of: dict[str, list[str]] = collections.defaultdict(list)
    for a, target in aliases.items():
        if target in datatype_names:
            alias_of[target].append(a)

    def datatype_entry(n: str) -> dict:
        t = types[n]
        by_role = collections.defaultdict(list)
        for h in holders.get(n, []):
            by_role[types[h]["role"]].append(h)
        return {
            "name": n,
            "kind": t["kind"],
            "base": t["conforms"][0] if t["conforms"] else "",
            "file": t["file"] + ".swift",
            "lines": files[t["file"]]["lines"],
            "used": usage.get(n, 0),
            "shared_backbone": n in shared_backbones,
            "aliases": sorted(alias_of.get(n, [])),
            "doc": "",                      # filled in from the file's /** … */ block below
            "props": [{"n": p["n"], "t": p["t"], "d": p["d"]} for p in documented.get(n, [])],
            "nprops": len(t["props"]),
            "holds": sorted(holds.get(n, set())),
            "holders": {role: sorted(names) for role, names in sorted(by_role.items())},
            "choices": [{"e": g["enum"], "n": len(g["cases"]),
                         "indirect": sum(1 for c in g["cases"] if c["indirect"]),
                         "cases": [{"n": c["name"], "t": c["payload"], "i": c["indirect"]}
                                   for c in g["cases"]]}
                        for g in nested.get(n, [])],
        }

    datatypes = sorted((datatype_entry(n) for n in sorted(datatype_names | shared_backbones)),
                       key=lambda d: -d["used"])

    # a one-line summary per datatype, from the /** … */ block above its declaration
    for entry in datatypes:
        src = open(os.path.join(models_dir, entry["file"]), encoding="utf-8").read()
        decl = re.search(r'^(?:public |open )?(?:indirect )?(?:final )?(?:struct|class) '
                         + re.escape(entry["name"]) + r'\b', src, re.M)
        before = src[:decl.start()] if decl else src
        blocks = re.findall(r'/\*\*\n (.+?)\n', before)
        entry["doc"] = blocks[-1].strip() if blocks else ""

    all_choices = sorted(
        ({"owner": owner, "enum": g["enum"], "n": len(g["cases"]),
          "indirect": sum(1 for c in g["cases"] if c["indirect"])}
         for owner, grps in nested.items() for g in grps),
        key=lambda c: -c["n"])

    choice_max = max((c["n"] for c in all_choices), default=0)
    at_max = [c for c in all_choices if c["n"] == choice_max]
    roles = collections.Counter(t["role"] for t in types.values())
    dt_edges = sorted([s, d] for (s, d) in edges
                      if s in datatype_names and d in datatype_names)

    return {
        "meta": {
            "fhir_version": fhir_version,
            "module": os.path.basename(os.path.abspath(models_dir)),
            "models_commit": git_commit(models_dir),
        },
        "counts": {
            "files_total": len(files),
            "files_model": sum(1 for f in files.values() if not f["terminology"]),
            "files_terminology": sum(1 for f in files.values() if f["terminology"]),
            "types_declared": len(types),
            "code_enums": len(code_enums),
            "edges": len(edges),
            "choice_enums": len(all_choices),
            "ref_properties": ref_property_count,
            "lines_total": sum(f["lines"] for f in files.values()),
            "lines_model": sum(f["lines"] for f in files.values() if not f["terminology"]),
            "structs": sum(1 for t in types.values() if t["kind"] == "struct"),
            "classes": sum(1 for t in types.values() if t["kind"] == "class"),
            "protocols": sum(1 for t in types.values() if t["kind"] == "protocol"),
            "shared_backbones": len(shared_backbones),
            "resource_proxy_cases": resource_proxy_cases(models_dir),
            "choice_max": choice_max,
            "choice_max_count": len(at_max),
            "choice_max_indirect": at_max[0]["indirect"] if at_max else 0,
        },
        "roles": dict(roles),
        "resources": resources,
        "datatypes": datatypes,
        "enums": enums,
        # datatype → [[resource, property, isArray], …]: where to look for a real
        # JSON fragment of that datatype inside a resource's published example
        "dt_props": {d: v[:14] for d, v in sorted(dt_props.items())},
        # datatype → property names that unambiguously mean that datatype, for a
        # deeper (but still safe) fragment search
        "dt_keys": {d: v[:12] for d, v in sorted(dt_keys.items())},
        "dt_edges": dt_edges,
        "choices_top": all_choices[:14],
        "aliases": {k: v for k, v in sorted(aliases.items()) if v in datatype_names},
        "shared_backbones": sorted(shared_backbones),
        "classes": sorted(n for n, t in types.items() if t["kind"] == "class"),
    }


def resource_proxy_cases(models_dir: str) -> int:
    path = os.path.join(models_dir, "ResourceProxy.swift")
    if not os.path.exists(path):
        return 0
    src = open(path, encoding="utf-8").read()
    return len(re.findall(r'^\tcase [a-zA-Z]', src, re.M))


def git_commit(path: str) -> str:
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def collect_sources(models_dir: str, payload: dict) -> dict[str, str]:
    """Verbatim Swift source for every file the viewer can open, keyed by filename.

    Keyed by file rather than by type because several datatypes share one file
    (ElementDefinition and its eight helpers, for one).
    """
    wanted = sorted({e["file"] for e in payload["resources"]} |
                    {e["file"] for e in payload["datatypes"]} |
                    {e["file"] for e in payload["enums"]})
    return {name: open(os.path.join(models_dir, name), encoding="utf-8").read()
            for name in wanted}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models-dir", required=True, help="path to Sources/ModelsXX")
    ap.add_argument("--out", required=True, help="structure payload JSON")
    ap.add_argument("--sources-out", help="Swift source bundle JSON")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.models_dir):
        sys.exit(f"not a directory: {args.models_dir}")

    payload = parse(args.models_dir)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(payload, fh, separators=(",", ":"))

    c = payload["counts"]
    print(f"{payload['meta']['module']} · FHIR {payload['meta']['fhir_version']} "
          f"@ {payload['meta']['models_commit'] or 'unknown commit'}")
    print(f"  {c['files_model']} model files + {c['files_terminology']} terminology files")
    print(f"  {c['types_declared']} declared types: " +
          ", ".join(f"{v} {k}" for k, v in sorted(payload['roles'].items(), key=lambda x: -x[1])))
    print(f"  {len(payload['resources'])} resources · {c['ref_properties']} reference properties "
          f"· {c['choice_enums']} choice enums · {c['edges']} edges")
    print(f"  wrote {args.out} ({os.path.getsize(args.out) / 1000:.0f} kB)")

    if args.sources_out:
        sources = collect_sources(args.models_dir, payload)
        with open(args.sources_out, "w") as fh:
            json.dump(sources, fh, separators=(",", ":"))
        print(f"  wrote {args.sources_out} "
              f"({os.path.getsize(args.sources_out) / 1e6:.2f} MB, {len(sources)} files)")
    print(f"  {len(payload['datatypes'])} datatypes detailed "
          f"({sum(1 for d in payload['datatypes'] if d['props'])} with documented properties)")
    bound = sum(1 for e in payload['enums'] if e['bound'])
    print(f"  {len(payload['enums'])} terminology enums "
          f"({bound} bound by at least one property, "
          f"{sum(e['ncases'] for e in payload['enums'])} cases total)")


if __name__ == "__main__":
    main()

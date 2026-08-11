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
            for m in re.finditer(r'^public enum ([A-Za-z0-9_]+)', src, re.M):
                code_enums.add(m.group(1))
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
    def role(name: str, t: dict) -> str:
        c = set(t["conforms"])
        if t["kind"] == "protocol":
            return "protocol"
        if name == "ResourceProxy":
            return "proxy"
        if "DomainResource" in c:
            return "domain-resource"
        if "Resource" in c:
            return "resource"
        if "BackboneElement" in c:
            return "backbone"
        if "Element" in c or "ElementReadOnly" in c:
            return "datatype"
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

    # --- how many distinct types hold each datatype -----------------------
    usage: collections.Counter = collections.Counter()
    for name, t in types.items():
        held = set()
        for p in t["props"]:
            held |= datatypes_in(p["type"])
        for grp in nested.get(name, []):
            for c in grp["cases"]:
                held |= datatypes_in(c["payload"])
        for d in held - {name}:
            usage[d] += 1

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

    datatypes = sorted(
        ({"name": n, "used": usage.get(n, 0), "props": len(types[n]["props"]),
          "base": types[n]["conforms"][0] if types[n]["conforms"] else "",
          "kind": types[n]["kind"], "shared_backbone": n in shared_backbones}
         for n in sorted(datatype_names | shared_backbones)),
        key=lambda d: -d["used"])

    all_choices = sorted(
        ({"owner": owner, "enum": g["enum"], "n": len(g["cases"]),
          "indirect": sum(1 for c in g["cases"] if c["indirect"])}
         for owner, grps in nested.items() for g in grps),
        key=lambda c: -c["n"])

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
        },
        "roles": dict(roles),
        "resources": resources,
        "datatypes": datatypes,
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


def collect_sources(models_dir: str, resources: list[dict]) -> dict[str, str]:
    """Verbatim Swift source for each resource's file, for the read-only viewer."""
    return {r["name"]: open(os.path.join(models_dir, r["file"]), encoding="utf-8").read()
            for r in resources}


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
        sources = collect_sources(args.models_dir, payload["resources"])
        with open(args.sources_out, "w") as fh:
            json.dump(sources, fh, separators=(",", ":"))
        print(f"  wrote {args.sources_out} "
              f"({os.path.getsize(args.sources_out) / 1e6:.2f} MB, {len(sources)} files)")


if __name__ == "__main__":
    main()

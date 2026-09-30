"""Inventory exact resolved runtime POM license declarations from copied cache.

This reports declarations and hashes. It does not turn the complete project's
license/notice gate on merely because each Maven POM names a license.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def audit(report: Path, cache: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError("Preserve prior Maven notice review")
    resolved = json.loads(report.read_text("utf-8"))
    if resolved.get("exit_code") != 0 or resolved.get("resolved") is not True:
        raise ValueError("Actual runtime dependency resolution required")
    unique = {(r["group"], r["name"], r["version"]) for r in resolved["runtime_dependencies"]}
    output.mkdir(parents=True)
    rows = []
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
    def pom(coordinate):
        group,name,version=coordinate
        if any("/" in p or "\\" in p or ":" in p or p in {"", ".", ".."} for p in coordinate):
            raise ValueError("Unsafe runtime coordinate")
        files=list((cache/"caches/modules-2/files-2.1"/group/name/version).glob("*/*.pom"))
        if len(files)!=1 or files[0].stat().st_size>1024*1024:
            raise ValueError("Exact runtime POM missing or ambiguous: "+":".join(coordinate))
        raw=files[0].read_bytes()
        target=output/"poms"/group/name/(version+".pom")
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(raw)
        return ET.fromstring(raw),raw
    def license_chain(root,coordinate,seen):
        licenses=[{"name":v.findtext("m:name","",ns),"url":v.findtext("m:url","",ns),
                   "distribution":v.findtext("m:distribution","",ns)} for v in root.findall("m:licenses/m:license",ns)]
        if licenses:
            return licenses,[]
        parent=root.find("m:parent",ns)
        if parent is None:
            return [],[]
        key=tuple(parent.findtext("m:"+tag,"",ns) for tag in ("groupId","artifactId","version"))
        if key in seen or len(seen)>=8:
            raise ValueError("Cyclic or excessive Maven license ancestry")
        other,raw=pom(key)
        inherited,chain=license_chain(other,key,seen|{key})
        return inherited,[{"coordinate":":".join(key),"pom_sha256":hashlib.sha256(raw).hexdigest()},*chain]
    for group, name, version in sorted(unique):
        if any("/" in p or "\\" in p or ":" in p or p in {"", ".", ".."} for p in (group, name, version)):
            raise ValueError("Unsafe runtime coordinate")
        root,raw=pom((group,name,version))
        def text(tag):
            return root.findtext("m:" + tag, "", ns)
        licenses,ancestry=license_chain(root,(group,name,version),{(group,name,version)})
        source = root.find("m:scm", ns)
        scm = {} if source is None else {key: source.findtext("m:"+key, "", ns) for key in ("url","connection","tag")}
        rows.append({"coordinate": ":".join((group,name,version)), "pom_sha256": hashlib.sha256(raw).hexdigest(),
                     "pom_bytes": len(raw), "licenses": licenses, "scm": scm,
                     "project_url": text("url"), "declared_licenses_present": bool(licenses), "license_pom_ancestry":ancestry})
    result = {"schema":1,"runtime_artifacts":len(resolved["runtime_dependencies"]),"unique_runtime_modules":len(rows),
              "all_pom_license_declarations_present":all(r["declared_licenses_present"] for r in rows),
              "modules":rows,"dependency_notices_verified":False,"public_release_ready":False,
              "remaining":"License texts, required NOTICE/copyright and non-Maven native/vendor components are reviewed separately."}
    with (output/"maven-notices-review.json").open("x",encoding="utf-8") as stream:
        json.dump(result,stream,indent=2); stream.write("\n")
    return {key: result[key] for key in ("runtime_artifacts","unique_runtime_modules","all_pom_license_declarations_present","public_release_ready")}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ("report","cache","output"):
        parser.add_argument("--"+key,type=Path,required=True)
    args=parser.parse_args(argv)
    print(json.dumps(audit(args.report,args.cache,args.output),indent=2))


if __name__=="__main__": main()

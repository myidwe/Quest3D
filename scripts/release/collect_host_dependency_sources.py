"""Collect exact MSYS2 source packages without executing their build recipes.

The .BUILDINFO-bound recipe SHA is the authority. Source packages are retained
whole, and only their small recipe is read through bsdtar. No archive tree is
extracted, no install is performed, and publication readiness is not inferred.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import urllib.parse
import urllib.request


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def safe_path(name: str) -> str:
    """Reject absolute, traversal and Windows aliases before reading members."""
    clean = name.rstrip("/")
    if not clean or clean.startswith("/") or "\\" in clean or ":" in clean:
        raise ValueError("Unsafe source package member")
    parts = clean.split("/")
    if any(p in ("", ".", "..") or p.rstrip(" .") != p or
           any(ord(c) < 32 for c in p) or
           re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p)
           for p in parts):
        raise ValueError("Unsafe source package member")
    return clean


def checked(path: Path, *, missing: bool = False) -> Path:
    path = Path(os.path.abspath(path))
    for node in (path, *path.parents):
        try:
            item = node.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise
        if stat.S_ISLNK(item.st_mode) or getattr(item, "st_file_attributes", 0) & 0x400:
            raise ValueError("Redirected source collection path")
    return path


def download(url: str, output: Path, limit: int) -> dict:
    """Allow official source hosts only, preserving incomplete attempts."""
    original = urllib.parse.urlsplit(url)
    if original.scheme != "https" or original.hostname not in (
            "repo.msys2.org", "raw.githubusercontent.com"):
        raise ValueError("Non-official source URL")
    req = urllib.request.Request(url, headers={"User-Agent": "Quest3D-source-supply/1.0"})
    with urllib.request.urlopen(req, timeout=60) as response:
        final = urllib.parse.urlsplit(response.geturl())
        if final.scheme != "https" or final.hostname != original.hostname:
            raise ValueError("Unexpected source redirect")
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > limit:
            raise ValueError("Source download exceeds limit")
        count = 0
        with checked(output, missing=True).open("xb") as stream:
            while block := response.read(1024 * 1024):
                count += len(block)
                if count > limit:
                    raise ValueError("Source stream exceeds limit")
                stream.write(block)
        if declared and count != int(declared):
            raise ValueError("Incomplete source download")
    return {"url": url, "sha256": sha(output), "bytes": count}


def inspect(archive: Path, bsdtar: Path) -> tuple[list[str], str]:
    """List rather than extract the package; read only its regular PKGBUILD."""
    result = subprocess.run([str(bsdtar), "-tf", str(archive)], capture_output=True,
                            check=True, timeout=60)
    if len(result.stdout) > 32 * 1024 * 1024:
        raise ValueError("Excessive source inventory")
    names = [safe_path(name) for name in result.stdout.decode("utf-8").splitlines()]
    if len(names) != len(set(names)) or len(names) > 200000:
        raise ValueError("Duplicate or excessive source members")
    recipes = [name for name in names if PurePosixPath(name).name == "PKGBUILD"]
    if len(recipes) != 1:
        raise ValueError("Source package must contain one recipe")
    verbose = subprocess.run([str(bsdtar), "-tvf", str(archive), recipes[0]],
                             capture_output=True, check=True, timeout=60)
    if not verbose.stdout.startswith(b"-"):
        raise ValueError("Recipe must be a regular member")
    return names, recipes[0]


def recipe_inputs(text: str, version: str) -> list[tuple[str, str]]:
    """Parse the reviewed simple arrays as data, refusing shell expressions."""
    arrays = [re.search(r"^" + key + r"=\((.*?)\)", text, re.M | re.S)
              for key in ("source", "sha256sums")]
    if not all(arrays):
        raise ValueError("Unknown source recipe arrays")
    real = re.search(r"^_realname=(\S+)\s*$", text, re.M)
    if not real:
        raise ValueError("Unknown source recipe name")
    sources = []
    for token in shlex.split(arrays[0].group(1)):
        token = token.replace("${_realname}", real.group(1)).replace(
            "${pkgver//./_}", version.replace(".", "_")).replace("${pkgver}", version)
        if "$" in token or "`" in token or "\\" in token:
            raise ValueError("Executable or unresolved source expression")
        if token.endswith("{,.asc}") or token.endswith("{,.sig}"):
            token, suffix = re.fullmatch(r"(.*)\{,(\.(?:asc|sig))\}", token).groups()
            if "::" in token:
                alias, address = token.split("::", 1)
                sources.extend((token, alias + suffix + "::" + address + suffix))
            else:
                sources.extend((token, token + suffix))
        else:
            sources.append(token)
    sums = shlex.split(arrays[1].group(1))
    if len(sums) != len(sources) or any(item != "SKIP" and not re.fullmatch(
            r"[0-9a-f]{64}", item) for item in sums):
        raise ValueError("Unknown source checksum array")
    return [(safe_path((url.split("::", 1)[0] if "::" in url else
                       PurePosixPath(urllib.parse.urlsplit(url).path).name if "://" in url
                       else url)), digest)
            for url, digest in zip(sources, sums)]


def verify_inputs(archive: Path, names: list[str], recipe: bytes,
                  version: str, bsdtar: Path) -> list[dict]:
    """Hash every recipe source/patch member against the official recipe."""
    result = []
    for basename, expected in recipe_inputs(recipe.decode("utf-8"), version):
        matches = [name for name in names if PurePosixPath(name).name == basename]
        if len(matches) != 1:
            raise ValueError("Recipe input missing or ambiguous in source package")
        proc = subprocess.Popen([str(bsdtar), "-xOf", str(archive), matches[0]],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        digest = hashlib.sha256()
        count = 0
        while block := proc.stdout.read(1024 * 1024):
            digest.update(block)
            count += len(block)
            if count > 400 * 1024 * 1024:
                proc.kill()
                raise ValueError("Recipe input is excessive")
        proc.stdout.close()
        error = proc.stderr.read()
        code = proc.wait(timeout=60)
        if code or error or expected != "SKIP" and digest.hexdigest() != expected:
            raise ValueError("Recipe input differs from official checksum")
        result.append({"member": matches[0], "sha256": digest.hexdigest(), "bytes": count,
                       "recipe_checksum": expected, "checksum_verified": expected != "SKIP"})
    return result


def collect_one(spec: dict, output: Path, bsdtar: Path, limit: int) -> dict:
    name = spec["package"].removeprefix("mingw-w64-ucrt-x86_64-")
    destination = output / name
    destination.mkdir(exist_ok=False)
    url = spec["source_only_archive_url"].replace("https://mirror.msys2.org/",
                                               "https://repo.msys2.org/")
    archive = destination / PurePosixPath(urllib.parse.urlsplit(url).path).name
    archive_info = download(url, archive, limit)
    official = destination / "PKGBUILD.official"
    recipe_info = download(spec["recipe_url"], official, 1024 * 1024)
    if recipe_info["sha256"] != spec["expected_recipe_sha256"]:
        raise ValueError("Official recipe differs from actual binary BUILDINFO")
    names, recipe = inspect(archive, bsdtar)
    raw = subprocess.run([str(bsdtar), "-xOf", str(archive), recipe],
                         capture_output=True, check=True, timeout=60).stdout
    if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != recipe_info["sha256"]:
        raise ValueError("Source package recipe differs from actual binary BUILDINFO")
    (destination / "PKGBUILD.source-package").write_bytes(raw)
    inputs = verify_inputs(archive, names, raw, spec["version"].rsplit("-", 1)[0], bsdtar)
    inventory = destination / "inventory.json"
    inventory.write_text(json.dumps({"members": names, "count": len(names),
                         "recipe_member": recipe, "full_tree_extracted": False},
                         indent=2) + "\n", encoding="utf-8")
    return {"name": name, "version": spec["version"], "package": spec["package"],
            "recipe_commit": spec["recipe_commit"], "recipe_matches_binary_buildinfo": True,
            "archive": {"path": archive.relative_to(output).as_posix(), **archive_info},
            "official_recipe": {"path": official.relative_to(output).as_posix(), **recipe_info},
            "inventory": {"path": inventory.relative_to(output).as_posix(), "sha256": sha(inventory)},
            "source_package_kept_whole": True, "recipe_executed": False,
            "recipe_inputs": inputs,
            "signature_verified": False}


def collect(evidence: Path, bsdtar: Path, output: Path, limit: int = 400 * 1024 * 1024) -> dict:
    evidence, bsdtar, output = checked(evidence), checked(bsdtar), checked(output, missing=True)
    if output.exists():
        raise FileExistsError("Use a new source collection directory")
    data = json.loads(evidence.read_text(encoding="utf-8"))
    specs = data["other_linked_source_recipe_bindings"]
    if len(specs) != 11 or any(s.get("recipe_matched") is not True for s in specs):
        raise ValueError("Exactly the reviewed eleven binary-bound recipes required")
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema": 1, "kind": "exact-host-dependency-source-collection",
              "evidence_sha256": sha(evidence), "candidate_host_sha256": data["candidate_host_sha256"],
              "full_tree_extracted": False, "recipes_executed": False,
              "runtime_or_pin_modified": False, "source_collection_complete": False,
              "release_source_gate": False, "components": [], "errors": []}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as workers:
        futures = {workers.submit(collect_one, spec, output, bsdtar, limit): spec for spec in specs}
        for future in concurrent.futures.as_completed(futures):
            try:
                report["components"].append(future.result())
            except Exception as exc:
                report["errors"].append({"package": futures[future]["package"],
                                         "type": type(exc).__name__, "message": str(exc)})
    report["components"].sort(key=lambda item: item["name"])
    report["source_collection_complete"] = len(report["components"]) == len(specs) and not report["errors"]
    (output / "source-collection.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--bsdtar", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = collect(args.evidence, args.bsdtar, args.output)
    print(json.dumps({"complete": report["source_collection_complete"],
                      "components": len(report["components"]), "errors": report["errors"]}, indent=2))
    raise SystemExit(0 if report["source_collection_complete"] else 1)


if __name__ == "__main__":
    main()

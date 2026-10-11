#!/usr/bin/env python3
"""Download a pinned manylinux aarch64 wheel from PyPI and verify it.

Used by build.sh so the image installs Python dependencies offline from
hash-checked files. Standard library only.

    fetch-wheel.py --package sqlite-vec --version 0.1.9 --sha256 HEX --dest DIR
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--sha256", default="", help="expected SHA-256 of the wheel (recommended)")
    ap.add_argument("--dest", required=True)
    ap.add_argument("--index", default="https://pypi.org/pypi")
    args = ap.parse_args()

    norm = args.package.replace("-", "_").lower()
    os.makedirs(args.dest, exist_ok=True)
    for name in os.listdir(args.dest):  # drop other versions of this package
        if name.lower().startswith(norm + "-") and not name.startswith(f"{norm}-{args.version}-"):
            os.remove(os.path.join(args.dest, name))

    with urllib.request.urlopen(f"{args.index}/{args.package}/{args.version}/json", timeout=60) as resp:
        meta = json.load(resp)
    wheels = [
        f for f in meta["urls"]
        if f["packagetype"] == "bdist_wheel" and "manylinux" in f["filename"] and f["filename"].endswith("_aarch64.whl")
    ]
    if not wheels:
        print(f"no manylinux aarch64 wheel for {args.package}=={args.version}", file=sys.stderr)
        return 1
    info = wheels[0]
    expected = args.sha256.lower() or info["digests"]["sha256"]
    if args.sha256 and info["digests"]["sha256"] != expected:
        print(f"PyPI reports sha256 {info['digests']['sha256']} for {info['filename']}, "
              f"but the build config pins {expected}", file=sys.stderr)
        return 1

    path = os.path.join(args.dest, info["filename"])
    if os.path.exists(path) and hashlib.sha256(open(path, "rb").read()).hexdigest() == expected:
        print(f"{info['filename']}: cached, sha256 ok")
        return 0
    with urllib.request.urlopen(info["url"], timeout=300) as resp:
        data = resp.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected:
        print(f"{info['filename']}: sha256 mismatch (got {digest}, expected {expected})", file=sys.stderr)
        return 1
    with open(path + ".part", "wb") as fh:
        fh.write(data)
    os.replace(path + ".part", path)
    if not args.sha256:
        print(f"warning: no pinned hash for {args.package}; recorded {digest}", file=sys.stderr)
    print(f"{info['filename']}: downloaded, sha256 ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())

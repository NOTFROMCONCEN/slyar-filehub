#!/usr/bin/env python3
"""汇总所有发版资产，生成 SHA256SUMS 与 latest.json。

在所有平台的资产合并到同一目录后调用：
  python release/make_manifest.py --dir release/dist
"""

import argparse
import datetime
import hashlib
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = "slyar-filehub"
HOMEPAGE = "https://github.com/NOTFROMCONCEN/slyar-filehub"
SKIP = {"latest.json", "SHA256SUMS"}
MANIFEST_KINDS = {
    ".exe": ("windows", "executable"),
    ".zip": ("windows", "portable"),
    ".deb": ("linux", "package"),
    ".rpm": ("linux", "package"),
    ".gz": ("linux", "portable"),
}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify(name):
    if name.endswith(".tar.gz"):
        return MANIFEST_KINDS[".gz"]
    suffix = pathlib.Path(name).suffix.lower()
    return MANIFEST_KINDS.get(suffix, ("unknown", "asset"))


def read_version(asset_dir):
    version_file = ROOT / "release" / "VERSION"
    if version_file.is_file():
        return version_file.read_text(encoding="utf-8").strip()
    for name in asset_dir.iterdir():
        match = re.search(r"-(\d+\.\d+\.\d+)-", name.name)
        if match:
            return match.group(1)
    return "0.0.0"


def main():
    parser = argparse.ArgumentParser(description="生成发版校验和与 manifest")
    parser.add_argument("--dir", default=str(ROOT / "release" / "dist"))
    args = parser.parse_args()
    asset_dir = pathlib.Path(args.dir).resolve()
    asset_dir.mkdir(parents=True, exist_ok=True)

    assets = sorted(
        (p for p in asset_dir.iterdir() if p.is_file() and p.name not in SKIP),
        key=lambda p: p.name,
    )
    if not assets:
        raise SystemExit(f"目录中没有任何资产: {asset_dir}")

    sums = []
    entries = []
    for path in assets:
        checksum = sha256(path)
        sums.append(f"{checksum}  {path.name}")
        platform_name, kind = classify(path.name)
        entries.append({
            "file": path.name,
            "platform": platform_name,
            "arch": "amd64",
            "kind": kind,
            "size": path.stat().st_size,
            "sha256": checksum,
        })

    (asset_dir / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    manifest = {
        "name": APP,
        "version": read_version(asset_dir),
        "released_at": now.isoformat(timespec="seconds"),
        "repository": HOMEPAGE,
        "assets": entries,
    }
    (asset_dir / "latest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已生成 latest.json / SHA256SUMS，共 {len(entries)} 个资产")
    for entry in entries:
        print(f"  [{entry['platform']}/{entry['kind']}] {entry['file']}")


if __name__ == "__main__":
    main()

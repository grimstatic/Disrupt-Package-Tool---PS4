#!/usr/bin/env python3
"""
build_namelist.py - (re)builds namelist.dat from the community-maintained
Watch_Dogs file lists, so fat3tool.py can resolve real filenames instead of
bare hashes when unpacking any of the game's .fat/.dat archives.

Source: https://github.com/Open-Source-Modding/WatchDogs-File-Lists
        (a fork of https://github.com/gibbed/WatchDogs-File-Lists)

You normally do NOT need to run this - a namelist.dat is already shipped
alongside fat3tool.py. Run this only if you want to pull in an updated file
list later (the source repo is community-maintained and may resolve more
names over time), or want to point it at a different fork/branch.

Usage:
    python3 build_namelist.py [output_path] [--repo OWNER/NAME] [--branch BRANCH]

Requires internet access (only needed for this maintenance step - the tool
itself never needs a network connection to unpack/pack archives).
"""
import argparse
import io
import os
import sys
import tarfile
import urllib.request
import zlib


def download_repo_tarball(repo, branch):
    url = f"https://codeload.github.com/{repo}/tar.gz/refs/heads/{branch}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()


def extract_names(tarball_bytes):
    names = set()
    with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith(".filelist"):
                continue
            f = tar.extractfile(member)
            if f is None:
                continue
            for line in f.read().decode("utf-8", "replace").splitlines():
                line = line.strip()
                if line and not line.startswith(";"):
                    names.add(line)
    return names


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", nargs="?", default="namelist.dat")
    ap.add_argument("--repo", default="Open-Source-Modding/WatchDogs-File-Lists")
    ap.add_argument("--branch", default="main")
    args = ap.parse_args()

    print(f"Downloading {args.repo}@{args.branch} ...")
    tarball = download_repo_tarball(args.repo, args.branch)

    print("Extracting names from all .filelist files ...")
    names = extract_names(tarball)
    print(f"Found {len(names)} unique names")

    data = "\n".join(sorted(names)).encode("utf-8")
    compressed = zlib.compress(data, 9)

    with open(args.output, "wb") as f:
        f.write(compressed)
    print(f"Wrote {args.output} ({len(compressed)} bytes compressed, "
          f"{len(data)} bytes uncompressed)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Failed: {e}")
        sys.exit(1)

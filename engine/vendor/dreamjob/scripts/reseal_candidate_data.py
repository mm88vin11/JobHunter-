#!/usr/bin/env python3
"""Re-seal `candidate_data/v1/manifest.json` after hand-editing the fact base.

`candidate.data.load_candidate_data()` hash-checks every file the manifest
pins, on every call — including at bot startup. Editing `career_facts.md` or a
master resume in an editor therefore stops the service from coming back up
until the manifest agrees again, with a `Candidate data manifest mismatch`
that names only the first offending file.

Run this after an edit session:

    python3 scripts/reseal_candidate_data.py            # reseal what drifted
    python3 scripts/reseal_candidate_data.py --check    # report only, exit 1 on drift

`--check` is the one to use in a pre-deploy step: it never writes, so it
cannot bless a file that was changed by accident.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "candidate_data" / "v1"
ROOT = Path(
    os.environ.get("CANDIDATE_DATA_ROOT", "").strip() or DEFAULT_ROOT
).expanduser().resolve()


def drifted(root: Path) -> list[str]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    out = []
    for name, expected in manifest.get("files", {}).items():
        path = root / name
        if not path.exists():
            out.append(name)
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            out.append(name)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift without writing")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()

    names = drifted(args.root)
    if not names:
        print("Манифест совпадает с файлами — пересчёт не нужен.")
        return 0
    if args.check:
        print("Расходятся с манифестом:")
        for name in names:
            print(f"  {name}")
        return 1

    manifest_path = args.root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name in names:
        digest = hashlib.sha256((args.root / name).read_bytes()).hexdigest()
        manifest.setdefault("files", {})[name] = digest
        print(f"  {name} -> {digest[:16]}")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Пересчитано записей: {len(names)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

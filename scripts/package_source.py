"""Build a validated, credential-free source bundle; never publish it."""

import argparse
from pathlib import Path
import zipfile
from check_repository import ROOT, check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "telly-source.zip")
    args = parser.parse_args()
    files, issues = check()
    if issues:
        for path, reason in issues:
            print(f"FAIL {path}: {reason}")
        raise SystemExit("Source checks failed; no bundle written.")
    output = args.output.resolve()
    if output.is_relative_to(ROOT) and not output.is_relative_to(ROOT / "dist"):
        raise SystemExit("Project-local bundles must be written under dist/.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, "telly/" + path.relative_to(ROOT).as_posix())
    print(
        f"Created {output.name}: {len(files)} checked source files, {output.stat().st_size} bytes."
    )


if __name__ == "__main__":
    main()

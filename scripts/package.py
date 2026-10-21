#!/usr/bin/env python3
"""Build the installable plugin ZIP: python3 scripts/package.py -> dist/qgissi-<version>.zip

Install it in QGIS with Plugins > Manage and Install Plugins > Install from ZIP.
"""
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "qgissi")


def main() -> int:
    with open(os.path.join(PLUGIN, "metadata.txt"), encoding="utf-8") as fh:
        version = re.search(r"^version=(.+)$", fh.read(), re.MULTILINE).group(1).strip()
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    target = os.path.join(ROOT, "dist", f"qgissi-{version}.zip")
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for folder, dirs, files in os.walk(PLUGIN):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith((".pyc", ".pyo")) or name == ".DS_Store":
                    continue
                path = os.path.join(folder, name)
                archive.write(path, os.path.relpath(path, ROOT))
                count += 1
        archive.write(os.path.join(ROOT, "LICENSE"), "qgissi/LICENSE")
    print(f"{target} ({count + 1} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

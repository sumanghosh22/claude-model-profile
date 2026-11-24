#!/usr/bin/env python3
"""Build the single-file installer: dist/install-claude-model-profile.sh

The result is one shell script with the whole package embedded. People download one file and
run it. The build is reproducible: the same sources always produce the same bytes (fixed
timestamps, sorted entries), so the printed SHA-256 can be verified.

    build_bundle.py [--output-dir DIR]
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import sys
import tarfile
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent
INCLUDED = (
    "install.sh",
    "scripts/install.py",
    "src",
    "README.md",
    "LICENSE",
    "VERSION",
)
IGNORED_PARTS = {
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".DS_Store",
}
IGNORED_SUFFIXES = (".pyc", ".bak")
BUNDLE_NAME = "install-claude-model-profile.sh"
DEFAULT_ARGS = "--enable --friendly"
FIXED_MTIME = 0
BASE64_LINE_WIDTH = 76
PAYLOAD_MARKER = "__PAYLOAD_BELOW__"
EXECUTABLE_PARTS = ("src/bin/", "install.sh", "scripts/install.py")

HEADER = r"""#!/usr/bin/env bash
# Claude Model Profile @VERSION@ - single-file installer
#
#   bash @NAME@              install and switch routing on
#   bash @NAME@ status       what is installed
#   bash @NAME@ uninstall    remove it (restores your settings)
#   bash @NAME@ --extract DIR  unpack the readable source, install nothing
#
# Requires python3 (3.10+). All other options: see README.md after --extract.
set -euo pipefail

fail() { printf 'install-claude-model-profile: %s\n' "$1" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || fail "python3 (3.10+) is required"

payload_start="$(awk '/^@MARKER@$/ { print NR + 1; exit }' "$0")"
[[ -n "${payload_start}" ]] || fail "installer is damaged (no payload)"

unpack() {
  tail -n +"${payload_start}" "$0" \
    | python3 -I -c 'import base64, sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read(), validate=False))' \
    | tar -xzf - -C "$1" 2>/dev/null || fail "installer is damaged (payload cannot be unpacked)"
}

if [[ "${1:-}" == "--extract" ]]; then
  [[ -n "${2:-}" ]] || fail "--extract needs a directory"
  [[ ! -e "$2" ]] || fail "$2 already exists; choose a new directory"
  mkdir -p -- "$2"
  unpack "$2"
  printf 'Unpacked to %s (see README.md). Nothing was installed.\n' "$2"
  exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
unpack "${work}"
if [[ $# -eq 0 ]]; then set -- @DEFAULT_ARGS@; fi
status=0
python3 -I "${work}/scripts/install.py" "$@" || status=$?
exit "${status}"
"""


def is_included(path: Path) -> bool:
    return not (set(path.parts) & IGNORED_PARTS) and not path.name.endswith(
        IGNORED_SUFFIXES
    )


def collect_files() -> list[Path]:
    files: list[Path] = []
    for entry in INCLUDED:
        root = SOURCE_ROOT / entry
        if not root.exists():
            raise FileNotFoundError(f"missing from the package: {entry}")
        candidates = (
            [root]
            if root.is_file()
            else sorted(p for p in root.rglob("*") if p.is_file() or p.is_symlink())
        )
        for path in candidates:
            if not is_included(path.relative_to(SOURCE_ROOT)):
                continue
            if path.is_symlink():
                raise ValueError(
                    f"refusing to bundle a symlink (it could ship a file from outside the package): {path}"
                )
            files.append(path)
    return sorted(files)


def build_tarball(files: list[Path]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in files:
            relative = path.relative_to(SOURCE_ROOT).as_posix()
            info = tarfile.TarInfo(relative)
            data = path.read_bytes()
            info.size = len(data)
            info.mtime = FIXED_MTIME
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if relative.startswith(EXECUTABLE_PARTS) else 0o644
            archive.addfile(info, io.BytesIO(data))
    # gzip with a fixed header so output is byte-for-byte reproducible
    compressed = io.BytesIO()
    with gzip.GzipFile(
        fileobj=compressed, mode="wb", mtime=FIXED_MTIME, compresslevel=9
    ) as gz:
        gz.write(buffer.getvalue())
    return compressed.getvalue()


def build(output_dir: Path) -> tuple[Path, str]:
    version = (SOURCE_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    encoded = base64.b64encode(build_tarball(collect_files())).decode("ascii")
    lines = [
        encoded[i : i + BASE64_LINE_WIDTH]
        for i in range(0, len(encoded), BASE64_LINE_WIDTH)
    ]
    header = (
        HEADER.replace("@VERSION@", version)
        .replace("@NAME@", BUNDLE_NAME)
        .replace("@DEFAULT_ARGS@", DEFAULT_ARGS)
        .replace("@MARKER@", PAYLOAD_MARKER)
    )
    script = header + PAYLOAD_MARKER + "\n" + "\n".join(lines) + "\n"
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / BUNDLE_NAME
    target.write_bytes(script.encode("utf-8"))
    target.chmod(0o755)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (output_dir / f"{target.name}.sha256").write_text(
        f"{digest}  {target.name}\n", encoding="utf-8"
    )
    return target, digest


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build the single-file installer.")
    parser.add_argument("--output-dir", type=Path, default=SOURCE_ROOT / "dist")
    args = parser.parse_args(argv[1:])
    try:
        target, digest = build(args.output_dir)
    except (OSError, ValueError) as err:
        sys.stderr.write(f"build_bundle: {err}\n")
        return 1
    sys.stdout.write(
        f"Built {target} ({target.stat().st_size // 1024} KB)\nSHA-256 {digest}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

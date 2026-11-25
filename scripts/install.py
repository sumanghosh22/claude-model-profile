#!/usr/bin/env python3
"""Install, inspect or remove Claude Model Profile in ~/.claude.

    install.py                 install or update (does not change your model settings)
    install.py --enable        install, then switch routing on
    install.py status          show what is installed and whether it was modified
    install.py uninstall       switch routing off and remove everything this tool installed

Safety rules:
- Files are tracked by SHA-256 in a manifest. A reinstall updates only files you have not edited.
- If any target file was edited by you (or already exists and is not ours), nothing is changed
  unless you pass --force; --force keeps your version as <file>.bak-<timestamp> first.
- Your ~/.claude/settings.json is only changed through `model-profile`: `on|off`, and after an
  install `install-hooks`, which registers this version's hooks (the daily update check;
  refreshed routing hooks while routing is on) and writes nothing otherwise.
- `uninstall` keeps the on/off history unless you pass --purge.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess  # nosec B404 - only runs our own installed script, with fixed arguments
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MIN_PYTHON = (3, 10)
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
HASH_CHUNK_BYTES = 1 << 16
SOURCE_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_NAME = "install-manifest.json"
EXECUTABLE_MODE = 0o755
REGULAR_MODE = 0o644
BACKUP_SUFFIX = ".bak"
PRIVATE_UMASK = 0o077


@dataclass(frozen=True)
class Rule:
    """Copy every file in `source` (relative to the repo) into `destination` (relative to ~/.claude)."""

    source: str
    destination: str
    mode: int
    recursive: bool = (
        False  # keep the folder structure below `source` (skills are folders)
    )


RULES: tuple[Rule, ...] = (
    Rule("src/bin", "bin", EXECUTABLE_MODE),
    Rule("src/commands", "commands", REGULAR_MODE),
    Rule("src/agents", "model-profile/agents", REGULAR_MODE),
    Rule("src/standards", "model-profile/standards", REGULAR_MODE),
    Rule("src/skills", "model-profile/skills", REGULAR_MODE, recursive=True),
    # the release channel and signing key that automatic updates trust
    Rule("src/channel", "model-profile/channel", REGULAR_MODE),
)
VERSION_DESTINATION = "model-profile/VERSION"
PRODUCT_NAME = "Claude Model Profile"
ROUTING_SUMMARY = "Opus plans, Sonnet builds, Haiku searches"
CHECK_MARK = "\u2713"
CHECK_MARK_FALLBACK = "[ok]"
EMPTY_DIRS_TO_PRUNE = (
    "model-profile/agents",
    "model-profile/channel",
    "model-profile/standards",
    "model-profile/review",
    "model-profile/skills",
    "model-profile",
    "commands",
    "bin",
)
RUNTIME_FILES = (
    "model-profile/.lock",
    "model-profile/.update.lock",
    "model-profile/.update-state.lock",
    "model-profile/update-state.json",
)
# versions before this one have no `uninstall-hooks` command and never registered its hook
FIRST_VERSION_WITH_HOOK_COMMANDS = (1, 11, 0)
UPDATE_LOCK = "model-profile/.update.lock"
# set by the automatic updater, which holds UPDATE_LOCK while it runs this installer
UPDATER_ENV = "CLAUDE_MODEL_PROFILE_UPDATING"
# per-session review-gate state (only files this tool names: <session>.json, never links)
RUNTIME_DIRS = ("model-profile/review",)
RUNTIME_SUFFIX = ".json"
PURGE_FILES = ("model-profile/history.jsonl",)


class InstallError(Exception):
    """A condition that must stop the command with a clear message."""


@dataclass(frozen=True)
class PlannedFile:
    source: Path
    destination: str
    mode: int


def claude_dir() -> Path:
    return Path.home() / ".claude"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_path() -> Path:
    return claude_dir() / "model-profile" / MANIFEST_NAME


def is_safe_destination(name: str) -> bool:
    """A manifest entry may only name a file this installer could have put in ~/.claude."""
    parts = Path(name).parts
    if Path(name).is_absolute() or ".." in parts or not parts:
        return False
    return name == VERSION_DESTINATION or any(
        name.startswith(rule.destination + "/") for rule in RULES
    )


def read_manifest() -> dict[str, str]:
    path = manifest_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        files = raw["files"]
    except (OSError, ValueError, KeyError, TypeError) as err:
        raise InstallError(
            f"install manifest is damaged ({path}); remove it and reinstall with --force: {err}"
        ) from err
    if not isinstance(files, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in files.items()
    ):
        raise InstallError(
            f"install manifest is damaged ({path}); remove it and reinstall with --force"
        )
    unsafe = [name for name in files if not is_safe_destination(name)]
    if unsafe:
        raise InstallError(
            f"install manifest lists paths outside the install folders ({unsafe[0]!r}); "
            f"it was tampered with. Remove {path} and reinstall with --force"
        )
    return files


def write_manifest(files: dict[str, str], version: str) -> None:
    write_atomic(
        manifest_path(),
        json.dumps(
            {"version": version, "files": files}, indent=2, sort_keys=True
        ).encode()
        + b"\n",
        REGULAR_MODE,
    )


def write_atomic(destination: Path, content: bytes, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        temp_path.replace(destination)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def read_version() -> str:
    try:
        return (SOURCE_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError as err:
        raise InstallError(f"cannot read VERSION: {err}") from err


def plan_files() -> list[PlannedFile]:
    planned: list[PlannedFile] = []
    for rule in RULES:
        source_dir = SOURCE_ROOT / rule.source
        if not source_dir.is_dir():
            raise InstallError(f"missing source directory: {source_dir}")
        candidates = source_dir.rglob("*") if rule.recursive else source_dir.iterdir()
        for source in sorted(
            p for p in candidates if p.is_file() and not p.is_symlink()
        ):
            relative = source.relative_to(source_dir).as_posix()
            planned.append(
                PlannedFile(source, f"{rule.destination}/{relative}", rule.mode)
            )
    planned.append(
        PlannedFile(SOURCE_ROOT / "VERSION", VERSION_DESTINATION, REGULAR_MODE)
    )
    return planned


def find_conflicts(planned: list[PlannedFile], manifest: dict[str, str]) -> list[str]:
    """Files that exist but are neither ours-and-unmodified nor identical to what we would write."""
    conflicts = []
    for item in planned:
        target = claude_dir() / item.destination
        if not (target.exists() or target.is_symlink()):
            continue
        if target.is_symlink() or not target.is_file():
            conflicts.append(item.destination)
            continue
        current = sha256_of(target)
        if (
            current == sha256_of(item.source)
            or manifest.get(item.destination) == current
        ):
            continue
        conflicts.append(item.destination)
    return conflicts


def plan_backups(conflicts: list[str]) -> list[tuple[Path, Path]]:
    """Choose a new, unused backup name for every file --force will replace, before any write."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    plan: list[tuple[Path, Path]] = []
    for name in conflicts:
        original = claude_dir() / name
        if original.is_dir() and not original.is_symlink():
            raise InstallError(f"{original} is a directory; move it away and run again")
        counter = 0
        backup = original.with_name(f"{original.name}{BACKUP_SUFFIX}-{stamp}")
        while os.path.lexists(backup):
            counter += 1
            backup = original.with_name(
                f"{original.name}{BACKUP_SUFFIX}-{stamp}-{counter}"
            )
        plan.append((original, backup))
    return plan


def copy_exclusive(source: Path, destination: Path) -> None:
    """Copy a file to a path that must not exist (O_EXCL: never follows or overwrites a link)."""
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer)
    shutil.copystat(source, destination)


def previous_version() -> str | None:
    """The version recorded by the last install, or None if there was none (or it is unreadable)."""
    try:
        raw = json.loads(manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    recorded = raw.get("version") if isinstance(raw, dict) else None
    return recorded if isinstance(recorded, str) and recorded else None


def installed_version_tuple() -> tuple[int, ...]:
    """The installed version as numbers; (0,) when unknown, so newer commands are not assumed."""
    recorded = previous_version() or ""
    parts = recorded.split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else (0,)


def check_mark() -> str:
    encoding = (sys.stdout.encoding or "").lower().replace("-", "")
    return CHECK_MARK if encoding == "utf8" else CHECK_MARK_FALLBACK


def friendly_summary(version: str, previous: str | None, routing_on: bool) -> str:
    """What a person installing this for the first time needs to read, and nothing else."""
    mark = check_mark()
    if previous is None:
        action = "Installed"
    elif previous == version:
        action = "Already installed, files refreshed"
    else:
        action = f"Updated (was {previous})"
    routing = (
        f"Routing is on: {ROUTING_SUMMARY}"
        if routing_on
        else "Routing is off. Switch it on with /model-profile on"
    )
    return (
        f"{PRODUCT_NAME} {version}\n\n"
        f"  {mark} {action}\n"
        f"  {mark} {routing}\n\n"
        "Next:\n"
        "  1. Restart Claude Code and open a new conversation\n"
        "  2. Type /model-profile to check it is on\n\n"
        "Switch it off any time with /model-profile off.\n"
    )


def install(enable: bool, dry_run: bool, force: bool, friendly: bool = False) -> int:
    version = read_version()
    planned = plan_files()
    manifest = read_manifest()
    previous = previous_version()
    conflicts = find_conflicts(planned, manifest)
    if conflicts and not force:
        listing = "\n  ".join(conflicts)
        raise InstallError(
            "these files exist and were not installed by this tool, or you edited them:\n  "
            f"{listing}\nNothing was changed. Re-run with --force to keep your copy as <file>{BACKUP_SUFFIX}-<timestamp> and install."
        )

    if dry_run:
        sys.stdout.write(
            f"Dry run: would install claude-model-profile {version} ({len(planned)} files) into {claude_dir()}\n"
        )
        for item in planned:
            sys.stdout.write(f"  {item.destination}\n")
        return EXIT_OK

    backups = plan_backups(conflicts)
    for original, backup in backups:
        copy_exclusive(original, backup)
        sys.stdout.write(f"Kept your version as {backup.name}\n")

    installed: dict[str, str] = {}
    for item in planned:
        target = claude_dir() / item.destination
        write_atomic(target, item.source.read_bytes(), item.mode)
        installed[item.destination] = sha256_of(target)

    stale = [name for name in manifest if name not in installed]
    for name in stale:
        target = claude_dir() / name
        if target.is_file() and sha256_of(target) == manifest[name]:
            target.unlink()
    write_manifest(installed, version)
    run_profile("install-hooks", quiet=True)
    if friendly:
        if enable and not profile_is_on():
            run_profile("on", quiet=True)
        sys.stdout.write(friendly_summary(version, previous, profile_is_on()))
        return EXIT_OK
    sys.stdout.write(
        f"Installed claude-model-profile {version} ({len(installed)} files) into {claude_dir()}\n"
    )

    if profile_is_on():
        sys.stdout.write(
            "Routing is already ON and keeps running with the updated files.\n"
        )
    elif enable:
        run_profile("on")
    else:
        sys.stdout.write(
            "Routing is not switched on yet. Run `/model-profile on` (or re-run with --enable).\n"
        )
    sys.stdout.write("Restart Claude Code so it loads the new commands and agents.\n")
    return EXIT_OK


def run_profile(action: str, quiet: bool = False) -> None:
    script = claude_dir() / "bin" / "model-profile"
    result = subprocess.run(  # nosec B603 - argv is [python, our script, 'on'|'off'], no shell
        [sys.executable, str(script), action],
        capture_output=True,
        text=True,
        check=False,
    )
    if not quiet:
        sys.stdout.write(result.stdout)
    if result.returncode != 0:
        raise InstallError(f"`model-profile {action}` failed: {result.stderr.strip()}")


def profile_is_on() -> bool:
    return (claude_dir() / "model-profile" / "state.json").exists()


@contextmanager
def no_update_running() -> Iterator[None]:
    """Install and uninstall never run while an automatic update is installing files."""
    lock = claude_dir() / UPDATE_LOCK
    if os.environ.get(UPDATER_ENV) or not lock.parent.is_dir():
        yield
        return
    with lock.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError(
                "an automatic update is being installed right now; nothing was changed. "
                "Try again in a minute."
            ) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def prune_empty_tree(root: Path) -> None:
    """Remove empty folders below `root`, deepest first; never follows links."""
    if not root.is_dir() or root.is_symlink():
        return
    folders = [p for p in root.rglob("*") if p.is_dir() and not p.is_symlink()]
    for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
        if not any(folder.iterdir()):
            folder.rmdir()


def uninstall(purge: bool, force: bool) -> int:
    manifest = read_manifest()
    if not manifest:
        sys.stdout.write("Nothing to uninstall: no install manifest found.\n")
        return EXIT_OK
    modified = [
        n
        for n, digest in manifest.items()
        if (claude_dir() / n).is_file() and sha256_of(claude_dir() / n) != digest
    ]
    if modified and not force:
        listing = "\n  ".join(modified)
        raise InstallError(
            f"you edited these files; nothing was removed:\n  {listing}\nRe-run with --force to remove them anyway."
        )

    if profile_is_on():
        run_profile("off")
    if installed_version_tuple() >= FIRST_VERSION_WITH_HOOK_COMMANDS:
        run_profile("uninstall-hooks", quiet=True)
    for name in manifest:
        (claude_dir() / name).unlink(missing_ok=True)
    for name in RUNTIME_FILES:
        (claude_dir() / name).unlink(missing_ok=True)
    for name in RUNTIME_DIRS:
        directory = claude_dir() / name
        if directory.is_dir() and not directory.is_symlink():
            for state in directory.glob(f"*{RUNTIME_SUFFIX}"):
                if state.is_file() and not state.is_symlink():
                    state.unlink()
    manifest_path().unlink(missing_ok=True)
    if purge:
        for name in PURGE_FILES:
            (claude_dir() / name).unlink(missing_ok=True)
    for rule in RULES:
        if rule.recursive:
            prune_empty_tree(claude_dir() / rule.destination)
    for name in EMPTY_DIRS_TO_PRUNE:
        directory = claude_dir() / name
        if (
            directory.is_dir()
            and not directory.is_symlink()
            and not any(directory.iterdir())
        ):
            directory.rmdir()
    sys.stdout.write(
        "Removed claude-model-profile. Your on/off history was kept.\n"
        if not purge
        else "Removed claude-model-profile and its data.\n"
    )
    return EXIT_OK


def status() -> int:
    manifest = read_manifest()
    if not manifest:
        sys.stdout.write("claude-model-profile is not installed.\n")
        return EXIT_OK
    modified = [
        n
        for n, digest in manifest.items()
        if (claude_dir() / n).is_file() and sha256_of(claude_dir() / n) != digest
    ]
    missing = [n for n in manifest if not (claude_dir() / n).exists()]
    try:
        version = json.loads(manifest_path().read_text(encoding="utf-8")).get(
            "version", "unknown"
        )
    except (OSError, ValueError):
        version = "unknown"
    sys.stdout.write(
        f"Installed: claude-model-profile {version} ({len(manifest)} files)\n"
    )
    sys.stdout.write(f"Routing: {'ON' if profile_is_on() else 'OFF'}\n")
    sys.stdout.write(f"Edited by you: {', '.join(modified) if modified else 'none'}\n")
    sys.stdout.write(f"Missing: {', '.join(missing) if missing else 'none'}\n")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install.py", description="Install Claude Model Profile into ~/.claude."
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("install", "uninstall", "status"),
        default="install",
    )
    parser.add_argument(
        "--enable", action="store_true", help="switch routing on after installing"
    )
    parser.add_argument(
        "--friendly",
        action="store_true",
        help="install: print a short, plain result instead of the technical details",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be installed, change nothing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite (keeping a .bak) or remove files you edited",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="uninstall: also delete the on/off history",
    )
    return parser


def main(argv: list[str]) -> int:
    if sys.version_info < MIN_PYTHON:
        sys.stderr.write(
            f"install.py: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required (found {sys.version.split()[0]})\n"
        )
        return EXIT_FAILURE
    os.umask(
        PRIVATE_UMASK
    )  # new files and folders are owner-only, whatever the shell's umask
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        sys.stderr.write(
            "install.py: do not run with sudo; it would make your ~/.claude root-owned\n"
        )
        return EXIT_FAILURE
    args = build_parser().parse_args(argv[1:])
    if args.command != "install" and (args.enable or args.dry_run or args.friendly):
        sys.stderr.write(
            "install.py: --enable, --dry-run and --friendly only apply to install\n"
        )
        return EXIT_USAGE
    if args.command != "uninstall" and args.purge:
        sys.stderr.write("install.py: --purge only applies to uninstall\n")
        return EXIT_USAGE
    try:
        if args.command == "status":
            return status()
        if args.dry_run:
            return install(args.enable, args.dry_run, args.force, args.friendly)
        with no_update_running():
            if args.command == "uninstall":
                return uninstall(args.purge, args.force)
            return install(args.enable, args.dry_run, args.force, args.friendly)
    except (InstallError, OSError) as err:
        sys.stderr.write(f"install.py: {err}\n")
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main(sys.argv))

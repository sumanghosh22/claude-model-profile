"""Tests for the single-file installer built by scripts/build_bundle.py.

The installer is one shell script with the whole package inside. Running it with no
arguments installs the tool and switches routing on, printing a short plain result.
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from loader import load_script
from paths import ROOT

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
BUILD_SCRIPT = ROOT / "scripts" / "build_bundle.py"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
BUNDLE_NAME = "install-claude-model-profile.sh"
BASELINE_SETTINGS = {"model": "sonnet", "effortLevel": "high"}
# every install registers exactly this one hook, outside routing, for daily signed updates
UPDATE_HOOK_SETTINGS = {
    "hooks": {
        "SessionStart": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": 'python3 -I "$HOME/.claude/bin/model-profile" '
                        "auto-update 2>/dev/null || true",
                        "timeout": 10,
                    }
                ]
            }
        ]
    }
}
SHIPPED_FILES = (
    "README.md",
    "LICENSE",
    "VERSION",
    "install.sh",
    "scripts/install.py",
    "src/bin/model-profile",
    "src/commands/model-profile.md",
    "src/commands/complex.md",
    "src/agents/Explore.md",
    "src/agents/code-reviewer.md",
    "src/agents/deep-reasoner.md",
    "src/agents/log-reader.md",
    "src/skills/deep-work/SKILL.md",
    "src/skills/critical-work/SKILL.md",
    "src/standards/standards.md",
)


class BundleTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build_dir = Path(tempfile.mkdtemp(prefix="routing-bundle-build-"))
        result = subprocess.run(
            [sys.executable, str(BUILD_SCRIPT), "--output-dir", str(cls.build_dir)],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert result.returncode == EXIT_OK, result.stderr
        cls.bundle = cls.build_dir / BUNDLE_NAME
        cls.build_output = result.stdout

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.build_dir, ignore_errors=True)

    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="routing-bundle-test-"))
        self.addCleanup(shutil.rmtree, self.workspace, True)
        self.home = self.workspace / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.settings = self.home / ".claude" / "settings.json"
        self.settings.write_text(json.dumps(BASELINE_SETTINGS) + "\n", encoding="utf-8")

    def installed(self, *parts: str) -> Path:
        return self.home.joinpath(".claude", *parts)

    def run_bundle(
        self, *args: str, bundle: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(bundle or self.bundle), *args],
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )


class TestBuild(BundleTestCase):
    def test_build_is_reproducible_byte_for_byte(self) -> None:
        other = Path(tempfile.mkdtemp(prefix="routing-bundle-build2-"))
        self.addCleanup(shutil.rmtree, other, True)
        subprocess.run(
            [sys.executable, str(BUILD_SCRIPT), "--output-dir", str(other)],
            check=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(self.bundle.read_bytes(), (other / BUNDLE_NAME).read_bytes())

    def test_checksum_file_matches_and_build_prints_it(self) -> None:
        digest = hashlib.sha256(self.bundle.read_bytes()).hexdigest()
        checksum = (self.build_dir / f"{BUNDLE_NAME}.sha256").read_text()
        self.assertEqual(checksum.split()[0], digest)
        self.assertIn(BUNDLE_NAME, checksum)
        self.assertIn(digest, self.build_output)

    def test_bundle_is_a_single_small_executable_file(self) -> None:
        self.assertTrue(os.access(self.bundle, os.X_OK))
        self.assertLess(self.bundle.stat().st_size, 1_000_000)
        self.assertTrue(
            self.bundle.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
        )

    def test_header_names_the_file_and_the_one_command(self) -> None:
        header = "\n".join(self.bundle.read_text(encoding="utf-8").splitlines()[:8])
        self.assertIn(f"Claude Model Profile {VERSION}", header)
        self.assertRegex(header, rf"bash {BUNDLE_NAME}\s+install and switch routing on")

    def test_bundle_never_contains_caches_or_backups(self) -> None:
        extract = self.workspace / "src-check"
        self.run_bundle("--extract", str(extract))
        names = {p.name for p in extract.rglob("*")}
        self.assertFalse(names & {"__pycache__", ".mypy_cache", ".DS_Store"})
        self.assertFalse([n for n in names if n.endswith((".pyc", ".bak"))])


class TestExtract(BundleTestCase):
    def test_extract_unpacks_readable_source_and_installs_nothing(self) -> None:
        target = self.workspace / "unpacked"
        result = self.run_bundle("--extract", str(target))
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        for relative in SHIPPED_FILES:
            self.assertTrue((target / relative).is_file(), relative)
        self.assertTrue(os.access(target / "src" / "bin" / "model-profile", os.X_OK))
        self.assertFalse((self.home / ".claude" / "bin").exists())
        self.assertEqual(json.loads(self.settings.read_text()), BASELINE_SETTINGS)

    def test_extracted_files_match_the_repository_exactly(self) -> None:
        target = self.workspace / "unpacked"
        self.run_bundle("--extract", str(target))
        for relative in SHIPPED_FILES:
            self.assertEqual(
                (target / relative).read_bytes(),
                (ROOT / relative).read_bytes(),
                relative,
            )

    def test_extract_refuses_an_existing_directory_and_missing_argument(self) -> None:
        existing = self.workspace / "exists"
        existing.mkdir()
        (existing / "keep.txt").write_text("keep", encoding="utf-8")
        result = self.run_bundle("--extract", str(existing))
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertIn("already exists", result.stderr)
        self.assertEqual((existing / "keep.txt").read_text(), "keep")
        self.assertEqual(self.run_bundle("--extract").returncode, EXIT_FAILURE)


class TestOneCommandInstall(BundleTestCase):
    def test_no_arguments_installs_and_switches_routing_on(self) -> None:
        result = self.run_bundle()
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        settings = json.loads(self.settings.read_text())
        self.assertEqual(
            (settings["model"], settings["effortLevel"]), ("opusplan", "medium")
        )
        out = result.stdout
        self.assertIn(f"Claude Model Profile {VERSION}", out)
        self.assertIn("✓ Installed", out)
        self.assertIn("Routing is on: Opus plans, Sonnet builds, Haiku searches", out)
        self.assertIn("Restart Claude Code and open a new conversation", out)
        self.assertIn("/model-profile off", out)
        self.assertLess(len(out.splitlines()), 14, "short enough to read at a glance")
        for jargon in ("manifest", "subagents", "effortLevel", "files)", "auto-off"):
            self.assertNotIn(jargon, out)
        self.assertEqual(result.stderr, "")

    def test_the_tool_and_its_skills_are_installed(self) -> None:
        self.run_bundle()
        for relative in (
            ("bin", "model-profile"),
            ("commands", "model-profile.md"),
            ("commands", "complex.md"),
            ("model-profile", "standards", "standards.md"),
            ("skills", "deep-work", "SKILL.md"),
            ("skills", "critical-work", "SKILL.md"),
        ):
            self.assertTrue(self.installed(*relative).is_file(), relative)

    def test_running_it_again_says_so_and_keeps_routing_on(self) -> None:
        self.run_bundle()
        again = self.run_bundle()
        self.assertEqual(again.returncode, EXIT_OK, again.stderr)
        self.assertIn("Already installed, files refreshed", again.stdout)
        self.assertIn("Routing is on", again.stdout)
        self.assertEqual(json.loads(self.settings.read_text())["model"], "opusplan")

    def test_an_upgrade_says_which_version_it_replaced(self) -> None:
        self.run_bundle()
        manifest = self.installed("model-profile", "install-manifest.json")
        recorded = json.loads(manifest.read_text())
        recorded["version"] = "0.9.0"
        manifest.write_text(json.dumps(recorded), encoding="utf-8")
        self.assertIn("Updated (was 0.9.0)", self.run_bundle().stdout)

    def test_friendly_without_enable_leaves_routing_off(self) -> None:
        result = self.run_bundle("--friendly")
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertIn(
            "Routing is off. Switch it on with /model-profile on", result.stdout
        )
        self.assertEqual(
            json.loads(self.settings.read_text()),
            {**BASELINE_SETTINGS, **UPDATE_HOOK_SETTINGS},
            "model settings untouched; only the update hook is added",
        )

    def test_a_terminal_without_unicode_gets_a_plain_marker(self) -> None:
        module = load_script(
            ROOT / "scripts" / "install.py", self.home, "install_marker_check"
        )
        original = sys.stdout
        try:
            for encoding, expected in (
                ("utf-8", "✓"),
                ("UTF8", "✓"),
                ("ascii", "[ok]"),
                ("latin-1", "[ok]"),
            ):
                with self.subTest(encoding=encoding):
                    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding=encoding)
                    self.assertEqual(module.check_mark(), expected)
        finally:
            sys.stdout = original

    def test_friendly_only_applies_to_install(self) -> None:
        result = self.run_bundle("status", "--friendly")
        self.assertEqual(result.returncode, EXIT_USAGE)
        self.assertIn("only apply to install", result.stderr)

    def test_a_conflict_stops_with_a_clear_message_and_changes_nothing(self) -> None:
        target = self.installed("bin", "model-profile")
        target.parent.mkdir(parents=True)
        target.write_text("my own script", encoding="utf-8")
        result = self.run_bundle()
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertIn("Nothing was changed", result.stderr)
        self.assertEqual(target.read_text(), "my own script")
        self.assertEqual(json.loads(self.settings.read_text()), BASELINE_SETTINGS)

    def test_routing_works_end_to_end_and_off_restores_the_settings(self) -> None:
        self.run_bundle()
        cli = self.installed("bin", "model-profile")
        env = {**os.environ, "HOME": str(self.home)}
        status = subprocess.run(
            [sys.executable, "-I", str(cli), "status"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertIn("profile: ON", status.stdout)
        self.assertIn("review gate: active", status.stdout)
        prompt = subprocess.run(
            [sys.executable, "-I", str(cli), "prompt-start"],
            input=json.dumps(
                {
                    "session_id": "s1",
                    "cwd": str(self.home),
                    "prompt": "fix the stripe webhook race condition",
                }
            ),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(prompt.returncode, EXIT_OK, prompt.stderr)
        self.assertIn("critical-work", prompt.stdout)
        subprocess.run(
            [sys.executable, "-I", str(cli), "off"],
            env=env,
            capture_output=True,
            check=True,
        )
        self.assertEqual(
            json.loads(self.settings.read_text()),
            {**BASELINE_SETTINGS, **UPDATE_HOOK_SETTINGS},
            "off restores the model settings and keeps updates running",
        )

    def test_status_dry_run_and_uninstall_pass_through(self) -> None:
        self.assertIn("not installed", self.run_bundle("status").stdout)
        dry = self.run_bundle("--dry-run")
        self.assertIn("Dry run", dry.stdout)
        self.assertFalse((self.home / ".claude" / "bin").exists())
        self.run_bundle()
        self.assertIn("Routing: ON", self.run_bundle("status").stdout)
        result = self.run_bundle("uninstall")
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertIn("Your on/off history was kept.", result.stdout)
        self.assertFalse(self.installed("bin", "model-profile").exists())
        self.assertFalse(self.installed("model-profile", "skills").exists())
        self.assertEqual(json.loads(self.settings.read_text()), BASELINE_SETTINGS)

    def test_exit_codes_and_messages_pass_through(self) -> None:
        result = self.run_bundle("uninstall", "--enable")
        self.assertEqual(result.returncode, EXIT_USAGE)
        self.assertNotIn("Traceback", result.stderr)

    def test_installed_version_matches_the_bundle(self) -> None:
        self.run_bundle()
        version = subprocess.run(
            [sys.executable, str(self.installed("bin", "model-profile")), "version"],
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertIn(VERSION, version.stdout)

    def test_leaves_no_temporary_files_behind(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="routing-bundle-tmp-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        subprocess.run(
            ["bash", str(self.bundle), "--dry-run"],
            env={**os.environ, "HOME": str(self.home), "TMPDIR": str(tmp)},
            capture_output=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(list(tmp.iterdir()), [])


class TestDamagedBundle(BundleTestCase):
    def damaged_copy(self, mutate) -> Path:  # type: ignore[no-untyped-def]
        copy = self.workspace / "damaged.sh"
        copy.write_text(
            mutate(self.bundle.read_text(encoding="utf-8")), encoding="utf-8"
        )
        return copy

    def test_corrupted_payload_fails_with_a_clear_message(self) -> None:
        copy = self.damaged_copy(lambda text: text.replace("H4sI", "XXXX", 1))
        result = self.run_bundle("status", bundle=copy)
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertIn("damaged", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((self.home / ".claude" / "bin").exists())

    def test_truncated_download_is_detected(self) -> None:
        copy = self.damaged_copy(lambda text: text[: len(text) // 2])
        result = self.run_bundle(bundle=copy)
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertFalse((self.home / ".claude" / "bin").exists())
        self.assertEqual(json.loads(self.settings.read_text()), BASELINE_SETTINGS)

    def test_missing_payload_marker_is_detected(self) -> None:
        copy = self.damaged_copy(
            lambda text: "nope\n".join(text.rsplit("__PAYLOAD_BELOW__\n", 1))
        )
        result = self.run_bundle("status", bundle=copy)
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertIn("damaged", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

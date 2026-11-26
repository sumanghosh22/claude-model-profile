"""Security regression tests: one or more tests for every finding of the security review.

Each test names the attack it blocks. They run the real tools in throwaway HOME folders.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from loader import load_script
from paths import AGENTS_DIR, PROFILE_CLI, ROOT
from test_model_profile import (
    CONTEXT_COMMAND,
    LEGACY_CONTEXT_COMMAND,
    LEGACY_SYNC_COMMAND,
    SYNC_COMMAND,
    USER_HOOKS,
    SandboxTestCase,
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
PRIVATE_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
MAX_LINE_CHARS = 16 * 1024 * 1024


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestHookIsolation(SandboxTestCase):
    """A hook fires on every prompt from the user's project folder: it must not import code from there."""

    def plant_trap(self) -> tuple[Path, Path]:
        project = self.home / "some-project"
        project.mkdir()
        marker = self.home / "trap-ran"
        (project / "json.py").write_text(
            f"open({str(marker)!r}, 'w').write('planted code ran')\n", encoding="utf-8"
        )
        return project, marker

    def run_in(self, project: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, *args, str(PROFILE_CLI), "sync"],
            cwd=project,
            env={**os.environ, "HOME": str(self.home), "PYTHONPATH": str(project)},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def test_the_trap_really_works_without_isolation(self) -> None:
        project, marker = self.plant_trap()
        self.run_in(project)
        self.assertTrue(
            marker.exists(), "control: the planted module must run when not isolated"
        )

    def test_isolated_hook_never_runs_planted_modules(self) -> None:
        project, marker = self.plant_trap()
        self.run_in(project, "-I")
        self.assertFalse(marker.exists())

    def test_every_registered_hook_command_uses_isolated_python(self) -> None:
        self.run_cli("on")
        commands = [
            hook["command"]
            for groups in self.read_settings()["hooks"].values()
            for group in groups
            for hook in group["hooks"]
            if "model-profile" in hook["command"]
        ]
        self.assertTrue(commands)
        for command in commands:
            self.assertTrue(command.startswith("python3 -I "), command)


class TestLegacyHookUpgrade(SandboxTestCase):
    def downgrade_hooks_to_legacy(self) -> None:
        text = self.settings_path.read_text(encoding="utf-8")
        text = text.replace(
            json.dumps(SYNC_COMMAND)[1:-1], json.dumps(LEGACY_SYNC_COMMAND)[1:-1]
        )
        text = text.replace(
            json.dumps(CONTEXT_COMMAND)[1:-1], json.dumps(LEGACY_CONTEXT_COMMAND)[1:-1]
        )
        self.settings_path.write_text(text, encoding="utf-8")

    def commands(self) -> list[str]:
        return [
            hook["command"]
            for groups in self.read_settings().get("hooks", {}).values()
            for group in groups
            for hook in group["hooks"]
        ]

    def test_status_flags_old_hooks_and_repair_upgrades_them_without_duplicates(
        self,
    ) -> None:
        self.write_settings({**BASELINE_SETTINGS_WITH_USER_HOOKS})
        self.run_cli("on")
        self.downgrade_hooks_to_legacy()
        self.assertIn("auto-off: inactive", self.run_cli("status").stdout)
        self.assertEqual(self.run_cli("repair").returncode, EXIT_OK)
        commands = self.commands()
        self.assertNotIn(LEGACY_SYNC_COMMAND, commands)
        self.assertNotIn(LEGACY_CONTEXT_COMMAND, commands)
        # PostModelSwitch + SessionStart; UserPromptSubmit runs prompt-start, which syncs too
        self.assertEqual(commands.count(SYNC_COMMAND), 2)
        self.assertEqual(commands.count(CONTEXT_COMMAND), 1)
        self.assertEqual(sum("prompt-start" in c for c in commands), 1)
        self.assertIn(
            "echo mine", commands, "the user's own hooks must survive the upgrade"
        )
        self.assertIn("auto-off: active", self.run_cli("status").stdout)

    def test_off_removes_old_style_hooks_too(self) -> None:
        self.write_settings({**BASELINE_SETTINGS_WITH_USER_HOOKS})
        self.run_cli("on")
        self.downgrade_hooks_to_legacy()
        self.run_cli("off")
        self.assertEqual(self.read_settings(), BASELINE_SETTINGS_WITH_USER_HOOKS)


BASELINE_SETTINGS_WITH_USER_HOOKS = {
    "env": {"PATH": "/usr/bin"},
    "model": "sonnet",
    "effortLevel": "low",
    "hooks": USER_HOOKS,
}


class TestPrivateFiles(SandboxTestCase):
    """Files the tools create must be owner-only even when the shell's umask is permissive."""

    def run_with_umask(
        self, *args: str, stdin: str = ""
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(PROFILE_CLI), *args],
            input=stdin,
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            preexec_fn=lambda: os.umask(0o022),
        )

    def test_state_history_and_lock_are_owner_only(self) -> None:
        self.assertEqual(self.run_with_umask("on").returncode, EXIT_OK)
        profile = self.claude_dir / "model-profile"
        for name in ("history.jsonl", ".lock", "state.json"):
            with self.subTest(file=name):
                self.assertEqual(mode_of(profile / name), PRIVATE_FILE_MODE)

    def test_the_hook_log_is_owner_only(self) -> None:
        self.run_with_umask("on")
        self.settings_path.write_text("{oops", encoding="utf-8")
        self.run_with_umask("sync")
        self.assertEqual(
            mode_of(self.claude_dir / "model-profile" / "sync.log"), PRIVATE_FILE_MODE
        )

    def test_settings_keep_their_original_mode(self) -> None:
        self.settings_path.chmod(0o640)
        self.run_with_umask("on")
        self.assertEqual(mode_of(self.settings_path), 0o640)


class TestSyncLogRobustness(SandboxTestCase):
    """A damaged log must never switch the safety net (auto-off) off."""

    def sync(self, stdin: str = "{}") -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(PROFILE_CLI), "sync"],
            input=stdin,
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    @property
    def log(self) -> Path:
        return self.claude_dir / "model-profile" / "sync.log"

    def test_binary_garbage_in_an_oversized_log_is_rotated_not_read(self) -> None:
        self.run_cli("on")
        self.log.write_bytes(b"\xff\xfe\x00garbage\n" * 40_000)
        self.assertGreater(self.log.stat().st_size, 256 * 1024)
        (self.claude_dir / "model-profile" / "debug").write_text("", encoding="utf-8")
        self.write_settings({**self.read_settings(), "model": "opus"})
        result = self.sync()
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertIn(
            "Routing switched OFF", json.loads(result.stdout)["systemMessage"]
        )
        self.assertTrue(
            self.log.with_name("sync.log.1").exists(), "old log kept, nothing lost"
        )
        self.assertLess(self.log.stat().st_size, 256 * 1024)

    def test_odd_characters_in_the_model_setting_cannot_break_auto_off(self) -> None:
        self.run_cli("on")
        (self.claude_dir / "model-profile" / "debug").write_text("", encoding="utf-8")
        text = self.settings_path.read_text(encoding="utf-8").replace(
            '"opusplan"', '"\\ud800\\u001b[31m"'
        )
        self.settings_path.write_text(text, encoding="utf-8")
        result = self.sync()
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        message = json.loads(result.stdout)["systemMessage"]
        self.assertIn("an unrecognised model", message)
        self.assertNotIn("\x1b", message)
        self.assertFalse(self.state_path.exists(), "routing must still switch off")

    def test_a_normal_model_name_is_still_named_in_the_message(self) -> None:
        self.run_cli("on")
        self.write_settings({**self.read_settings(), "model": "claude-opus-4-7[1m]"})
        self.assertIn(
            "`claude-opus-4-7[1m]`", json.loads(self.sync().stdout)["systemMessage"]
        )


class TestConcurrentSettingsWrites(SandboxTestCase):
    def test_a_settings_file_changed_by_someone_else_is_never_overwritten(self) -> None:
        module = load_script(PROFILE_CLI, self.home, "model_profile_cas")
        signature = module.settings_signature()
        theirs = {**BASELINE_SETTINGS_DICT, "theme": "dark"}
        time.sleep(0.01)
        self.write_settings(theirs)  # another tool saves after we read
        with self.assertRaises(module.ProfileError) as caught:
            module.write_settings_checked({"model": "mine"}, signature)
        self.assertIn("changed while it was being updated", str(caught.exception))
        self.assertEqual(self.read_settings(), theirs, "their write must survive")

    def test_an_unchanged_file_is_written(self) -> None:
        module = load_script(PROFILE_CLI, self.home, "model_profile_cas_ok")
        signature = module.settings_signature()
        module.write_settings_checked({"model": "mine"}, signature)
        self.assertEqual(self.read_settings(), {"model": "mine"})

    def test_a_missing_file_is_created_when_still_missing(self) -> None:
        self.settings_path.unlink()
        module = load_script(PROFILE_CLI, self.home, "model_profile_cas_missing")
        signature = module.settings_signature()
        self.assertIsNone(signature)
        module.write_settings_checked({"a": 1}, signature)
        self.assertEqual(self.read_settings(), {"a": 1})


BASELINE_SETTINGS_DICT = {
    "env": {"PATH": "/usr/bin"},
    "effortLevel": "low",
    "model": "sonnet",
    "permissions": {"allow": ["Bash(ls:*)"]},
}


class TestSudoGuard(SandboxTestCase):
    def test_running_through_sudo_is_refused_so_settings_do_not_become_root_owned(
        self,
    ) -> None:
        module = load_script(PROFILE_CLI, self.home, "model_profile_sudo")
        with (
            mock.patch("os.geteuid", return_value=0),
            mock.patch.dict(os.environ, {"SUDO_USER": "someone"}),
            mock.patch("sys.stderr") as stderr,
        ):
            self.assertEqual(module.main(["model-profile", "status"]), EXIT_FAILURE)
        self.assertIn(
            "do not run with sudo",
            "".join(call.args[0] for call in stderr.write.call_args_list),
        )

    def test_plain_root_such_as_a_ci_container_is_allowed(self) -> None:
        module = load_script(PROFILE_CLI, self.home, "model_profile_root")
        env = {k: v for k, v in os.environ.items() if k != "SUDO_USER"}
        with (
            mock.patch("os.geteuid", return_value=0),
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("sys.stdout"),
        ):
            self.assertEqual(module.main(["model-profile", "status"]), EXIT_OK)


class TestInstallerHardening(unittest.TestCase):
    def setUp(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="security-install-"))
        self.addCleanup(shutil.rmtree, workspace, True)
        self.home = workspace / "home"
        self.home.mkdir()
        self.repo = workspace / "repo"
        for name in ("scripts", "src", "VERSION"):
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(
                    source,
                    self.repo / name,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
            else:
                self.repo.mkdir(exist_ok=True)
                shutil.copy2(source, self.repo / name)
        self.claude = self.home / ".claude"
        self.victim = workspace / "victim.txt"
        self.victim.write_text("do not delete", encoding="utf-8")

    def installer(
        self, *args: str, umask: int | None = None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", str(self.repo / "scripts" / "install.py"), *args],
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            preexec_fn=(lambda: os.umask(umask)) if umask is not None else None,
        )

    def tamper_manifest(self, key: str) -> None:
        path = self.claude / "model-profile" / "install-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["files"][key] = "0" * 64
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_a_tampered_manifest_cannot_make_uninstall_delete_other_files(self) -> None:
        self.installer()
        for key in (
            str(self.victim),
            "../../victim.txt",
            "bin/../../../victim.txt",
            "settings.json",
            "projects/x.jsonl",
        ):
            with self.subTest(key=key):
                self.installer(
                    "uninstall", "--force"
                )  # may fail; must never touch victim
                self.installer()
                self.tamper_manifest(key)
                for command in (
                    ("uninstall", "--force"),
                    ("status",),
                    ("install", "--force"),
                ):
                    result = self.installer(*command)
                    self.assertEqual(
                        result.returncode, EXIT_FAILURE, (command, result.stdout)
                    )
                    self.assertIn("tampered with", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.victim.read_text(), "do not delete")
                (self.claude / "model-profile" / "install-manifest.json").unlink()
                shutil.rmtree(self.claude)
                self.claude.mkdir()

    def test_force_never_overwrites_an_earlier_backup(self) -> None:
        self.installer()
        edited = self.claude / "model-profile" / "agents" / "Explore.md"
        edited.write_text("first edit", encoding="utf-8")
        self.installer("--force")
        edited.write_text("second edit", encoding="utf-8")
        self.installer("--force")
        backups = sorted(edited.parent.glob("Explore.md.bak-*"))
        self.assertEqual(len(backups), 2)
        self.assertEqual(
            {b.read_text() for b in backups}, {"first edit", "second edit"}
        )

    def test_a_directory_in_the_way_stops_the_install_before_anything_is_written(
        self,
    ) -> None:
        (self.claude / "commands").mkdir(parents=True)
        (self.claude / "commands" / "model-profile.md").mkdir()
        result = self.installer("--force")
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertIn("is a directory", result.stderr)
        self.assertFalse((self.claude / "bin").exists(), "no partial install")

    def test_a_planted_backup_symlink_is_never_written_through(self) -> None:
        self.installer()
        edited = self.claude / "model-profile" / "agents" / "Explore.md"
        edited.write_text("my edit", encoding="utf-8")
        module = load_script(
            self.repo / "scripts" / "install.py", self.home, "install_plan"
        )
        conflict = ["model-profile/agents/Explore.md"]
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            planned = module.plan_backups(conflict)[0][1]
            # safety net: this test must only ever touch its own sandbox, never a real ~/.claude
            self.assertTrue(str(planned).startswith(str(self.home)), planned)
            planned.symlink_to(self.victim)
            again = module.plan_backups(conflict)[0][1]
        self.assertNotEqual(
            again, planned, "an existing link (even dangling) must be skipped"
        )
        self.assertEqual(self.victim.read_text(), "do not delete")

    def test_new_folders_and_files_are_owner_only_even_with_a_permissive_umask(
        self,
    ) -> None:
        self.assertEqual(self.installer("--enable", umask=0o022).returncode, EXIT_OK)
        for path in (self.claude, self.claude / "model-profile", self.claude / "bin"):
            with self.subTest(path=path.name):
                self.assertEqual(mode_of(path), PRIVATE_MODE)
        self.assertEqual(
            mode_of(self.claude / "model-profile" / "history.jsonl"), PRIVATE_FILE_MODE
        )

    def test_the_installer_refuses_to_run_through_sudo(self) -> None:
        module = load_script(
            self.repo / "scripts" / "install.py", self.home, "install_sudo"
        )
        with (
            mock.patch("os.geteuid", return_value=0),
            mock.patch.dict(os.environ, {"SUDO_USER": "someone"}),
            mock.patch("sys.stderr") as stderr,
        ):
            self.assertEqual(module.main(["install.py", "status"]), EXIT_FAILURE)
        self.assertIn(
            "do not run with sudo",
            "".join(call.args[0] for call in stderr.write.call_args_list),
        )


class TestBundleHardening(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build_dir = Path(tempfile.mkdtemp(prefix="security-bundle-"))
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "build_bundle.py"),
                "--output-dir",
                str(cls.build_dir),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        cls.bundle = cls.build_dir / "install-claude-model-profile.sh"

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.build_dir, ignore_errors=True)

    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="security-bundle-run-"))
        self.addCleanup(shutil.rmtree, self.workspace, True)
        self.home = self.workspace / "home"
        (self.home / ".claude").mkdir(parents=True)

    def test_a_planted_module_next_to_the_download_never_runs(self) -> None:
        """The user typically runs the bundle from ~/Downloads, where anything can be sitting."""
        downloads = self.workspace / "Downloads"
        downloads.mkdir()
        marker = self.workspace / "bundle-trap-ran"
        for module in ("base64", "sys", "json", "tarfile", "argparse"):
            (downloads / f"{module}.py").write_text(
                f"open({str(marker)!r}, 'w').write('x')\n", encoding="utf-8"
            )
        result = subprocess.run(
            ["bash", str(self.bundle), "status"],
            cwd=downloads,
            env={**os.environ, "HOME": str(self.home), "PYTHONPATH": str(downloads)},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertFalse(marker.exists(), "planted code must not run")
        self.assertIn("not installed", result.stdout)

    def test_every_python_call_in_the_bundle_header_is_isolated(self) -> None:
        header = self.bundle.read_text(encoding="utf-8").split("__PAYLOAD_BELOW__\n")[0]
        calls = [
            line
            for line in header.splitlines()
            if re.search(r"\bpython3\b", line) and not line.lstrip().startswith("#")
        ]
        executed = [
            line
            for line in calls
            if "command -v" not in line and "is required" not in line
        ]
        self.assertTrue(executed)
        for line in executed:
            self.assertRegex(line, r"python3 -I\b", line)

    def test_the_wrapper_script_uses_isolated_python(self) -> None:
        self.assertIn("python3 -I ", (ROOT / "install.sh").read_text(encoding="utf-8"))

    def test_a_symlink_in_the_sources_stops_the_build(self) -> None:
        repo = self.workspace / "repo"
        for name in (
            "scripts",
            "src",
            "VERSION",
            "README.md",
            "LICENSE",
            "install.sh",
        ):
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(
                    source, repo / name, ignore=shutil.ignore_patterns("__pycache__")
                )
            else:
                repo.mkdir(exist_ok=True)
                shutil.copy2(source, repo / name)
        secret = self.workspace / "secret.txt"
        secret.write_text("api-key-123", encoding="utf-8")
        (repo / "src" / "agents" / "innocent.md").symlink_to(secret)
        result = subprocess.run(
            [
                sys.executable,
                str(repo / "scripts" / "build_bundle.py"),
                "--output-dir",
                str(self.workspace / "out"),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertIn("refusing to bundle a symlink", result.stderr)
        self.assertFalse(
            list((self.workspace / "out").glob("*.sh"))
            if (self.workspace / "out").exists()
            else []
        )

    def test_extract_accepts_a_directory_that_starts_with_a_dash(self) -> None:
        target = "-weird-dir"
        result = subprocess.run(
            ["bash", str(self.bundle), "--extract", target],
            cwd=self.workspace,
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertTrue((self.workspace / target / "README.md").exists())


class TestShippedConfigurationHardening(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_the_two_untrusted_content_readers_have_no_shell(self) -> None:
        for name in ("Explore", "log-reader"):
            with self.subTest(agent=name):
                tools = re.search(
                    r"(?m)^tools: (.+)$",
                    (AGENTS_DIR / f"{name}.md").read_text(encoding="utf-8"),
                )
                assert tools is not None
                self.assertEqual(
                    {t.strip() for t in tools.group(1).split(",")},
                    {"Read", "Grep", "Glob"},
                )

    def test_the_reviewer_keeps_bash_only_for_reading_git_history(self) -> None:
        text = (AGENTS_DIR / "deep-reasoner.md").read_text(encoding="utf-8")
        self.assertIn("tools: Read, Grep, Glob, Bash", text)
        self.assertIn("read-only inspection only", text)

    def test_ci_tooling_versions_are_pinned(self) -> None:
        for relative in (".github/workflows/ci.yml", "Makefile"):
            with self.subTest(file=relative):
                text = self.read(relative)
                self.assertIn("mypy==", text)
                self.assertNotRegex(text, r"pip install[^\n]*\bmypy\b(?!==)")

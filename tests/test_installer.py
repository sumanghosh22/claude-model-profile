"""Tests for scripts/install.py: install, update, conflict safety, status and uninstall.

Each test runs the real installer in a throwaway HOME. A copy of the repository is used as the
install source so a "new version" can be simulated without touching the real source tree.
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paths import ROOT

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
BASELINE_SETTINGS = {"env": {"A": "1"}, "model": "sonnet", "effortLevel": "low"}
EXPECTED_BIN = {"model-profile"}
EXPECTED_COMMANDS = {"model-profile.md", "complex.md"}
EXPECTED_AGENTS = {
    "Explore.md",
    "code-reviewer.md",
    "deep-reasoner.md",
    "log-reader.md",
}
COPIED_TREES = ("scripts", "src", "VERSION")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InstallerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="routing-installer-test-"))
        self.addCleanup(shutil.rmtree, workspace, True)
        self.home = workspace / "home"
        self.repo = workspace / "repo"
        self.home.mkdir()
        self.claude = self.home / ".claude"
        self.claude.mkdir()
        self.settings = self.claude / "settings.json"
        self.settings.write_text(
            json.dumps(BASELINE_SETTINGS, indent=2) + "\n", encoding="utf-8"
        )
        self.repo.mkdir()
        for name in COPIED_TREES:
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(
                    source,
                    self.repo / name,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
            else:
                shutil.copy2(source, self.repo / name)

    def run_installer(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.repo / "scripts" / "install.py"), *args],
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    def installed(self, relative: str) -> Path:
        return self.claude / relative

    def manifest(self) -> dict:
        return json.loads(
            self.installed("model-profile/install-manifest.json").read_text(
                encoding="utf-8"
            )
        )


class TestInstall(InstallerTestCase):
    def test_installs_every_component_with_correct_permissions(self) -> None:
        result = self.run_installer()
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        for name in EXPECTED_BIN:
            path = self.installed(f"bin/{name}")
            self.assertTrue(os.access(path, os.X_OK), f"{name} must be executable")
        self.assertEqual(
            {p.name for p in self.installed("commands").iterdir()}, EXPECTED_COMMANDS
        )
        self.assertEqual(
            {p.name for p in self.installed("model-profile/agents").iterdir()},
            EXPECTED_AGENTS,
        )
        self.assertEqual(
            self.installed("model-profile/VERSION").read_text().strip(),
            (ROOT / "VERSION").read_text().strip(),
        )
        for rel in ("commands/model-profile.md", "model-profile/agents/Explore.md"):
            self.assertEqual(stat.S_IMODE(self.installed(rel).stat().st_mode), 0o644)

    def test_the_standards_file_is_installed_and_removed_with_everything_else(
        self,
    ) -> None:
        self.run_installer()
        installed = self.installed("model-profile/standards/standards.md")
        self.assertEqual(
            sha256(installed), sha256(ROOT / "src" / "standards" / "standards.md")
        )
        self.run_installer("uninstall")
        self.assertFalse(self.installed("model-profile/standards").exists())

    def test_installed_files_match_the_source_exactly(self) -> None:
        self.run_installer()
        for name in EXPECTED_BIN:
            self.assertEqual(
                sha256(self.installed(f"bin/{name}")),
                sha256(ROOT / "src" / "bin" / name),
            )
        manifest = self.manifest()["files"]
        for relative, digest in manifest.items():
            self.assertEqual(sha256(self.installed(relative)), digest)

    def test_install_only_adds_the_update_hook_and_never_switches_routing_on(
        self,
    ) -> None:
        result = self.run_installer()
        settings = json.loads(self.settings.read_text())
        hooks = settings.pop("hooks")
        self.assertEqual(settings, BASELINE_SETTINGS, "model settings untouched")
        self.assertEqual(
            [h["command"] for group in hooks["SessionStart"] for h in group["hooks"]],
            [
                'python3 -I "$HOME/.claude/bin/model-profile" auto-update 2>/dev/null || true'
            ],
        )
        self.assertEqual(list(hooks), ["SessionStart"], "no routing hooks while off")
        self.assertFalse(self.installed("model-profile/state.json").exists())
        self.assertIn("not switched on yet", result.stdout)
        self.assertFalse(
            self.installed("agents").exists(),
            "agents are only installed when routing is switched on",
        )

    def test_enable_flag_switches_routing_on(self) -> None:
        result = self.run_installer("--enable")
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        settings = json.loads(self.settings.read_text())
        self.assertEqual(
            (settings["model"], settings["effortLevel"]), ("opusplan", "high")
        )
        self.assertEqual(
            {p.name for p in self.installed("agents").iterdir()}, EXPECTED_AGENTS
        )
        self.assertIn("profile: ON", result.stdout)

    def test_reinstalling_while_routing_is_on_reports_it_and_keeps_it_on(self) -> None:
        self.run_installer("--enable")
        for args in ((), ("--enable",)):
            with self.subTest(args=args):
                result = self.run_installer(*args)
                self.assertEqual(result.returncode, EXIT_OK, result.stderr)
                self.assertIn("already ON", result.stdout)
                self.assertNotIn("not switched on yet", result.stdout)
        self.assertEqual(json.loads(self.settings.read_text())["model"], "opusplan")

    def test_installs_without_an_existing_claude_directory(self) -> None:
        shutil.rmtree(self.claude)
        self.assertEqual(self.run_installer().returncode, EXIT_OK)
        self.assertTrue(self.installed("bin/model-profile").is_file())

    def test_reinstall_is_idempotent(self) -> None:
        self.run_installer()
        first = self.manifest()
        result = self.run_installer()
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertEqual(self.manifest(), first)

    def test_dry_run_changes_nothing(self) -> None:
        result = self.run_installer("--dry-run")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn("Dry run", result.stdout)
        self.assertFalse(self.installed("bin").exists())
        self.assertFalse(self.installed("model-profile").exists())

    def test_other_user_files_are_left_alone(self) -> None:
        (self.claude / "commands").mkdir()
        (self.claude / "commands" / "mine.md").write_text("keep", encoding="utf-8")
        (self.claude / "bin").mkdir()
        (self.claude / "bin" / "my-tool").write_text("keep", encoding="utf-8")
        self.run_installer()
        self.run_installer("uninstall")
        self.assertEqual((self.claude / "commands" / "mine.md").read_text(), "keep")
        self.assertEqual((self.claude / "bin" / "my-tool").read_text(), "keep")

    def test_installed_tools_run_from_their_installed_location(self) -> None:
        self.run_installer()
        env = {**os.environ, "HOME": str(self.home)}
        for tool, args in (
            ("model-profile", ["status"]),
            ("model-profile", ["version"]),
        ):
            with self.subTest(tool=tool, args=args):
                result = subprocess.run(
                    [sys.executable, str(self.installed(f"bin/{tool}")), *args],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
                self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        version = subprocess.run(
            [sys.executable, str(self.installed("bin/model-profile")), "version"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertIn((ROOT / "VERSION").read_text().strip(), version.stdout)

    def test_slash_commands_point_at_installed_tools(self) -> None:
        self.run_installer()
        for command_file in self.installed("commands").glob("model-*.md"):
            text = command_file.read_text(encoding="utf-8")
            self.assertIn("~/.claude/bin/", text)
            self.assertEqual(command_file.name, "model-profile.md")
            self.assertTrue(self.installed("bin/model-profile").is_file())


class TestUpdateAndConflicts(InstallerTestCase):
    def test_update_replaces_files_you_have_not_edited(self) -> None:
        self.run_installer()
        (self.repo / "VERSION").write_text("2.0.0\n", encoding="utf-8")
        agent = self.repo / "src" / "agents" / "Explore.md"
        agent.write_text(agent.read_text() + "\nNew rule.\n", encoding="utf-8")
        result = self.run_installer()
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertIn(
            "New rule.", self.installed("model-profile/agents/Explore.md").read_text()
        )
        self.assertEqual(self.manifest()["version"], "2.0.0")

    def test_files_removed_from_the_source_are_removed_from_the_install(self) -> None:
        self.run_installer()
        (self.repo / "src" / "agents" / "log-reader.md").unlink()
        self.assertEqual(self.run_installer().returncode, EXIT_OK)
        self.assertFalse(self.installed("model-profile/agents/log-reader.md").exists())
        self.assertTrue(self.installed("model-profile/agents/Explore.md").exists())

    def test_a_file_you_edited_blocks_the_update_and_nothing_changes(self) -> None:
        self.run_installer()
        edited = self.installed("model-profile/agents/Explore.md")
        edited.write_text("my custom explorer", encoding="utf-8")
        (self.repo / "VERSION").write_text("2.0.0\n", encoding="utf-8")
        before = self.manifest()
        result = self.run_installer()
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertIn("model-profile/agents/Explore.md", result.stderr)
        self.assertIn("Nothing was changed", result.stderr)
        self.assertEqual(edited.read_text(), "my custom explorer")
        self.assertEqual(self.manifest(), before)
        self.assertEqual(
            self.installed("model-profile/VERSION").read_text().strip(),
            (ROOT / "VERSION").read_text().strip(),
        )

    def test_force_keeps_your_copy_as_a_backup_then_installs(self) -> None:
        self.run_installer()
        edited = self.installed("model-profile/agents/Explore.md")
        edited.write_text("my custom explorer", encoding="utf-8")
        result = self.run_installer("--force")
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        backups = list(edited.parent.glob("Explore.md.bak-*"))
        self.assertEqual(len(backups), 1, "one timestamped backup, never a fixed name")
        self.assertEqual(backups[0].read_text(), "my custom explorer")
        self.assertEqual(sha256(edited), sha256(ROOT / "src" / "agents" / "Explore.md"))

    def test_a_foreign_file_with_the_same_name_is_never_overwritten(self) -> None:
        (self.claude / "commands").mkdir()
        foreign = self.claude / "commands" / "model-profile.md"
        foreign.write_text("someone else's command", encoding="utf-8")
        result = self.run_installer()
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertEqual(foreign.read_text(), "someone else's command")
        self.assertFalse(
            self.installed("bin/model-profile").exists(),
            "install must be all-or-nothing",
        )

    def test_an_identical_existing_file_is_adopted_without_complaint(self) -> None:
        (self.claude / "commands").mkdir()
        shutil.copy2(
            ROOT / "src" / "commands" / "model-profile.md",
            self.claude / "commands" / "model-profile.md",
        )
        self.assertEqual(self.run_installer().returncode, EXIT_OK)

    def test_a_symlink_at_a_destination_is_a_conflict(self) -> None:
        (self.claude / "commands").mkdir()
        (self.claude / "commands" / "model-profile.md").symlink_to(
            self.home / "elsewhere.md"
        )
        result = self.run_installer()
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertFalse(
            (self.home / "elsewhere.md").exists(), "must not write through a symlink"
        )

    def test_a_damaged_manifest_fails_cleanly(self) -> None:
        self.run_installer()
        self.installed("model-profile/install-manifest.json").write_text(
            "{garbage", encoding="utf-8"
        )
        result = self.run_installer()
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertIn("manifest is damaged", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class TestStatus(InstallerTestCase):
    def test_status_before_and_after_install(self) -> None:
        self.assertIn("not installed", self.run_installer("status").stdout)
        self.run_installer()
        out = self.run_installer("status").stdout
        self.assertIn("Installed: claude-model-profile", out)
        self.assertIn("Routing: OFF", out)
        self.assertIn("Edited by you: none", out)

    def test_status_reports_routing_on_and_edited_files(self) -> None:
        self.run_installer("--enable")
        self.installed("model-profile/agents/Explore.md").write_text(
            "edited", encoding="utf-8"
        )
        out = self.run_installer("status").stdout
        self.assertIn("Routing: ON", out)
        self.assertIn("model-profile/agents/Explore.md", out)


class TestUninstall(InstallerTestCase):
    def test_uninstall_restores_settings_and_removes_everything(self) -> None:
        self.run_installer("--enable")
        result = self.run_installer("uninstall")
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertEqual(json.loads(self.settings.read_text()), BASELINE_SETTINGS)
        for relative in (
            "bin/model-profile",
            "commands/model-profile.md",
            "model-profile/agents",
            "agents",
        ):
            self.assertFalse(self.installed(relative).exists(), relative)
        self.assertFalse(self.installed("model-profile/install-manifest.json").exists())

    def test_uninstall_keeps_the_history_unless_purged(self) -> None:
        self.run_installer("--enable")
        self.run_installer("uninstall")
        self.assertTrue(self.installed("model-profile/history.jsonl").exists())

    def test_purge_removes_the_history_and_the_folder(self) -> None:
        self.run_installer("--enable")
        self.run_installer("uninstall", "--purge")
        self.assertFalse(self.installed("model-profile").exists())

    def test_uninstall_refuses_to_delete_files_you_edited(self) -> None:
        self.run_installer()
        edited = self.installed("model-profile/agents/Explore.md")
        edited.write_text("mine", encoding="utf-8")
        result = self.run_installer("uninstall")
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertTrue(edited.exists())
        self.assertTrue(
            self.installed("bin/model-profile").exists(), "nothing may be removed"
        )
        self.assertEqual(self.run_installer("uninstall", "--force").returncode, EXIT_OK)
        self.assertFalse(edited.exists())

    def test_uninstall_removes_review_gate_state_but_never_follows_links(self) -> None:
        self.run_installer("--enable")
        review = self.installed("model-profile/review")
        review.mkdir(parents=True, exist_ok=True)
        (review / "session-1.json").write_text("{}", encoding="utf-8")
        outside = self.home / "keep.json"
        outside.write_text("keep", encoding="utf-8")
        (review / "link.json").symlink_to(outside)
        self.run_installer("uninstall")
        self.assertFalse((review / "session-1.json").exists())
        self.assertEqual(
            outside.read_text(), "keep", "a link's target must never be touched"
        )

    def test_uninstall_when_nothing_is_installed_is_a_no_op(self) -> None:
        result = self.run_installer("uninstall")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn("Nothing to uninstall", result.stdout)

    def test_full_cycle_leaves_a_clean_claude_directory(self) -> None:
        before = sorted(p.name for p in self.claude.iterdir())
        self.run_installer("--enable")
        self.run_installer("uninstall", "--purge")
        self.assertEqual(sorted(p.name for p in self.claude.iterdir()), before)


class TestUsage(InstallerTestCase):
    def test_flag_misuse_is_a_usage_error(self) -> None:
        for args in (
            ("uninstall", "--enable"),
            ("status", "--dry-run"),
            ("install", "--purge"),
            ("bogus",),
        ):
            with self.subTest(args=args):
                result = self.run_installer(*args)
                self.assertEqual(result.returncode, EXIT_USAGE)
                self.assertNotIn("Traceback", result.stderr)

    def test_wrapper_script_runs_the_installer(self) -> None:
        result = subprocess.run(
            ["bash", str(ROOT / "install.sh"), "status"],
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertIn("not installed", result.stdout)


class TestUpgradeRefreshesAgentsAndSkills(InstallerTestCase):
    """An upgrade gives the copies in ~/.claude/agents and ~/.claude/skills this version's
    content while routing is on, without ever touching a copy you edited or deleted."""

    REVIEWER = "agents/code-reviewer.md"
    DEEP_WORK = "skills/deep-work/SKILL.md"

    def setUp(self) -> None:
        super().setUp()
        result = self.run_installer("--enable")
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)

    def state(self) -> dict:
        return json.loads(self.installed("model-profile/state.json").read_text())

    def change_source(self, relative: str, extra: str) -> None:
        path = self.repo / "src" / relative
        path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")

    def upgrade(self) -> None:
        result = self.run_installer()
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)

    def run_profile(self, action: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", str(self.installed("bin/model-profile")), action],
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_a_changed_agent_reaches_an_unedited_copy_and_is_recorded(self) -> None:
        self.change_source(self.REVIEWER, "\nNew review rule.\n")
        self.upgrade()
        copy = self.installed(self.REVIEWER)
        self.assertIn("New review rule.", copy.read_text())
        self.assertEqual(
            self.state()["installed_agents"]["code-reviewer.md"], sha256(copy)
        )
        self.assertEqual(self.run_profile("off").returncode, EXIT_OK)
        self.assertFalse(copy.exists(), "off still removes the refreshed copy")

    def test_a_changed_skill_reaches_an_unedited_copy(self) -> None:
        self.change_source(self.DEEP_WORK, "\nNew skill rule.\n")
        self.upgrade()
        copy = self.installed(self.DEEP_WORK)
        self.assertIn("New skill rule.", copy.read_text())
        self.assertEqual(self.state()["installed_skills"]["deep-work"], sha256(copy))

    def test_a_copy_you_edited_is_never_overwritten(self) -> None:
        copy = self.installed(self.REVIEWER)
        copy.write_text("my own reviewer", encoding="utf-8")
        recorded = self.state()["installed_agents"]["code-reviewer.md"]
        self.change_source(self.REVIEWER, "\nNew review rule.\n")
        self.upgrade()
        self.assertEqual(copy.read_text(), "my own reviewer")
        self.assertEqual(self.state()["installed_agents"]["code-reviewer.md"], recorded)
        self.assertEqual(self.run_profile("off").returncode, EXIT_OK)
        self.assertEqual(copy.read_text(), "my own reviewer", "off keeps it too")

    def test_a_copy_replaced_by_a_link_is_left_alone(self) -> None:
        copy = self.installed(self.REVIEWER)
        target = self.home / "elsewhere.md"
        target.write_text("linked", encoding="utf-8")
        copy.unlink()
        copy.symlink_to(target)
        self.change_source(self.REVIEWER, "\nNew review rule.\n")
        self.upgrade()
        self.assertTrue(copy.is_symlink())
        self.assertEqual(target.read_text(), "linked")

    def test_a_copy_you_deleted_stays_deleted_across_upgrades(self) -> None:
        self.installed(self.REVIEWER).unlink()
        self.change_source(self.REVIEWER, "\nNew review rule.\n")
        for _ in range(2):
            self.upgrade()
            self.assertFalse(self.installed(self.REVIEWER).exists())
            self.assertIn("code-reviewer.md", self.state()["installed_agents"])
        self.assertEqual(self.run_profile("off").returncode, EXIT_OK)

    def test_a_folder_in_place_of_a_copy_is_left_alone(self) -> None:
        copy = self.installed(self.REVIEWER)
        copy.unlink()
        copy.mkdir()
        self.change_source(self.REVIEWER, "\nNew review rule.\n")
        self.upgrade()
        self.assertTrue(copy.is_dir())

    def test_an_unreadable_copy_is_skipped_and_the_install_succeeds(self) -> None:
        copy = self.installed(self.REVIEWER)
        copy.chmod(0)
        self.addCleanup(copy.chmod, 0o600)
        self.change_source(self.REVIEWER, "\nNew review rule.\n")
        self.upgrade()
        copy.chmod(0o600)
        self.assertNotIn("New review rule.", copy.read_text())

    def test_a_refresh_stopped_before_recording_is_repaired(self) -> None:
        self.change_source(self.REVIEWER, "\nNew review rule.\n")
        copy = self.installed(self.REVIEWER)
        copy.write_text((self.repo / "src" / self.REVIEWER).read_text())  # new content
        self.upgrade()  # state still holds the old hash
        self.assertEqual(
            self.state()["installed_agents"]["code-reviewer.md"], sha256(copy)
        )
        self.assertEqual(self.run_profile("off").returncode, EXIT_OK)
        self.assertFalse(copy.exists(), "off removes it, not 'modified since install'")

    def test_a_damaged_state_file_still_gets_the_hooks_and_says_what_failed(
        self,
    ) -> None:
        settings = json.loads(self.settings.read_text())
        del settings["hooks"]["Stop"]
        self.settings.write_text(json.dumps(settings))
        self.installed("model-profile/state.json").write_text("{damaged")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, EXIT_OK)
        self.assertIn("agents and skills were not refreshed", result.stderr)
        self.assertIn("Stop", json.loads(self.settings.read_text())["hooks"])

    def test_a_skills_folder_the_refresh_creates_is_removed_by_off(self) -> None:
        state = self.state()
        shutil.rmtree(self.installed("skills"))
        self.installed("model-profile/state.json").write_text(
            json.dumps({**state, "installed_skills": {}, "created_skills_dir": False})
        )
        self.upgrade()
        self.assertTrue(self.installed("skills/deep-work/SKILL.md").is_file())
        self.assertTrue(self.state()["created_skills_dir"])
        self.assertEqual(self.run_profile("off").returncode, EXIT_OK)
        self.assertFalse(self.installed("skills").exists())

    def test_a_new_agent_is_added_and_removed_again_by_off(self) -> None:
        source = self.repo / "src" / "agents" / "test-designer.md"
        source.write_text(
            (self.repo / "src" / self.REVIEWER)
            .read_text()
            .replace("name: code-reviewer", "name: test-designer"),
            encoding="utf-8",
        )
        self.upgrade()
        copy = self.installed("agents/test-designer.md")
        self.assertTrue(copy.is_file())
        self.assertEqual(
            self.state()["installed_agents"]["test-designer.md"], sha256(copy)
        )
        self.assertEqual(self.run_profile("off").returncode, EXIT_OK)
        self.assertFalse(copy.exists())

    def test_a_new_agent_never_overwrites_a_file_of_yours_with_its_name(self) -> None:
        mine = self.installed("agents/test-designer.md")
        mine.write_text("mine", encoding="utf-8")
        (self.repo / "src" / "agents" / "test-designer.md").write_text(
            "shipped", encoding="utf-8"
        )
        self.upgrade()
        self.assertEqual(mine.read_text(), "mine")
        self.assertNotIn("test-designer.md", self.state()["installed_agents"])

    def test_a_new_skill_never_enters_a_folder_of_yours_with_its_name(self) -> None:
        mine = self.installed("skills/careful-work")
        mine.mkdir()
        (mine / "notes.txt").write_text("mine", encoding="utf-8")
        shipped = self.repo / "src" / "skills" / "careful-work"
        shipped.mkdir()
        (shipped / "SKILL.md").write_text(
            "---\nname: careful-work\n---\nbody\n", encoding="utf-8"
        )
        self.upgrade()
        self.assertFalse((mine / "SKILL.md").exists())
        self.assertNotIn("careful-work", self.state()["installed_skills"])

    def test_a_new_skill_is_added(self) -> None:
        shipped = self.repo / "src" / "skills" / "careful-work"
        shipped.mkdir()
        (shipped / "SKILL.md").write_text(
            "---\nname: careful-work\n---\nbody\n", encoding="utf-8"
        )
        self.upgrade()
        copy = self.installed("skills/careful-work/SKILL.md")
        self.assertEqual(copy.read_text(), "---\nname: careful-work\n---\nbody\n")
        self.assertEqual(self.state()["installed_skills"]["careful-work"], sha256(copy))

    def test_an_agent_no_longer_shipped_is_removed(self) -> None:
        (self.repo / "src" / "agents" / "log-reader.md").unlink()
        self.upgrade()
        self.assertFalse(self.installed("agents/log-reader.md").exists())
        self.assertTrue(self.installed("agents").is_dir(), "the shared folder stays")
        self.assertNotIn("log-reader.md", self.state()["installed_agents"])

    def test_a_skill_no_longer_shipped_is_removed_with_its_folder(self) -> None:
        shutil.rmtree(self.repo / "src" / "skills" / "deep-work")
        self.upgrade()
        self.assertFalse(self.installed("skills/deep-work").exists())
        self.assertNotIn("deep-work", self.state()["installed_skills"])

    def test_nothing_changed_means_nothing_is_written(self) -> None:
        state = self.installed("model-profile/state.json")
        before = (state.stat().st_mtime_ns, sha256(self.installed(self.REVIEWER)))
        self.upgrade()
        self.assertEqual(
            (state.stat().st_mtime_ns, sha256(self.installed(self.REVIEWER))), before
        )

    def test_routing_off_means_no_copies_are_made(self) -> None:
        self.assertEqual(self.run_profile("off").returncode, EXIT_OK)
        self.change_source(self.REVIEWER, "\nNew review rule.\n")
        self.upgrade()
        self.assertFalse(self.installed(self.REVIEWER).exists())
        self.assertFalse(self.installed("model-profile/state.json").exists())

    def test_the_shipped_reviewers_demand_a_test_for_every_scenario(self) -> None:
        for name in ("code-reviewer", "deep-reasoner"):
            with self.subTest(agent=name):
                text = self.installed(f"agents/{name}.md").read_text()
                self.assertIn("scenario coverage is mandatory", text.lower())
                self.assertIn("`NO TEST`", text)
                self.assertIn("effort: high", text)


class TestUpgradeMovesEarlierDefaults(InstallerTestCase):
    """Earlier versions set effort `medium` while ON; an upgrade moves exactly that value."""

    def settings_now(self) -> dict:
        return json.loads(self.settings.read_text())

    def write_settings(self, **changes: object) -> None:
        self.settings.write_text(json.dumps({**self.settings_now(), **changes}))

    def run_profile(self, action: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", str(self.installed("bin/model-profile")), action],
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_the_old_default_becomes_high_and_off_restores_your_own(self) -> None:
        self.write_settings(effortLevel="low")
        self.assertEqual(self.run_installer("--enable").returncode, EXIT_OK)
        self.write_settings(effortLevel="medium")  # as left by 1.11 or earlier
        self.assertEqual(self.run_installer().returncode, EXIT_OK)
        self.assertEqual(self.settings_now()["effortLevel"], "high")
        self.assertNotIn("warning:", self.run_profile("status").stdout)
        off = self.run_profile("off")
        self.assertEqual(off.returncode, EXIT_OK, off.stderr)
        self.assertNotIn("keeping your value", off.stderr)
        self.assertEqual(self.settings_now()["effortLevel"], "low")

    def test_an_effort_you_chose_while_on_is_kept(self) -> None:
        self.assertEqual(self.run_installer("--enable").returncode, EXIT_OK)
        self.write_settings(effortLevel="xhigh")
        self.assertEqual(self.run_installer().returncode, EXIT_OK)
        self.assertEqual(self.settings_now()["effortLevel"], "xhigh")

    def test_nothing_changes_while_routing_is_off(self) -> None:
        self.write_settings(effortLevel="medium")
        self.assertEqual(self.run_installer().returncode, EXIT_OK)
        self.assertEqual(self.settings_now()["effortLevel"], "medium")

    def test_repair_also_sets_high(self) -> None:
        self.assertEqual(self.run_installer("--enable").returncode, EXIT_OK)
        self.write_settings(effortLevel="medium")
        self.assertEqual(self.run_profile("repair").returncode, EXIT_OK)
        self.assertEqual(self.settings_now()["effortLevel"], "high")


if __name__ == "__main__":
    unittest.main(verbosity=2)

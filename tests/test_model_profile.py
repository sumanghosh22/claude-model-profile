"""End-to-end tests for ~/.claude/bin/model-profile.

Every test runs the real CLI in a throwaway HOME so the real configuration is
never read or written. Run with:  python3 -m unittest -v test_model_profile
"""

import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paths import AGENTS_DIR, PROFILE_CLI

CLI_PATH = PROFILE_CLI
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXPECTED_MODEL = "opusplan"
EXPECTED_EFFORT = "high"
ALLOWED_AGENT_MODELS = {"haiku", "sonnet", "opus"}
BASELINE_SETTINGS = {
    "env": {"PATH": "/usr/bin"},
    "effortLevel": "low",
    "model": "sonnet",
    "permissions": {"allow": ["Bash(ls:*)"]},
}
FRONTMATTER_PATTERN = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


class SandboxTestCase(unittest.TestCase):
    """Builds an isolated HOME containing a copy of the profile directory."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="model-profile-test-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.claude_dir = self.home / ".claude"
        self.claude_dir.mkdir()
        shutil.copytree(AGENTS_DIR, self.claude_dir / "model-profile" / "agents")
        self.settings_path = self.claude_dir / "settings.json"
        self.agents_dir = self.claude_dir / "agents"
        self.state_path = self.claude_dir / "model-profile" / "state.json"
        self.write_settings(BASELINE_SETTINGS)

    def write_settings(self, payload: object, mode: int = 0o644) -> None:
        self.settings_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        self.settings_path.chmod(mode)

    def read_settings(self) -> dict:
        return json.loads(self.settings_path.read_text(encoding="utf-8"))

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CLI_PATH), *args],
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
        )

    def installed_agent_names(self) -> list[str]:
        return (
            sorted(p.name for p in self.agents_dir.glob("*.md"))
            if self.agents_dir.exists()
            else []
        )

    def source_agent_names(self) -> list[str]:
        return sorted(
            p.name for p in (self.claude_dir / "model-profile" / "agents").glob("*.md")
        )


class TestToggleHappyPath(SandboxTestCase):
    def test_status_reports_off_and_current_values(self) -> None:
        result = self.run_cli("status")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn("profile: OFF", result.stdout)
        self.assertIn("model: sonnet", result.stdout)

    def test_on_installs_agents_and_sets_model(self) -> None:
        result = self.run_cli("on")
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertEqual(self.installed_agent_names(), self.source_agent_names())
        settings = self.read_settings()
        self.assertEqual(settings["model"], EXPECTED_MODEL)
        self.assertEqual(settings["effortLevel"], EXPECTED_EFFORT)
        self.assertTrue(self.state_path.exists())

    def test_on_preserves_unrelated_settings_and_key_order(self) -> None:
        self.run_cli("on")
        settings = self.read_settings()
        self.assertEqual(settings["env"], BASELINE_SETTINGS["env"])
        self.assertEqual(settings["permissions"], BASELINE_SETTINGS["permissions"])
        self.assertEqual(list(settings), [*BASELINE_SETTINGS, "hooks"])

    def test_off_restores_exact_previous_settings(self) -> None:
        self.run_cli("on")
        result = self.run_cli("off")
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertEqual(self.read_settings(), BASELINE_SETTINGS)
        self.assertEqual(self.installed_agent_names(), [])
        self.assertFalse(self.state_path.exists())
        self.assertFalse(self.agents_dir.exists(), "empty agents dir should be removed")

    def test_off_removes_keys_that_were_absent_before_on(self) -> None:
        self.write_settings({"env": {"A": "1"}})
        self.run_cli("on")
        self.run_cli("off")
        self.assertEqual(self.read_settings(), {"env": {"A": "1"}})

    def test_repeated_cycles_are_stable(self) -> None:
        for _ in range(3):
            self.assertEqual(self.run_cli("on").returncode, EXIT_OK)
            self.assertEqual(self.run_cli("off").returncode, EXIT_OK)
        self.assertEqual(self.read_settings(), BASELINE_SETTINGS)

    def test_status_while_on(self) -> None:
        self.run_cli("on")
        result = self.run_cli("status")
        self.assertIn("profile: ON", result.stdout)
        self.assertIn(f"model: {EXPECTED_MODEL}", result.stdout)
        for name in self.source_agent_names():
            self.assertIn(name, result.stdout)


class TestDriftAndRepair(SandboxTestCase):
    def settings_without(self, *keys: str) -> None:
        self.write_settings(
            {k: v for k, v in self.read_settings().items() if k not in keys}
        )

    def test_status_warns_when_something_removed_the_model_while_on(self) -> None:
        self.run_cli("on")
        self.settings_without("model")
        out = self.run_cli("status").stdout
        self.assertIn(
            "warning: routing is ON but `model` is (default) (expected opusplan)", out
        )
        self.assertIn("/model-profile repair", out)

    def test_status_warns_when_the_model_was_changed_to_another_value(self) -> None:
        self.run_cli("on")
        self.write_settings({**self.read_settings(), "model": "sonnet"})
        out = self.run_cli("status").stdout
        self.assertIn("`model` is sonnet (expected opusplan)", out)
        self.assertNotIn("effortLevel", out.split("warning:")[1])

    def test_status_has_no_warning_when_in_sync_or_off(self) -> None:
        self.assertNotIn("warning", self.run_cli("status").stdout)
        self.run_cli("on")
        self.assertNotIn("warning", self.run_cli("status").stdout)

    def test_repair_reapplies_routing_and_keeps_everything_else(self) -> None:
        self.run_cli("on")
        self.write_settings({"env": {"A": "1"}, "effortLevel": "low", "theme": "dark"})
        result = self.run_cli("repair")
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        settings = self.read_settings()
        self.assertEqual(
            (settings["model"], settings["effortLevel"]),
            (EXPECTED_MODEL, EXPECTED_EFFORT),
        )
        self.assertEqual((settings["env"], settings["theme"]), ({"A": "1"}, "dark"))
        self.assertNotIn("warning", result.stdout)
        self.assertIn("/model opusplan", result.stdout)

    def test_off_after_repair_still_restores_the_original_settings(self) -> None:
        self.run_cli("on")
        self.settings_without("model")
        self.run_cli("repair")
        self.run_cli("off")
        self.assertEqual(self.read_settings(), BASELINE_SETTINGS)

    def test_repair_when_already_in_sync_does_not_rewrite_the_file(self) -> None:
        self.run_cli("on")
        before = self.settings_path.stat().st_mtime_ns
        self.assertEqual(self.run_cli("repair").returncode, EXIT_OK)
        self.assertEqual(self.settings_path.stat().st_mtime_ns, before)

    def test_repair_when_off_fails_cleanly(self) -> None:
        result = self.run_cli("repair")
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertIn("routing is OFF", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.read_settings(), BASELINE_SETTINGS)

    def test_repair_does_not_log_a_routing_period_change(self) -> None:
        self.run_cli("on")
        history = self.claude_dir / "model-profile" / "history.jsonl"
        before = history.read_text(encoding="utf-8")
        self.settings_without("model")
        self.run_cli("repair")
        self.assertEqual(history.read_text(encoding="utf-8"), before)

    def test_help_lists_repair(self) -> None:
        self.assertIn("model-profile repair", self.run_cli("help").stdout)


SYNC_COMMAND = 'python3 -I "$HOME/.claude/bin/model-profile" sync 2>/dev/null || true'
CONTEXT_COMMAND = (
    'python3 -I "$HOME/.claude/bin/model-profile" context 2>/dev/null || true'
)
LEGACY_SYNC_COMMAND = '"$HOME/.claude/bin/model-profile" sync 2>/dev/null || true'
LEGACY_CONTEXT_COMMAND = '"$HOME/.claude/bin/model-profile" context 2>/dev/null || true'
SNAPSHOT_COMMAND = (
    'python3 -I "$HOME/.claude/bin/model-profile" snapshot 2>/dev/null || true'
)
PROMPT_START_COMMAND = (
    'python3 -I "$HOME/.claude/bin/model-profile" prompt-start 2>/dev/null || true'
)
REVIEW_GATE_COMMAND = (
    'python3 -I "$HOME/.claude/bin/model-profile" review-gate 2>/dev/null || true'
)
EXPECTED_HOOKS = {
    "PostModelSwitch": (SYNC_COMMAND,),
    "SessionStart": (SYNC_COMMAND, CONTEXT_COMMAND),
    "UserPromptSubmit": (PROMPT_START_COMMAND,),
    "Stop": (REVIEW_GATE_COMMAND,),
}
HOOK_EVENTS = tuple(EXPECTED_HOOKS)
STANDARDS_RULES = (
    "Everything must be enterprise-grade, production-ready and Google/Microsoft level, every time.",
    'Never claim "done" or "verified" without driving the real flow end to end.',
    "Production is live with many clients: be careful, backward-compatible and additive.",
    "Testable, SOLID design.",
    "Fix only what was asked, but at the highest standard.",
    "Security by default: validate all inputs and external data, prevent injection and XSS, never hard-code secrets, and flag any vulnerability you notice.",
    "Every final answer says what you verified and how, and what you did not verify.",
    "Test every scenario, none skipped:",
)
MAX_STANDARDS_WORDS = 160
USER_HOOKS = {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo mine"}]}],
    "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "echo stop"}]}],
}


class TestAutoOffHooks(SandboxTestCase):
    def routing_commands(self, event: str, command: str = SYNC_COMMAND) -> list[dict]:
        return [
            hook
            for group in self.read_settings().get("hooks", {}).get(event, [])
            for hook in group["hooks"]
            if hook["command"] == command
        ]

    def test_on_registers_the_hook_for_every_event_with_a_timeout(self) -> None:
        self.run_cli("on")
        for event, commands in EXPECTED_HOOKS.items():
            for command in commands:
                with self.subTest(event=event, command=command[-30:]):
                    found = self.routing_commands(event, command)
                    self.assertEqual(len(found), 1)
                    self.assertEqual(found[0]["type"], "command")
                    self.assertGreater(found[0]["timeout"], 0)

    def test_exactly_these_routing_hooks_are_registered(self) -> None:
        self.run_cli("on")
        hooks = self.read_settings()["hooks"]
        ours = {
            event: tuple(
                hook["command"]
                for group in hooks.get(event, [])
                for hook in group["hooks"]
                if "model-profile" in hook["command"]
            )
            for event in EXPECTED_HOOKS
        }
        self.assertEqual(ours, EXPECTED_HOOKS)

    def test_standards_hook_runs_once_per_conversation_not_on_every_prompt(
        self,
    ) -> None:
        self.run_cli("on")
        self.assertEqual(len(self.routing_commands("SessionStart", CONTEXT_COMMAND)), 1)
        for event in ("UserPromptSubmit", "PostModelSwitch"):
            self.assertEqual(self.routing_commands(event, CONTEXT_COMMAND), [], event)

    def test_on_keeps_your_existing_hooks_and_off_restores_settings_exactly(
        self,
    ) -> None:
        baseline = {**BASELINE_SETTINGS, "hooks": USER_HOOKS}
        self.write_settings(baseline)
        self.run_cli("on")
        hooks = self.read_settings()["hooks"]
        self.assertEqual(
            hooks["Stop"][0],
            USER_HOOKS["Stop"][0],
            "the user's own Stop hook stays first",
        )
        self.assertIn(USER_HOOKS["UserPromptSubmit"][0], hooks["UserPromptSubmit"])
        self.run_cli("off")
        self.assertEqual(self.read_settings(), baseline)

    def test_off_removes_the_hooks_entirely_when_there_were_none_before(self) -> None:
        self.run_cli("on")
        self.run_cli("off")
        self.assertNotIn("hooks", self.read_settings())
        self.assertEqual(self.read_settings(), BASELINE_SETTINGS)

    def test_status_reports_whether_auto_off_is_active(self) -> None:
        self.run_cli("on")
        self.assertIn("auto-off: active", self.run_cli("status").stdout)
        settings = self.read_settings()
        del settings["hooks"]
        self.write_settings(settings)
        self.assertIn("auto-off: inactive", self.run_cli("status").stdout)
        self.run_cli("repair")
        self.assertIn("auto-off: active", self.run_cli("status").stdout)

    def test_repair_re_registers_missing_hooks_without_duplicating(self) -> None:
        self.run_cli("on")
        self.run_cli("repair")
        self.run_cli("repair")
        for event, commands in EXPECTED_HOOKS.items():
            for command in commands:
                self.assertEqual(len(self.routing_commands(event, command)), 1)

    def test_malformed_hooks_section_fails_cleanly_and_rolls_back(self) -> None:
        self.write_settings({**BASELINE_SETTINGS, "hooks": "nonsense"})
        result = self.run_cli("on")
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertIn("`hooks` must be an object", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.installed_agent_names(), [])

    def test_hook_command_is_safe_when_the_tool_is_gone(self) -> None:
        for command in (SYNC_COMMAND, CONTEXT_COMMAND):
            with self.subTest(command=command[-30:]):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        command.replace("$HOME", str(self.home / "missing")),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout + result.stderr, "")


class TestStandards(SandboxTestCase):
    @property
    def standards_path(self) -> Path:
        return self.claude_dir / "model-profile" / "standards" / "standards.md"

    def setUp(self) -> None:
        super().setUp()
        self.standards_path.parent.mkdir(parents=True)
        self.standards_path.write_text(
            "Routing standards:\n"
            + "\n".join(f"- {rule}" for rule in STANDARDS_RULES)
            + "\n",
            encoding="utf-8",
        )

    def context_payload(self) -> dict:
        result = self.context()
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def injected(self) -> str:
        payload = self.context_payload()
        return payload.get("hookSpecificOutput", {}).get("additionalContext", "")

    def context(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CLI_PATH), "context"],
            input='{"hook_event_name": "SessionStart", "source": "startup"}',
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def test_the_standards_reach_a_new_conversation_while_routing_is_on(self) -> None:
        self.run_cli("on")
        payload = self.context_payload()
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("Model routing: ON", payload["systemMessage"])
        for rule in STANDARDS_RULES:
            self.assertIn(rule, payload["hookSpecificOutput"]["additionalContext"])

    def test_nothing_is_added_while_routing_is_off(self) -> None:
        result = self.context()
        self.assertEqual((result.returncode, result.stdout), (EXIT_OK, ""))

    def test_nothing_is_added_once_the_user_chose_another_model(self) -> None:
        self.run_cli("on")
        self.write_settings({**self.read_settings(), "model": "opus"})
        self.assertEqual(self.context().stdout, "")

    def test_off_removes_the_standards_hook_and_stops_the_injection(self) -> None:
        self.run_cli("on")
        self.run_cli("off")
        self.assertNotIn("model-profile", json.dumps(self.read_settings()))
        self.assertEqual(self.context().stdout, "")

    def test_a_missing_standards_file_is_logged_and_never_fails_the_session(
        self,
    ) -> None:
        self.run_cli("on")
        self.standards_path.unlink()
        result = self.context()
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (EXIT_OK, "", "")
        )
        log = (self.claude_dir / "model-profile" / "sync.log").read_text()
        self.assertIn("standards_missing", log)
        self.assertIn("standards: missing", self.run_cli("status").stdout)

    def test_an_empty_or_unreadable_standards_file_adds_nothing(self) -> None:
        self.run_cli("on")
        self.standards_path.write_text("  \n", encoding="utf-8")
        self.assertEqual(self.injected(), "")
        self.standards_path.write_bytes(b"\xff\xfe\x00bad")
        result = self.context()
        self.assertEqual((result.returncode, result.stdout), (EXIT_OK, ""))
        self.assertNotIn("Traceback", result.stderr)

    def test_an_oversized_standards_file_is_capped_to_protect_token_spend(self) -> None:
        self.run_cli("on")
        self.standards_path.write_text("- rule\n" * 5000, encoding="utf-8")
        self.assertLessEqual(len(self.injected()), 4000)
        self.assertIn(
            "standards_truncated",
            (self.claude_dir / "model-profile" / "sync.log").read_text(),
        )

    def test_your_own_edits_to_the_standards_are_used(self) -> None:
        self.run_cli("on")
        self.standards_path.write_text(
            "- Always write a changelog entry.\n", encoding="utf-8"
        )
        self.assertEqual(self.injected(), "- Always write a changelog entry.")

    def test_status_reports_the_standards_state(self) -> None:
        self.run_cli("on")
        self.assertIn("standards: active", self.run_cli("status").stdout)

    def test_a_damaged_settings_file_never_fails_the_hook(self) -> None:
        self.run_cli("on")
        self.settings_path.write_text("{oops", encoding="utf-8")
        result = self.context()
        self.assertEqual((result.returncode, result.stdout), (EXIT_OK, ""))
        self.assertIn(
            "context_failed",
            (self.claude_dir / "model-profile" / "sync.log").read_text(),
        )


class TestStatusLine(SandboxTestCase):
    def statusline(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", str(CLI_PATH), "statusline"],
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def test_the_tag_follows_the_routing_state(self) -> None:
        self.assertIn("[ROUTING:OFF]", self.statusline().stdout)
        self.run_cli("on")
        self.assertIn("[ROUTING:ON]", self.statusline().stdout)
        self.run_cli("off")
        self.assertIn("[ROUTING:OFF]", self.statusline().stdout)

    def test_the_tag_is_one_line_and_never_fails(self) -> None:
        self.run_cli("on")
        self.state_path.write_text("garbage", encoding="utf-8")
        result = self.statusline()
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertNotIn("\n", result.stdout)
        self.assertEqual(result.stderr, "")


class TestShippedStandards(unittest.TestCase):
    def test_the_shipped_standards_are_your_five_rules_verbatim(self) -> None:
        text = (AGENTS_DIR.parent / "standards" / "standards.md").read_text(
            encoding="utf-8"
        )
        for rule in STANDARDS_RULES:
            self.assertIn(rule, text)

    def test_the_standards_stay_small_because_they_enter_every_conversation(
        self,
    ) -> None:
        text = (AGENTS_DIR.parent / "standards" / "standards.md").read_text(
            encoding="utf-8"
        )
        self.assertLessEqual(len(text.split()), MAX_STANDARDS_WORDS)

    def test_the_reviewer_agent_judges_against_the_shared_standards_file(self) -> None:
        text = (AGENTS_DIR / "deep-reasoner.md").read_text(encoding="utf-8")
        self.assertIn("~/.claude/model-profile/standards/standards.md", text)
        self.assertNotIn("~/.claude/model-profile/standards.md", text, "old wrong path")

    def test_complex_requires_the_real_flow_to_be_driven_before_done(self) -> None:
        text = (COMMANDS_DIR / "complex.md").read_text(encoding="utf-8")
        self.assertIn("drove that real flow end to end", text)
        self.assertIn("routing standards", text)


class TestSync(SandboxTestCase):
    def sync(
        self, stdin: str = '{"hook_event_name": "UserPromptSubmit"}'
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CLI_PATH), "sync"],
            input=stdin,
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def history_entries(self) -> list[dict]:
        path = self.claude_dir / "model-profile" / "history.jsonl"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def test_sync_is_silent_and_changes_nothing_when_routing_is_off(self) -> None:
        before = self.settings_path.read_bytes()
        result = self.sync()
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (EXIT_OK, "", "")
        )
        self.assertEqual(self.settings_path.read_bytes(), before)

    def test_sync_is_silent_and_changes_nothing_while_the_profile_model_is_set(
        self,
    ) -> None:
        self.run_cli("on")
        before = (self.settings_path.read_bytes(), self.installed_agent_names())
        result = self.sync()
        self.assertEqual((result.returncode, result.stdout), (EXIT_OK, ""))
        self.assertEqual(
            (self.settings_path.read_bytes(), self.installed_agent_names()), before
        )
        self.assertTrue(self.state_path.exists())

    def test_choosing_another_model_switches_routing_off_and_keeps_the_choice(
        self,
    ) -> None:
        self.run_cli("on")
        self.write_settings({**self.read_settings(), "model": "opus"})
        result = self.sync()
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        message = json.loads(result.stdout)["systemMessage"]
        self.assertIn("Routing switched OFF because you chose `opus`", message)
        self.assertIn("Your choice is kept", message)
        self.assertIn("/model-profile on", message)
        settings = self.read_settings()
        self.assertEqual(settings["model"], "opus", "the user's choice must survive")
        self.assertEqual(
            settings["effortLevel"],
            BASELINE_SETTINGS["effortLevel"],
            "effort goes back",
        )
        self.assertNotIn("hooks", settings)
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.installed_agent_names(), [])
        self.assertIn("profile: OFF", self.run_cli("status").stdout)

    def test_choosing_the_default_model_also_switches_routing_off(self) -> None:
        self.run_cli("on")
        settings = self.read_settings()
        del settings["model"]
        self.write_settings(settings)
        message = json.loads(self.sync().stdout)["systemMessage"]
        self.assertIn("because you chose the default model", message)
        self.assertNotIn("model", self.read_settings())

    def test_auto_off_is_recorded_in_history_with_its_reason(self) -> None:
        self.run_cli("on")
        self.write_settings({**self.read_settings(), "model": "haiku"})
        self.sync()
        last = self.history_entries()[-1]
        self.assertEqual((last["state"], last["reason"]), ("off", "model_changed"))

    def test_switching_routing_on_again_takes_over_and_off_returns_to_your_choice(
        self,
    ) -> None:
        self.run_cli("on")
        self.write_settings({**self.read_settings(), "model": "opus"})
        self.sync()
        self.assertEqual(self.run_cli("on").returncode, EXIT_OK)
        settings = self.read_settings()
        self.assertEqual(settings["model"], EXPECTED_MODEL)
        self.assertEqual(self.installed_agent_names(), self.source_agent_names())
        self.run_cli("off")
        self.assertEqual(
            self.read_settings()["model"], "opus", "off returns to what you chose"
        )

    def test_choosing_the_profile_model_yourself_does_not_switch_it_off(self) -> None:
        self.run_cli("on")
        self.write_settings({**self.read_settings(), "model": EXPECTED_MODEL})
        self.assertEqual(self.sync().stdout, "")
        self.assertTrue(self.state_path.exists())

    def test_effort_changes_alone_do_not_switch_routing_off(self) -> None:
        self.run_cli("on")
        self.write_settings({**self.read_settings(), "effortLevel": "low"})
        self.assertEqual(self.sync().stdout, "")
        self.assertTrue(self.state_path.exists())
        self.assertIn("effortLevel", self.run_cli("status").stdout.split("warning:")[1])

    def test_other_hooks_and_settings_survive_an_auto_off(self) -> None:
        self.write_settings({**BASELINE_SETTINGS, "hooks": USER_HOOKS})
        self.run_cli("on")
        self.write_settings({**self.read_settings(), "model": "opus"})
        self.sync()
        settings = self.read_settings()
        self.assertEqual(settings["hooks"], USER_HOOKS)
        self.assertEqual(settings["permissions"], BASELINE_SETTINGS["permissions"])

    def test_sync_never_fails_the_session(self) -> None:
        self.run_cli("on")
        cases = {
            "settings is not json": lambda: self.settings_path.write_text(
                "{oops", encoding="utf-8"
            ),
            "state file damaged": lambda: self.state_path.write_text(
                "garbage", encoding="utf-8"
            ),
        }
        for name, corrupt in cases.items():
            with self.subTest(case=name):
                corrupt()
                result = self.sync()
                self.assertEqual(result.returncode, EXIT_OK)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(result.stdout, "")
            self.run_cli("off")
            self.write_settings(BASELINE_SETTINGS)
            self.state_path.unlink(missing_ok=True)
            self.run_cli("on")
        log = (self.claude_dir / "model-profile" / "sync.log").read_text()
        self.assertIn("sync_failed", log)

    def test_sync_skips_quietly_while_another_run_holds_the_lock(self) -> None:
        self.run_cli("on")
        self.write_settings({**self.read_settings(), "model": "opus"})
        lock = self.claude_dir / "model-profile" / ".lock"
        with lock.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            result = self.sync()
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (EXIT_OK, "", "")
        )
        self.assertTrue(
            self.state_path.exists(), "must not act while another run is in progress"
        )

    def test_sync_ignores_whatever_the_hook_sends_on_stdin(self) -> None:
        self.run_cli("on")
        for payload in ("", "not json", '{"model": "claude-opus-5-5", "x": [1,2,3]}'):
            with self.subTest(payload=payload):
                result = self.sync(payload)
                self.assertEqual((result.returncode, result.stdout), (EXIT_OK, ""))

    def test_sync_log_is_size_capped(self) -> None:
        log = self.claude_dir / "model-profile" / "sync.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "\n".join(json.dumps({"n": i, "pad": "x" * 200}) for i in range(2000))
            + "\n",
            encoding="utf-8",
        )
        self.assertGreater(log.stat().st_size, 256 * 1024)
        self.run_cli("on")
        self.settings_path.write_text("{oops", encoding="utf-8")
        self.sync()
        self.assertLess(log.stat().st_size, 256 * 1024)

    def test_help_lists_sync(self) -> None:
        self.assertIn("model-profile sync", self.run_cli("help").stdout)


class TestNoticesAndDiagnostics(SandboxTestCase):
    def sync_with(self, stdin: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CLI_PATH), "sync"],
            input=stdin,
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    @property
    def sync_log(self) -> Path:
        return self.claude_dir / "model-profile" / "sync.log"

    def test_on_tells_you_how_to_apply_routing_to_an_already_open_conversation(
        self,
    ) -> None:
        out = self.run_cli("on").stdout
        self.assertIn("New conversations use routing automatically", out)
        self.assertIn("run `/model opusplan`", out)

    def test_off_points_at_a_new_conversation_without_the_opusplan_hint(self) -> None:
        self.run_cli("on")
        out = self.run_cli("off").stdout
        self.assertIn("Open a new conversation", out)
        self.assertNotIn("/model opusplan", out)

    def test_repair_gives_the_same_apply_here_hint(self) -> None:
        self.run_cli("on")
        self.assertIn("/model opusplan", self.run_cli("repair").stdout)

    def test_nothing_is_logged_for_quiet_hook_runs_unless_debug_is_enabled(
        self,
    ) -> None:
        self.run_cli("on")
        self.sync_with(
            '{"hook_event_name": "UserPromptSubmit", "prompt": "secret text"}'
        )
        self.assertFalse(self.sync_log.exists())

    def test_debug_flag_logs_event_and_model_fields_but_never_prompt_text(self) -> None:
        self.run_cli("on")
        (self.claude_dir / "model-profile" / "debug").write_text("", encoding="utf-8")
        payload = {
            "hook_event_name": "PostModelSwitch",
            "model": "claude-haiku-5-5",
            "previous_model": "opusplan",
            "prompt": "my secret prompt text",
            "cwd": "/Users/someone/private",
            "session_id": "abc",
            "tool_input": {"model_hint": "nested is ignored"},
        }
        self.sync_with(json.dumps(payload))
        raw = self.sync_log.read_text(encoding="utf-8")
        entry = json.loads(raw.splitlines()[-1])
        self.assertEqual(entry["event"], "hook_invoked")
        self.assertEqual(
            entry["hook"],
            {
                "hook_event_name": "PostModelSwitch",
                "model": "claude-haiku-5-5",
                "previous_model": "opusplan",
            },
        )
        self.assertEqual(
            (entry["settings_model"], entry["routing_on"]), (EXPECTED_MODEL, True)
        )
        for leaked in (
            "my secret prompt text",
            "/Users/someone/private",
            "nested is ignored",
            "abc",
        ):
            self.assertNotIn(leaked, raw)

    def test_debug_logging_never_changes_what_sync_does(self) -> None:
        self.run_cli("on")
        (self.claude_dir / "model-profile" / "debug").write_text("", encoding="utf-8")
        self.write_settings({**self.read_settings(), "model": "opus"})
        result = self.sync_with('{"hook_event_name": "UserPromptSubmit"}')
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn(
            "Routing switched OFF", json.loads(result.stdout)["systemMessage"]
        )
        self.assertFalse(self.state_path.exists())

    def test_debug_logging_tolerates_garbage_and_oversized_stdin(self) -> None:
        self.run_cli("on")
        (self.claude_dir / "model-profile" / "debug").write_text("", encoding="utf-8")
        for stdin in ("", "not json", "[1, 2]", "x" * 500_000):
            with self.subTest(size=len(stdin)):
                result = self.sync_with(stdin)
                self.assertEqual((result.returncode, result.stdout), (EXIT_OK, ""))
        entries = [json.loads(line) for line in self.sync_log.read_text().splitlines()]
        self.assertTrue(all(e["hook"] == {} for e in entries))


class TestGuards(SandboxTestCase):
    def test_on_when_already_on_is_a_harmless_no_op(self) -> None:
        self.run_cli("on")
        snapshot = (
            self.read_settings(),
            self.installed_agent_names(),
            self.state_path.read_text(),
        )
        history = self.claude_dir / "model-profile" / "history.jsonl"
        events_before = history.read_text(encoding="utf-8")
        result = self.run_cli("on")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn("Routing is already ON; nothing changed.", result.stdout)
        self.assertIn("profile: ON", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            snapshot,
            (
                self.read_settings(),
                self.installed_agent_names(),
                self.state_path.read_text(),
            ),
        )
        self.assertEqual(
            history.read_text(encoding="utf-8"), events_before, "no new history event"
        )

    def test_on_when_already_on_still_explains_how_to_apply_it_to_this_conversation(
        self,
    ) -> None:
        self.run_cli("on")
        out = self.run_cli("on").stdout
        self.assertIn("already ON", out)
        self.assertIn("run `/model opusplan`", out)
        self.assertNotIn(
            "/model opusplan", self.run_cli("off").stdout + self.run_cli("off").stdout
        )

    def test_off_when_already_off_is_a_harmless_no_op(self) -> None:
        result = self.run_cli("off")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn("Routing is already OFF; nothing changed.", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.read_settings(), BASELINE_SETTINGS)
        self.assertFalse((self.claude_dir / "model-profile" / "history.jsonl").exists())

    def test_unknown_or_extra_argument_is_usage_error_with_help(self) -> None:
        for args in (("bogus",), ("on", "off"), ("--force",)):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, EXIT_USAGE)
                self.assertIn("usage", result.stderr)
                self.assertIn("model-profile on", result.stderr)

    def test_no_argument_shows_status(self) -> None:
        result = self.run_cli()
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn("profile: OFF", result.stdout)

    def test_version_reports_installed_version_or_unknown(self) -> None:
        self.assertIn("unknown", self.run_cli("version").stdout)
        (self.claude_dir / "model-profile" / "VERSION").write_text(
            "9.8.7\n", encoding="utf-8"
        )
        for flag in ("version", "--version"):
            with self.subTest(flag=flag):
                result = self.run_cli(flag)
                self.assertEqual(result.returncode, EXIT_OK)
                self.assertEqual(result.stdout.strip(), "claude-model-profile 9.8.7")

    def test_help_variants_print_usage_and_succeed(self) -> None:
        for flag in ("help", "--help", "-h"):
            with self.subTest(flag=flag):
                result = self.run_cli(flag)
                self.assertEqual(result.returncode, EXIT_OK)
                self.assertIn("model-profile on", result.stdout)
                self.assertIn("/model-profile on|off|status", result.stdout)

    def test_status_explains_meaning_and_next_step_in_both_states(self) -> None:
        off = self.run_cli("status").stdout
        self.assertIn("meaning: standard setup", off)
        self.assertIn("next: `/model-profile on`", off)
        self.run_cli("on")
        on = self.run_cli("status").stdout
        self.assertIn("meaning: cost-saving routing", on)
        self.assertIn("next: `/model-profile off`", on)

    def test_conflicting_agent_file_is_never_overwritten(self) -> None:
        self.agents_dir.mkdir()
        user_file = self.agents_dir / "Explore.md"
        user_file.write_text("user content", encoding="utf-8")
        result = self.run_cli("on")
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertEqual(user_file.read_text(encoding="utf-8"), "user content")
        self.assertEqual(self.read_settings(), BASELINE_SETTINGS)
        self.assertFalse(self.state_path.exists())

    def test_user_agents_survive_on_and_off(self) -> None:
        self.agents_dir.mkdir()
        user_file = self.agents_dir / "mine.md"
        user_file.write_text("keep me", encoding="utf-8")
        self.run_cli("on")
        self.run_cli("off")
        self.assertEqual(user_file.read_text(encoding="utf-8"), "keep me")
        self.assertTrue(
            self.agents_dir.exists(), "non-empty agents dir must not be removed"
        )


class TestFailureHandling(SandboxTestCase):
    def assert_clean_failure(self, result: subprocess.CompletedProcess) -> None:
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertIn("model-profile:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_json_settings_fails_cleanly_and_changes_nothing(self) -> None:
        self.settings_path.write_text("{not json", encoding="utf-8")
        self.assert_clean_failure(self.run_cli("on"))
        self.assertEqual(self.installed_agent_names(), [])
        self.assertFalse(self.state_path.exists())

    def test_missing_settings_is_treated_as_empty_and_round_trips(self) -> None:
        self.settings_path.unlink()
        self.assertEqual(self.run_cli("on").returncode, EXIT_OK)
        self.assertEqual(self.read_settings()["model"], EXPECTED_MODEL)
        self.assertEqual(self.run_cli("off").returncode, EXIT_OK)
        self.assertEqual(self.read_settings(), {})

    def test_non_utf8_settings_fails_cleanly(self) -> None:
        self.settings_path.write_bytes(b"\xff\xfe\x00bad")
        self.assert_clean_failure(self.run_cli("on"))
        self.assertEqual(self.installed_agent_names(), [])

    def test_status_with_invalid_settings_fails_cleanly(self) -> None:
        self.settings_path.write_text("{oops", encoding="utf-8")
        self.assert_clean_failure(self.run_cli("status"))

    def test_non_object_settings_fails_cleanly(self) -> None:
        self.write_settings(["not", "an", "object"])
        self.assert_clean_failure(self.run_cli("on"))

    def test_empty_profile_agents_dir_fails_cleanly(self) -> None:
        for source in (self.claude_dir / "model-profile" / "agents").glob("*.md"):
            source.unlink()
        self.assert_clean_failure(self.run_cli("on"))
        self.assertFalse(self.state_path.exists())

    def test_corrupt_state_file_makes_off_fail_cleanly(self) -> None:
        self.run_cli("on")
        self.state_path.write_text("garbage", encoding="utf-8")
        self.assert_clean_failure(self.run_cli("off"))

    def test_state_missing_expected_keys_makes_off_fail_cleanly(self) -> None:
        self.run_cli("on")
        self.state_path.write_text("{}", encoding="utf-8")
        self.assert_clean_failure(self.run_cli("off"))

    def test_unwritable_agents_dir_rolls_back_completely(self) -> None:
        self.agents_dir.mkdir()
        self.agents_dir.chmod(0o555)
        self.addCleanup(self.agents_dir.chmod, 0o755)
        if os.access(self.agents_dir, os.W_OK):
            self.skipTest("running as a user that ignores directory permissions")
        self.assert_clean_failure(self.run_cli("on"))
        self.assertEqual(self.read_settings(), BASELINE_SETTINGS)
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.installed_agent_names(), [])

    def test_off_tolerates_agent_file_already_deleted(self) -> None:
        self.run_cli("on")
        (self.agents_dir / "Explore.md").unlink()
        self.assertEqual(self.run_cli("off").returncode, EXIT_OK)
        self.assertEqual(self.read_settings(), BASELINE_SETTINGS)


class TestSettingsFileIntegrity(SandboxTestCase):
    def test_file_mode_preserved_across_on_and_off(self) -> None:
        for mode in (0o644, 0o600):
            with self.subTest(mode=oct(mode)):
                self.write_settings(BASELINE_SETTINGS, mode=mode)
                self.run_cli("on")
                self.assertEqual(stat.S_IMODE(self.settings_path.stat().st_mode), mode)
                self.run_cli("off")
                self.assertEqual(stat.S_IMODE(self.settings_path.stat().st_mode), mode)

    def test_no_temp_files_left_behind(self) -> None:
        self.run_cli("on")
        self.run_cli("off")
        self.assertEqual(list(self.claude_dir.glob("*.tmp")), [])
        self.assertEqual(list((self.claude_dir / "model-profile").glob("*.tmp")), [])

    def test_symlinked_settings_keeps_link_and_updates_target(self) -> None:
        real_target = self.home / "dotfiles-settings.json"
        real_target.write_text(json.dumps(BASELINE_SETTINGS), encoding="utf-8")
        self.settings_path.unlink()
        self.settings_path.symlink_to(real_target)
        self.assertEqual(self.run_cli("on").returncode, EXIT_OK)
        self.assertTrue(self.settings_path.is_symlink(), "symlink must survive")
        self.assertEqual(json.loads(real_target.read_text())["model"], EXPECTED_MODEL)
        self.run_cli("off")
        self.assertTrue(self.settings_path.is_symlink())
        self.assertEqual(json.loads(real_target.read_text()), BASELINE_SETTINGS)

    def test_unicode_settings_round_trip(self) -> None:
        payload = {**BASELINE_SETTINGS, "note": "café ✓ 日本語"}
        self.write_settings(payload)
        self.run_cli("on")
        self.run_cli("off")
        self.assertEqual(self.read_settings(), payload)


class TestUserChangesWhileOn(SandboxTestCase):
    def test_agent_file_edited_while_on_is_kept_with_warning(self) -> None:
        self.run_cli("on")
        edited = self.agents_dir / "Explore.md"
        edited.write_text("my own edits", encoding="utf-8")
        result = self.run_cli("off")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertEqual(edited.read_text(encoding="utf-8"), "my own edits")
        self.assertIn("left in place", result.stderr)
        self.assertEqual(self.installed_agent_names(), ["Explore.md"])
        self.assertFalse(self.state_path.exists())

    def test_model_changed_while_on_is_kept_and_other_key_restored(self) -> None:
        self.run_cli("on")
        self.write_settings({**self.read_settings(), "model": "opus"})
        result = self.run_cli("off")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn("`model` was changed", result.stderr)
        settings = self.read_settings()
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["effortLevel"], BASELINE_SETTINGS["effortLevel"])

    def test_key_removed_while_on_stays_removed(self) -> None:
        self.run_cli("on")
        settings = self.read_settings()
        del settings["model"]
        self.write_settings(settings)
        self.run_cli("off")
        self.assertNotIn("model", self.read_settings())

    def test_unrelated_settings_added_while_on_survive_off(self) -> None:
        self.run_cli("on")
        self.write_settings({**self.read_settings(), "theme": "dark"})
        self.run_cli("off")
        self.assertEqual(self.read_settings()["theme"], "dark")

    def test_restore_keeps_original_key_order(self) -> None:
        self.run_cli("on")
        self.run_cli("off")
        self.assertEqual(list(self.read_settings()), list(BASELINE_SETTINGS))

    def test_explicit_null_value_is_restored_not_dropped(self) -> None:
        self.write_settings({"model": None, "env": {}})
        self.run_cli("on")
        self.run_cli("off")
        self.assertEqual(self.read_settings(), {"model": None, "env": {}})


class TestAgentsDirectoryHandling(SandboxTestCase):
    def test_preexisting_empty_agents_dir_is_kept(self) -> None:
        self.agents_dir.mkdir()
        self.run_cli("on")
        self.run_cli("off")
        self.assertTrue(self.agents_dir.is_dir())

    def test_symlinked_agents_dir_never_breaks_off(self) -> None:
        real_dir = self.home / "dotfiles-agents"
        real_dir.mkdir()
        self.agents_dir.symlink_to(real_dir)
        self.assertEqual(self.run_cli("on").returncode, EXIT_OK)
        self.assertEqual(
            sorted(p.name for p in real_dir.glob("*.md")), self.source_agent_names()
        )
        self.assertEqual(self.run_cli("off").returncode, EXIT_OK)
        self.assertTrue(self.agents_dir.is_symlink())
        self.assertEqual(list(real_dir.glob("*.md")), [])
        self.assertFalse(self.state_path.exists())

    def test_dangling_symlink_at_destination_is_a_conflict_not_written_through(
        self,
    ) -> None:
        self.agents_dir.mkdir()
        outside_target = self.home / "elsewhere.md"
        (self.agents_dir / "Explore.md").symlink_to(outside_target)
        result = self.run_cli("on")
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertFalse(outside_target.exists(), "must not write through a symlink")
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.read_settings(), BASELINE_SETTINGS)


class TestStateFileValidation(SandboxTestCase):
    def assert_off_rejects_state(self, state: object) -> None:
        self.run_cli("on")
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        before = self.read_settings()
        result = self.run_cli("off")
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("state file damaged", result.stderr)
        self.assertEqual(self.read_settings(), before)

    def test_path_traversal_in_agent_name_is_rejected(self) -> None:
        victim = self.home / "victim.md"
        victim.write_text("do not delete", encoding="utf-8")
        self.assert_off_rejects_state(
            {
                "previous_settings": {},
                "installed_agents": {"../../victim.md": "x"},
                "created_agents_dir": True,
            }
        )
        self.assertTrue(victim.exists())

    def test_absolute_agent_path_is_rejected(self) -> None:
        self.assert_off_rejects_state(
            {
                "previous_settings": {},
                "installed_agents": {"/etc/hosts.md": "x"},
                "created_agents_dir": False,
            }
        )

    def test_foreign_settings_key_in_state_is_rejected(self) -> None:
        self.assert_off_rejects_state(
            {
                "previous_settings": {"permissions": {}},
                "installed_agents": {},
                "created_agents_dir": False,
            }
        )

    def test_wrong_types_in_state_are_rejected(self) -> None:
        self.assert_off_rejects_state(
            {
                "previous_settings": [],
                "installed_agents": "x",
                "created_agents_dir": "yes",
            }
        )


class TestConcurrency(SandboxTestCase):
    def test_simultaneous_on_runs_leave_consistent_state(self) -> None:
        env = {**os.environ, "HOME": str(self.home)}
        processes = [
            subprocess.Popen(
                [sys.executable, str(CLI_PATH), "on"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(6)
        ]
        codes = [process.wait() for process in processes]
        for process in processes:
            process.stdout.close()
            process.stderr.close()
        self.assertGreaterEqual(codes.count(EXIT_OK), 1)
        self.assertTrue(set(codes) <= {EXIT_OK, EXIT_FAILURE})
        self.assertEqual(self.installed_agent_names(), self.source_agent_names())
        self.assertEqual(self.read_settings()["model"], EXPECTED_MODEL)
        self.assertEqual(self.run_cli("off").returncode, EXIT_OK)
        self.assertEqual(self.read_settings(), BASELINE_SETTINGS)


class TestSettingsEncoding(SandboxTestCase):
    def test_non_ascii_is_written_literally_not_escaped(self) -> None:
        self.write_settings({**BASELINE_SETTINGS, "note": "café 日本語"})
        self.run_cli("on")
        self.assertIn("café 日本語", self.settings_path.read_text(encoding="utf-8"))


COMMANDS_DIR = AGENTS_DIR.parent / "commands"
VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
DOCUMENTED_COMMAND_FIELDS = {
    "description",
    "argument-hint",
    "model",
    "effort",
    "allowed-tools",
    "disable-model-invocation",
    "user-invocable",
}
MAX_COMPLEX_COMMAND_WORDS = 400
EXPECTED_ROLE_EFFORT = {
    "Explore": "low",
    "log-reader": "low",
    "deep-reasoner": "high",
    "code-reviewer": "high",
}


def read_frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    _, header, body = text.split("---", 2)
    return dict(line.split(": ", 1) for line in header.strip().splitlines()), body


class TestShippedCommands(unittest.TestCase):
    def test_every_command_has_documented_fields_and_valid_values(self) -> None:
        for path in sorted(COMMANDS_DIR.glob("*.md")):
            with self.subTest(command=path.name):
                meta, body = read_frontmatter(path)
                self.assertIn("description", meta)
                self.assertFalse(
                    set(meta) - DOCUMENTED_COMMAND_FIELDS,
                    "undocumented frontmatter field",
                )
                if "effort" in meta:
                    self.assertIn(meta["effort"], VALID_EFFORTS)
                self.assertEqual(
                    meta.get("disable-model-invocation"),
                    "true",
                    "a prompt injection must never be able to run this command",
                )
                self.assertTrue(body.strip())

    def test_complex_runs_on_sonnet_at_high_effort_and_only_when_asked_for(
        self,
    ) -> None:
        meta, body = read_frontmatter(COMMANDS_DIR / "complex.md")
        self.assertEqual((meta["model"], meta["effort"]), ("sonnet", "high"))
        self.assertEqual(
            meta["disable-model-invocation"], "true", "must never run unprompted"
        )
        self.assertEqual(body.count("$ARGUMENTS"), 1)

    def test_complex_prompt_encodes_the_tiered_workflow(self) -> None:
        _, body = read_frontmatter(COMMANDS_DIR / "complex.md")
        for needle in (
            "SIMPLE",
            "MODERATE",
            "COMPLEX",
            "Explore",
            "deep-reasoner",
            "narrowest check",
            "fails twice",
            "Never claim done without evidence",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)

    def test_complex_prompt_stays_small_because_it_is_paid_for_on_every_use(
        self,
    ) -> None:
        _, body = read_frontmatter(COMMANDS_DIR / "complex.md")
        self.assertLessEqual(len(body.split()), MAX_COMPLEX_COMMAND_WORDS)


class TestShippedAgentDefinitions(unittest.TestCase):
    def test_effort_follows_the_role_and_turns_are_capped(self) -> None:
        for path in sorted(AGENTS_DIR.glob("*.md")):
            with self.subTest(agent=path.name):
                meta, _ = read_frontmatter(path)
                self.assertEqual(meta["effort"], EXPECTED_ROLE_EFFORT[path.stem])
                self.assertIn(meta["effort"], VALID_EFFORTS)
                self.assertTrue(
                    0 < int(meta["maxTurns"]) <= 50, "turn cap bounds token spend"
                )

    """Static checks on the agent files that will be installed."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.agent_files = sorted((AGENTS_DIR).glob("*.md"))

    @staticmethod
    def parse_frontmatter(path: Path) -> dict[str, str]:
        match = FRONTMATTER_PATTERN.match(path.read_text(encoding="utf-8"))
        if match is None:
            raise AssertionError(f"{path.name}: missing frontmatter")
        return dict(line.split(": ", 1) for line in match.group(1).splitlines())

    def test_expected_agents_exist(self) -> None:
        self.assertEqual(
            [p.stem for p in self.agent_files],
            ["Explore", "code-reviewer", "deep-reasoner", "log-reader"],
        )

    def test_frontmatter_is_valid_for_every_agent(self) -> None:
        for path in self.agent_files:
            with self.subTest(agent=path.name):
                meta = self.parse_frontmatter(path)
                self.assertEqual(meta["name"], path.stem)
                self.assertIn(meta["model"], ALLOWED_AGENT_MODELS)
                self.assertGreater(len(meta["description"]), 40)
                self.assertTrue(meta["tools"])

    def test_every_agent_is_read_only(self) -> None:
        forbidden_tools = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
        for path in self.agent_files:
            with self.subTest(agent=path.name):
                tools = {
                    t.strip() for t in self.parse_frontmatter(path)["tools"].split(",")
                }
                self.assertFalse(tools & forbidden_tools)
                self.assertIn(
                    "never edit",
                    path.read_text(encoding="utf-8")
                    .lower()
                    .replace("do not edit", "never edit"),
                )

    def test_model_assignments_match_routing_design(self) -> None:
        models = {p.stem: self.parse_frontmatter(p)["model"] for p in self.agent_files}
        self.assertEqual(
            models,
            {
                "Explore": "haiku",
                "log-reader": "haiku",
                "deep-reasoner": "opus",
                "code-reviewer": "opus",
            },
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

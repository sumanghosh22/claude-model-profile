"""End-to-end tests for the Opus review gate (`snapshot` + `review-gate` hooks).

Each test builds a real git repository and a real-format transcript inside a throwaway HOME.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from paths import PROFILE_CLI
from test_model_profile import REVIEW_GATE_COMMAND, SandboxTestCase

EXIT_OK = 0
SESSION = "session-123"
GATE_SECONDS_LIMIT = 5.0


class ReviewGateTestCase(SandboxTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.run_cli("on")
        self.repo = self.home / "work" / "repo"
        self.repo.mkdir(parents=True)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        (self.repo / "app.py").write_text("print('v1')\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "initial")
        self.transcript = (
            self.claude_dir / "projects" / "-work-repo" / f"{SESSION}.jsonl"
        )
        self.transcript.parent.mkdir(parents=True)
        self.entries: list[dict] = []
        self.new_prompt()

    # -- helpers -------------------------------------------------------------------------
    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def hook(self, command: str, payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", str(PROFILE_CLI), command],
            input=json.dumps(payload),
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def payload(self, **extra: object) -> dict:
        return {
            "session_id": SESSION,
            "transcript_path": str(self.transcript),
            "cwd": str(self.repo),
            **extra,
        }

    def write_transcript(self) -> None:
        self.transcript.write_text(
            "\n".join(json.dumps(e) for e in self.entries) + "\n", encoding="utf-8"
        )

    def new_prompt(self, text: str = "please do the task") -> None:
        self.entries.append(
            {"type": "user", "message": {"role": "user", "content": text}}
        )
        self.write_transcript()
        result = self.hook(
            "snapshot", self.payload(hook_event_name="UserPromptSubmit", prompt=text)
        )
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)

    def record_review(self, agent: str, verdict: str, call_id: str = "toolu_1") -> None:
        self.entries.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": "Agent",
                            "input": {
                                "subagent_type": agent,
                                "prompt": "review",
                                "description": "review",
                            },
                        }
                    ]
                },
            }
        )
        self.entries.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": [
                                {"type": "text", "text": f"Findings...\n{verdict}"}
                            ],
                        }
                    ]
                },
            }
        )
        self.write_transcript()

    def record_edit(self, file_path: str) -> None:
        self.entries.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"edit-{len(self.entries)}",
                            "name": "Edit",
                            "input": {
                                "file_path": file_path,
                                "old_string": "a",
                                "new_string": "b",
                            },
                        }
                    ]
                },
            }
        )
        self.write_transcript()

    def record_check(
        self,
        command: str = "python3 -m pytest -q",
        passed: bool = True,
        **extra: object,
    ) -> None:
        call_id = f"check-{len(self.entries)}"
        self.entries.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": "Bash",
                            "input": {"command": command, **extra},
                        }
                    ]
                },
            }
        )
        self.entries.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "is_error": not passed,
                            "content": "3 passed"
                            if passed
                            else "Exit code 1\n1 failed",
                        }
                    ]
                },
            }
        )
        self.write_transcript()

    def gate(self, **extra: object) -> dict | None:
        result = self.hook(
            "review-gate",
            self.payload(hook_event_name="Stop", stop_hook_active=False, **extra),
        )
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def assert_blocks_for(self, decision: dict | None, agent: str) -> str:
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision["decision"], "block")
        self.assertIn(f"Call the {agent} agent", decision["reason"])
        return decision["reason"]


class TestWhenToReview(ReviewGateTestCase):
    def test_a_task_that_changed_nothing_is_never_reviewed(self) -> None:
        self.assertIsNone(self.gate())

    def test_a_changed_file_requires_an_opus_review(self) -> None:
        (self.repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        reason = self.assert_blocks_for(self.gate(), "code-reviewer")
        self.assertIn("`app.py`", reason)
        self.assertIn("git diff", reason)

    def test_a_new_untracked_file_counts(self) -> None:
        (self.repo / "new_module.py").write_text("x = 1\n", encoding="utf-8")
        self.assertIn(
            "`new_module.py`", self.assert_blocks_for(self.gate(), "code-reviewer")
        )

    def test_a_commit_made_during_the_task_counts(self) -> None:
        (self.repo / "app.py").write_text("print('v3')\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "change")
        self.assertIn("`app.py`", self.assert_blocks_for(self.gate(), "code-reviewer"))

    def test_files_already_dirty_before_the_task_are_not_blamed_on_it(self) -> None:
        (self.repo / "app.py").write_text("local only change\n", encoding="utf-8")
        self.new_prompt("next task")
        self.assertIsNone(self.gate())

    def test_a_deleted_file_counts(self) -> None:
        (self.repo / "app.py").unlink()
        self.assertIn("`app.py`", self.assert_blocks_for(self.gate(), "code-reviewer"))


class TestCostTiering(ReviewGateTestCase):
    def test_risky_areas_get_the_deep_reviewer(self) -> None:
        for name in (
            "billing/charge.py",
            "src/auth/login.ts",
            "db/migrations/0042_add.sql",
            "webhooks.py",
        ):
            with self.subTest(path=name):
                self.new_prompt(f"task for {name}")
                target = self.repo / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("x = 1\n", encoding="utf-8")
                self.assert_blocks_for(self.gate(), "deep-reasoner")
                target.unlink()

    def test_a_large_change_gets_the_deep_reviewer(self) -> None:
        (self.repo / "app.py").write_text(
            "".join(f"line {i}\n" for i in range(400)), encoding="utf-8"
        )
        self.assert_blocks_for(self.gate(), "deep-reasoner")

    def test_many_files_get_the_deep_reviewer(self) -> None:
        for i in range(12):
            (self.repo / f"m{i}.py").write_text("x = 1\n", encoding="utf-8")
        reason = self.assert_blocks_for(self.gate(), "deep-reasoner")
        self.assertIn("and 4 more", reason)

    def test_a_small_ordinary_change_gets_the_cheaper_reviewer(self) -> None:
        (self.repo / "app.py").write_text("print('small')\n", encoding="utf-8")
        self.assert_blocks_for(self.gate(), "code-reviewer")


class TestReviewOutcome(ReviewGateTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        self.record_check()

    def test_an_approving_review_lets_the_task_finish(self) -> None:
        self.record_review("code-reviewer", "VERDICT: APPROVE")
        self.assertIsNone(self.gate())

    def test_a_deep_review_also_counts(self) -> None:
        self.record_review("deep-reasoner", "VERDICT: APPROVE")
        self.assertIsNone(self.gate())

    def test_changes_required_forces_a_fix_and_a_second_review(self) -> None:
        self.record_review("code-reviewer", "VERDICT: CHANGES REQUIRED", "toolu_1")
        reason = self.assert_blocks_for_changes(self.gate())
        self.assertIn("The last review requested changes", reason)
        self.assertIn("The reviewer checks every scenario for a test", reason)
        self.record_review("code-reviewer", "VERDICT: APPROVE", "toolu_2")
        self.assertIsNone(self.gate())

    def assert_blocks_for_changes(self, decision: dict | None) -> str:
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision["decision"], "block")
        return decision["reason"]

    def test_a_review_from_an_earlier_task_does_not_count(self) -> None:
        self.record_review("code-reviewer", "VERDICT: APPROVE")
        self.git("commit", "-q", "-am", "reviewed earlier")
        self.new_prompt("new task")
        (self.repo / "app.py").write_text("print('v3 unreviewed')\n", encoding="utf-8")
        self.assert_blocks_for(self.gate(), "code-reviewer")

    def test_other_agents_do_not_count_as_a_review(self) -> None:
        self.record_review("Explore", "VERDICT: APPROVE")
        self.assert_blocks_for(self.gate(), "code-reviewer")


class TestNeverTrapsTheSession(ReviewGateTestCase):
    def test_after_two_requests_the_gate_gives_up_with_a_visible_notice(self) -> None:
        (self.repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        self.assertEqual(self.gate()["decision"], "block")
        self.assertEqual(self.gate()["decision"], "block")
        third = self.gate()
        assert third is not None
        self.assertNotIn("decision", third)
        self.assertIn("did not approve this change", third["systemMessage"])
        self.assertIn(
            "review_gate_gave_up",
            (self.claude_dir / "model-profile" / "sync.log").read_text(),
        )

    def test_a_new_prompt_resets_the_counter(self) -> None:
        (self.repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        self.gate()
        self.gate()
        self.new_prompt("try again")
        (self.repo / "app.py").write_text("print('v3')\n", encoding="utf-8")
        self.assertEqual(self.gate()["decision"], "block")

    def test_routing_off_means_no_gate(self) -> None:
        (self.repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        self.run_cli("off")
        self.assertIsNone(self.gate())

    def test_broken_inputs_fail_open_and_are_logged(self) -> None:
        (self.repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        state = self.claude_dir / "model-profile" / "review" / f"{SESSION}.json"
        state.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.gate())
        self.assertIn(
            "review_gate_failed",
            (self.claude_dir / "model-profile" / "sync.log").read_text(),
        )
        for stdin in ("", "garbage", "[1,2]"):
            with self.subTest(stdin=stdin):
                result = subprocess.run(
                    [sys.executable, "-I", str(PROFILE_CLI), "review-gate"],
                    input=stdin,
                    env={**os.environ, "HOME": str(self.home)},
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                self.assertEqual((result.returncode, result.stdout), (EXIT_OK, ""))

    def test_the_gate_is_fast_on_a_busy_working_tree(self) -> None:
        for i in range(300):
            (self.repo / f"generated_{i}.txt").write_text("x\n" * 50, encoding="utf-8")
        started = time.monotonic()
        self.assertIsNotNone(self.gate())
        self.assertLess(time.monotonic() - started, GATE_SECONDS_LIMIT)


class TestOutsideGit(ReviewGateTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.plain = self.home / "notes"
        self.plain.mkdir()

    def test_edits_seen_in_the_transcript_still_require_a_review(self) -> None:
        self.entries.append({"type": "user", "message": {"content": "edit my notes"}})
        self.write_transcript()
        self.hook("snapshot", self.payload(cwd=str(self.plain)))
        self.record_edit(str(self.plain / "todo.md"))
        decision = self.hook("review-gate", self.payload(cwd=str(self.plain)))
        self.assertIn("todo.md", json.loads(decision.stdout)["reason"])

    def test_no_edits_means_no_review(self) -> None:
        self.hook("snapshot", self.payload(cwd=str(self.plain)))
        self.assertEqual(
            self.hook("review-gate", self.payload(cwd=str(self.plain))).stdout, ""
        )


class TestGateSecurity(ReviewGateTestCase):
    def test_repo_configured_programs_never_run_from_the_hook(self) -> None:
        marker = self.home / "repo-config-ran"
        script = self.home / "evil.sh"
        script.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        script.chmod(0o755)
        self.git("config", "core.fsmonitor", str(script))
        self.git("config", "diff.external", str(script))
        self.git("config", "core.pager", str(script))
        self.new_prompt("task in a hostile repo")
        (self.repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        self.assertIsNotNone(self.gate())
        self.assertFalse(
            marker.exists(), "fsmonitor / external diff / pager must not execute"
        )

    def test_a_crafted_session_id_cannot_write_outside_the_review_folder(self) -> None:
        absolute = self.home / "absolute-escape"
        attempts = ("../../escape", str(absolute), "a/b", "x" * 300, "")
        for session in attempts:
            with self.subTest(session=session[:20]):
                self.hook("snapshot", self.payload(session_id=session))
                self.hook("review-gate", self.payload(session_id=session))
        self.assertFalse((self.claude_dir / "escape.json").exists())
        self.assertFalse(Path(f"{absolute}.json").exists())
        review_dir = self.claude_dir / "model-profile" / "review"
        self.assertEqual(
            sorted(p.name for p in review_dir.glob("*.json")), [f"{SESSION}.json"]
        )

    def test_a_transcript_outside_claude_projects_is_ignored(self) -> None:
        outside = self.home / "elsewhere.jsonl"
        outside.write_text(self.transcript.read_text(), encoding="utf-8")
        self.record_review("code-reviewer", "VERDICT: APPROVE")
        outside.write_text(self.transcript.read_text(), encoding="utf-8")
        (self.repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        decision = self.gate(transcript_path=str(outside))
        self.assertIsNotNone(
            decision, "a planted 'approval' outside the projects folder must not count"
        )

    def test_odd_file_names_are_shown_safely(self) -> None:
        odd = "weird`name\tok.py"
        (self.repo / odd).write_text("x\n", encoding="utf-8")
        reason = self.gate()["reason"]
        self.assertNotIn("\t", reason)
        self.assertIn("weirdnameok.py", reason.replace("`", ""))


class TestHookRegistration(ReviewGateTestCase):
    def commands(self, event: str) -> list[dict]:
        return [
            hook
            for group in self.read_settings()["hooks"].get(event, [])
            for hook in group["hooks"]
            if "model-profile" in hook["command"]
        ]

    def test_stop_and_prompt_hooks_are_registered_with_generous_timeouts(self) -> None:
        stop = self.commands("Stop")
        self.assertEqual([h["command"] for h in stop], [REVIEW_GATE_COMMAND])
        self.assertGreaterEqual(stop[0]["timeout"], 30)
        self.assertTrue(
            any(
                "prompt-start" in h["command"]
                for h in self.commands("UserPromptSubmit")
            )
        )

    def test_status_reports_the_review_gate(self) -> None:
        self.assertIn("review gate: active", self.run_cli("status").stdout)

    def test_off_removes_the_gate(self) -> None:
        self.run_cli("off")
        self.assertNotIn("model-profile", json.dumps(self.read_settings()))

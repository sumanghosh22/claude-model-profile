"""The review gate's evidence check (tests must pass after the last edit) and secret scan."""

import json

from test_review_gate import ReviewGateTestCase

APPROVE = "VERDICT: APPROVE"
# Built from parts so this test file never contains a scannable credential itself.
AWS_KEY = "AKIA" + "Q7RZ3MVK2WJ5TP4N"
STRIPE_KEY = "sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc"
PEM_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"


class TestEvidenceGate(ReviewGateTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.repo / "app.py").write_text("print('v2')\n", encoding="utf-8")

    def reason(self) -> str:
        decision = self.gate()
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision["decision"], "block")
        return str(decision["reason"])

    def test_an_approved_change_without_a_check_run_is_blocked(self) -> None:
        self.record_review("code-reviewer", APPROVE)
        reason = self.reason()
        self.assertIn("No passing test", reason)
        self.assertNotIn("Call the code-reviewer agent", reason, "review already done")

    def test_a_passing_check_after_the_last_edit_satisfies_it(self) -> None:
        self.record_edit(str(self.repo / "app.py"))
        self.record_check("make check")
        self.record_review("code-reviewer", APPROVE)
        self.assertIsNone(self.gate())

    def test_a_check_before_the_last_edit_does_not_count(self) -> None:
        self.record_check()
        self.record_edit(str(self.repo / "app.py"))
        self.record_review("code-reviewer", APPROVE)
        self.assertIn("No passing test", self.reason())

    def test_the_latest_check_must_be_green(self) -> None:
        self.record_check(passed=True)
        self.record_check("npm test", passed=False)
        self.record_review("code-reviewer", APPROVE)
        self.assertIn("No passing test", self.reason())

    def test_ordinary_commands_and_background_runs_are_not_evidence(self) -> None:
        self.record_check("ls -la && git status")
        self.record_check("pytest", run_in_background=True)
        self.record_review("code-reviewer", APPROVE)
        self.assertIn("No passing test", self.reason())

    def test_common_toolchains_are_recognised(self) -> None:
        module = self.load_profile_module()
        for command in (
            "python3 -m unittest discover -s tests",
            "npx tsc --noEmit",
            "npm run lint",
            "pnpm test",
            "go test ./...",
            "cargo clippy",
            "NEXT_PUBLIC_APP_ENV=production next build",
            "ruff check . && mypy --strict src",
        ):
            with self.subTest(command=command):
                self.assertTrue(module.is_check_command("Bash", {"command": command}))
        self.assertFalse(module.is_check_command("Bash", {"command": "cat tests.txt"}))
        self.assertFalse(module.is_check_command("Read", {"command": "pytest"}))

    def test_one_block_asks_for_everything_missing(self) -> None:
        reason = self.reason()
        self.assertIn("1. No passing test", reason)
        self.assertIn("2. An Opus review is required", reason)

    def test_docs_only_changes_need_no_check_run(self) -> None:
        self.git("checkout", "--", "app.py")
        (self.repo / "README.md").write_text("# docs\n", encoding="utf-8")
        self.record_review("code-reviewer", APPROVE)
        self.assertIsNone(self.gate())

    def test_docs_plus_code_still_need_a_check_run(self) -> None:
        (self.repo / "README.md").write_text("# docs\n", encoding="utf-8")
        self.record_review("code-reviewer", APPROVE)
        self.assertIn("No passing test", self.reason())

    def test_give_up_notice_names_what_is_missing(self) -> None:
        self.record_review("code-reviewer", APPROVE)
        self.gate()
        self.gate()
        notice = self.gate()
        assert notice is not None
        self.assertIn("no passing check run was seen", notice["systemMessage"])
        self.assertNotIn("did not approve", notice["systemMessage"])

    def load_profile_module(self):  # type: ignore[no-untyped-def]
        from loader import load_script
        from paths import PROFILE_CLI

        return load_script(PROFILE_CLI, self.home, "model_profile_quality")


class TestSecretScan(ReviewGateTestCase):
    def finish_cleanly(self) -> None:
        self.record_check()
        self.record_review("code-reviewer", APPROVE)

    def test_a_committed_style_key_in_new_code_blocks_finishing(self) -> None:
        (self.repo / "config.py").write_text(
            f"import os\nAWS_KEY = '{AWS_KEY}'\n", encoding="utf-8"
        )
        self.finish_cleanly()
        decision = self.gate()
        assert decision is not None
        reason = decision["reason"]
        self.assertIn("`config.py:2` (AWS access key)", reason)
        self.assertNotIn(AWS_KEY, reason, "the secret value is never repeated")
        self.assertNotIn(
            AWS_KEY, (self.claude_dir / "model-profile" / "sync.log").read_text()
        )

    def test_only_added_lines_are_scanned(self) -> None:
        (self.repo / "keys.py").write_text(
            f"STRIPE = '{STRIPE_KEY}'\n", encoding="utf-8"
        )
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "old secret, not this task's")
        self.new_prompt("tidy keys.py")
        with (self.repo / "keys.py").open("a", encoding="utf-8") as handle:
            handle.write("VERSION = 2\n")
        self.finish_cleanly()
        self.assertIsNone(self.gate())

    def test_removing_a_secret_is_never_flagged(self) -> None:
        (self.repo / "keys.py").write_text(
            f"STRIPE = '{STRIPE_KEY}'\nVERSION = 1\n", encoding="utf-8"
        )
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "old secret")
        self.new_prompt("remove the hard-coded key")
        (self.repo / "keys.py").write_text("VERSION = 1\n", encoding="utf-8")
        self.finish_cleanly()
        self.assertIsNone(self.gate())

    def test_line_numbers_follow_the_diff(self) -> None:
        (self.repo / "app.py").write_text(
            "print('v1')\nx = 1\ny = 2\n" + PEM_HEADER + "\n", encoding="utf-8"
        )
        self.finish_cleanly()
        self.assertIn("`app.py:4` (private key)", self.gate()["reason"])

    def test_environment_lookups_and_placeholders_are_not_flagged(self) -> None:
        (self.repo / "settings.py").write_text(
            "password = os.environ['DB_PASSWORD']\n"
            "api_key = 'your-api-key-goes-here'\n"
            "token = '${API_TOKEN_FROM_VAULT}'\n"
            "EXAMPLE = 'AKIAIOSFODNN7EXAMPLE'\n",
            encoding="utf-8",
        )
        self.finish_cleanly()
        self.assertIsNone(self.gate())

    def test_a_hard_coded_password_is_flagged(self) -> None:
        (self.repo / "db.py").write_text(
            'DB_PASSWORD = "Hunter2-prod-Secr3t"\n', encoding="utf-8"
        )
        self.finish_cleanly()
        self.assertIn("hard-coded credential", self.gate()["reason"])

    def test_fixture_passwords_in_tests_are_not_flagged_but_real_keys_are(self) -> None:
        tests_dir = self.repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_login.py").write_text(
            'PASSWORD = "Fixture-Passw0rd-123"\n', encoding="utf-8"
        )
        (self.repo / ".env.example").write_text(
            'API_KEY="replace-with-real-value"\n', encoding="utf-8"
        )
        self.finish_cleanly()
        self.assertIsNone(self.gate())
        (tests_dir / "test_aws.py").write_text(f"KEY = '{AWS_KEY}'\n", encoding="utf-8")
        self.assertIn("`tests/test_aws.py:1` (AWS access key)", self.gate()["reason"])

    def test_a_kept_secret_is_flagged_once_then_only_reported(self) -> None:
        (self.repo / "fixture.py").write_text(f"KEY = '{AWS_KEY}'\n", encoding="utf-8")
        self.finish_cleanly()
        self.assertEqual(self.gate()["decision"], "block")
        second = self.gate()
        assert second is not None
        self.assertNotIn("decision", second, "no loop: Claude said it is deliberate")
        self.assertIn("`fixture.py:1` (AWS access key)", second["systemMessage"])
        self.assertNotIn(AWS_KEY, second["systemMessage"])

    def test_binary_files_are_skipped(self) -> None:
        (self.repo / "blob.bin").write_bytes(b"\0\1" + AWS_KEY.encode())
        self.finish_cleanly()
        self.assertIsNone(self.gate())

    def test_outside_git_edited_files_are_scanned(self) -> None:
        plain = self.home / "notes"
        plain.mkdir()
        target = plain / "deploy.sh"
        target.write_text(f"export AWS={AWS_KEY}\n", encoding="utf-8")
        self.hook("snapshot", self.payload(cwd=str(plain)))
        self.record_edit(str(target))
        self.finish_cleanly()
        result = self.hook("review-gate", self.payload(cwd=str(plain)))
        self.assertIn("(AWS access key)", json.loads(result.stdout)["reason"])

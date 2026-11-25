"""The evidence check (critical work answered from memory) and the fast transcript reader.

The incident behind it: a production-database analysis, sized critical, was answered with facts
about the data and no tool call at all. The check asks once for a real check or an honest
"not verified"; it never loops, never reads or logs the answer, and fails open.
"""

import json
import time
from pathlib import Path

from loader import load_script
from paths import PROFILE_CLI, ROOT
from test_review_gate import SESSION, ReviewGateTestCase

DIAGNOSIS = "you have db access so just read and find the solution"
FACTS = "The 500,249 comes from the subscriptions table: 963 documents are read for each account."
EXIT_OK = 0
HUGE_TRANSCRIPT_MB = 40
READ_SECONDS_LIMIT = 3.0


class EvidenceTestCase(ReviewGateTestCase):
    def prompt_start(self, text: str) -> None:
        self.entries.append(
            {"type": "user", "message": {"role": "user", "content": text}}
        )
        self.write_transcript()
        result = self.hook(
            "prompt-start",
            self.payload(hook_event_name="UserPromptSubmit", prompt=text),
        )
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)

    def use_tool(self, name: str = "Bash") -> None:
        self.entries.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"t-{len(self.entries)}",
                            "name": name,
                            "input": {"command": "ls"},
                        }
                    ]
                },
            }
        )
        self.write_transcript()

    def answer(self, text: str) -> None:
        self.entries.append(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
            }
        )
        self.write_transcript()

    def review_file(self) -> Path:
        return self.claude_dir / "model-profile" / "review" / f"{SESSION}.json"

    def log(self) -> str:
        return (self.claude_dir / "model-profile" / "sync.log").read_text()


class TestEvidenceCheck(EvidenceTestCase):
    def test_a_critical_answer_with_data_facts_and_no_check_is_sent_back_once(
        self,
    ) -> None:
        self.prompt_start(DIAGNOSIS)
        self.answer(FACTS)
        decision = self.gate()
        assert decision is not None
        self.assertEqual(decision["decision"], "block")
        self.assertIn("evidence check", decision["reason"])
        self.assertIn("not verified", decision["reason"])
        self.assertIsNone(self.gate(), "asked once; never a loop")

    def test_a_real_check_in_the_turn_satisfies_it(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.use_tool("Bash")
        self.answer(FACTS)
        self.assertIsNone(self.gate())

    def test_calling_a_skill_or_planning_is_not_a_check(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.use_tool("Skill")
        self.use_tool("TodoWrite")
        self.answer(FACTS)
        self.assertIsNotNone(self.gate())

    def test_saying_plainly_what_is_unverified_satisfies_it(self) -> None:
        for marker in (
            "Not verified: ",
            "This is inferred from the counts, not measured. ",
            "I haven't checked the live data. ",
            "Assuming the indexes match dev, ",
        ):
            with self.subTest(marker=marker):
                self.new_prompt("reset")
                self.prompt_start(DIAGNOSIS)
                self.answer(marker + FACTS)
                self.assertIsNone(self.gate())

    def test_only_critical_turns_are_checked(self) -> None:
        for text in ("what does this function return?", "how can i make this faster"):
            with self.subTest(text=text):
                self.prompt_start(text)
                self.answer(FACTS)
                self.assertIsNone(self.gate())

    def test_answers_without_data_facts_pass(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.answer(
            "Yes, the code was correct. The slowdown comes from the query shape."
        )
        self.assertIsNone(self.gate(), "no numbers")
        self.new_prompt("reset")
        self.prompt_start(DIAGNOSIS)
        self.answer("It took 120 ms in the checkout page and 3 seconds in the export.")
        self.assertIsNone(self.gate(), "numbers, but nothing about data or production")

    def test_a_follow_up_in_a_critical_conversation_is_checked_too(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.use_tool("Bash")
        self.answer("Read the plan: 372 accounts scanned.")
        self.assertIsNone(self.gate())
        self.prompt_start("500,249 this data in which table have?")
        self.answer(FACTS)
        decision = self.gate()
        assert decision is not None, (
            "the question that was answered wrongly is now checked"
        )
        self.assertIn("evidence check", decision["reason"])

    def test_the_next_message_starts_fresh(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.answer(FACTS)
        self.assertIsNotNone(self.gate())
        self.assertIsNone(self.gate())
        self.prompt_start(DIAGNOSIS)
        self.answer(FACTS)
        self.assertIsNotNone(self.gate(), "a new turn gets its own single request")

    def test_the_answer_of_this_turn_is_judged_not_an_earlier_one(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.answer(FACTS)
        self.gate()
        self.prompt_start(DIAGNOSIS)
        self.use_tool("Read")
        self.answer("Looked at it: nothing to report yet.")
        self.assertIsNone(self.gate())

    def test_the_final_answer_of_the_turn_is_judged_not_a_draft_before_it(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.answer(FACTS)
        self.answer("Not verified yet; I will read the plan before stating numbers.")
        self.assertIsNone(self.gate())

    def test_file_changes_get_the_review_gate_first(self) -> None:
        self.prompt_start(DIAGNOSIS)
        (self.repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
        self.answer(FACTS)
        decision = self.gate()
        assert decision is not None
        self.assertIn("review is required", decision["reason"])
        self.assertNotIn("evidence check", decision["reason"])

    def test_routing_off_means_no_check(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.answer(FACTS)
        self.run_cli("off")
        self.assertIsNone(self.gate())


ROUTINE = "fix the typo in the readme"


class TestPassClaimsAgainstTheLastCheck(EvidenceTestCase):
    def test_tests_said_to_pass_after_a_failing_check_are_sent_back_on_any_task(
        self,
    ) -> None:
        self.prompt_start(ROUTINE)
        self.record_check("python3 -m pytest -q", passed=False)
        self.answer("Fixed it. All tests pass now.")
        decision = self.gate()
        assert decision is not None
        self.assertEqual(decision["decision"], "block")
        self.assertIn("last check command in this turn failed", decision["reason"])
        self.assertNotIn("critical work", decision["reason"])
        self.assertIsNone(self.gate(), "once")

    def test_the_same_answer_after_a_passing_check_is_fine(self) -> None:
        self.prompt_start(ROUTINE)
        self.record_check("python3 -m pytest -q", passed=True)
        self.answer("Fixed it. All tests pass now.")
        self.assertIsNone(self.gate())

    def test_saying_honestly_that_it_fails_is_fine(self) -> None:
        self.prompt_start(ROUTINE)
        self.record_check("python3 -m pytest -q", passed=False)
        self.answer("Not done: one test still fails, 1 failed out of 12.")
        self.assertIsNone(self.gate())

    def test_wishes_instructions_and_conditions_are_not_claims(self) -> None:
        for text in (
            "Run the tests to make sure they pass.",
            "The MR is ready to open after the tests pass.",
            "I need the build to succeed before I can merge.",
        ):
            with self.subTest(text=text):
                self.new_prompt("reset")
                self.prompt_start(ROUTINE)
                self.record_check("make check", passed=False)
                self.answer(text)
                self.assertIsNone(self.gate())

    def test_it_is_the_last_check_that_counts(self) -> None:
        self.prompt_start(ROUTINE)
        self.record_check("npm test", passed=False)
        self.record_check("npm test", passed=True)
        self.answer("All tests pass.")
        self.assertIsNone(self.gate(), "failed first, then fixed")
        self.new_prompt("reset")
        self.prompt_start(ROUTINE)
        self.record_check("npm test", passed=True)
        self.record_check("npm test", passed=False)
        self.answer("All tests pass.")
        self.assertIsNotNone(self.gate(), "passed first, then broke")

    def test_both_problems_are_asked_for_in_one_message(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.record_check("pytest", passed=False)
        self.entries = [
            e
            for e in self.entries
            if not (
                e.get("type") == "assistant"
                and any(b.get("type") == "tool_use" for b in e["message"]["content"])
            )
        ]
        self.answer(FACTS + " All tests pass.")
        decision = self.gate()
        assert decision is not None
        self.assertEqual(decision["reason"].count("evidence check"), 1)


class TestUnprovenClaimNotice(EvidenceTestCase):
    def test_claiming_verification_with_no_tool_gets_a_notice_not_a_block(self) -> None:
        self.prompt_start(ROUTINE)
        self.answer("I verified that the config is correct.")
        decision = self.gate()
        assert decision is not None
        self.assertNotIn("decision", decision)
        self.assertIn("no tool ran in this turn", decision["systemMessage"])
        self.assertIsNone(self.gate(), "said once")

    def test_a_tool_call_in_the_turn_removes_the_notice(self) -> None:
        self.prompt_start(ROUTINE)
        self.use_tool("Read")
        self.answer("I verified that the config is correct.")
        self.assertIsNone(self.gate())

    def test_summaries_of_earlier_work_and_plain_answers_get_no_notice(self) -> None:
        for text in (
            "Earlier I verified the config and the tests passed.",
            "As above, all checks pass.",
            "The config looks right to me.",
            "Use `make check`; the tests should pass afterwards.",
        ):
            with self.subTest(text=text):
                self.new_prompt("reset")
                self.prompt_start(ROUTINE)
                self.answer(text)
                self.assertIsNone(self.gate())

    def test_the_notice_resets_with_the_next_message(self) -> None:
        self.prompt_start(ROUTINE)
        self.answer("I checked the settings.")
        self.assertIsNotNone(self.gate())
        self.prompt_start(ROUTINE)
        self.answer("I checked the settings.")
        self.assertIsNotNone(self.gate())

    def test_nothing_of_the_answer_is_logged_or_saved(self) -> None:
        self.prompt_start(ROUTINE)
        self.answer("I verified the ZEBRA-77 invoice setting.")
        self.gate()
        self.assertIn("claim_notice", self.log())
        self.assertNotIn("ZEBRA", self.log())
        self.assertNotIn("ZEBRA", self.review_file().read_text())

    def test_a_message_that_was_already_sent_back_gets_no_extra_notice(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.answer(FACTS + " I verified this.")
        first = self.gate()
        assert first is not None
        self.assertEqual(first["decision"], "block")
        self.assertIsNone(self.gate(), "neither another block nor a notice")

    def test_the_shipped_standards_demand_an_honest_account_of_what_was_verified(
        self,
    ) -> None:
        standards = (ROOT / "src" / "standards" / "standards.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("what you verified and how", standards)
        self.assertIn("did not verify", standards)
        self.assertLess(
            len(standards), 1500, "added to every conversation: keep it short"
        )

    def test_routing_off_means_no_notice(self) -> None:
        self.prompt_start(ROUTINE)
        self.answer("I verified that the config is correct.")
        self.run_cli("off")
        self.assertIsNone(self.gate())


class TestClaimWording(EvidenceTestCase):
    def test_what_counts_as_a_claim(self) -> None:
        module = load_script(PROFILE_CLI, self.home, "model_profile_claims")
        claims = (
            "All tests pass.",
            "The build succeeded and there are no errors.",
            "0 failed, 12 passed",
            "Tests are green now.",
            "If you want details, ask me. All tests pass.",
        )
        not_claims = (
            "Run the tests to make sure they pass.",
            "If the build succeeds, merge it.",
            "You need the checks to pass first.",
            "One test fails.",
            "Passwords must be hashed.",
        )
        for text in claims:
            with self.subTest(claim=text):
                self.assertTrue(module.asserts_pass(text))
        for text in not_claims:
            with self.subTest(not_claim=text):
                self.assertFalse(module.asserts_pass(text))
        for text in (
            "I verified that it works.",
            "I've checked the logs.",
            "Confirmed against the staging data.",
        ):
            with self.subTest(verification=text):
                self.assertTrue(module.asserts_verification(text))
        self.assertFalse(module.asserts_verification("Earlier I verified the logs."))


class TestEvidenceSafety(EvidenceTestCase):
    def test_a_broken_or_hostile_state_file_fails_open(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.answer(FACTS)
        for content in (
            "{not json",
            json.dumps({"turn_level": ["critical"], "evidence_blocks": 0}),
            json.dumps({"turn_level": "critical", "evidence_blocks": True}),
            json.dumps({"turn_level": "critical", "evidence_blocks": "0"}),
            json.dumps({"turn_level": "critical", "evidence_blocks": 99}),
            json.dumps([1, 2]),
        ):
            with self.subTest(content=content):
                self.review_file().write_text(content, encoding="utf-8")
                self.assertIsNone(self.gate())

    def test_a_missing_or_foreign_transcript_fails_open(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.answer(FACTS)
        for transcript in (
            self.home / "elsewhere.jsonl",
            self.transcript.with_name("gone.jsonl"),
        ):
            with self.subTest(transcript=transcript.name):
                result = self.hook(
                    "review-gate", self.payload(transcript_path=str(transcript))
                )
                self.assertEqual(result.returncode, EXIT_OK, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_the_log_records_the_request_but_never_the_answer(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.answer(FACTS)
        self.gate()
        log = self.log()
        self.assertIn("evidence_requested", log)
        self.assertNotIn("500,249", log)
        self.assertNotIn("subscriptions", log)
        self.assertNotIn("db access", log)

    def test_the_answer_is_never_saved_in_the_state(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.answer(FACTS)
        self.gate()
        saved = self.review_file().read_text()
        self.assertNotIn("500,249", saved)
        self.assertIn('"evidence_blocks": 1', saved.replace('":1', '": 1'))


class TestTranscriptTail(EvidenceTestCase):
    def module(self):  # type: ignore[no-untyped-def]
        return load_script(PROFILE_CLI, self.home, "model_profile_tail")

    def filler(self, megabytes: int) -> str:
        line = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "x" * 900}]},
            }
        )
        return (line + "\n") * (megabytes * 1024 * 1024 // (len(line) + 1))

    def test_a_huge_transcript_is_read_in_a_blink(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.answer(FACTS)
        tail = self.transcript.read_text(encoding="utf-8")
        self.transcript.write_text(
            self.filler(HUGE_TRANSCRIPT_MB) + tail, encoding="utf-8"
        )
        started = time.monotonic()
        decision = self.gate()
        self.assertLess(time.monotonic() - started, READ_SECONDS_LIMIT)
        assert decision is not None
        self.assertIn("evidence check", decision["reason"])

    def test_only_the_needed_tail_is_parsed_not_the_whole_file(self) -> None:
        module = self.module()
        self.prompt_start(DIAGNOSIS)
        self.answer(FACTS)
        tail = self.transcript.read_text(encoding="utf-8")
        filler = self.filler(HUGE_TRANSCRIPT_MB)
        self.transcript.write_text(filler + tail, encoding="utf-8")
        entries = module.read_turn_entries(self.transcript)
        whole_file_lines = filler.count("\n")
        self.assertLess(len(entries), whole_file_lines // 5)
        self.assertTrue(any(module._is_real_prompt(e) for e in entries))

    def test_the_window_grows_until_it_finds_the_prompt(self) -> None:
        module = self.module()
        module.TURN_TAIL_START_BYTES = 512
        self.prompt_start(DIAGNOSIS)
        for _ in range(60):
            self.answer("y" * 300)
        entries = module.read_turn_entries(self.transcript)
        positions = [i for i, e in enumerate(entries) if module._is_real_prompt(e)]
        self.assertTrue(positions)
        self.assertEqual(entries[positions[-1]]["message"]["content"], DIAGNOSIS)
        self.assertEqual(
            len(entries) - positions[-1] - 1, 60, "every entry of the turn"
        )

    def test_a_turn_longer_than_the_first_window_still_sees_its_edits(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.record_edit(str(self.repo / "app.py"))
        for _ in range(40):
            self.answer("z" * 2000)
        module = self.module()
        module.TURN_TAIL_START_BYTES = 1024
        activity = module.turn_activity(str(self.transcript))
        self.assertEqual(activity.edited_files, [str(self.repo / "app.py")])

    def test_a_cut_first_line_never_breaks_parsing(self) -> None:
        module = self.module()
        self.prompt_start(DIAGNOSIS)
        for size in range(200, 1400, 37):
            module.TURN_TAIL_START_BYTES = size
            entries = module.read_turn_entries(self.transcript)
            self.assertTrue(any(module._is_real_prompt(e) for e in entries), size)

    def test_a_transcript_without_any_prompt_is_read_up_to_the_cap(self) -> None:
        module = self.module()
        module.TURN_TAIL_START_BYTES = 256
        module.TURN_TAIL_MAX_BYTES = 4096
        self.transcript.write_text(self.filler(1), encoding="utf-8")
        entries = module.read_turn_entries(self.transcript)
        self.assertLess(len(entries), 10, "stopped at the cap instead of reading 1 MB")
        self.assertGreater(len(entries), 0)

    def test_the_cap_is_never_exceeded_even_when_it_is_not_a_power_of_the_growth(
        self,
    ) -> None:
        module = self.module()
        module.TURN_TAIL_START_BYTES = 256
        module.TURN_TAIL_MAX_BYTES = 3000
        self.transcript.write_text(self.filler(1), encoding="utf-8")
        entries = module.read_turn_entries(self.transcript)
        parsed_bytes = sum(
            len(json.dumps(e, separators=(",", ":"))) + 1 for e in entries
        )
        self.assertLessEqual(parsed_bytes, 3000)
        self.assertGreater(len(entries), 0)

    def test_lines_cut_at_every_step_are_put_back_together_exactly(self) -> None:
        module = self.module()
        self.prompt_start(DIAGNOSIS)
        for i in range(40):
            self.answer(f"answer {i} " + "w" * (37 * i % 300))
        whole = [json.loads(line) for line in self.transcript.read_text().splitlines()]
        last_prompt = max(i for i, e in enumerate(whole) if module._is_real_prompt(e))
        for start in (64, 100, 333, 1000):
            with self.subTest(start=start):
                module.TURN_TAIL_START_BYTES = start
                module.TURN_TAIL_GROWTH = 2
                entries = module.read_turn_entries(self.transcript)
                tail_prompt = max(
                    i for i, e in enumerate(entries) if module._is_real_prompt(e)
                )
                self.assertEqual(entries[tail_prompt:], whole[last_prompt:])

    def test_the_stop_hook_parses_the_transcript_once(self) -> None:
        module = self.module()
        self.prompt_start(DIAGNOSIS)
        self.answer(FACTS)
        calls = []
        original = module.turn_activity

        def counting(path: object):  # type: ignore[no-untyped-def]
            calls.append(path)
            return original(path)

        module.turn_activity = counting
        module.read_hook_payload = lambda: self.payload(stop_hook_active=False)
        module.run_review_gate()
        self.assertEqual(len(calls), 1)

    def test_odd_entries_in_the_transcript_never_break_the_check(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.entries.append({"type": "user", "message": "hi"})
        self.entries.append({"type": "assistant", "message": ["not", "a", "dict"]})
        self.answer(FACTS)
        decision = self.gate()
        assert decision is not None, "still judged, not crashed"
        self.assertIn("evidence check", decision["reason"])

    def test_a_negative_counter_in_the_state_file_is_refused(self) -> None:
        self.prompt_start(DIAGNOSIS)
        self.answer(FACTS)
        state = json.loads(self.review_file().read_text())
        self.review_file().write_text(
            json.dumps({**state, "evidence_blocks": -3}), encoding="utf-8"
        )
        self.assertIsNone(self.gate())

    def test_a_failed_snapshot_still_resets_the_message_state(self) -> None:
        module = self.module()
        self.review_file().parent.mkdir(parents=True, exist_ok=True)
        self.review_file().write_text(
            json.dumps(
                {"turn_level": "critical", "evidence_blocks": 1, "claim_notice": True}
            ),
            encoding="utf-8",
        )

        def broken(_cwd: str) -> None:
            raise OSError("disk went away")

        module.tree_fingerprint = broken
        module.take_snapshot(
            {"session_id": SESSION, "cwd": str(self.repo)}, None, "routine"
        )
        saved = json.loads(self.review_file().read_text())
        self.assertEqual(saved["turn_level"], "routine")
        self.assertEqual(saved["evidence_blocks"], 0)
        self.assertNotIn("claim_notice", saved)
        self.assertIsNone(saved["fingerprint"])

    def test_an_empty_transcript_is_fine(self) -> None:
        module = self.module()
        self.transcript.write_text("", encoding="utf-8")
        self.assertEqual(module.read_turn_entries(self.transcript), [])
        self.assertEqual(module.turn_activity(str(self.transcript)).tool_calls, 0)

"""Tests for the dynamic effort ladder: prompt triage, the prompt-start hook and the skills."""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from loader import load_script
from paths import AGENTS_DIR, PROFILE_CLI
from test_model_profile import (
    PROMPT_START_COMMAND,
    REVIEW_GATE_COMMAND,
    SNAPSHOT_COMMAND,
    SYNC_COMMAND,
    USER_HOOKS,
    SandboxTestCase,
)

EXIT_OK = 0
SKILLS_SOURCE = AGENTS_DIR.parent / "skills"
SKILL_NAMES = ["critical-work", "deep-work"]
MAX_SKILL_WORDS = 200
PROMPT_START_SECONDS_LIMIT = 1.5


class TestClassifier(SandboxTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.module = load_script(PROFILE_CLI, self.home, "model_profile_triage")

    def level(self, text: str) -> str:
        return self.module.classify_prompt(text)[0]

    def test_questions_and_small_asks_stay_routine_and_cheap(self) -> None:
        for text in (
            "what does this function return?",
            "explain how auth works in this repo",
            "is billing calculated monthly?",
            "rename the variable x to count",
            "how much token are we using today",
            "",
            "   ",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.level(text), "routine")

    def test_a_short_question_about_a_hard_topic_stays_routine(self) -> None:
        self.assertEqual(self.level("why is the CI flaky?"), "routine")
        self.assertEqual(self.level("what is a regression?"), "routine")

    def test_a_long_design_discussion_is_complex_even_without_a_verb(self) -> None:
        text = "thinking about the architecture of the reporting module " * 5
        self.assertEqual(self.level(text), "complex")

    def test_real_engineering_work_is_complex(self) -> None:
        for text in (
            "refactor the report builder into smaller functions",
            "debug why the parser test is failing",
            "implement CSV export for the usage report",
            "optimise the search query performance",
            "update app.py, utils.py and views.py to use the new client",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.level(text), "complex")

    def test_costly_mistakes_are_critical(self) -> None:
        for text in (
            "fix the stripe webhook race condition",
            "add password reset with rate limiting to the login flow",
            "write a migration to backfill invoices",
            "harden the API against injection - security review needed",
            "debug the production outage in payments",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.level(text), "critical")

    def test_being_stuck_escalates_to_critical(self) -> None:
        self.assertEqual(
            self.level("the export test is still failing, fix it"), "critical"
        )
        self.assertIn(
            "repeated failure",
            self.module.classify_prompt("same error again, debug it")[1],
        )

    def test_reasons_are_keywords_only_never_prompt_text(self) -> None:
        level, reasons = self.module.classify_prompt(
            "fix the billing bug for customer ACME-42"
        )
        self.assertEqual(level, "critical")
        self.assertEqual(reasons, ["billing"])
        self.assertNotIn("ACME", " ".join(reasons))

    def hint(self, text: str) -> str | None:
        return self.module.triage_hint(*self.module.classify_prompt(text))

    def test_hint_wording_names_the_right_skill(self) -> None:
        self.assertIsNone(self.hint("what is this?"))
        self.assertIn("deep-work skill", self.hint("refactor the parser module"))
        self.assertIn("critical-work skill", self.hint("fix the payment refund flow"))


# The investigation that went wrong on 2026-11-25: a production database slowdown, analysed
# on Sonnet at medium effort because every short follow-up looked routine on its own.
# (minutes since the first message, the real wording). The 35-minute gap before the
# "500,249" question is real: it is what an earlier 30-minute memory missed.
INCIDENT_MESSAGES = (
    (0, "this query in which service has been used first tell me"),
    (
        9,
        "how can i improve this so that its become faster just tell me first dont start changing",
    ),
    (42, "you have db access so just read and find the solution"),
    (47, "code was correct you mean?"),
    (68, "then what should i do then?"),
    (
        69,
        "you do check the prod db access you have just check and tell me you know the read only right",
    ),
    (73, "then first dev can be fixed right"),
    (74, "on which db ?"),
    (76, "feedbacks on this dont any exisitng index?"),
    (111, "500,249 this data in which table have?"),
    (
        113,
        "500,249 rows examine that is my point from which tbales and in which enviroment?",
    ),
    (121, "what would be the fix?"),
    (122, "the table you want to add the index what indexes are already have?"),
    (125, "so now what i needs to be done tell me that"),
    (129, "when this query does call on which api"),
)
INCIDENT_PROMPTS = tuple(text for _, text in INCIDENT_MESSAGES)
INCIDENT_LEVELS = [
    "routine",  # a plain lookup is not escalated
    "complex",  # wants it faster
    "critical",  # read the database and find the solution
    "complex",  # "code was correct you mean?": generic follow-up, one level lower
    "complex",  # "then what should i do then?"
    "critical",  # check the prod db
    "complex",  # "then first dev can be fixed right": not a question, one level lower
    "critical",  # "on which db ?"
    "critical",  # the index question
    "critical",  # "500,249 this data in which table" - the answer that was wrong
    "critical",  # "500,249 rows examine ..."
    "critical",  # "what would be the fix?"
    "critical",  # the indexes question
    "routine",  # "so now what i needs to be done": the memory is used up by now
    "routine",  # a plain lookup again
]
MINUTE = 60.0


class TestInvestigationLanguage(SandboxTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.module = load_script(PROFILE_CLI, self.home, "model_profile_incident")

    def level(self, text: str) -> str:
        return self.module.classify_prompt(text)[0]

    def test_reading_evidence_about_a_database_or_production_is_critical(self) -> None:
        for text in (
            "you have db access so just read and find the solution",
            "why is the mongo query so slow, check the explain plan",
            "500,249 keys examined on the subscriptions collection, find the cause",
            "the prod api is slow, profile it",
            "you do check the prod db access you have just check",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.level(text), "critical")

    def test_wanting_something_faster_is_at_least_complex(self) -> None:
        for text in (
            "how can i improve this so that its become faster",
            "why is the build so slow?",
            "find out why the page takes forever, figure out why",
        ):
            with self.subTest(text=text):
                self.assertIn(self.level(text), ("complex", "critical"))

    def test_plain_lookups_and_explanations_stay_cheap(self) -> None:
        for text in (
            "explain how auth works in this repo",
            "this query in which service has been used first tell me",
            "what does the users table store?",
            "where is the database url configured?",
            "check the git status",
            "rename the table class",
            "come on man model profile is on",
            "100% confirm me",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.level(text), "routine")

    def test_the_skill_descriptions_cover_investigation_questions(self) -> None:
        text = (SKILLS_SOURCE / "critical-work" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("even if the message is only a question", text)
        self.assertIn("query plans", text)


class TestFollowUps(SandboxTestCase):
    DIAGNOSIS = "you have db access so just read and find the solution"

    def setUp(self) -> None:
        super().setUp()
        self.module = load_script(PROFILE_CLI, self.home, "model_profile_followups")
        self.now = 1_000_000.0

    def ask(self, text: str, state: object, minutes: float = 2) -> tuple:
        self.now += minutes * MINUTE
        return self.module.decide_triage(text, state, self.now)

    def conversation(self, prompts: tuple[str, ...]) -> list[str]:
        state: object = None
        levels = []
        for text in prompts:
            level, _, state = self.ask(text, state)
            levels.append(level)
        return levels

    def test_the_incident_with_its_real_timing_keeps_its_stakes_from_the_diagnosis_on(
        self,
    ) -> None:
        state: object = None
        levels = []
        for minute, text in INCIDENT_MESSAGES:
            level, _, state = self.module.decide_triage(
                text, state, self.now + minute * MINUTE
            )
            levels.append(level)
        self.assertEqual(levels, INCIDENT_LEVELS)

    def test_the_same_conversation_typed_rapidly_gives_the_same_levels(self) -> None:
        self.assertEqual(self.conversation(INCIDENT_PROMPTS), INCIDENT_LEVELS)

    def test_a_follow_up_on_the_same_subject_says_what_it_inherited_from(self) -> None:
        _, _, state = self.ask(self.DIAGNOSIS, None)
        level, reasons, _ = self.ask("on which db ?", state)
        self.assertEqual(level, "critical")
        self.assertEqual(reasons[0], "follow-up to critical work")
        self.assertIn("db", reasons)

    def test_a_generic_follow_up_drops_one_level_and_burns_the_memory_faster(
        self,
    ) -> None:
        _, _, state = self.ask(self.DIAGNOSIS, None)
        level, _, after_generic = self.ask("code was correct you mean?", state)
        self.assertEqual(level, "complex")
        self.assertEqual(
            after_generic["left"], state["left"] - self.module.OFF_TOPIC_FOLLOWUP_COST
        )
        _, _, after_topical = self.ask("and the index on that table?", state)
        self.assertEqual(after_topical["left"], state["left"] - 1)
        self.assertEqual(self.ask("what about the index?", state)[0], "critical")

    def test_a_question_about_the_fix_keeps_the_level_a_new_instruction_does_not(
        self,
    ) -> None:
        _, _, state = self.ask(self.DIAGNOSIS, None)
        for text, expected in (
            ("what would be the fix?", "critical"),
            ("why is that?", "critical"),
            ("How do we roll this back", "critical"),
            ("fix the typo in the readme", "complex"),
            ("add a test for it", "complex"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.ask(text, state)[0], expected)

    def test_complex_work_is_not_inherited_by_generic_follow_ups(self) -> None:
        _, _, state = self.ask("how can i improve this so that its become faster", None)
        self.assertEqual(self.ask("then what should i do then?", state)[0], "routine")
        self.assertEqual(self.ask("and the latency after that?", state)[0], "complex")

    def test_big_numbers_count_as_evidence_but_small_ones_do_not(self) -> None:
        _, _, state = self.ask(self.DIAGNOSIS, None)
        self.assertEqual(
            self.ask("500,249 comes from where exactly?", state)[0], "critical"
        )
        self.assertEqual(self.ask("100% confirm me about that", state)[0], "complex")

    def test_closing_phrases_and_tiny_messages_do_not_inherit(self) -> None:
        _, _, state = self.ask("fix the stripe webhook race condition", None)
        for text in (
            "ok",
            "thanks!",
            "Yes",
            "go on",
            "done",
            "no",
            "why?",
            "thank you!!!!",
            "understood!!!!!",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.ask(text, state)[0], "routine")

    def test_the_memory_expires_after_a_quiet_spell(self) -> None:
        _, _, state = self.ask("fix the stripe webhook race condition", None)
        self.assertEqual(
            self.ask("and what about the db?", state, minutes=119)[0], "critical"
        )
        self.assertEqual(
            self.ask("and what about the db?", state, minutes=121)[0], "routine"
        )

    def test_the_memory_runs_out_after_a_number_of_follow_ups(self) -> None:
        _, _, state = self.ask("fix the stripe webhook race condition", None)
        levels = []
        for _ in range(self.module.FOLLOWUP_PROMPTS + 2):
            level, _, state = self.ask("and what about the other db then?", state)
            levels.append(level)
        self.assertEqual(
            levels[: self.module.FOLLOWUP_PROMPTS],
            ["critical"] * self.module.FOLLOWUP_PROMPTS,
        )
        self.assertEqual(levels[self.module.FOLLOWUP_PROMPTS :], ["routine", "routine"])

    def test_a_milder_message_never_downgrades_the_conversation(self) -> None:
        _, _, state = self.ask("how can i improve this so that its become faster", None)
        level, _, state = self.ask(self.DIAGNOSIS, state)
        self.assertEqual(level, "critical")
        level, _, saved = self.ask("how can i make this faster", state)
        self.assertEqual(level, "critical")
        self.assertEqual(saved["level"], "critical")

    def test_a_routine_conversation_is_never_escalated(self) -> None:
        self.assertEqual(
            set(
                self.conversation(
                    (
                        "what does this function return?",
                        "and the other one?",
                        "ok then explain it again",
                    )
                )
            ),
            {"routine"},
        )

    def test_a_damaged_or_hostile_state_is_ignored(self) -> None:
        for state in (
            "critical",
            {"level": "critical"},
            {"level": "critical", "at": "now", "left": 3, "reasons": []},
            {"level": "critical", "at": self.now, "left": 0, "reasons": []},
            {"level": "critical", "at": self.now, "left": True, "reasons": []},
            {"level": "ultra", "at": self.now, "left": 3, "reasons": []},
            {"level": "critical", "at": self.now + 99999, "left": 3, "reasons": []},
            {"level": "critical", "at": self.now, "left": 3, "reasons": "db"},
        ):
            with self.subTest(state=state):
                self.assertEqual(
                    self.ask("and what about the db?", state, minutes=0)[0], "routine"
                )

    def test_saved_keywords_are_sanitised(self) -> None:
        state = {
            "level": "critical",
            "at": self.now,
            "left": 3,
            "reasons": ["db", "<script>alert(1)</script>", 5, "x" * 200],
        }
        _, reasons, _ = self.ask("and what about the db?", state, minutes=0)
        self.assertEqual(reasons, ["follow-up to critical work", "db"])


class PromptStartTestCase(SandboxTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.run_cli("on")
        self.repo = self.home / "work"
        self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)

    def prompt_start(self, prompt: str, cwd: Path | None = None) -> dict:
        result = subprocess.run(
            [sys.executable, "-I", str(PROFILE_CLI), "prompt-start"],
            input=json.dumps(
                {
                    "session_id": "s-1",
                    "cwd": str(cwd or self.repo),
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": prompt,
                }
            ),
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else {}


class TestPromptStartHook(PromptStartTestCase):
    def test_a_routine_prompt_adds_nothing(self) -> None:
        self.assertEqual(self.prompt_start("what does main.py do?"), {})

    def test_a_complex_prompt_gets_a_deep_work_hint(self) -> None:
        output = self.prompt_start("refactor the report module and add tests")
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "UserPromptSubmit")
        self.assertIn("deep-work skill", specific["additionalContext"])

    def test_a_critical_prompt_gets_a_critical_work_hint(self) -> None:
        output = self.prompt_start("fix the stripe billing webhook")
        self.assertIn(
            "critical-work skill", output["hookSpecificOutput"]["additionalContext"]
        )

    def test_it_also_takes_the_review_gate_snapshot(self) -> None:
        self.prompt_start("anything")
        state = self.claude_dir / "model-profile" / "review" / "s-1.json"
        self.assertTrue(state.exists())
        self.assertEqual(json.loads(state.read_text())["blocks"], 0)

    def test_it_also_switches_routing_off_when_you_picked_another_model(self) -> None:
        self.write_settings({**self.read_settings(), "model": "opus"})
        output = self.prompt_start("refactor everything")
        self.assertIn("Routing switched OFF", output["systemMessage"])
        self.assertNotIn(
            "hookSpecificOutput", output, "no routing hint once routing is off"
        )

    def test_nothing_happens_while_routing_is_off(self) -> None:
        self.run_cli("off")
        self.assertEqual(self.prompt_start("refactor the parser"), {})

    def test_prompt_text_is_never_logged(self) -> None:
        secret = "customer card 4242-4242-4242-4242 billing refactor"
        self.prompt_start(secret)
        log = (self.claude_dir / "model-profile" / "sync.log").read_text()
        self.assertNotIn("4242", log)
        self.assertIn('"triage": "critical"', log)

    def test_one_process_per_prompt_is_fast(self) -> None:
        for i in range(50):
            (self.repo / f"f{i}.py").write_text("x = 1\n", encoding="utf-8")
        started = time.monotonic()
        self.prompt_start("refactor the module")
        self.assertLess(time.monotonic() - started, PROMPT_START_SECONDS_LIMIT)


class TestIncidentThroughTheRealHook(PromptStartTestCase):
    def test_every_message_of_the_incident_gets_the_right_hint_across_separate_processes(
        self,
    ) -> None:
        hints = []
        for text in INCIDENT_PROMPTS:
            output = self.prompt_start(text)
            context = output.get("hookSpecificOutput", {}).get("additionalContext", "")
            hints.append(
                "critical-work"
                if "critical-work skill" in context
                else "deep-work"
                if "deep-work skill" in context
                else "none"
            )
        expected = {
            "routine": "none",
            "complex": "deep-work",
            "critical": "critical-work",
        }
        self.assertEqual(hints, [expected[level] for level in INCIDENT_LEVELS])

    def test_a_follow_up_hint_names_its_origin_and_closing_words_get_none(self) -> None:
        self.prompt_start("you have db access so just read and find the solution")
        follow = self.prompt_start("on which db ?")
        self.assertIn(
            "follow-up to critical work",
            follow["hookSpecificOutput"]["additionalContext"],
        )
        self.assertEqual(self.prompt_start("thanks"), {})

    def test_the_conversation_memory_is_per_session(self) -> None:
        self.prompt_start("you have db access so just read and find the solution")
        other = subprocess.run(
            [sys.executable, "-I", str(PROFILE_CLI), "prompt-start"],
            input=json.dumps(
                {
                    "session_id": "another",
                    "cwd": str(self.repo),
                    "prompt": "on which db ?",
                }
            ),
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(other.stdout.strip(), "")

    def test_only_keywords_are_saved_never_the_message(self) -> None:
        secret = "you have db access so just read and find the solution for customer ZEBRA-77"
        self.prompt_start(secret)
        saved = (self.claude_dir / "model-profile" / "review" / "s-1.json").read_text()
        self.assertNotIn("ZEBRA", saved)
        self.assertNotIn("customer", saved)
        self.assertIn('"critical"', saved)


class TestHookUpgrade(SandboxTestCase):
    def our_commands(self, event: str) -> list[str]:
        return [
            hook["command"]
            for group in self.read_settings().get("hooks", {}).get(event, [])
            for hook in group["hooks"]
            if "model-profile" in hook["command"]
        ]

    def test_a_1_5_0_install_is_upgraded_to_one_hook_per_prompt(self) -> None:
        self.write_settings({**self.read_settings(), "hooks": USER_HOOKS})
        self.run_cli("on")
        settings = self.read_settings()
        # rewrite the prompt hooks the way 1.5.0 registered them
        old_groups = [
            {"hooks": [{"type": "command", "command": c, "timeout": 10}]}
            for c in (SYNC_COMMAND, SNAPSHOT_COMMAND)
        ]
        prompt_groups = [
            g
            for g in settings["hooks"]["UserPromptSubmit"]
            if "model-profile" not in json.dumps(g)
        ]
        settings["hooks"]["UserPromptSubmit"] = prompt_groups + old_groups
        self.write_settings(settings)
        self.assertIn("inactive", self.run_cli("status").stdout)
        self.run_cli("repair")
        self.assertEqual(self.our_commands("UserPromptSubmit"), [PROMPT_START_COMMAND])
        self.assertEqual(self.our_commands("Stop"), [REVIEW_GATE_COMMAND])
        self.assertIn(SYNC_COMMAND, self.our_commands("PostModelSwitch"))
        user_prompt_hooks = [
            h["command"]
            for g in self.read_settings()["hooks"]["UserPromptSubmit"]
            for h in g["hooks"]
        ]
        self.assertIn("echo mine", user_prompt_hooks)
        self.assertIn("active", self.run_cli("status").stdout)


class TestSkillsInstall(SandboxTestCase):
    def setUp(self) -> None:
        super().setUp()
        source = self.claude_dir / "model-profile" / "skills"
        for name in SKILL_NAMES:
            (source / name).mkdir(parents=True)
            (source / name / "SKILL.md").write_bytes(
                (SKILLS_SOURCE / name / "SKILL.md").read_bytes()
            )
        self.skills = self.claude_dir / "skills"

    def test_on_installs_the_skills_and_off_removes_them(self) -> None:
        self.assertEqual(self.run_cli("on").returncode, EXIT_OK)
        for name in SKILL_NAMES:
            installed = self.skills / name / "SKILL.md"
            self.assertEqual(
                installed.read_bytes(), (SKILLS_SOURCE / name / "SKILL.md").read_bytes()
            )
        self.run_cli("off")
        self.assertFalse(
            self.skills.exists(), "a skills folder we created is removed again"
        )

    def test_your_own_skills_are_untouched(self) -> None:
        mine = self.skills / "my-skill"
        mine.mkdir(parents=True)
        (mine / "SKILL.md").write_text("mine", encoding="utf-8")
        self.run_cli("on")
        self.run_cli("off")
        self.assertEqual((mine / "SKILL.md").read_text(), "mine")
        self.assertTrue(self.skills.is_dir())

    def test_a_skill_with_the_same_name_is_never_overwritten(self) -> None:
        clash = self.skills / "deep-work"
        clash.mkdir(parents=True)
        (clash / "SKILL.md").write_text("someone else's", encoding="utf-8")
        result = self.run_cli("on")
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to overwrite", result.stderr)
        self.assertEqual((clash / "SKILL.md").read_text(), "someone else's")
        self.assertFalse(self.state_path.exists())
        self.assertFalse((self.skills / "critical-work").exists(), "all-or-nothing")

    def test_an_edited_skill_is_kept_on_off(self) -> None:
        self.run_cli("on")
        edited = self.skills / "deep-work" / "SKILL.md"
        edited.write_text("my tuned version", encoding="utf-8")
        result = self.run_cli("off")
        self.assertIn("left in place", result.stderr)
        self.assertEqual(edited.read_text(), "my tuned version")

    def test_a_state_file_from_before_skills_still_switches_off(self) -> None:
        self.run_cli("on")
        state = json.loads(self.state_path.read_text())
        for key in ("installed_skills", "created_skills_dir"):
            state.pop(key)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(self.run_cli("off").returncode, EXIT_OK)

    def test_status_explains_the_effort_ladder(self) -> None:
        self.run_cli("on")
        out = self.run_cli("status").stdout
        self.assertIn("high by default", out)
        self.assertIn("deep-work", out)
        self.assertIn("critical-work skill (Opus, high)", out)


class TestShippedSkills(SandboxTestCase):
    def frontmatter(self, name: str) -> tuple[dict[str, str], str]:
        text = (SKILLS_SOURCE / name / "SKILL.md").read_text(encoding="utf-8")
        _, header, body = text.split("---", 2)
        return dict(line.split(": ", 1) for line in header.strip().splitlines()), body

    def test_deep_work_raises_effort_but_keeps_the_cheaper_model(self) -> None:
        meta, _ = self.frontmatter("deep-work")
        self.assertEqual(meta["effort"], "high")
        self.assertNotIn(
            "model", meta, "deep-work must not switch to the expensive model"
        )

    def test_critical_work_switches_to_opus_at_high_effort(self) -> None:
        meta, _ = self.frontmatter("critical-work")
        self.assertEqual((meta["model"], meta["effort"]), ("opus", "high"))

    def test_both_are_model_invocable_and_say_when_not_to_use_them(self) -> None:
        for name in SKILL_NAMES:
            with self.subTest(skill=name):
                meta, body = self.frontmatter(name)
                self.assertEqual(meta["name"], name)
                self.assertNotIn("disable-model-invocation", meta)
                self.assertRegex(meta["description"], r"Do not use for")
                self.assertLessEqual(
                    len(body.split()),
                    MAX_SKILL_WORDS,
                    "skills are paid for each time they load",
                )
                self.assertTrue(re.search(r"end to end", body))

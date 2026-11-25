"""Signed automatic updates, end to end.

A local server plays GitHub's release API. Real installers are built from copies of this
repository, signed with a throwaway key, installed into a throwaway HOME, and updated by the
real commands: the `auto-update` SessionStart hook, which starts the check in the background,
and `auto-update-run`, that background check itself. An attacker key, tampered and replayed
files and a hostile server prove that only signed releases from the channel's key, at the
version they were signed as, are ever installed.
"""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from loader import load_script
from paths import ROOT

REPO = "tester/claude-model-profile"
# the release asset named in src/channel/update-channel.json
ASSET = "install-claude-model-profile.sh"
LATEST_PATH = f"/repos/{REPO}/releases/latest"
CURRENT = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
MAJOR, MINOR, PATCH = (int(part) for part in CURRENT.split("."))
NEWER = f"{MAJOR}.{MINOR + 1}.0"
NEWEST = f"{MAJOR}.{MINOR + 2}.0"
NEXT_MAJOR = f"{MAJOR + 1}.0.0"
BASELINE_SETTINGS = {"model": "sonnet", "effortLevel": "high"}
OVERSIZED_BYTES = 6 * 1024 * 1024
DEEPLY_NESTED_JSON = b"[" * 200_000
HOOK_MAX_SECONDS = 5
# the hook waits at most 1 s for the state file; 3 s leaves room for interpreter start-up
BUSY_STATE_MAX_SECONDS = 3
BACKGROUND_WAIT_SECONDS = 120
POLL_SECONDS = 0.25
EXIT_OK = 0
UPDATE_HOOK = (
    'python3 -I "$HOME/.claude/bin/model-profile" auto-update 2>/dev/null || true'
)
IGNORED = shutil.ignore_patterns(
    ".git", "dist", "__pycache__", ".mypy_cache", ".ruff_cache", "node_modules"
)


def make_key(folder: Path, name: str) -> tuple[Path, str]:
    key = folder / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)],
        check=True,
    )
    return key, (folder / f"{name}.pub").read_text(encoding="utf-8").strip()


def copy_repo(work: Path, label: str, version: str, channel: dict) -> Path:
    copy = work / f"repo-{label}"
    shutil.copytree(ROOT, copy, ignore=IGNORED)
    (copy / "VERSION").write_text(version + "\n", encoding="utf-8")
    (copy / "src" / "channel" / "update-channel.json").write_text(
        json.dumps(channel), encoding="utf-8"
    )
    return copy


def build_command(copy: Path, out: Path, key: Path) -> list:
    return [
        sys.executable,
        str(copy / "scripts" / "build_bundle.py"),
        "--output-dir",
        str(out),
        "--sign",
        str(key),
    ]


def build_installer(
    work: Path, label: str, version: str, signer: str, key: Path
) -> tuple[bytes, bytes | None]:
    """A real installer for `version` whose channel trusts `signer`, signed by `key`."""
    copy = copy_repo(
        work, label, version, {"repo": REPO, "asset": ASSET, "signer": signer}
    )
    out = work / f"out-{label}"
    subprocess.run(build_command(copy, out, key), check=True, capture_output=True)
    bundle = next(p for p in out.glob("*.sh"))
    signature = bundle.with_name(bundle.name + ".sig")
    return bundle.read_bytes(), signature.read_bytes() if signature.exists() else None


class QuietServer(ThreadingHTTPServer):
    """A client that stops reading (an oversized file) is expected, not an error to print."""

    def handle_error(self, request: object, client_address: object) -> None:
        return


class ReleaseServer:
    """Plays GitHub: the latest-release record, downloads, redirects and broken responses."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.latest: dict | None = None
        self.raw_latest: bytes | None = None
        self.redirects: dict[str, str] = {}
        self.truncated: set[str] = set()
        self.undeclared: set[str] = set()
        self.on_latest = None
        self.hits: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def do_GET(self) -> None:
                owner.hits.append(self.path)
                if self.path in owner.redirects:
                    self.send_response(302)
                    self.send_header("Location", owner.redirects[self.path])
                    self.end_headers()
                    return
                body = owner.body_for(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                if self.path not in owner.undeclared:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                cut = self.path in owner.truncated
                self.wfile.write(body[: len(body) // 2] if cut else body)

        self.server = QuietServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def body_for(self, path: str) -> bytes | None:
        if path == LATEST_PATH:
            if self.on_latest is not None:
                self.on_latest()
            if self.raw_latest is not None:
                return self.raw_latest
            return json.dumps(self.latest).encode() if self.latest else None
        name = path.removeprefix("/download/")
        return self.files.get(name) if path.startswith("/download/") else None

    def reset(self) -> None:
        self.files, self.latest, self.raw_latest = {}, None, None
        self.redirects, self.truncated, self.undeclared = {}, set(), set()
        self.on_latest = None
        self.hits.clear()

    def publish(
        self, version: str, installer: bytes | None, signature: bytes | None
    ) -> None:
        self.files = {}
        assets = []
        for name, body in ((ASSET, installer), (ASSET + ".sig", signature)):
            if body is not None:
                self.files[name] = body
                assets.append(
                    {
                        "name": name,
                        "browser_download_url": f"{self.base}/download/{name}",
                    }
                )
        self.latest = {"tag_name": f"v{version}", "assets": assets}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class AutoUpdateTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.work = Path(tempfile.mkdtemp(prefix="auto-update-build-"))
        cls.key, cls.signer = make_key(cls.work, "release")
        attacker, _ = make_key(cls.work, "attacker")
        cls.old = build_installer(cls.work, "old", CURRENT, cls.signer, cls.key)
        cls.new = build_installer(cls.work, "new", NEWER, cls.signer, cls.key)
        cls.major = build_installer(cls.work, "major", NEXT_MAJOR, cls.signer, cls.key)
        cls.forged = build_installer(cls.work, "forged", NEWER, cls.signer, attacker)
        cls.releases = ReleaseServer()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.releases.close()
        shutil.rmtree(cls.work, ignore_errors=True)

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="auto-update-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.profile = self.home / ".claude" / "model-profile"
        (self.home / ".claude").mkdir()
        self.settings = self.home / ".claude" / "settings.json"
        self.settings.write_text(json.dumps(BASELINE_SETTINGS), encoding="utf-8")
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "CLAUDE_MODEL_PROFILE_UPDATE_API": self.releases.base,
        }
        self.env.pop("CLAUDE_MODEL_PROFILE_NO_AUTO_UPDATE", None)
        self.releases.reset()
        self.install(self.old[0])

    def install(self, bundle: bytes, *args: str) -> subprocess.CompletedProcess[str]:
        path = self.home / "installer.sh"
        path.write_bytes(bundle)
        result = subprocess.run(
            ["bash", str(path), *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        return result

    def cli(
        self, command: str, env: dict | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                str(self.home / ".claude" / "bin" / "model-profile"),
                command,
            ],
            env=env or self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )

    def background_check(self, env: dict | None = None) -> None:
        """The detached daily check, run in the foreground so the test can wait for it."""
        result = self.cli("auto-update-run", env)
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertEqual(result.stdout, "", "the background check never prints")
        self.assertNotIn("Traceback", result.stderr)

    def session_start(self, env: dict | None = None) -> dict:
        """The SessionStart hook: returns the notice it shows, if any."""
        result = self.cli("auto-update", env)
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def auto_update(self, env: dict | None = None) -> dict:
        """One day's check, then the next conversation start that reports it."""
        self.background_check(env)
        return self.session_start(env)

    def installed_version(self) -> str:
        return (self.profile / "VERSION").read_text().strip()

    def state(self) -> dict:
        return json.loads((self.profile / "update-state.json").read_text())

    def log(self) -> str:
        path = self.profile / "sync.log"
        return path.read_text() if path.exists() else ""

    def a_day_later(self) -> None:
        state = self.state()
        (self.profile / "update-state.json").write_text(
            json.dumps({**state, "checked_at": state["checked_at"] - 2 * 86400})
        )

    def hold_lock(self, name: str, seconds: float) -> threading.Thread:
        """Hold one of the tool's lock files from another thread, like another process."""
        ready = threading.Event()

        def hold() -> None:
            with (self.profile / name).open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                ready.set()
                time.sleep(seconds)
                fcntl.flock(handle, fcntl.LOCK_UN)

        thread = threading.Thread(target=hold)
        thread.start()
        ready.wait()
        self.addCleanup(thread.join)
        return thread

    def session_start_hooks(self) -> list[str]:
        settings = json.loads(self.settings.read_text())
        groups = settings.get("hooks", {}).get("SessionStart", [])
        return [hook["command"] for group in groups for hook in group["hooks"]]


class TestSignedUpdates(AutoUpdateTestCase):
    def test_a_newer_signed_release_is_installed_and_announced_once(self) -> None:
        self.releases.publish(NEWER, *self.new)
        output = self.auto_update()
        self.assertIn(f"updated to {NEWER}", output["systemMessage"])
        self.assertIn(f"/{REPO}/releases/tag/v{NEWER}", output["systemMessage"])
        self.assertEqual(self.installed_version(), NEWER)
        settings = json.loads(self.settings.read_text())
        self.assertEqual(settings["model"], "opusplan", "routing stays on")
        self.assertEqual(self.session_start(), {}, "said once")

    def test_session_start_never_waits_and_the_check_runs_in_the_background(
        self,
    ) -> None:
        self.releases.publish(NEWER, *self.new)
        started = time.monotonic()
        self.assertEqual(self.session_start(), {}, "nothing to report yet")
        self.assertLess(time.monotonic() - started, HOOK_MAX_SECONDS)
        waited_until = time.monotonic() + BACKGROUND_WAIT_SECONDS
        while self.installed_version() != NEWER or "pending" not in self.state():
            self.assertLess(time.monotonic(), waited_until, "background update")
            time.sleep(POLL_SECONDS)
        self.assertIn(f"updated to {NEWER}", self.session_start()["systemMessage"])

    def test_it_checks_at_most_once_a_day(self) -> None:
        self.releases.publish(NEWER, *self.new)
        self.auto_update()
        hits = len(self.releases.hits)
        self.assertEqual(self.auto_update(), {}, "nothing new to say")
        self.assertEqual(len(self.releases.hits), hits, "no network the second time")

    def test_the_same_or_an_older_release_downloads_nothing(self) -> None:
        for version in (CURRENT, "0.0.1"):
            with self.subTest(version=version):
                self.releases.hits.clear()
                self.releases.publish(version, *self.new)
                if (self.profile / "update-state.json").exists():
                    self.a_day_later()
                self.assertEqual(self.auto_update(), {})
                self.assertEqual(self.releases.hits, [LATEST_PATH])
                self.assertEqual(self.installed_version(), CURRENT)

    def test_a_major_release_is_announced_once_not_installed(self) -> None:
        self.releases.publish(NEXT_MAJOR, *self.major)
        output = self.auto_update()
        self.assertIn("major update", output["systemMessage"])
        self.assertEqual(self.installed_version(), CURRENT)
        self.a_day_later()
        self.assertEqual(self.auto_update(), {}, "said once")

    def test_a_redirect_to_an_allowed_address_is_followed(self) -> None:
        self.releases.publish(NEWER, *self.new)
        self.releases.redirects[f"/download/{ASSET}"] = (
            f"{self.releases.base}/download/file-host-copy"
        )
        self.releases.files["file-host-copy"] = self.new[0]
        self.auto_update()
        self.assertEqual(self.installed_version(), NEWER)
        self.assertIn("/download/file-host-copy", self.releases.hits)


class TestRefusals(AutoUpdateTestCase):
    def assert_refused(self, output: dict, words: str, version: str = CURRENT) -> None:
        self.assertIn("not installed", output["systemMessage"])
        self.assertIn(words, output["systemMessage"])
        self.assertEqual(self.installed_version(), version)

    def test_a_tampered_installer_is_refused_and_said_once(self) -> None:
        installer, signature = self.new
        tampered = installer.replace(b"set -euo pipefail", b"set -eu  pipefail", 1)
        self.assertNotEqual(tampered, installer)
        self.releases.publish(NEWER, tampered, signature)
        self.assert_refused(self.auto_update(), "signature did not verify")
        self.assertIn("update_signature_failed", self.log())
        self.a_day_later()
        self.assertEqual(self.auto_update(), {}, "the same problem is reported once")
        self.assertEqual(self.installed_version(), CURRENT)

    def test_each_new_refused_release_is_reported_once(self) -> None:
        tampered = self.new[0].replace(b"set -euo pipefail", b"set -eu  pipefail", 1)
        self.releases.publish(NEWER, tampered, self.new[1])
        self.assertIn(f"{NEWER} was not installed", self.auto_update()["systemMessage"])
        self.a_day_later()
        self.releases.publish(NEWEST, tampered, self.new[1])
        self.assertIn(
            f"{NEWEST} was not installed", self.auto_update()["systemMessage"]
        )

    def test_a_release_signed_by_another_key_is_refused(self) -> None:
        self.releases.publish(NEWER, *self.forged)
        self.assert_refused(self.auto_update(), "signature did not verify")

    def test_a_release_without_a_signature_is_refused(self) -> None:
        self.releases.publish(NEWER, self.new[0], None)
        self.assert_refused(self.auto_update(), "no signed installer")

    def test_an_oversized_installer_is_refused(self) -> None:
        self.releases.publish(NEWER, b"x" * OVERSIZED_BYTES, self.new[1])
        self.assert_refused(self.auto_update(), "larger than")

    def test_an_oversized_installer_without_a_declared_size_is_refused(self) -> None:
        self.releases.publish(NEWER, b"x" * OVERSIZED_BYTES, self.new[1])
        self.releases.undeclared.add(f"/download/{ASSET}")
        self.assert_refused(self.auto_update(), "larger than")

    def test_a_declared_oversize_is_refused_before_reading(self) -> None:
        self.releases.publish(NEWER, b"x" * OVERSIZED_BYTES, self.new[1])
        self.releases.truncated.add(f"/download/{ASSET}")  # sends only half
        self.assert_refused(self.auto_update(), "larger than")

    def test_a_release_api_that_is_not_a_local_test_server_is_refused(self) -> None:
        for api in (
            "http://example.com",
            "https://example.com",
            "http://localhost:1@evil.example",
            "http://127.0.0.1:1@evil.example",
        ):
            with self.subTest(api=api):
                (self.profile / "update-state.json").unlink(missing_ok=True)
                env = {**self.env, "CLAUDE_MODEL_PROFILE_UPDATE_API": api}
                self.releases.publish(NEWER, *self.new)
                self.assert_refused(
                    self.auto_update(env), "must be a test server on this machine"
                )
                self.assertEqual(self.releases.hits, [])

    def test_download_links_that_are_not_https_are_refused(self) -> None:
        for link in ("http://evil.example/x", "http://localhost:1@evil.example/x"):
            with self.subTest(link=link):
                (self.profile / "update-state.json").unlink(missing_ok=True)
                self.releases.publish(NEWER, *self.new)
                assert self.releases.latest is not None
                for asset in self.releases.latest["assets"]:
                    asset["browser_download_url"] = link
                self.assert_refused(self.auto_update(), "not https")

    def test_a_redirect_to_plain_http_is_refused(self) -> None:
        self.releases.publish(NEWER, *self.new)
        self.releases.redirects[f"/download/{ASSET}"] = "http://evil.example/x.sh"
        self.assert_refused(self.auto_update(), "redirected")
        self.assertNotIn("/x.sh", "".join(self.releases.hits))

    def test_an_old_signed_installer_under_a_newer_tag_never_runs(self) -> None:
        self.install(self.new[0], "--friendly")
        manifest = self.profile / "install-manifest.json"
        before = manifest.stat().st_mtime_ns
        self.releases.publish(NEWEST, *self.old)  # genuine signature, but it is CURRENT
        self.assert_refused(
            self.auto_update(), f"signed installer is version {CURRENT}", NEWER
        )
        self.assertEqual(manifest.stat().st_mtime_ns, before, "the installer never ran")

    def test_a_major_installer_under_a_minor_tag_never_runs(self) -> None:
        self.releases.publish(NEWER, *self.major)
        self.assert_refused(self.auto_update(), f"is version {NEXT_MAJOR}")

    def test_an_unknown_installed_version_is_not_updated_automatically(self) -> None:
        (self.profile / "VERSION").write_text("garbled", encoding="utf-8")
        self.releases.publish(NEWER, *self.new)
        output = self.auto_update()
        self.assertIn("installed version is unknown", output["systemMessage"])
        self.assertEqual(self.installed_version(), "garbled")

    def test_a_file_you_edited_stops_the_update_and_is_kept(self) -> None:
        standards = self.profile / "standards" / "standards.md"
        standards.write_text("my own rules", encoding="utf-8")
        self.releases.publish(NEWER, *self.new)
        self.assert_refused(self.auto_update(), "installer stopped")
        self.assertEqual(standards.read_text(), "my own rules")


class TestQuietFailures(AutoUpdateTestCase):
    """Offline, cut-short or garbage answers: nothing shown, logged, tried again tomorrow."""

    def assert_quiet(self, env: dict | None = None) -> None:
        self.assertEqual(self.auto_update(env), {})
        self.assertEqual(self.installed_version(), CURRENT)
        self.assertIn("checked_at", self.state())
        self.assertIn("update_failed", self.log())

    def test_offline(self) -> None:
        self.assert_quiet(
            {**self.env, "CLAUDE_MODEL_PROFILE_UPDATE_API": "http://127.0.0.1:9"}
        )

    def test_a_download_cut_short(self) -> None:
        self.releases.publish(NEWER, *self.new)
        self.releases.truncated.add(f"/download/{ASSET}")
        self.assert_quiet()

    def test_deeply_nested_release_information(self) -> None:
        self.releases.raw_latest = DEEPLY_NESTED_JSON
        self.assert_quiet()


class TestHooksAndSwitches(AutoUpdateTestCase):
    def test_the_installer_registers_the_hook_and_upgrades_add_it(self) -> None:
        self.assertIn(UPDATE_HOOK, self.session_start_hooks())
        settings = json.loads(self.settings.read_text())
        settings["hooks"]["SessionStart"] = [
            group
            for group in settings["hooks"]["SessionStart"]
            if UPDATE_HOOK not in [h["command"] for h in group["hooks"]]
        ]
        self.settings.write_text(json.dumps(settings))  # as left by 1.10.0
        self.assertIn("auto-update: inactive", self.cli("status").stdout)
        self.install(self.new[0], "--friendly")  # a manual upgrade, routing still on
        self.assertEqual(self.session_start_hooks().count(UPDATE_HOOK), 1)
        self.assertIn("auto-update: on", self.cli("status").stdout)
        self.assertIn("review gate: active", self.cli("status").stdout)

    def test_auto_update_on_registers_a_missing_hook(self) -> None:
        settings = json.loads(self.settings.read_text())
        settings["hooks"]["SessionStart"] = [
            group
            for group in settings["hooks"]["SessionStart"]
            if UPDATE_HOOK not in [h["command"] for h in group["hooks"]]
        ]
        self.settings.write_text(json.dumps(settings))
        self.cli("auto-update-on")
        self.assertEqual(self.session_start_hooks().count(UPDATE_HOOK), 1)

    def test_an_upgrade_registers_routing_hooks_that_are_missing(self) -> None:
        settings = json.loads(self.settings.read_text())
        del settings["hooks"]["Stop"]  # a hook a newer version added
        self.settings.write_text(json.dumps(settings))
        self.assertIn("review gate: inactive", self.cli("status").stdout)
        self.install(self.new[0], "--friendly")
        self.assertIn("review gate: active", self.cli("status").stdout)

    def test_an_update_waits_for_a_session_holding_the_profile_lock(self) -> None:
        self.releases.publish(NEWER, *self.new)
        self.hold_lock(".lock", 3)  # another conversation's prompt hook
        self.assertIn(f"updated to {NEWER}", self.auto_update()["systemMessage"])
        self.assertEqual(self.session_start_hooks().count(UPDATE_HOOK), 1)

    def test_files_installed_but_hooks_blocked_is_said_plainly(self) -> None:
        self.releases.publish(NEWER, *self.new)
        self.hold_lock(".lock", 30)
        message = self.auto_update()["systemMessage"]
        self.assertIn(f"{NEWER} was not installed", message)
        self.assertIn("hooks could not be registered", message)
        self.assertIn("run the installer again", message)

    def test_uninstall_waits_for_a_running_update(self) -> None:
        self.hold_lock(".update.lock", 3)
        path = self.home / "installer.sh"
        path.write_bytes(self.old[0])
        result = subprocess.run(
            ["bash", str(path), "uninstall"],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, EXIT_OK)
        self.assertIn("automatic update is being installed", result.stderr)
        self.assertTrue((self.home / ".claude" / "bin" / "model-profile").exists())

    def test_an_uninstall_during_the_check_is_not_undone(self) -> None:
        self.releases.publish(NEWER, *self.new)
        channel = self.profile / "channel" / "update-channel.json"
        manifest = self.profile / "install-manifest.json"
        before = manifest.stat().st_mtime_ns
        self.releases.on_latest = channel.unlink  # removed while the check runs
        self.background_check()
        self.assertEqual(self.installed_version(), CURRENT)
        self.assertEqual(manifest.stat().st_mtime_ns, before, "the installer never ran")
        self.assertNotIn(f"/download/{ASSET}", self.releases.hits)

    def test_a_damaged_channel_does_not_block_repair(self) -> None:
        channel = self.profile / "channel" / "update-channel.json"
        channel.write_text("{damaged")
        result = self.cli("repair")
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertIn("review gate: active", result.stdout)

    def test_a_damaged_state_file_keeps_updates_off_until_reset(self) -> None:
        self.cli("auto-update-off")
        state = self.profile / "update-state.json"
        state.write_text("{damaged")
        self.releases.publish(NEWER, *self.new)
        self.assertEqual(self.auto_update(), {})
        self.assertEqual(self.releases.hits, [])
        self.assertEqual(state.read_text(), "{damaged", "never overwritten")
        self.assertIn("update state file is damaged", self.cli("status").stdout)
        self.cli("auto-update-on")
        self.assertIn("auto-update: on", self.cli("status").stdout)

    def test_a_busy_state_file_never_holds_up_a_conversation(self) -> None:
        self.hold_lock(".update-state.lock", 8)
        started = time.monotonic()
        self.assertEqual(self.session_start(), {})
        self.assertLess(time.monotonic() - started, BUSY_STATE_MAX_SECONDS)

    def test_updates_continue_while_routing_is_off(self) -> None:
        self.assertEqual(self.cli("off").returncode, EXIT_OK)
        self.assertIn(UPDATE_HOOK, self.session_start_hooks())
        self.releases.publish(NEWER, *self.new)
        self.auto_update()
        self.assertEqual(self.installed_version(), NEWER)
        self.assertFalse((self.profile / "state.json").exists(), "routing stays off")

    def test_uninstall_removes_the_hook_and_the_update_files(self) -> None:
        self.releases.publish(NEWER, *self.new)
        self.auto_update()
        self.install(self.new[0], "uninstall")
        self.assertNotIn(UPDATE_HOOK, self.session_start_hooks())
        for name in ("update-state.json", ".update.lock", ".update-state.lock"):
            self.assertFalse((self.profile / name).exists(), name)

    def test_switched_off_means_no_network(self) -> None:
        self.releases.publish(NEWER, *self.new)
        self.assertIn("off", self.cli("auto-update-off").stdout)
        self.assertEqual(self.auto_update(), {})
        self.assertIn("auto-update: off", self.cli("status").stdout)
        env = {**self.env, "CLAUDE_MODEL_PROFILE_NO_AUTO_UPDATE": "1"}
        self.cli("auto-update-on")
        self.assertEqual(self.auto_update(env), {})
        self.assertEqual(self.releases.hits, [])
        self.assertEqual(self.installed_version(), CURRENT)

    def test_an_install_without_a_channel_never_goes_online(self) -> None:
        shutil.rmtree(self.profile / "channel")
        self.releases.publish(NEWER, *self.new)
        self.assertEqual(self.auto_update(), {})
        self.assertEqual(self.releases.hits, [])
        self.assertIn(
            "auto-update: not used by this install", self.cli("status").stdout
        )

    def test_a_damaged_channel_file_is_not_trusted(self) -> None:
        channel = self.profile / "channel" / "update-channel.json"
        good = json.loads(channel.read_text())
        self.releases.publish(NEWER, *self.new)
        for field, bad in (
            ("repo", "../../evil"),
            ("repo", "tester/claude-model-profile/../../x"),
            ("asset", "../../../bin/evil.sh"),
            ("asset", "installer.exe"),
            ("signer", "nope"),
            ("signer", "ssh-rsa AAAA"),
        ):
            with self.subTest(field=field, value=bad):
                self.releases.hits.clear()
                channel.write_text(json.dumps({**good, field: bad}))
                self.assertEqual(self.auto_update(), {})
                self.assertEqual(self.releases.hits, [])
                self.assertEqual(self.installed_version(), CURRENT)
                self.assertIn("damaged", self.cli("status").stdout)

    def test_two_sessions_starting_together_update_once(self) -> None:
        self.releases.publish(NEWER, *self.new)
        with (self.profile / ".update.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.background_check()
            fcntl.flock(handle, fcntl.LOCK_UN)
        self.assertEqual(self.installed_version(), CURRENT, "the busy one stood back")
        self.assertEqual(self.releases.hits, [])

    def test_manual_commands(self) -> None:
        self.releases.publish(NEWER, *self.new)
        self.cli("auto-update-off")
        check = self.cli("update-check")
        self.assertIn(f"{NEWER} is available", check.stdout)
        self.assertEqual(self.installed_version(), CURRENT)
        update = self.cli("update")
        self.assertEqual(update.returncode, EXIT_OK, update.stderr)
        self.assertIn(f"updated to {NEWER}", update.stdout)
        self.assertEqual(self.installed_version(), NEWER, "manual ignores the switch")

    def test_a_manual_update_offline_is_a_clear_error(self) -> None:
        env = {**self.env, "CLAUDE_MODEL_PROFILE_UPDATE_API": "http://127.0.0.1:9"}
        result = self.cli("update", env)
        self.assertNotEqual(result.returncode, EXIT_OK)
        self.assertIn("could not finish the update check", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class TrickleServer:
    """Sends a small body one byte at a time, slowly: a server stalling a download."""

    def __init__(self, size: int, pause: float) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Length", str(size))
                self.end_headers()
                for _ in range(size):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(pause)

        self.server = QuietServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class TestUpdateRules(unittest.TestCase):
    """The address, version and time rules, on the functions themselves."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.home = Path(tempfile.mkdtemp(prefix="auto-update-unit-"))
        cls.tool = load_script(
            ROOT / "src" / "bin" / "model-profile", cls.home, "model_profile_updates"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.home, ignore_errors=True)

    def test_only_https_is_allowed_against_the_real_api(self) -> None:
        allowed = self.tool.is_allowed_update_url
        github = "https://api.github.com"
        for url, expected in (
            ("https://objects.githubusercontent.com/x", True),
            ("http://github.com/x", False),
            ("http://127.0.0.1:8000/x", False),
            ("https://user:secret@github.com/x", False),
            ("https:///x", False),
            ("ftp://github.com/x", False),
            ("file:///etc/passwd", False),
            ("https://[::1/x", False),
            (None, False),
        ):
            with self.subTest(url=url):
                self.assertEqual(allowed(url, github), expected)

    def test_loopback_http_is_allowed_only_against_a_loopback_api(self) -> None:
        allowed = self.tool.is_allowed_update_url
        local = "http://127.0.0.1:9000"
        for url, expected in (
            ("http://127.0.0.1:9000/download/x", True),
            ("http://localhost:9000/x", True),
            ("http://localhost:1@evil.example/x", False),
            ("http://user@127.0.0.1:9000/x", False),
            ("http://evil.example/x", False),
        ):
            with self.subTest(url=url):
                self.assertEqual(allowed(url, local), expected)

    def test_the_signed_version_is_read_only_from_the_header(self) -> None:
        read = self.tool.signed_release_version
        self.assertEqual(read(b"#!/bin/bash\n# release-version: 1.12.0\n"), (1, 12, 0))
        self.assertIsNone(read(b"#!/bin/bash\n# release-version: 1.12\n"))
        self.assertIsNone(read(b"x" * 5000 + b"\n# release-version: 1.12.0\n"))
        self.assertIsNone(read(b"echo '# release-version: 9.9.9'\n"))

    def test_a_settings_save_at_the_same_instant_is_retried_not_lost(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"model": "sonnet"}), encoding="utf-8")
        calls = []

        def change(current: dict) -> dict:
            calls.append(1)
            if len(calls) == 1:  # Claude Code saves its own change meanwhile
                settings.write_text(
                    json.dumps({"model": "sonnet", "theme": "dark"}), encoding="utf-8"
                )
            return {**current, "added": True}

        self.tool.change_hooks(change)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            json.loads(settings.read_text()),
            {"model": "sonnet", "theme": "dark", "added": True},
        )

    def test_a_stalling_server_cannot_outlast_the_deadline(self) -> None:
        server = TrickleServer(size=60, pause=0.1)
        self.addCleanup(server.close)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.tool.download(
                f"{server.base}/x", server.base, 1024, time.monotonic() + 0.5
            )
        self.assertLess(time.monotonic() - started, 3)


class TestShippedChannel(unittest.TestCase):
    """The committed channel points at this repository's releases and the real signing key."""

    def test_the_shipped_channel_names_this_release_file(self) -> None:
        channel = json.loads(
            (ROOT / "src" / "channel" / "update-channel.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(channel["repo"], "sumanghosh22/claude-model-profile")
        self.assertEqual(channel["asset"], ASSET)
        self.assertRegex(
            channel["signer"],
            r"^ssh-ed25519 [A-Za-z0-9+/]{68} claude-model-profile-release$",
        )

    def test_the_build_is_published_under_the_channel_name(self) -> None:
        work = Path(tempfile.mkdtemp(prefix="auto-update-name-"))
        self.addCleanup(shutil.rmtree, work, True)
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "build_bundle.py"),
                "--output-dir",
                str(work),
            ],
            check=True,
            capture_output=True,
        )
        self.assertTrue((work / ASSET).is_file())


class TestReleaseBuilds(unittest.TestCase):
    def test_only_a_build_named_like_the_channel_asset_is_signed(self) -> None:
        work = Path(tempfile.mkdtemp(prefix="auto-update-sign-"))
        self.addCleanup(shutil.rmtree, work, True)
        key, signer = make_key(work, "release")
        copy = copy_repo(
            work,
            "other",
            CURRENT,
            {"repo": REPO, "asset": "other.sh", "signer": signer},
        )
        result = subprocess.run(
            build_command(copy, work / "out", key),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, EXIT_OK)
        self.assertIn("only a release build is signed", result.stderr)
        self.assertFalse(list((work / "out").glob("*.sig")))

    def test_the_signed_header_carries_the_release_version(self) -> None:
        work = Path(tempfile.mkdtemp(prefix="auto-update-header-"))
        self.addCleanup(shutil.rmtree, work, True)
        key, signer = make_key(work, "release")
        installer, _ = build_installer(work, "header", NEWER, signer, key)
        header = installer[:4096].decode("utf-8", errors="replace").splitlines()
        self.assertIn(f"# release-version: {NEWER}", header)


if __name__ == "__main__":
    unittest.main()

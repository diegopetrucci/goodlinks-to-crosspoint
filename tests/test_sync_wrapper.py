from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "sync.sh"
SYNTHETIC_TOKEN = "synthetic-wrapper-token"
# RFC1918 addresses below are synthetic fixture values, never real LAN targets.
SYNTHETIC_ADDRESS = "192.168.254.242"
SYNTHETIC_SECOND_ADDRESS = "192.168.254.243"


sys.path.insert(0, str(ROOT / "src"))

from goodlinks_crosspoint.cli import build_parser


class SyncWrapperFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copy2(SCRIPT, self.root / "sync.sh")
        (self.root / "sync.sh").chmod(
            (self.root / "sync.sh").stat().st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )
        source_package = self.root / "src" / "goodlinks_crosspoint"
        source_package.mkdir(parents=True)
        (source_package / "__main__.py").write_text(
            "# synthetic source tree marker\n", encoding="utf-8"
        )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for command in ("bash", "dirname", "pwd"):
            executable = shutil.which(command)
            if executable is not None:
                (self.bin / command).symlink_to(executable)
        self.home = self.root / "home"
        self.home.mkdir()
        self.python_log = self.root / "python-invocation.json"
        self.pass_log = self.root / "pass-invocation.txt"
        self._write_tool(
            "uname",
            "#!/bin/sh\nprintf '%s\\n' Darwin\n",
        )
        self._write_pass()
        self._write_dscacheutil()
        self._write_fake_python()
        self.write_config("GOODLINKS_TAG=x3\n")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_tool(self, name: str, content: str) -> Path:
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def _write_pass(self) -> None:
        self._write_tool(
            "pass",
            f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(SYNTHETIC_TOKEN)}\n"
            f"printf '%s\\n' \"$*\" > {shlex.quote(str(self.pass_log))}\n",
        )

    def _write_dscacheutil(
        self,
        *,
        with_address: bool = True,
        addresses: tuple[str, ...] | None = None,
    ) -> None:
        output = ["name: crosspoint.local"]
        if with_address:
            for address in addresses or (
                SYNTHETIC_ADDRESS,
                SYNTHETIC_SECOND_ADDRESS,
            ):
                output.append(f"ip_address: {address}")
        lines = " ".join(shlex.quote(value) for value in output)
        self._write_tool(
            "dscacheutil",
            f"#!/bin/sh\nprintf '%s\\n' {lines}\n",
        )

    def _write_fake_python(self, exit_status: int = 0) -> None:
        self._write_tool(
            "python",
            "#!" + sys.executable + "\n"
            "import json\n"
            "import os\n"
            "import pathlib\n"
            "import sys\n"
            f"pathlib.Path({str(self.python_log)!r}).write_text("
            "json.dumps({\"args\": sys.argv[1:], "
            "\"token\": os.environ.get(\"GOODLINKS_TOKEN\"), "
            "\"pythonpath\": os.environ.get(\"PYTHONPATH\")}), "
            "encoding=\"utf-8\")\n"
            f"raise SystemExit({exit_status})\n",
        )
        venv_bin = self.root / ".venv" / "bin"
        venv_bin.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.bin / "python", venv_bin / "python")
        (venv_bin / "python").chmod(
            (venv_bin / "python").stat().st_mode | stat.S_IXUSR
        )

    def write_config(self, content: str) -> None:
        (self.root / ".sync.env").write_text(content, encoding="utf-8")

    def run_wrapper(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = {
            "HOME": str(self.home),
            "PATH": os.pathsep.join(
                (str(self.bin), *getattr(self, "path_tail", ()))
            ),
            # An inherited value must not win over the pass entry.
            "GOODLINKS_TOKEN": "synthetic-inherited-token",
        }
        return subprocess.run(
            ["./sync.sh", *arguments],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_no_private_diagnostic(
        self, result: subprocess.CompletedProcess[str]
    ) -> None:
        combined = result.stdout + result.stderr
        self.assertNotIn(SYNTHETIC_TOKEN, combined)
        self.assertNotIn("synthetic-inherited-token", combined)
        self.assertNotIn(SYNTHETIC_ADDRESS, combined)

    def test_zero_arguments_use_fixed_options_and_scoped_token(self) -> None:
        result = self.run_wrapper()

        self.assertEqual(result.returncode, 0, result.stderr)
        invocation = json.loads(self.python_log.read_text(encoding="utf-8"))
        self.assertEqual(
            invocation["args"],
            [
                "-m",
                "goodlinks_crosspoint",
                "sync",
                "--tag",
                "x3",
                "--output-dir",
                str((self.root / "export").resolve()),
                "--device-url",
                f"http://{SYNTHETIC_ADDRESS}",
            ],
        )
        self.assertEqual(invocation["token"], SYNTHETIC_TOKEN)
        self.assertNotIn(SYNTHETIC_TOKEN, invocation["args"])
        self.assert_no_private_diagnostic(result)

    def test_forwards_flags_and_scopes_pass_token_to_child(self) -> None:
        result = self.run_wrapper(
            "--dry-run",
            "--api-url",
            "http://127.0.0.1:9/api/v1",
            "--pandoc-executable",
            "synthetic-pandoc",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        invocation = json.loads(self.python_log.read_text(encoding="utf-8"))
        self.assertEqual(
            invocation["args"],
            [
                "-m",
                "goodlinks_crosspoint",
                "sync",
                "--dry-run",
                "--api-url",
                "http://127.0.0.1:9/api/v1",
                "--pandoc-executable",
                "synthetic-pandoc",
                "--tag",
                "x3",
                "--output-dir",
                str((self.root / "export").resolve()),
                "--device-url",
                f"http://{SYNTHETIC_ADDRESS}",
            ],
        )
        self.assertEqual(invocation["token"], SYNTHETIC_TOKEN)
        self.assertEqual(invocation["pythonpath"], str((self.root / "src").resolve()))
        self.assertNotIn(SYNTHETIC_TOKEN, result.stdout + result.stderr)
        self.assertEqual(
            self.pass_log.read_text(encoding="utf-8").strip(),
            "show goodlinks-crosspoint/goodlinks-token",
        )

    def test_fixed_options_remain_repository_owned(self) -> None:
        result = self.run_wrapper(
            "--output-dir",
            str(self.root / "outside"),
            "--device-url",
            "http://192.0.2.99",
            "--tag",
            "synthetic-override",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        invocation = json.loads(self.python_log.read_text(encoding="utf-8"))
        args = invocation["args"]
        self.assertEqual(args[-6:], [
            "--tag",
            "x3",
            "--output-dir",
            str((self.root / "export").resolve()),
            "--device-url",
            f"http://{SYNTHETIC_ADDRESS}",
        ])

    def test_comments_and_multiword_unicode_tag_are_preserved(self) -> None:
        tag = "Read later – 日本語"
        self.write_config(f"# synthetic comment\n   # indented comment\n \t\nGOODLINKS_TAG={tag}")

        result = self.run_wrapper("--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        invocation = json.loads(self.python_log.read_text(encoding="utf-8"))
        self.assertIn("--tag", invocation["args"])
        self.assertEqual(
            invocation["args"][invocation["args"].index("--tag") + 1],
            tag,
        )

    def test_shell_syntax_is_data_not_code(self) -> None:
        marker = self.root / "should-not-exist"
        tag = "$(touch " + str(marker) + "); synthetic"
        self.write_config(f"GOODLINKS_TAG={tag}\n")

        result = self.run_wrapper("--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        invocation = json.loads(self.python_log.read_text(encoding="utf-8"))
        self.assertEqual(
            invocation["args"][invocation["args"].index("--tag") + 1],
            tag,
        )

    def test_invalid_configuration_is_not_sourced_or_echoed(self) -> None:
        invalid_values = (
            "GOODLINKS_TAG=",
            "GOODLINKS_TAG=   ",
            "GOODLINKS_TAG= x3",
            "GOODLINKS_TAG=x3 ",
            "GOODLINKS_TAG=-x3",
            "GOODLINKS_TAG=x3\x01",
            "GOODLINKS_TAG=x3\r",
            "GOODLINKS_TAG=x3\nGOODLINKS_TAG=x4",
            "GOODLINKS_TOKEN=synthetic-config-token",
            "UNKNOWN_SETTING=x3",
            "# GOODLINKS_TAG=x3\n",
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                self.write_config(invalid)
                self.python_log.unlink(missing_ok=True)
                result = self.run_wrapper("--dry-run")

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.python_log.exists())
                self.assertNotIn("synthetic-config-token", result.stdout + result.stderr)
                self.assert_no_private_diagnostic(result)

    def test_missing_configuration_is_actionable(self) -> None:
        (self.root / ".sync.env").unlink()

        result = self.run_wrapper("--dry-run")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".sync.env", result.stderr)
        self.assertIn(".sync.env.example", result.stderr)
        self.assertFalse(self.python_log.exists())
        self.assert_no_private_diagnostic(result)

    def test_symlink_configuration_is_rejected(self) -> None:
        source = self.root / "source-config"
        source.write_text("GOODLINKS_TAG=x3\n", encoding="utf-8")
        (self.root / ".sync.env").unlink()
        (self.root / ".sync.env").symlink_to(source)

        result = self.run_wrapper("--dry-run")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("regular file", result.stderr)
        self.assertFalse(self.python_log.exists())
        self.assert_no_private_diagnostic(result)

    def test_missing_prerequisites_have_safe_errors(self) -> None:
        cases = (
            ("venv", "the repository virtual environment is missing", ".venv"),
            ("pass", "the pass executable is missing", "pass"),
            ("dscacheutil", "dscacheutil executable is missing", "dscacheutil"),
        )
        for missing, expected, label in cases:
            with self.subTest(missing=missing):
                if missing == "venv":
                    shutil.rmtree(self.root / ".venv")
                else:
                    (self.bin / missing).unlink()
                result = self.run_wrapper("--dry-run")

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)
                self.assertIn(label, result.stderr)
                self.assertFalse(self.python_log.exists())
                self.assert_no_private_diagnostic(result)
                self._write_fake_python()
                self._write_pass()
                self._write_dscacheutil()

    def test_empty_pass_entry_and_missing_device_are_rejected_without_address(self) -> None:
        self._write_tool("pass", "#!/bin/sh\nexit 0\n")
        result = self.run_wrapper("--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pass entry", result.stderr)
        self.assertFalse(self.python_log.exists())
        self.assert_no_private_diagnostic(result)

        self._write_pass()
        self._write_dscacheutil(with_address=False)
        result = self.run_wrapper("--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usable private CrossPoint IPv4 address", result.stderr)
        self.assertNotIn(SYNTHETIC_ADDRESS, result.stdout + result.stderr)
        self.assertFalse(self.python_log.exists())

    def test_device_resolution_skips_unusable_answers(self) -> None:
        invalid_addresses = (
            "127.0.0.42",
            "169.254.254.242",
            "0.0.0.0",
            "203.0.113.8",
            "010.0.0.1",
        )
        self._write_dscacheutil(
            addresses=invalid_addresses
            + (SYNTHETIC_ADDRESS, SYNTHETIC_SECOND_ADDRESS)
        )

        result = self.run_wrapper("--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        invocation = json.loads(self.python_log.read_text(encoding="utf-8"))
        self.assertEqual(
            invocation["args"][invocation["args"].index("--device-url") + 1],
            f"http://{SYNTHETIC_ADDRESS}",
        )
        self.assert_no_private_diagnostic(result)

    def test_non_macos_is_rejected_before_credentials_or_resolution(self) -> None:
        self._write_tool("uname", "#!/bin/sh\nprintf '%s\\n' Linux\n")

        result = self.run_wrapper("--dry-run")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("macOS only", result.stderr)
        self.assertFalse(self.pass_log.exists())
        self.assertFalse(self.python_log.exists())
        self.assert_no_private_diagnostic(result)


class SyncWrapperParserTests(unittest.TestCase):
    def test_fixed_sync_options_use_argparse_last_wins_behavior(self) -> None:
        parser = build_parser()
        parsed = parser.parse_args(
            [
                "sync",
                "--tag",
                "caller-tag",
                "--output-dir",
                "outside",
                "--device-url",
                "http://192.0.2.99",
                "--tag",
                "Read later – 日本語",
                "--output-dir",
                "export",
                "--device-url",
                f"http://{SYNTHETIC_ADDRESS}",
            ]
        )

        self.assertEqual(parsed.tag, "Read later – 日本語")
        self.assertEqual(parsed.output_dir, "export")
        self.assertEqual(parsed.device_url, f"http://{SYNTHETIC_ADDRESS}")


class SyncWrapperRepositoryTests(unittest.TestCase):
    def test_script_is_executable_and_local_env_ignore_rule_is_scoped(self) -> None:
        self.assertTrue(SCRIPT.is_file())
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        git = shutil.which("git")
        if git is None:
            self.skipTest("git executable unavailable")

        ignored = subprocess.run(
            [git, "-C", str(ROOT), "check-ignore", "--no-index", "-q", ".sync.env"],
            capture_output=True,
            text=True,
            check=False,
        )
        example = subprocess.run(
            [
                git,
                "-C",
                str(ROOT),
                "check-ignore",
                "--no-index",
                "-q",
                ".sync.env.example",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(ignored.returncode, 0)
        self.assertEqual(example.returncode, 1)


if __name__ == "__main__":
    unittest.main()

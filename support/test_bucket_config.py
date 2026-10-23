"""Configuration tests use temporary state and a fake AWS executable only."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import bucket_config as config
import s3_transfer as api


class BucketConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.env = dict(os.environ, S3_TRANSFER_STATE_DIR=str(self.state), PYTHONDONTWRITEBYTECODE="1")
        state_patch = patch.dict(os.environ, {"S3_TRANSFER_STATE_DIR": str(self.state)})
        state_patch.start()
        self.addCleanup(state_patch.stop)
        old_bucket, old_root = api.BUCKET, api.ROOT
        self.addCleanup(setattr, api, "BUCKET", old_bucket)
        self.addCleanup(setattr, api, "ROOT", old_root)
        api.BUCKET = None
        api.ROOT = "s3://<버킷 미설정>/transfer/"

    def run_cli(self, *args, input=""):
        return subprocess.run(["bash", str(api.ENTRY_SCRIPT), *args], input=input, text=True,
                              capture_output=True, env=self.env, cwd=self.root, timeout=10)

    def test_first_run_prompts_validates_pwd_and_persists(self):
        result = self.run_cli(input="pwd\nINVALID_BUCKET\nexample-data-bucket\n0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("버킷 미설정", result.stdout)
        self.assertIn("일반 S3 버킷 이름", result.stderr)
        self.assertIn("s3://example-data-bucket/transfer/", result.stdout)
        self.assertEqual(json.loads(config.config_path().read_text()), {"bucket": "example-data-bucket"})
        again = self.run_cli("--pwd")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn("최초 실행", again.stdout)
        self.assertIn("s3://example-data-bucket/transfer/", again.stdout)

    def test_set_and_show_change_persisted_bucket(self):
        for name in ("first-data-bucket", "second-data-bucket"):
            result = self.run_cli("--set-bucket", name)
            self.assertEqual(result.returncode, 0, result.stderr)
            shown = self.run_cli("--show-config")
            self.assertIn("버킷: " + name, shown.stdout)
            current = self.run_cli("pwd")
            self.assertIn("s3://" + name + "/transfer/", current.stdout)
        self.assertEqual(list(self.state.iterdir()), [config.config_path()])

    def test_help_and_show_without_configuration_do_not_prompt_or_write(self):
        for option in ("--help", "--show-config"):
            result = self.run_cli(option)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("최초 실행", result.stdout)
            self.assertFalse(self.state.exists())

    def test_invalid_change_preserves_saved_bucket(self):
        config.save_bucket("original-data-bucket")
        before = config.config_path().read_bytes()
        for invalid in ("", "UPPERCASE", "s3://other-bucket", "bucket/transfer", "192.168.0.1", "a..b", "-invalid", "a" * 64, "name--x-s3"):
            with self.subTest(invalid=invalid):
                result = self.run_cli("--set-bucket", invalid)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(config.config_path().read_bytes(), before)

    def test_invalid_config_fails_and_explicit_set_repairs(self):
        self.state.mkdir()
        for invalid in ("broken", "[]", "{}", '{"bucket": 123}'):
            config.config_path().write_text(invalid)
            result = self.run_cli("--pwd")
            self.assertEqual(result.returncode, 1)
            self.assertIn("--set-bucket", result.stderr)
            self.assertEqual(config.config_path().read_text(), invalid)
        repaired = self.run_cli("--set-bucket", "repaired-data-bucket")
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual(config.load_bucket(), "repaired-data-bucket")

    def test_first_run_cancellation_never_calls_aws(self):
        for answer in ("", EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=answer), patch("builtins.input", side_effect=[answer]), patch.object(api, "aws") as aws, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertNotEqual(api.entrypoint(["cli", "--search", "sample"]), 0)
                aws.assert_not_called()
                self.assertFalse(config.config_path().exists())

    def test_failed_save_preserves_config_and_active_bucket(self):
        config.save_bucket("original-data-bucket")
        api.activate_bucket("original-data-bucket")
        with patch.object(Path, "replace", side_effect=OSError("read-only")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(api.entrypoint(["cli", "--set-bucket", "other-data-bucket"]), 1)
        self.assertEqual(config.load_bucket(), "original-data-bucket")
        self.assertEqual(api.BUCKET, "original-data-bucket")
        self.assertEqual(list(self.state.iterdir()), [config.config_path()])

    def test_changed_bucket_is_used_by_aws_and_legacy_wrappers(self):
        fake = self.root / "aws"
        log = self.root / "calls.jsonl"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['TEST_AWS_LOG'], 'a') as output: output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1] == 's3api': print(json.dumps({'Contents': [{'Key': 'transfer/sample.txt'}]}))\n"
        )
        fake.chmod(0o755)
        self.env.update(PATH=str(self.root) + os.pathsep + os.environ["PATH"], TEST_AWS_LOG=str(log))
        (self.root / "sample.txt").touch()
        for bucket in ("first-data-bucket", "second-data-bucket"):
            self.assertEqual(self.run_cli("--set-bucket", bucket).returncode, 0)
            log.write_text("")
            self.assertEqual(self.run_cli("--search", "sample").returncode, 0)
            self.assertEqual(self.run_cli("--shell", input="cat sample.txt\nexit\n").returncode, 0)
            legacy = subprocess.run(["bash", str(api.SCRIPT_DIR / "transfer_file.sh"), "sample.txt"],
                                    env=self.env, cwd=self.root, text=True, capture_output=True, timeout=10)
            self.assertEqual(legacy.returncode, 0, legacy.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            for call in calls:
                if call[0] == "s3api":
                    self.assertEqual(call[call.index("--bucket") + 1], bucket)
                else:
                    remote = next(arg for arg in call if arg.startswith("s3://"))
                    self.assertTrue(remote.startswith("s3://" + bucket + "/transfer/"))

    def test_legacy_first_run_can_configure_before_search(self):
        with patch("builtins.input", return_value="example-data-bucket"), patch.object(api, "search_main", return_value=0) as search, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(api.entrypoint(["search", "sample"]), 0)
        self.assertEqual(config.load_bucket(), "example-data-bucket")
        search.assert_called_once_with(["sample"])


if __name__ == "__main__":
    unittest.main()

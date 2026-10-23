"""Offline regression tests for the extended shell."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import s3_transfer as api
from shell_features import ShellFeatures, path_matches, split_pipeline


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"S3_TRANSFER_STATE_DIR": str(self.root / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.session = ShellFeatures(api)
        self.session.local = self.root

    def output(self, command, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(self.session.handle(command, list(args)))
        return out.getvalue()

    def success(self):
        return subprocess.CompletedProcess([], 0, "{}", "")

    def test_lcd_lpwd_lls_and_process_directory_stays_unchanged(self):
        original = Path.cwd()
        directory = self.root / "sample dir"
        directory.mkdir()
        (directory / "data.txt").write_text("hello")
        self.output("lcd", "sample dir")
        self.assertEqual(Path.cwd(), original)
        self.assertIn(str(directory.resolve()), self.output("lpwd"))
        self.assertIn("5B", self.output("lls", "-lh"))
        with self.assertRaises(ValueError):
            self.output("lcd", "missing")
        self.assertEqual(self.session.local, directory.resolve())

    def test_cd_previous_and_bookmark_persistence(self):
        with patch.object(api, "remote_exists", return_value=True):
            self.output("cd", "results")
            self.output("bookmark", "add", "work")
            self.output("cd", "/other")
            self.output("cd", "-")
            self.assertEqual(self.session.cwd, "results")
            other = ShellFeatures(api)
            self.assertEqual(other.bookmarks, {"work": "results"})
            self.output("cd", "/")
            self.output("bookmark", "go", "work")
            self.assertEqual(self.session.cwd, "results")
            self.output("bookmark", "rm", "work")
            self.assertEqual(ShellFeatures(api).bookmarks, {})
        self.assertFalse(any(p.name.startswith("tmp") for p in (self.root / "state").iterdir()))

    def test_corrupt_bookmarks_are_not_overwritten(self):
        state = self.root / "state"
        state.mkdir()
        path = state / "bookmarks.json"
        path.write_text("not json")
        with contextlib.redirect_stderr(io.StringIO()):
            other = ShellFeatures(api)
        with self.assertRaises(ValueError):
            other.save_bookmarks({"new": ""})
        self.assertEqual(path.read_text(), "not json")

    def test_history_counts(self):
        self.session.history = ["pwd", "ls", "get file.txt"]
        out = self.output("history", "2")
        self.assertNotIn("pwd", out)
        self.assertIn("2  ls", out)
        self.assertEqual(self.output("history", "0"), "")

    def test_head_tail_empty_newlines_utf8_and_large_lines(self):
        cases = [b"", b"one", b"a\nb\n", b"a\nb", b"\n\n\n", "한글\n문장\n끝".encode(),
                 b"a" * 65535 + "한글\n".encode() + b"last\n", b"a" * 140000 + b"\nlast"]
        for data in cases:
            for command in ("head", "tail"):
                for count in (0, 1, 2, 10):
                    with self.subTest(command=command, size=len(data), count=count), patch.object(
                        self.session, "metadata", return_value={"ContentLength": len(data), "ETag": "v1"}
                    ), patch.object(self.session, "range_bytes", side_effect=lambda key, start, end, meta: data[start:end + 1]):
                        out = self.output(command, "-n", str(count), "file.txt")
                    lines = data.splitlines(keepends=True)
                    expected = b"".join(lines[:count] if command == "head" else lines[-count:] if count else [])
                    text = expected.decode()
                    if text and not text.endswith("\n"):
                        text += "\n"
                    self.assertEqual(out, text)

    def test_preview_fetches_only_needed_regions(self):
        data = b"first\n" + b"x\n" * 100000 + b"last\n"
        for command in ("head", "tail"):
            calls = []
            def read(key, start, end, meta):
                calls.append((start, end))
                return data[start:end + 1]
            with patch.object(self.session, "metadata", return_value={"ContentLength": len(data)}), patch.object(self.session, "range_bytes", side_effect=read):
                self.output(command, "-n", "1", "file.txt")
            self.assertLess(sum(end - start + 1 for start, end in calls), len(data))
            self.assertTrue(all(end - start + 1 <= 65536 for start, end in calls))

    def test_get_object_range_and_etag(self):
        paths = []
        def aws(*args):
            target = Path(args[args.index("--output") - 1])
            paths.append(target)
            target.write_bytes(b"abc")
            self.assertIn("bytes=5-7", args)
            self.assertIn('"etag"', args)
            return self.success()
        with patch.object(api, "aws", side_effect=aws):
            self.assertEqual(self.session.range_bytes("file", 5, 7, {"ETag": '"etag"'}), b"abc")
        self.assertFalse(paths[0].exists())

    def test_binary_preview_reports_error(self):
        with patch.object(self.session, "metadata", return_value={"ContentLength": 3}), patch.object(self.session, "range_bytes", return_value=b"a\0b"):
            with self.assertRaisesRegex(ValueError, "바이너리"):
                self.output("head", "binary")

    def test_recursive_download_preserves_structure(self):
        objects = [{"Key": "transfer/results/a.txt"}, {"Key": "transfer/results/nested/b.txt"}]
        with patch.object(self.session, "directory_objects", return_value=iter(objects)), patch.object(api, "aws", return_value=self.success()) as aws:
            self.output("get", "-r", "/results", "download")
        self.assertEqual(aws.call_args_list[0].args[3], str(self.root / "download/a.txt"))
        self.assertEqual(aws.call_args_list[1].args[3], str(self.root / "download/nested/b.txt"))
        self.assertTrue((self.root / "download/nested").is_dir())

    def test_recursive_download_rejects_escape_and_symlink(self):
        base = self.root / "download"
        base.mkdir()
        with self.assertRaises(ValueError):
            self.session.safe_download_target(base, "../outside")
        (base / "linked").symlink_to(self.root)
        with self.assertRaises(ValueError):
            self.session.safe_download_target(base, "linked/file.txt")

    def test_dry_run_does_not_create_destination_or_call_copy(self):
        with patch.object(self.session, "directory_objects", return_value=iter([{"Key": "transfer/results/x.txt"}])), patch.object(api, "aws") as aws:
            out = self.output("get", "-r", "--dry-run", "results", "new")
        self.assertIn("미리보기", out)
        self.assertFalse((self.root / "new").exists())
        aws.assert_not_called()

    def test_skip_existing_files_and_custom_destination(self):
        (self.root / "existing.txt").write_text("keep")
        with patch.object(api, "remote_exists", return_value=True), patch.object(api, "aws", return_value=self.success()) as aws:
            self.assertIn("건너뜀", self.output("get", "-n", "remote.txt", "existing.txt"))
            aws.assert_not_called()
            self.output("get", "remote.txt", "renamed.txt")
            self.assertEqual(aws.call_args.args[3], str(self.root / "renamed.txt"))
        self.assertEqual((self.root / "existing.txt").read_text(), "keep")

    def test_put_recursive_dry_run_and_skip(self):
        local = self.root / "data"
        (local / "nested").mkdir(parents=True)
        (local / "a.txt").write_text("a")
        (local / "nested/b.txt").write_text("b")
        with patch.object(api, "aws", return_value=self.success()) as aws:
            self.output("put", "-r", "--dry-run", "data", "/backup")
            aws.assert_not_called()
            self.output("put", "-r", "data", "/backup")
        self.assertEqual([call.args[3] for call in aws.call_args_list], [api.ROOT + "backup/a.txt", api.ROOT + "backup/nested/b.txt"])
        with patch.object(api, "remote_exists", return_value=True), patch.object(api, "aws") as aws:
            self.output("put", "-n", "data/a.txt", "/backup/a.txt")
            aws.assert_not_called()

    def test_put_defaults_to_transfer_root_from_nested_shell_location(self):
        folder = self.root / "parent" / "sample dir"
        (folder / "nested").mkdir(parents=True)
        (folder / "nested/file.txt").write_text("data")
        self.session.cwd = "elsewhere"
        with patch.object(api, "aws", return_value=self.success()) as aws:
            self.output("put", "parent/sample dir/nested/file.txt")
            self.assertEqual(aws.call_args.args[3], api.ROOT + "file.txt")
            self.output("put", "-r", str(folder) + "/")
            self.assertEqual(aws.call_args.args[3], api.ROOT + "sample dir/nested/file.txt")
            self.output("put", str(folder / "nested/file.txt"), "./")
            self.assertEqual(aws.call_args.args[3], api.ROOT + "elsewhere/file.txt")
            self.output("put", "-r", str(folder), "/custom")
            self.assertEqual(aws.call_args.args[3], api.ROOT + "custom/nested/file.txt")

    def test_put_similar_source_confirmation_keeps_destination_and_dry_run(self):
        (self.root / "report.txt").write_text("data")
        (self.root / "results").mkdir()
        (self.root / "results/file.txt").touch()
        self.session.cwd = "nested"
        cases = [(["repotr.txt", "./"], "nested/report.txt", False),
                 (["-r", "resutls"], "results/file.txt", False),
                 (["--dry-run", "repotr.txt"], "report.txt", True)]
        for args, destination, dry in cases:
            for answer in ("y", "n"):
                with self.subTest(args=args, answer=answer), patch("builtins.input", return_value=answer), patch.object(api, "aws", return_value=self.success()) as aws, contextlib.redirect_stderr(io.StringIO()) as err:
                    out = self.output("put", *args)
                if answer == "y" and not dry:
                    self.assertEqual(aws.call_args.args[3], api.ROOT + destination)
                else:
                    aws.assert_not_called()
                if answer == "y" and dry:
                    self.assertIn("미리보기", out)
                self.assertIn("목적지:", err.getvalue())

    def test_pwd_during_confirmation_keeps_shell_location_and_waits_for_yes(self):
        (self.root / "report.txt").touch()
        self.session.cwd = "nested"
        def answer(prompt):
            aws.assert_not_called()
            return next(answers)
        answers = iter(["pwd", "y"])
        with patch.object(api, "aws", return_value=self.success()) as aws, patch("builtins.input", side_effect=answer) as prompt, contextlib.redirect_stderr(io.StringIO()):
            out = self.output("put", "repotr.txt")
        self.assertEqual(prompt.call_count, 2)
        self.assertIn("로컬: " + str(self.root), out)
        self.assertIn("S3: " + api.ROOT + "nested/", out)
        self.assertEqual(aws.call_args.args[3], api.ROOT + "report.txt")

    def test_wildcards_stay_within_path_components(self):
        self.assertFalse(path_matches("results/nested/a.bam", "results/*.bam"))
        self.assertTrue(path_matches("results/a.bam", "results/*.bam"))
        pages = [
            {"Contents": [{"Key": "transfer/results/a.bam", "Size": 10}, {"Key": "transfer/results/nested/b.bam"}],
             "IsTruncated": True, "NextContinuationToken": "next"},
            {"Contents": [{"Key": "transfer/results/z.bam", "Size": 30}]},
        ]
        with patch.object(api, "list_page", side_effect=pages):
            out = self.output("ls", "results/*.bam")
        self.assertIn("results/a.bam", out)
        self.assertIn("results/z.bam", out)
        self.assertNotIn("nested/b.bam", out)

    def test_wildcard_download_collision_fails_before_transfer(self):
        with patch.object(self.session, "expand_remote", return_value=[("a/x.txt", 1, None), ("b/x.txt", 1, None)]), patch.object(api, "aws") as aws:
            with self.assertRaisesRegex(ValueError, "같은 파일명"):
                self.output("get", "*/x.txt")
            aws.assert_not_called()

    def test_du_excludes_directory_markers_and_counts_all_pages(self):
        pages = [
            {"Contents": [{"Key": "transfer/a/"}, {"Key": "transfer/a/x", "Size": 1024}], "IsTruncated": True, "NextContinuationToken": "next"},
            {"Contents": [{"Key": "transfer/b", "Size": 1024}]},
        ]
        with patch.object(api, "list_page", side_effect=pages):
            out = self.output("du", "-h")
        self.assertIn("파일 2개", out)
        self.assertIn("2.0K", out)

    def test_tree_depth_limits_requests(self):
        def listing(prefix, **kwargs):
            if prefix == "transfer/":
                return {"CommonPrefixes": [{"Prefix": "transfer/a/"}], "Contents": [{"Key": "transfer/file.txt"}]}
            if prefix == "transfer/a/":
                return {"CommonPrefixes": [{"Prefix": "transfer/a/b/"}], "Contents": [{"Key": "transfer/a/x.txt"}]}
            self.fail("requested a directory below requested depth")
        with patch.object(api, "list_page", side_effect=listing) as requests:
            out = self.output("tree", "-L", "2")
        self.assertIn("├── a/", out)
        self.assertIn("x.txt", out)
        self.assertEqual(requests.call_count, 2)

    def test_stat_displays_metadata(self):
        with patch.object(self.session, "metadata", return_value={"ContentLength": 50, "ETag": "etag", "ContentType": "text/plain", "Metadata": {"sample": "01"}}):
            out = self.output("stat", "file.txt")
        self.assertIn("50", out)
        self.assertIn("text/plain", out)
        self.assertIn("sample", out)

    def test_local_and_destination_completion(self):
        (self.root / "sample dir").mkdir()
        (self.root / "sample.txt").touch()
        completer = api.S3Completer(None, lambda: "", lambda: self.root, lambda: {"work": "results"})
        self.assertEqual(completer.candidates("lcd sam"), [r"lcd sample\ dir/"])
        self.assertEqual(completer.candidates("get remote.txt sam"), [r"get remote.txt sample\ dir/", "get remote.txt sample.txt "])
        self.assertEqual(completer.candidates("bookmark go wo"), ["bookmark go work "])
        with patch.object(completer, "paths", return_value=[("results/", True)]):
            self.assertEqual(completer.candidates("put sample.txt re"), ["put sample.txt results/"])
            self.assertEqual(completer.candidates("get -r re"), ["get -r results/"])

    def test_extended_shell_dispatch_and_history(self):
        prompts = ["lcd " + str(self.root), "lpwd", "history", "exit"]
        out = io.StringIO()
        with patch("builtins.input", side_effect=prompts), contextlib.redirect_stdout(out):
            self.assertEqual(api.shell_main(), 0)
        self.assertIn(str(self.root), out.getvalue())
        self.assertIn("lpwd", out.getvalue())

    def test_pipe_quote_parsing_and_head_tail(self):
        self.assertEqual(split_pipeline('cat "a|b.txt" | head -n 2'), ['cat "a|b.txt" ', ' head -n 2'])
        self.assertEqual(split_pipeline(r'cat a\|b.txt'), [r'cat a\|b.txt'])
        for command, expected in (("head", "a\n"), ("tail", "c\n")):
            with patch.object(self.session, "metadata", return_value={"ContentLength": 6}), patch.object(
                self.session, "range_bytes", side_effect=lambda key, start, end, meta: b"a\nb\nc\n"[start:end + 1]
            ):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.session.pipeline(split_pipeline(f'cat "a|b.txt" | {command} -n 1'))
                self.assertEqual(out.getvalue(), expected)

    def test_pipe_less_and_invalid_stages(self):
        self.session.cwd = "results"
        with patch.object(api, "remote_exists", return_value=True), patch.object(api, "shell_less") as less:
            self.session.pipeline(split_pipeline("cat file.py | less"))
            less.assert_called_once_with("results/file.py")
        for line in ("cat x |", "cat x || head", "cat x | rm x", "cat x | head other", "cat x | head | tail"):
            with self.subTest(line=line), self.assertRaises(ValueError), patch.object(api, "aws") as aws:
                self.session.pipeline(split_pipeline(line))
            aws.assert_not_called()

    def test_pipeline_completion(self):
        completer = api.S3Completer(None, lambda: "")
        self.assertEqual(completer.candidates("cat x | he"), ["cat x | head "])
        self.assertEqual(completer.candidates("cat x | head -"), ["cat x | head -n "])


if __name__ == "__main__":
    unittest.main()

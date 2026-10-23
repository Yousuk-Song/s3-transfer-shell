"""Offline tests: no real S3 reads, uploads or downloads."""

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import s3_transfer as s3


def setUpModule():
    s3.activate_bucket("test-bucket")


def response(data=None, code=0, error=""):
    return subprocess.CompletedProcess([], code, json.dumps(data or {}), error)


def page(*keys, **extra):
    return {"Contents": [{"Key": "transfer/" + key} for key in keys], **extra}


class TransferTests(unittest.TestCase):
    def setUp(self):
        config = patch.object(s3, "ensure_configuration")
        config.start()
        self.addCleanup(config.stop)

    def test_pwd_in_each_menu_prompt_preserves_pending_action(self):
        commands = ["pwd", "2", "pwd", "file.txt", "3", "pwd", "folder", "4", "pwd", "remote.txt", "5", "pwd", "remote_dir", "6", "pwd", "sample", "0"]
        with patch("builtins.input", side_effect=commands), patch.object(s3, "main", return_value=0) as transfer, patch.object(s3, "search_main", return_value=0) as search, patch.object(s3, "aws") as aws, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(s3.menu_main(), 0)
        self.assertEqual(out.getvalue().count("S3: " + s3.ROOT), 6)
        self.assertEqual(out.getvalue().count("로컬: " + str(Path.cwd())), 6)
        self.assertEqual([call.args[0][-1] for call in transfer.call_args_list], ["file.txt", "folder", "remote.txt", "remote_dir"])
        search.assert_called_once_with(["--", "sample"])
        aws.assert_not_called()

    def test_pwd_direct_options_and_literal_prompt_input(self):
        with patch.object(s3, "aws") as aws:
            for option in ("pwd", "--pwd"):
                code, out, err = self.invoke("cli", option)
                self.assertEqual(code, 0)
                self.assertIn("로컬: " + str(Path.cwd()), out)
                self.assertIn("S3: " + s3.ROOT, out)
            aws.assert_not_called()
        with patch("builtins.input", return_value=r"\pwd"):
            self.assertEqual(s3.menu_input("검색: "), "pwd")

    def test_similar_local_upload_requires_yes_for_files_and_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "report.txt").write_text("data")
            (root / "results").mkdir()
            for kind, typo, name, operation in (("file", "repotr.txt", "report.txt", "cp"), ("dir", "resutls", "results", "sync")):
                for answer in ("y", "Y", "n", ""):
                    with self.subTest(kind=kind, answer=answer), patch("builtins.input", return_value=answer) as prompt, patch.object(s3, "aws", return_value=response()) as aws:
                        code, _, err = self.invoke("upload", kind, str(root / typo))
                    prompt.assert_called_once()
                    self.assertIn("목적지: " + s3.ROOT + name, err)
                    if answer.lower() == "y":
                        self.assertEqual(code, 0)
                        self.assertEqual(aws.call_args.args, ("s3", operation, str(root / name), s3.ROOT + name))
                    else:
                        self.assertEqual(code, 1)
                        aws.assert_not_called()

    def test_upload_confirmation_invalid_input_retries_and_eof_cancels(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "report.txt"
            source.touch()
            for answers, accepted in ((["maybe", "y"], True), ([EOFError()], False), ([KeyboardInterrupt()], False)):
                with self.subTest(answers=answers), patch("builtins.input", side_effect=answers), patch.object(s3, "aws", return_value=response()) as aws:
                    code, _, _ = self.invoke("upload", "file", str(source.parent / "repotr.txt"))
                self.assertEqual(code, 0 if accepted else 1)
                self.assertEqual(aws.call_count, int(accepted))

    def test_local_candidates_are_ranked_and_match_requested_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "report.txt").touch()
            (root / "report-old.txt").touch()
            (root / "repotr.txt.dir").mkdir()
            matches = s3.find_similar_local(root / "repotr.txt", "file")
            self.assertEqual(matches[0], root / "report.txt")
            self.assertNotIn(root / "repotr.txt.dir", matches)

    def test_upload_paths_use_basename_for_files_and_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "parent" / "sample dir"
            folder.mkdir(parents=True)
            source = folder / "sample file.txt"
            source.write_text("data")
            for kind, path, name, operation in (
                ("file", str(source), source.name, "cp"),
                ("file", os.path.relpath(source), source.name, "cp"),
                ("dir", str(folder) + "/", folder.name, "sync"),
                ("dir", os.path.relpath(folder) + "/", folder.name, "sync"),
                ("dir", str(folder) + "/../sample dir/.", folder.name, "sync"),
            ):
                with self.subTest(path=path), patch.object(s3, "aws", return_value=response()) as aws:
                    code, _, err = self.invoke("cli", "--upload-" + kind, path)
                self.assertEqual(code, 0, err)
                self.assertEqual(aws.call_args.args[:2], ("s3", operation))
                self.assertEqual(aws.call_args.args[3], s3.ROOT + name)
                self.assertEqual(Path(aws.call_args.args[2]).resolve(), Path(path).resolve())

    def test_suggestions_stop_after_three_pages_and_keep_partial_results(self):
        pages = [page("report.csv", IsTruncated=True, NextContinuationToken=str(i)) for i in range(3)]
        with patch.object(s3, "list_page", side_effect=pages) as listing, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIn("report.csv", s3.find_similar("repotr.csv", "file"))
        self.assertEqual(listing.call_count, 3)
        self.assertIn("조회량 제한", err.getvalue())
        self.assertTrue(all(0 < call.kwargs["timeout"] <= 3 for call in listing.call_args_list))

    def test_suggestions_stop_when_deadline_expires(self):
        with patch.object(s3.time, "monotonic", side_effect=[0, 4]), patch.object(s3, "list_page") as listing, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(s3.find_similar("missing", "file"), [])
        listing.assert_not_called()

    def test_missing_menu_target_returns_to_mode_selection_after_timeout(self):
        with patch("builtins.input", side_effect=["4", "missing", "0"]) as prompt, patch.object(s3, "remote_exists", return_value=False), patch.object(
            s3.subprocess, "run", side_effect=subprocess.TimeoutExpired("aws", 3)
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(s3.menu_main(), 0)
        self.assertEqual(prompt.call_args.args, ("모드 선택: ",))
        self.assertIn("제한 시간", err.getvalue())

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = s3.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_missing_file_suggests_but_never_copies(self):
        with patch.object(s3, "aws", side_effect=[
            response(code=254, error="An error occurred (404) when calling HeadObject"),
            response(page("nested/report.csv", "unrelated.bam", "report/")),
        ]) as aws:
            code, _, err = self.invoke("download", "file", "repotr.csv")
        self.assertEqual(code, 1)
        self.assertIn("nested/report.csv", err)
        self.assertIn("혹시", err)
        self.assertNotIn("unrelated.bam", err)
        self.assertTrue(all(call.args[0] == "s3api" for call in aws.call_args_list))

    def test_auth_error_is_not_reported_as_missing(self):
        with patch.object(s3, "aws", return_value=response(code=254, error="(403) Forbidden")) as aws:
            code, _, err = self.invoke("download", "file", "report.csv")
        self.assertEqual(code, 1)
        self.assertIn("Forbidden", err)
        self.assertNotIn("찾을 수 없습니다", err)
        self.assertEqual(aws.call_count, 1)

    def test_directory_check_requires_slash_boundary(self):
        with patch.object(s3, "aws", return_value=response(page())) as aws:
            self.assertFalse(s3.remote_exists("sample/", "dir"))
        args = aws.call_args.args
        self.assertEqual(args[args.index("--prefix") + 1], "transfer/sample/")
        self.assertEqual(args[args.index("--max-keys") + 1], "1")

    def test_all_pages_and_implicit_directories(self):
        with patch.object(s3, "aws", side_effect=[
            response(page("parent/sample/a.txt", IsTruncated=True, NextContinuationToken="page2")),
            response(page("parent/sample/b.txt", "parent/empty/", "late/result.txt")),
        ]) as aws:
            entries = list(s3.iter_entries())
        self.assertEqual(entries.count(("dir", "parent/sample")), 1)
        self.assertIn(("dir", "parent/empty"), entries)
        self.assertIn(("file", "late/result.txt"), entries)
        self.assertNotIn(("file", "parent/empty/"), entries)
        self.assertIn("--continuation-token", aws.call_args.args)
        self.assertEqual(aws.call_args.args[-1], "page2")

    def test_missing_directory_suggestions(self):
        with patch.object(s3, "aws", side_effect=[
            response(page()), response(page("sample/output.txt", "sample/more.txt")),
        ]):
            code, _, err = self.invoke("download", "dir", "sampl")
        self.assertEqual(code, 1)
        self.assertIn(s3.ROOT + "sample/", err)
        self.assertNotIn("output.txt", err)

    def test_missing_upload_searches_remote(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            s3, "aws", return_value=response(page("report.csv"))
        ) as aws:
            code, _, err = self.invoke("upload", "file", str(Path(tmp) / "repotr.csv"))
        self.assertEqual(code, 1)
        self.assertIn("s3_transfer --download-file", err)
        self.assertIn("로컬 원본", err)
        self.assertEqual(aws.call_count, 1)

    def test_wrong_local_type_does_not_transfer(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(s3, "aws") as aws:
            code, _, err = self.invoke("upload", "file", tmp)
        self.assertEqual(code, 1)
        self.assertIn("종류", err)
        aws.assert_not_called()

    def test_existing_downloads_keep_cp_and_sync(self):
        for kind, operation, check in (
            ("file", "cp", response()), ("dir", "sync", response(page("sample/data"))),
        ):
            with self.subTest(kind=kind), patch.object(s3, "aws", side_effect=[check, response()]) as aws:
                code, _, _ = self.invoke("download", kind, "sample")
                self.assertEqual(code, 0)
                self.assertEqual(aws.call_args.args[:2], ("s3", operation))

    def test_case_insensitive_literal_search_and_no_parent_only_matches(self):
        with patch.object(s3, "aws", return_value=response(page(
            "Group/SAMPLE.txt", "sample_folder/other.txt", "sample_folder/x.txt", "literal[1].csv"
        ))):
            code, out, err = self.invoke("search", "sAmPlE")
        self.assertEqual(code, 0)
        self.assertIn("file\t" + s3.ROOT + "Group/SAMPLE.txt", out)
        self.assertIn("dir\t" + s3.ROOT + "sample_folder/", out)
        self.assertNotIn("other.txt", out)
        self.assertIn("2개", err)
        with patch.object(s3, "aws", return_value=response(page("literal[1].csv"))):
            code, out, _ = self.invoke("search", "[1]")
        self.assertEqual(code, 0)
        self.assertIn("literal[1].csv", out)

    def test_no_search_results(self):
        with patch.object(s3, "aws", return_value=response(page("abc.txt"))):
            code, out, err = self.invoke("search", "xyz")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("0개", err)

    def test_incomplete_search_is_an_error(self):
        with patch.object(s3, "aws", return_value=response(page(IsTruncated=True))):
            code, _, err = self.invoke("search", "sample")
        self.assertEqual(code, 2)
        self.assertIn("검색 미완료", err)

    def test_suggestion_commands_quote_special_characters(self):
        name = "sample $(touch unwanted) 'file'.txt"
        with patch.object(s3, "find_similar", return_value=[name]):
            _, _, err = self.invoke("upload", "file", "missing_sample.txt")
        command = next(line.split("다운로드: ", 1)[1] for line in err.splitlines() if "다운로드:" in line)
        self.assertEqual(shlex.split(command), [str(s3.ENTRY_SCRIPT), "--download-file", name])

    def test_cli_invalid_options_never_call_aws(self):
        for arguments in (
            ["--search"], ["--unknown", "sample"],
            ["--upload-file", "sample", "--download-file", "sample"],
        ):
            with self.subTest(arguments=arguments), patch.object(s3, "aws") as aws:
                with self.assertRaises(SystemExit) as raised:
                    self.invoke("cli", *arguments)
                self.assertEqual(raised.exception.code, 2)
                aws.assert_not_called()

    def test_default_menu_selects_shell_then_returns_to_menu(self):
        with patch("builtins.input", side_effect=["invalid", "1", "0"]), patch.object(s3, "shell_main", return_value=0) as shell:
            code, out, err = self.invoke("cli")
        self.assertEqual(code, 0)
        self.assertIn("1. S3 탐색", out)
        self.assertIn("0~6", out)
        self.assertEqual(err, "")
        shell.assert_called_once_with()

    def test_menu_dispatches_transfers_and_search_with_spaces(self):
        with patch("builtins.input", side_effect=["2", "sample file.txt", "4", "", "6", "sample 01", "0"]), patch.object(
            s3, "main", return_value=0
        ) as transfer, patch.object(s3, "search_main", return_value=0) as search, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(s3.menu_main(), 0)
        transfer.assert_called_once_with(["upload", "file", "--", "sample file.txt"])
        search.assert_called_once_with(["--", "sample 01"])

    def test_menu_eof_exits_cleanly(self):
        with patch("builtins.input", side_effect=EOFError()):
            code, _, err = self.invoke("cli")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_cli_literal_search_starting_with_dash(self):
        with patch.object(s3, "aws", return_value=response(page("file-test.txt"))):
            code, out, _ = self.invoke("cli", "--search=-test")
        self.assertEqual(code, 0)
        self.assertIn("file-test.txt", out)

    def test_wrappers_from_other_directory_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_aws = root / "aws"
            fake_aws.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "with open(os.environ['TEST_AWS_LOG'], 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "if sys.argv[1] == 's3api': print(json.dumps({'Contents': [{'Key': 'transfer/sample file.txt'}]}))\n"
                "if sys.argv[1:3] == ['s3', 'cp'] and sys.argv[3].startswith('s3://') and sys.argv[4] != '-': Path(sys.argv[4]).write_text('mock S3 content')\n"
                "if sys.argv[1] == 's3': sys.exit(int(os.environ.get('TEST_COPY_STATUS', '0')))\n"
            )
            fake_aws.chmod(0o755)
            (root / "sample file.txt").touch()
            (root / "sample dir").mkdir()
            log = root / "calls.jsonl"
            state = root / "state"
            state.mkdir()
            (state / "config.json").write_text(json.dumps({"bucket": "test-bucket"}))
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"], TEST_AWS_LOG=str(log), S3_TRANSFER_STATE_DIR=str(state))
            for script, arguments, op in (
                ("transfer_file.sh", ["sample file.txt"], "cp"),
                ("transfer_dir.sh", ["sample dir"], "sync"),
                ("get_file.sh", ["sample file.txt"], "cp"),
                ("get_dir.sh", ["sample dir"], "sync"),
                ("search_s3.sh", ["SAMPLE"], None),
                ("s3_transfer", ["--upload-file", "sample file.txt"], "cp"),
                ("s3_transfer", ["--upload-dir", "sample dir"], "sync"),
                ("s3_transfer", ["--download-file", "sample file.txt"], "cp"),
                ("s3_transfer", ["--download-dir", "sample dir"], "sync"),
                ("s3_transfer", ["--search", "SAMPLE"], None),
            ):
                with self.subTest(script=script):
                    log.write_text("")
                    result = subprocess.run(["bash", str(s3.ENTRY_SCRIPT if script == "s3_transfer" else s3.SCRIPT_DIR / script), *arguments],
                                            cwd=root, env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    calls = [json.loads(line) for line in log.read_text().splitlines()]
                    if op:
                        self.assertEqual(calls[-1][:2], ["s3", op])
                        self.assertEqual(len(calls[-1]), 4)
                    else:
                        self.assertIn("sample file.txt", result.stdout)
            env["TEST_COPY_STATUS"] = "7"
            result = subprocess.run(["bash", str(s3.SCRIPT_DIR / "transfer_file.sh"), "sample file.txt"],
                                    cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 7)

            env["TEST_COPY_STATUS"] = "0"
            log.write_text("")
            result = subprocess.run(
                ["bash", str(s3.ENTRY_SCRIPT), "--shell"],
                input='pwd\nls\ncat "sample file.txt"\ncd results\nget "downloaded sample.txt"\nexit\n',
                cwd=root, env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(s3.ROOT, result.stdout)
            self.assertIn("sample file.txt", result.stdout)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertIn(["s3", "cp", s3.ROOT + "sample file.txt", "-", "--only-show-errors"], calls)
            self.assertIn(["s3", "cp", s3.ROOT + "results/downloaded sample.txt",
                           str(root.resolve() / "downloaded sample.txt")], calls)
            self.assertEqual((root / "downloaded sample.txt").read_text(), "mock S3 content")
            self.assertFalse((root / "results").exists())


class ShellTests(unittest.TestCase):
    def setUp(self):
        config = patch.object(s3, "ensure_configuration")
        config.start()
        self.addCleanup(config.stop)

    def test_pwd_reports_shell_local_and_remote_locations(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(s3, "remote_exists", return_value=True):
            code, out, err = self.session(["cd results", "lcd " + shlex.quote(tmp), "pwd", "exit"])
        self.assertEqual(code, 0)
        self.assertIn("로컬: " + str(Path(tmp).resolve()), out)
        self.assertIn("S3: " + s3.ROOT + "results/", out)

    def test_missing_target_returns_to_prompt_without_opening_pager(self):
        for command in ("cat missing", "less missing", "get missing", "cd missing", "cat missing | less", "cat missing | head"):
            with self.subTest(command=command), patch.object(s3, "remote_exists", return_value=False), patch.object(
                s3, "aws", side_effect=[response(code=254, error="(404)"), response(page())] if command.endswith("head") else [response(page())]
            ), patch.object(s3, "shell_less") as pager:
                code, out, err = self.session([command, "pwd", "exit"])
            self.assertEqual(code, 0)
            self.assertIn(s3.ROOT, out)
            self.assertIn("대상을 찾을 수 없습니다", err)
            pager.assert_not_called()

    def session(self, commands):
        out, err = io.StringIO(), io.StringIO()
        with patch("builtins.input", side_effect=commands), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = s3.main(["cli", "--shell"])
        return code, out.getvalue(), err.getvalue()

    def test_paths_stay_inside_transfer(self):
        cases = [
            ("a/b", "../c", "a/c"), ("a/b", "/c", "c"),
            ("a", "../../../", ""), ("", ".", ""),
            ("a", "folder with spaces", "a/folder with spaces"),
            ("a", s3.ROOT + "b/file.txt", "b/file.txt"),
        ]
        for cwd, path, expected in cases:
            self.assertEqual(s3.shell_path(cwd, path), expected)
        with self.assertRaises(ValueError):
            s3.shell_path("", "s3://other-bucket/private")

    def test_cd_pwd_parent_and_failed_cd_preserve_location(self):
        with patch.object(s3, "remote_exists", side_effect=[True, False]) as exists, patch.object(
            s3, "find_similar", return_value=["folder with spaces"]
        ):
            code, out, err = self.session([
                'cd "folder with spaces"', "pwd", "cd missing", "pwd", "cd ..", "pwd", "exit",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(out.count(s3.ROOT + "folder with spaces/"), 2)
        self.assertIn("cd '/folder with spaces'", err)
        self.assertEqual(exists.call_args_list[1].args, ("folder with spaces/missing", "dir"))

    def test_ls_uses_delimiter_and_all_pages(self):
        pages = [
            {"CommonPrefixes": [{"Prefix": "transfer/sub/"}],
             "Contents": [{"Key": "transfer/"}, {"Key": "transfer/a.txt", "Size": 12, "LastModified": "2026-10-23"}],
             "IsTruncated": True, "NextContinuationToken": "next"},
            {"Contents": [{"Key": "transfer/z.txt", "Size": 24, "LastModified": "2026-10-22"}]},
        ]
        with patch.object(s3, "list_page", side_effect=pages) as listing:
            code, out, err = self.session(["ls -l", "exit"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("sub/", out)
        self.assertIn("12  2026-10-23  a.txt", out)
        self.assertIn("z.txt", out)
        self.assertEqual(listing.call_args_list[0].args, ("transfer/",))
        self.assertEqual(listing.call_args_list[1].kwargs, {"token": "next", "delimiter": "/"})

    def test_cat_streams_from_resolved_path(self):
        with patch.object(s3, "remote_exists", return_value=True), patch.object(
            s3, "aws", return_value=response()
        ) as aws:
            code, _, err = self.session(["cd results", 'cat "summary file.txt"', "exit"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        aws.assert_called_once_with("s3", "cp", s3.ROOT + "results/summary file.txt", "-", "--only-show-errors", capture=False)

    def test_ls_ltrh_orders_across_pages_and_formats_size(self):
        pages = [
            {"CommonPrefixes": [{"Prefix": "transfer/folder/"}],
             "Contents": [{"Key": "transfer/new.txt", "Size": 1048576, "LastModified": "2026-10-23T00:00:00Z"}],
             "IsTruncated": True, "NextContinuationToken": "next"},
            {"Contents": [{"Key": "transfer/old.txt", "Size": 2048, "LastModified": "2026-10-20T00:00:00Z"}]},
        ]
        with patch.object(s3, "list_page", side_effect=pages):
            code, out, err = self.session(["ls -ltrh", "exit"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertLess(out.index("old.txt"), out.index("new.txt"))
        self.assertLess(out.index("new.txt"), out.index("folder/"))
        self.assertIn("2.0K", out)
        self.assertIn("1.0M", out)

    def test_ls_sort_variants_and_separate_flags(self):
        objects = {"Contents": [
            {"Key": "transfer/a.txt", "Size": 0, "LastModified": "2026-10-20T01:00:00+00:00"},
            {"Key": "transfer/b.txt", "Size": 1024, "LastModified": "2026-10-20T11:00:00+09:00"},
        ]}
        for command, expected in (("ls -t", ["b.txt", "a.txt"]), ("ls -r", ["b.txt", "a.txt"]),
                                  ("ls -tr", ["a.txt", "b.txt"]), ("ls -l -t -r -h", ["a.txt", "b.txt"])):
            with self.subTest(command=command), patch.object(s3, "list_page", return_value=objects):
                _, out, err = self.session([command, "exit"])
            self.assertEqual(err, "")
            self.assertLess(out.index(expected[0]), out.index(expected[1]))
        self.assertEqual(s3.parse_ls_args(["results", "-hlrt"]), (set("hlrt"), ["results"]))
        self.assertEqual(s3.parse_ls_args(["-lh", "--", "-report"]), (set("lh"), ["-report"]))
        with self.assertRaises(ValueError):
            s3.parse_ls_args(["-z"])

    def test_less_resolves_path_and_suggests_missing_file(self):
        with patch.object(s3, "remote_exists", side_effect=[True, True, False]), patch.object(
            s3, "shell_less"
        ) as pager, patch.object(s3, "find_similar", return_value=["results/run.log"]):
            code, out, err = self.session(["cd results", 'less "run log.txt"', "less rn.log", "pwd", "exit"])
        self.assertEqual(code, 0)
        pager.assert_called_once_with("results/run log.txt")
        self.assertIn("less /results/run.log", err)
        self.assertIn(s3.ROOT + "results/", out)

    def test_less_missing_program_or_nonterminal_fails_before_download(self):
        with patch.object(s3.shutil, "which", return_value=None), patch.object(s3.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(ValueError, "less 명령이 없습니다"):
                s3.shell_less("file.txt")
            launch.assert_not_called()
        with patch.object(s3.shutil, "which", return_value="/usr/bin/less"), patch.object(
            s3.sys.stdin, "isatty", return_value=False
        ), patch.object(s3.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(ValueError, "대화형 터미널"):
                s3.shell_less("file.txt")
            launch.assert_not_called()

    def test_less_reports_upstream_failure_after_pager_exits(self):
        download = Mock(stdout=io.BytesIO(), returncode=1)
        download.poll.return_value = 1
        pager = Mock(returncode=0)
        pager.wait.return_value = 0
        pager.poll.return_value = 0

        def launch(args, **kwargs):
            if args[0] == "aws":
                kwargs["stderr"].write(b"AccessDenied from S3")
                return download
            return pager

        with patch.object(s3.shutil, "which", return_value="/usr/bin/less"), patch.object(
            s3.sys.stdin, "isatty", return_value=True
        ), patch.object(s3.sys.stdout, "isatty", return_value=True), patch.object(s3.subprocess, "Popen", side_effect=launch):
            with self.assertRaisesRegex(s3.AWSError, "AccessDenied"):
                s3.shell_less("file.txt")
        self.assertTrue(download.stdout.closed)

    def test_less_q_stops_and_reaps_active_download(self):
        download = Mock(stdout=io.BytesIO(), returncode=-15)
        download.poll.side_effect = [None, None, -15]
        pager = Mock(returncode=0)
        pager.wait.return_value = 0
        pager.poll.return_value = 0
        with patch.object(s3.shutil, "which", return_value="/usr/bin/less"), patch.object(
            s3.sys.stdin, "isatty", return_value=True
        ), patch.object(s3.sys.stdout, "isatty", return_value=True), patch.object(s3.subprocess, "Popen", side_effect=[download, pager]):
            s3.shell_less("file.txt")
        download.terminate.assert_called_once_with()
        download.wait.assert_called_once_with(timeout=2)

    @unittest.skipUnless(os.name == "posix" and s3.shutil.which("less"), "requires less and a POSIX terminal")
    def test_real_less_quit_returns_to_shell_and_menu(self):
        import pty
        import select
        import signal

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_aws = root / "aws"
            fake_aws.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys, time\n"
                "from pathlib import Path\n"
                "if sys.argv[1] == 's3api':\n"
                "    print('{}')\n"
                "else:\n"
                "    Path(os.environ['TEST_DOWNLOAD_PID']).write_text(str(os.getpid()))\n"
                "    for index in range(1, 100000):\n"
                "        print(f'pager-line-{index:05d}', flush=True)\n"
                "        time.sleep(0.002)\n"
            )
            fake_aws.chmod(0o755)
            pid_file = root / "download.pid"
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                       TERM="xterm-256color", LESS="", TEST_DOWNLOAD_PID=str(pid_file),
                       PYTHONDONTWRITEBYTECODE="1", S3_TRANSFER_STATE_DIR=str(root / "state"))
            pid, terminal = pty.fork()
            if pid == 0:
                try:
                    os.chdir(root)
                    os.execve(str(s3.ENTRY_SCRIPT), [str(s3.ENTRY_SCRIPT)], env)
                except BaseException:
                    os._exit(127)

            def receive(expected):
                output = b""
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if select.select([terminal], [], [], 0.1)[0]:
                        try:
                            output += os.read(terminal, 65536)
                        except OSError:
                            break
                        if expected.encode() in output:
                            return output.decode(errors="replace")
                self.fail(f"Expected {expected!r}, received {output!r}")

            reaped = False
            try:
                receive("S3 버킷 이름 (빈 입력: 취소): ")
                os.write(terminal, b"test-bucket\n")
                receive("모드 선택: ")
                os.write(terminal, b"1\n")
                receive("s3:/transfer> ")
                os.write(terminal, b"less report.txt\n")
                receive("pager-line-00001")
                os.write(terminal, b"q")
                receive("s3:/transfer> ")
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), 0)
                os.write(terminal, b"pwd\n")
                self.assertIn(s3.ROOT, receive("s3:/transfer> "))
                os.write(terminal, b"exit\n")
                receive("모드 선택: ")
                os.write(terminal, b"0\n")
                receive("0\r\n")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    result, status = os.waitpid(pid, os.WNOHANG)
                    if result:
                        reaped = True
                        self.assertTrue(os.WIFEXITED(status))
                        self.assertEqual(os.WEXITSTATUS(status), 0)
                        break
                    time.sleep(0.02)
                self.assertTrue(reaped, "main menu did not exit")
            finally:
                if not reaped:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                os.close(terminal)

    def test_missing_cat_suggests_without_transfer(self):
        with patch.object(s3, "remote_exists", return_value=False), patch.object(
            s3, "find_similar", return_value=["results/summary.txt"]
        ), patch.object(s3, "aws") as aws:
            code, _, err = self.session(["cat summry.txt", "exit"])
        self.assertEqual(code, 0)
        self.assertIn("cat /results/summary.txt", err)
        aws.assert_not_called()

    def test_errors_and_interrupt_leave_shell_running(self):
        with patch.object(s3, "remote_exists", side_effect=s3.AWSError("(403) Forbidden")):
            code, out, err = self.session([
                "cd denied", "cat", 'cd "unclosed', "rm file", KeyboardInterrupt(), "pwd", EOFError(),
            ])
        self.assertEqual(code, 0)
        self.assertIn("Forbidden", err)
        self.assertIn("사용법: cat", err)
        self.assertIn("No closing quotation", err)
        self.assertIn("지원하지 않는 명령", err)
        self.assertIn("현재 명령을 중단", out)
        self.assertIn(s3.ROOT, out)

    def test_search_in_shell_uses_existing_search(self):
        with patch.object(s3, "search_main", return_value=0) as search:
            code, _, _ = self.session(['search "sample 01"', "quit"])
        self.assertEqual(code, 0)
        search.assert_called_once_with(["--", "sample 01"])

    def test_cat_failure_returns_to_prompt(self):
        with patch.object(s3, "remote_exists", return_value=True), patch.object(
            s3, "aws", return_value=response(code=1)
        ):
            code, out, err = self.session(["cat file.txt", "pwd", "exit"])
        self.assertEqual(code, 0)
        self.assertIn("파일 읽기에 실패", err)
        self.assertIn(s3.ROOT, out)

    def test_get_saves_basename_in_local_cwd_after_remote_cd(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(s3.Path, "cwd", return_value=Path(tmp)), patch.object(
            s3, "remote_exists", return_value=True
        ), patch.object(s3, "aws", return_value=response()) as aws:
            code, out, err = self.session([
                "cd results", 'get "summary file.txt"', "get /other/report.csv", "pwd", "exit",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(aws.call_args_list[0].args, ("s3", "cp", s3.ROOT + "results/summary file.txt", str(Path(tmp) / "summary file.txt")))
        self.assertEqual(aws.call_args_list[1].args, ("s3", "cp", s3.ROOT + "other/report.csv", str(Path(tmp) / "report.csv")))
        self.assertIn("다운로드 완료", out)
        self.assertIn(s3.ROOT + "results/", out)

    def test_missing_get_suggests_get_without_downloading(self):
        with patch.object(s3, "remote_exists", return_value=False), patch.object(
            s3, "find_similar", return_value=["results/summary.txt"]
        ), patch.object(s3, "aws") as aws:
            code, _, err = self.session(["get summry.txt", "exit"])
        self.assertEqual(code, 0)
        self.assertIn("get /results/summary.txt", err)
        aws.assert_not_called()

    def test_get_rejects_invalid_arguments_and_local_directory_collision(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(s3.Path, "cwd", return_value=Path(tmp)), patch.object(
            s3, "remote_exists", return_value=True
        ), patch.object(s3, "aws") as aws:
            (Path(tmp) / "report.txt").mkdir()
            code, _, err = self.session(['get', 'get ""', 'get /', 'get results/', 'get report.txt', 'exit'])
        self.assertEqual(code, 0)
        self.assertIn("사용법: get", err)
        self.assertIn("파일 경로", err)
        self.assertIn("같은 이름의 로컬 디렉터리", err)
        aws.assert_not_called()

    def test_get_failure_and_access_error_keep_shell_running(self):
        with patch.object(s3, "remote_exists", side_effect=[True, s3.AWSError("(403) Forbidden")]), patch.object(
            s3, "aws", return_value=response(code=1)
        ) as aws:
            code, out, err = self.session(["get report.txt", "get denied.txt", "pwd", "exit"])
        self.assertEqual(code, 0)
        self.assertIn("다운로드에 실패", err)
        self.assertIn("Forbidden", err)
        self.assertNotIn("다운로드 완료", out)
        self.assertIn(s3.ROOT, out)
        self.assertEqual(aws.call_count, 1)


class CompletionTests(unittest.TestCase):
    def test_menu_local_paths_preserve_spaces_and_filter_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample dir").mkdir()
            (root / "sample file.txt").touch()
            (root / "sample dir/한글.txt").touch()
            with patch.object(s3.Path, "cwd", return_value=root):
                files = s3.LocalPathCompleter()
                self.assertEqual(files.candidates("sam"), ["sample dir/", "sample file.txt"])
                self.assertEqual(files.candidates("sample dir/한"), ["sample dir/한글.txt"])
                self.assertEqual(files.candidates(str(root) + "/sample f"), [str(root / "sample file.txt")])
                self.assertEqual(s3.LocalPathCompleter(True).candidates("sam"), ["sample dir/"])
                self.assertEqual(files.candidates("missing/"), [])
                self.assertEqual(files.candidates("~"), ["~/"])
                self.assertEqual(files.candidates(".."), ["../"])

    def test_menu_input_scopes_and_restores_completion_even_on_interrupt(self):
        try:
            import readline
        except ImportError:
            self.skipTest("readline is unavailable")
        previous, delimiters = readline.get_completer(), readline.get_completer_delims()
        def interrupted(prompt):
            self.assertIsInstance(readline.get_completer().__self__, s3.LocalPathCompleter)
            self.assertEqual(readline.get_completer_delims(), "")
            raise KeyboardInterrupt
        with patch("builtins.input", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                s3.menu_input("local: ", local_kind="file")
        self.assertEqual(readline.get_completer(), previous)
        self.assertEqual(readline.get_completer_delims(), delimiters)
        def selected(prompt):
            self.assertIsNone(readline.get_completer()("ca", 0))
            return "0"
        with patch("builtins.input", side_effect=selected):
            self.assertEqual(s3.menu_input("mode: "), "0")

    @unittest.skipUnless(os.name == "posix", "requires a POSIX terminal")
    def test_real_terminal_menu_local_completion(self):
        try:
            import readline
        except ImportError:
            self.skipTest("readline is unavailable")
        import pty
        import select
        import sys
        fixture = '''import s3_transfer as s, readline, os, sys
s.activate_bucket("test-bucket")
readline.parse_and_bind("bind -e" if "libedit" in (readline.__doc__ or "") else "set editing-mode emacs")
os.chdir(sys.argv[1])
def transfer(args):
    print("SELECTED=" + repr(args[-1]), flush=True)
    return 0
s.main = transfer
s.menu_main()
'''
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sample file.txt").touch()
            (Path(tmp) / "sample folder").mkdir()
            master, slave = pty.openpty()
            process = subprocess.Popen([sys.executable, "-c", fixture, tmp], cwd=s3.SCRIPT_DIR,
                                       stdin=slave, stdout=slave, stderr=slave)
            os.close(slave)
            def receive(expected):
                output = b""
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            output += os.read(master, 65536)
                        except OSError:
                            break
                        if expected.encode() in output:
                            return output.decode(errors="replace")
                self.fail(f"Expected {expected!r}, received {output!r}")
            try:
                receive("모드 선택: ")
                os.write(master, b"2\n")
                receive("로컬 경로 (빈 입력: 메뉴로): ")
                os.write(master, b"sam\t\t")
                output = receive("sample folder/")
                self.assertIn("sample file.txt", output)
                os.write(master, b"\x15sample fi\t\n")
                output = receive("모드 선택: ")
                self.assertIn("SELECTED='sample file.txt'", output)
                os.write(master, b"3\n")
                receive("로컬 경로 (빈 입력: 메뉴로): ")
                os.write(master, b"sam\t\n")
                output = receive("모드 선택: ")
                self.assertIn("SELECTED='sample folder/'", output)
                os.write(master, b"0\n")
                receive("0\r\n")
                self.assertEqual(process.wait(timeout=5), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                os.close(master)

    def setUp(self):
        self.cwd = ""
        self.completer = s3.S3Completer(None, lambda: self.cwd)

    def test_command_completion_needs_no_s3(self):
        with patch.object(s3, "aws") as aws:
            self.assertEqual(self.completer.candidates("pw"), ["pwd "])
            self.assertEqual(self.completer.candidates("c"), ["cat ", "cd "])
            self.assertEqual(self.completer.candidates("g"), ["get "])
            self.assertEqual(self.completer.candidates("le"), ["less "])
            self.assertEqual(self.completer.candidates("search sample"), [])
            aws.assert_not_called()

    def test_cd_filters_files_and_cat_allows_directory_traversal(self):
        data = {"CommonPrefixes": [{"Prefix": "transfer/results/"}],
                "Contents": [{"Key": "transfer/report.txt"}]}
        with patch.object(s3, "list_page", return_value=data) as listing:
            self.assertEqual(self.completer.candidates("cd re"), ["cd results/"])
            self.assertEqual(self.completer.candidates("cat re"), ["cat report.txt ", "cat results/"])
            self.assertEqual(self.completer.candidates("get re"), ["get report.txt ", "get results/"])
            self.assertEqual(self.completer.candidates("less re"), ["less report.txt ", "less results/"])
        listing.assert_called_once_with("transfer/re", token=None, delimiter="/")

    def test_nested_absolute_and_parent_paths(self):
        self.cwd = "work/sub"
        for line, query, obj, expected in (
            ("cat sum", "transfer/work/sub/sum", "transfer/work/sub/summary.txt", "cat summary.txt "),
            ("cat ../sum", "transfer/work/sum", "transfer/work/summary.txt", "cat ../summary.txt "),
            ("cat /sum", "transfer/sum", "transfer/summary.txt", "cat /summary.txt "),
        ):
            with self.subTest(line=line), patch.object(s3, "list_page", return_value={"Contents": [{"Key": obj}]}) as listing:
                self.assertEqual(self.completer.candidates(line), [expected])
                self.assertEqual(listing.call_args.args, (query,))

    def test_spaces_quotes_and_shell_characters_round_trip(self):
        for name in ("sample dir/", "sample's dir/", 'sample"dir/', "sample $(touch nope)/", "sample한글/"):
            for raw in ("sam", '"sam', "'sam"):
                with self.subTest(name=name, raw=raw), patch.object(
                    s3, "list_page", return_value={"CommonPrefixes": [{"Prefix": "transfer/" + name}]}
                ):
                    self.completer.cache.clear()
                    result = self.completer.candidates("cd " + raw)
                    self.assertEqual(shlex.split(result[0]), ["cd", name])
                    self.assertEqual(self.completer.candidates(result[0]), [])

    def test_ls_options_and_escaped_word(self):
        with patch.object(s3, "list_page", return_value={"Contents": [{"Key": "transfer/sample file.txt"}]}) as listing:
            result = self.completer.candidates(r"ls -l sample\ f")
            self.assertEqual(shlex.split(result[0]), ["ls", "-l", "sample file.txt"])
            self.assertIn("ls -l ", self.completer.candidates("ls -"))
            self.assertEqual(shlex.split(self.completer.candidates(r"ls -ltrh sample\ f")[0]),
                             ["ls", "-ltrh", "sample file.txt"])
            self.assertEqual(listing.call_count, 1)

    def test_completion_pagination_cache_and_expiry(self):
        data = [
            {"CommonPrefixes": [{"Prefix": "transfer/results/"}], "IsTruncated": True, "NextContinuationToken": "next"},
            {"CommonPrefixes": [{"Prefix": "transfer/reports/"}]},
            {"CommonPrefixes": [{"Prefix": "transfer/recent/"}]},
        ]
        with patch.object(s3, "list_page", side_effect=data) as listing, patch.object(s3.time, "monotonic", return_value=0):
            expected = ["cd reports/", "cd results/"]
            self.assertEqual(self.completer.candidates("cd re"), expected)
            self.assertEqual(self.completer.candidates("cd re"), expected)
            self.assertEqual(listing.call_count, 2)
            self.assertEqual(listing.call_args.kwargs["token"], "next")
            with patch.object(s3.time, "monotonic", return_value=6):
                self.assertEqual(self.completer.candidates("cd re"), ["cd recent/"])

    def test_parent_shortcut_and_case_sensitive_match(self):
        with patch.object(s3, "list_page", return_value={"CommonPrefixes": [{"Prefix": "transfer/Results/"}]}):
            self.assertEqual(self.completer.candidates("cd re"), [])
            self.assertEqual(self.completer.candidates("cd .."), ["cd ../"])

    @unittest.skipUnless(os.name == "posix", "requires a POSIX terminal")
    def test_real_terminal_tab_completion(self):
        try:
            import readline
        except ImportError:
            self.skipTest("readline is unavailable")
        import pty
        import select
        import sys

        fixture = '''import s3_transfer as s, readline
s.activate_bucket("test-bucket")
readline.parse_and_bind("bind -e" if "libedit" in (readline.__doc__ or "") else "set editing-mode emacs")
keys = ["transfer/results/summary.txt", "transfer/reports/report.txt", "transfer/sample dir/data.txt", "transfer/한글자료/data.txt"]
def listing(prefix, **kwargs):
    folders, files = set(), []
    for key in keys:
        if not key.startswith(prefix): continue
        tail = key[len(prefix):]
        if "/" in tail: folders.add(prefix + tail.split("/", 1)[0] + "/")
        else: files.append({"Key": key})
    return {"CommonPrefixes": [{"Prefix": key} for key in sorted(folders)], "Contents": files}
s.list_page = listing
s.remote_exists = lambda key, kind: any(k.startswith("transfer/" + key.rstrip("/") + "/") for k in keys) if kind == "dir" else "transfer/" + key in keys
def fake_aws(*args, **kwargs):
    print("READ: " + args[2])
    return s.subprocess.CompletedProcess(args, 0)
s.aws = fake_aws
s.shell_main()
'''
        master, slave = pty.openpty()
        process = subprocess.Popen([sys.executable, "-c", fixture], cwd=s3.SCRIPT_DIR,
                                   stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)

        def receive(expected):
            output = b""
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        output += os.read(master, 65536)
                    except OSError:
                        break
                    if expected.encode() in output:
                        return output.decode(errors="replace")
            self.fail(f"Expected {expected!r}, received {output!r}")

        try:
            receive("s3:/transfer> ")
            os.write(master, b"pw\t\n")
            self.assertIn(s3.ROOT, receive("s3:/transfer> "))
            os.write(master, b"cd re\t\t")
            output = receive("s3:/transfer> cd re")
            self.assertIn("reports/", output)
            self.assertIn("results/", output)
            os.write(master, b'\x15cd "sam\t\n')
            receive("s3:/transfer/sample dir> ")
            os.write(master, b"cat da\t\n")
            self.assertIn("READ: " + s3.ROOT + "sample dir/data.txt", receive("s3:/transfer/sample dir> "))
            os.write(master, b"get da\t\n")
            self.assertIn("다운로드 완료:", receive("s3:/transfer/sample dir> "))
            os.write(master, b"cd /\n")
            receive("s3:/transfer> ")
            os.write(master, "cd 한글\t\n".encode())
            receive("s3:/transfer/한글자료> ")
            os.write(master, b"exit\n")
            receive("exit\r\n")
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            os.close(master)


if __name__ == "__main__":
    unittest.main()

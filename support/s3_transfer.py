#!/usr/bin/env python3
"""Shared implementation for transfer_{file,dir}.sh and get_{file,dir}.sh."""

import argparse
from datetime import datetime, timezone
from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from shell_features import ShellFeatures, HELP as EXTRA_HELP, split_pipeline
from bucket_config import config_path, load_bucket, save_bucket, validate_bucket


BUCKET = None
PREFIX = "transfer/"
ROOT = f"s3://<버킷 미설정>/{PREFIX}"
SCRIPT_DIR = Path(__file__).resolve().parent
ENTRY_SCRIPT = SCRIPT_DIR.parent / "s3_transfer"


class AWSError(RuntimeError):
    pass


def activate_bucket(name):
    global BUCKET, ROOT
    BUCKET = validate_bucket(name)
    ROOT = f"s3://{BUCKET}/{PREFIX}"


def ensure_configuration():
    name = load_bucket()
    if name is None:
        print("최초 실행 설정: 사용할 S3 버킷 이름을 입력하세요. 작업 경로는 버킷의 transfer/입니다.")
        while True:
            name = menu_input("S3 버킷 이름 (빈 입력: 취소): ").strip()
            if not name:
                raise ValueError("버킷 설정을 취소했습니다. 전송을 실행하지 않았습니다.")
            try:
                name = validate_bucket(name)
            except ValueError as exc:
                print(f"오류: {exc}", file=sys.stderr)
                continue
            name = save_bucket(name)
            print(f"버킷 설정 저장: {config_path()}")
            break
    activate_bucket(name)


def aws(*args, capture=True, timeout=None):
    if not BUCKET:
        raise AWSError("버킷이 설정되지 않았습니다. 프로그램을 다시 실행하거나 --set-bucket 이름을 사용하세요.")
    env = dict(os.environ, AWS_PAGER="", AWS_CLI_AUTO_PROMPT="off")
    try:
        return subprocess.run(
            ["aws", *args], text=True, capture_output=capture, env=env, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise AWSError("S3 조회 제한 시간이 지났습니다.") from exc
    except FileNotFoundError as exc:
        raise AWSError("aws 명령을 찾을 수 없습니다. AWS CLI 설치를 확인하세요.") from exc


def read_json(result):
    if result.returncode:
        raise AWSError(result.stderr.strip() or "S3 조회에 실패했습니다.")
    try:
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data
    except (ValueError, TypeError) as exc:
        raise AWSError("AWS CLI의 JSON 응답을 읽을 수 없습니다.") from exc


def list_page(prefix, limit=1000, token=None, delimiter=None, timeout=None):
    args = [
        "s3api", "list-objects-v2", "--bucket", BUCKET,
        "--prefix", prefix, "--max-keys", str(limit),
        "--no-paginate", "--output", "json",
    ]
    if token:
        args.extend(["--continuation-token", token])
    if delimiter is not None:
        args.extend(["--delimiter", delimiter])
    return read_json(aws(*args, **({"timeout": timeout} if timeout is not None else {})))


def remote_exists(key, kind):
    if kind == "dir":
        return bool(list_page(PREFIX + key.rstrip("/") + "/", limit=1).get("Contents"))
    result = aws("s3api", "head-object", "--bucket", BUCKET,
                 "--key", PREFIX + key, "--output", "json")
    if not result.returncode:
        return True
    # Permissions, credentials and network errors must not trigger a missing-file search.
    if re.search(r"\((404|NoSuchKey|NotFound)\)", result.stderr):
        return False
    raise AWSError(result.stderr.strip() or "S3 파일 확인에 실패했습니다.")


def similarity(wanted, candidate):
    wanted = wanted.rstrip("/").casefold()
    candidate = candidate.rstrip("/").casefold()
    name = wanted.rsplit("/", 1)[-1]
    other = candidate.rsplit("/", 1)[-1]
    name_score = SequenceMatcher(None, name, other).ratio()
    path_score = SequenceMatcher(None, wanted, candidate).ratio()
    if name in other or other in name:
        name_score = max(name_score, 0.8)
    return 0.85 * name_score + 0.15 * path_score


def iter_entries(kind=None, *, deadline=None, max_pages=None):
    """Read every page; S3 directories need not have an explicit marker object."""
    seen_dirs = set()
    token = None
    pages = 0
    while True:
        if max_pages is not None and pages >= max_pages:
            raise AWSError("자동 추천 조회량 제한에 도달했습니다.")
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise AWSError("자동 추천 제한 시간이 지났습니다.")
        page = list_page(PREFIX, token=token, **({"timeout": remaining} if remaining is not None else {}))
        pages += 1
        for item in page.get("Contents", []):
            if deadline is not None and time.monotonic() >= deadline:
                raise AWSError("자동 추천 제한 시간이 지났습니다.")
            object_key = item["Key"]
            if not object_key.startswith(PREFIX):
                continue
            relative = object_key[len(PREFIX):]
            if kind != "file":
                parts = relative.split("/")
                for depth in range(1, len(parts)):
                    directory = "/".join(parts[:depth])
                    if directory and directory not in seen_dirs:
                        seen_dirs.add(directory)
                        yield "dir", directory
            if kind != "dir" and relative and not relative.endswith("/"):
                yield "file", relative
        if not page.get("IsTruncated"):
            return
        next_token = page.get("NextContinuationToken")
        if not next_token or next_token == token:
            raise AWSError("S3 목록의 다음 페이지 토큰이 없어 검색을 완료하지 못했습니다.")
        token = next_token


def find_similar(key, kind):
    best = []
    try:
        for _, candidate in iter_entries(kind, deadline=time.monotonic() + 3, max_pages=3):
            score = similarity(key, candidate)
            if score >= 0.55:
                best.append((score, candidate))
                best.sort(key=lambda entry: (-entry[0], entry[1]))
                del best[10:]
    except AWSError as exc:
        print(f"자동 추천을 마칩니다: {exc} 전체 검색은 search 문자열을 사용하세요.", file=sys.stderr)
    return [candidate for _, candidate in best]


def search_main(argv):
    parser = argparse.ArgumentParser(
        prog="search_s3.sh",
        description="S3 transfer/ 하위의 파일·디렉터리 이름에서 문자열 검색 (대소문자 무시)",
    )
    parser.add_argument("text", help="이름에 포함된 문자열; 공백은 따옴표로 감싸세요")
    args = parser.parse_args(argv)
    if not args.text:
        parser.error("검색 문자열이 비어 있습니다.")
    query = args.text.casefold()
    count = 0
    print(f"{ROOT}에서 이름에 {args.text!r}이 포함된 항목 검색...", file=sys.stderr, flush=True)
    try:
        for kind, relative in iter_entries():
            if query in relative.rsplit("/", 1)[-1].casefold():
                suffix = "/" if kind == "dir" else ""
                print(f"{kind}\t{ROOT}{relative}{suffix}", flush=True)
                count += 1
    except AWSError as exc:
        print(f"S3 오류 (검색 미완료): {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n검색을 중단했습니다.", file=sys.stderr)
        return 130
    print(f"검색 결과: {count}개", file=sys.stderr)
    return 0 if count else 1


def suggest(key, kind, action):
    label = "디렉터리" if kind == "dir" else "파일"
    print(f"{ROOT}에서 비슷한 {label} 이름을 검색합니다...", file=sys.stderr, flush=True)
    candidates = find_similar(key, kind)
    if not candidates:
        print("비슷한 이름의 후보를 찾지 못했습니다.", file=sys.stderr)
        return
    print("혹시 아래 항목을 찾으셨나요? (이름이 비슷한 순서, 최대 10개)", file=sys.stderr)
    if action == "upload":
        print("아래는 S3에 있는 항목입니다. 로컬 원본이 필요하면 내려받은 뒤 확인하세요.",
              file=sys.stderr)
    script = ENTRY_SCRIPT
    for index, candidate in enumerate(candidates, 1):
        suffix = "/" if kind == "dir" else ""
        print(f"  {index}. {ROOT}{candidate}{suffix}", file=sys.stderr)
        option = f"--download-{kind}"
        arguments = [option + "=" + candidate] if candidate.startswith("-") else [option, candidate]
        print(f"     다운로드: {shlex.join([str(script), *arguments])}", file=sys.stderr)
    print("자동으로 전송하지 않았습니다. 원하는 항목의 명령을 직접 실행하세요.", file=sys.stderr)


SHELL_HELP = """명령:
  pwd                    현재 로컬 경로와 S3 경로
  ls [-altrh] [경로]     목록 (l: 상세, t: 최신순, r: 역순, h: 읽기 쉬운 크기)
  cd [경로]              디렉터리 이동 (생략 또는 /: transfer/ 루트)
  cd ..                  상위 디렉터리로 이동
  cat 파일               파일 내용을 바로 출력
  less 파일              스크롤·검색하며 읽기 (Space/b: 이동, /: 검색, q: 돌아오기)
  get 파일               현재 S3 위치의 파일을 로컬 현재 디렉터리에 다운로드
  search 문자열          transfer/ 전체에서 이름 검색 (대소문자 무시)
  help                   도움말
  exit / quit            종료 (Ctrl-D도 가능)
  Tab                    명령어·S3 경로 자동완성 (여러 후보는 Tab을 다시 눌러 표시)
공백이 있는 경로는 따옴표로 감싸세요. /는 S3 transfer/ 루트를 뜻합니다.
cat 파이프와 ls/get 패턴은 아래 확장 명령 안내를 확인하세요.
"""


def shell_path(cwd, path):
    """Resolve virtual paths without ever leaving the transfer prefix."""
    if path.startswith(ROOT):
        path = "/" + path[len(ROOT):]
    elif path.startswith("s3://"):
        raise ValueError(f"이 셸에서는 {ROOT} 안의 경로만 사용할 수 있습니다.")
    parts = [] if path.startswith("/") else cwd.split("/") if cwd else []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def shell_missing(key, kind, command=None):
    print(f"대상을 찾을 수 없습니다: {ROOT}{key}", file=sys.stderr, flush=True)
    print("비슷한 이름을 잠시 확인한 뒤 입력으로 돌아갑니다 (최대 3초·3페이지).", file=sys.stderr, flush=True)
    candidates = find_similar(key, kind)
    if not candidates:
        print("비슷한 이름의 후보를 찾지 못했습니다.", file=sys.stderr)
    else:
        print("혹시 아래 항목을 찾으셨나요?", file=sys.stderr)
        command = command or ("cd" if kind == "dir" else "cat")
        for candidate in candidates:
            print(f"  {shlex.join([command, '/' + candidate])}", file=sys.stderr)


def parse_ls_args(args):
    flags, paths = set(), []
    options = True
    for arg in args:
        if options and arg == "--":
            options = False
        elif options and arg.startswith("-") and arg != "-":
            if not set(arg[1:]) <= set("altrh"):
                raise ValueError("사용법: ls [-altrh] [경로]")
            flags.update(arg[1:])
        else:
            paths.append(arg)
    if len(paths) > 1:
        raise ValueError("사용법: ls [-altrh] [경로]")
    return flags, paths


def readable_size(size):
    value = float(size)
    for unit in ("B", "K", "M", "G", "T", "P", "E"):
        if value < 1024 or unit == "E":
            return f"{size}B" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024


def modified_timestamp(value):
    if not value or value == "-":
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.timestamp()
    except (TypeError, ValueError):
        return None


def print_listing(entries, detailed=False, time_sort=False, reverse=False, human=False):
    ordered = sorted(entries, key=lambda entry: entry[0])
    if time_sort:
        dated = [entry for entry in ordered if modified_timestamp(entry[2]) is not None]
        undated = [entry for entry in ordered if modified_timestamp(entry[2]) is None]
        dated.sort(key=lambda entry: modified_timestamp(entry[2]), reverse=True)
        if reverse:
            dated.reverse()
            undated.reverse()
        # S3 prefixes have no directory mtime; keep them after dated objects.
        ordered = dated + undated
    elif reverse:
        ordered.reverse()
    for name, size, modified in ordered:
        if detailed:
            display_size = "DIR" if size is None else readable_size(size) if human else str(size)
            print(f"{display_size:>12}  {modified or '-'}  {name}")
        else:
            print(name)


def shell_list(key, detailed=False, time_sort=False, reverse=False, human=False):
    if key and not remote_exists(key, "dir"):
        if remote_exists(key, "file"):
            metadata = read_json(aws("s3api", "head-object", "--bucket", BUCKET,
                                     "--key", PREFIX + key, "--output", "json"))
            name = key.rsplit("/", 1)[-1]
            print_listing([(name, metadata.get("ContentLength", 0), metadata.get("LastModified", "-"))],
                          detailed, time_sort, reverse, human)
        else:
            shell_missing(key, "dir")
        return
    prefix = PREFIX + (key + "/" if key else "")
    token = None
    entries = []
    while True:
        page = list_page(prefix, token=token, delimiter="/")
        for entry in page.get("CommonPrefixes", []):
            entries.append((entry["Prefix"][len(prefix):], None, None))
        for entry in page.get("Contents", []):
            if entry["Key"] != prefix:  # Hide this directory's own marker.
                entries.append((entry["Key"][len(prefix):], entry.get("Size", 0), entry.get("LastModified", "-")))
        if not page.get("IsTruncated"):
            break
        next_token = page.get("NextContinuationToken")
        if not next_token or next_token == token:
            raise AWSError("S3 목록의 다음 페이지 토큰이 없어 조회를 완료하지 못했습니다.")
        token = next_token
    print_listing(entries, detailed, time_sort, reverse, human)


def stop_child(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def shell_less(key):
    pager_path = shutil.which("less")
    if not pager_path:
        raise ValueError("less 명령이 없습니다. less를 설치하거나 cat으로 확인하세요.")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("less는 대화형 터미널에서 실행하세요. 바로 출력하려면 cat을 사용하세요.")
    download = pager = None
    env = dict(os.environ, AWS_PAGER="", AWS_CLI_AUTO_PROMPT="off")
    # Only diagnostics go to a temporary file; object bytes flow straight into less.
    with tempfile.TemporaryFile() as errors:
        try:
            sys.stdout.flush()
            download = subprocess.Popen(
                ["aws", "s3", "cp", ROOT + key, "-", "--only-show-errors"],
                stdout=subprocess.PIPE, stderr=errors, env=env,
            )
            pager = subprocess.Popen([pager_path], stdin=download.stdout)
            download.stdout.close()
            pager_status = pager.wait()
            cancelled = download.poll() is None
            stop_child(download)  # q must stop a download that is still producing data.
            errors.seek(0)
            message = errors.read().decode("utf-8", errors="replace").strip()
            if download.returncode and not cancelled and "broken pipe" not in message.casefold():
                raise AWSError(message or f"파일 읽기에 실패했습니다 (종료 코드 {download.returncode}).")
            if pager_status:
                raise ValueError(f"less가 종료 코드 {pager_status}로 종료되었습니다.")
        finally:
            stop_child(pager)
            stop_child(download)
            if download is not None and download.stdout is not None:
                download.stdout.close()


def completion_context(line):
    """Find the current shell word, including unfinished quotes and escaped spaces."""
    quote = None
    escaped = False
    start = 0
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char.isspace():
            start = index + 1
    prefix, raw = line[:start], line[start:]
    # A trailing escape is unfinished, so leave it for the user to finish.
    if escaped:
        raise ValueError("unfinished escape")
    words = shlex.split(raw + (quote or ""))
    return prefix, shlex.split(prefix), words[0] if words else "", raw


def completion_quote(path, raw):
    if raw.startswith('"'):
        return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if raw.startswith("'"):
        return "'" + path.replace("'", "'\"'\"'") + "'"
    # Backslash escaping preserves the common prefix while completing names with spaces.
    return "".join("\\" + char if char.isspace() or char in "\\\"'`$;&|<>()*?[]{}!" else char
                   for char in path)


def local_path_candidates(path, base, directory_only=False):
    """Return literal paths; callers choose raw menu text or shell quoting."""
    if path == "~":
        return [("~/", True)]
    parent, separator, fragment = path.rpartition("/")
    typed_parent = parent + separator
    parent_path = Path(typed_parent or ".").expanduser()
    if not parent_path.is_absolute():
        parent_path = base / parent_path
    if not parent_path.is_dir():
        return []
    entries = []
    for item in parent_path.iterdir():
        directory = item.is_dir()
        if item.name.startswith(fragment) and (not directory_only or directory):
            entries.append((typed_parent + item.name + ("/" if directory else ""), directory))
    if fragment in (".", ".."):
        entries.extend((typed_parent + name, True) for name in ("./", "../") if name.startswith(fragment))
    return sorted(entries)


class LocalPathCompleter:
    """Menu paths are literal input: preserve spaces without adding shell escapes."""
    def __init__(self, directory_only=False):
        self.directory_only = directory_only
        self.matches = []

    def candidates(self, line):
        return [path for path, _ in local_path_candidates(line, Path.cwd(), self.directory_only)]

    def complete(self, text, state):
        if state == 0:
            self.matches = []
            try:
                self.matches = self.candidates(text)
            except (OSError, ValueError, RuntimeError, KeyboardInterrupt):
                pass
        return self.matches[state] if state < len(self.matches) else None


def bind_completion(readline):
    if getattr(readline, "backend", "") == "editline" or "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
        readline.parse_and_bind("set show-all-if-ambiguous off")
        readline.parse_and_bind("set show-all-if-unmodified off")


def print_location(local=None, remote=""):
    print(f"로컬: {Path.cwd() if local is None else local}")
    print(f"S3: {ROOT}{remote + '/' if remote else ''}")


def menu_input(prompt, local_kind=None, location=None):
    while True:
        value = _menu_input_once(prompt, local_kind)
        if value.strip() == "pwd":
            print_location(*(location or (None, "")))
            continue
        # Escape a literal name/search term that would otherwise be a command.
        return "pwd" if value == r"\pwd" else value


def _menu_input_once(prompt, local_kind=None):
    try:
        import readline
    except ImportError:
        return input(prompt)
    previous, delimiters = readline.get_completer(), readline.get_completer_delims()
    try:
        completer = LocalPathCompleter(directory_only=local_kind == "dir") if local_kind else None
        readline.set_completer_delims("")
        readline.set_completer(completer.complete if completer else lambda text, state: None)
        bind_completion(readline)
        return input(prompt)
    finally:
        readline.set_completer(previous)
        readline.set_completer_delims(delimiters)


class S3Completer:
    COMMANDS = ("bookmark", "cat", "cd", "du", "exit", "get", "head", "help", "history", "lcd", "less", "lls", "lpwd", "ls", "put", "pwd", "quit", "search", "stat", "tail", "tree")

    def __init__(self, readline, get_cwd, get_local=Path.cwd, get_bookmarks=lambda: {}):
        self.readline = readline
        self.get_cwd = get_cwd
        self.get_local = get_local
        self.get_bookmarks = get_bookmarks
        self.matches = []
        self.cache = {}

    def paths(self, parent, fragment):
        base = PREFIX + (parent + "/" if parent else "")
        prefix = base + fragment
        cached = self.cache.get(prefix)
        if cached and time.monotonic() - cached[0] < 5:
            return cached[1]
        entries = set()
        token = None
        while True:
            page = list_page(prefix, token=token, delimiter="/")
            for item in page.get("CommonPrefixes", []):
                name = item["Prefix"][len(base):]
                if name.startswith(fragment) and name.endswith("/"):
                    entries.add((name, True))
            for item in page.get("Contents", []):
                name = item["Key"][len(base):]
                if name and name.startswith(fragment) and not name.endswith("/"):
                    entries.add((name, False))
            if not page.get("IsTruncated"):
                break
            next_token = page.get("NextContinuationToken")
            if not next_token or next_token == token:
                raise AWSError("자동완성 목록의 다음 페이지를 조회할 수 없습니다.")
            token = next_token
        if len(self.cache) >= 32:
            self.cache.clear()
        if fragment in (".", ".."):
            entries.update((name, True) for name in ("./", "../") if name.startswith(fragment))
        result = sorted(entries)
        self.cache[prefix] = (time.monotonic(), result)
        return result

    def candidates(self, line):
        stages = split_pipeline(line)
        if len(stages) > 1:
            leading = "|".join(stages[:-1]) + "|"
            prefix, words, path, _ = completion_context(stages[-1])
            if not words:
                return [leading + prefix + command + " " for command in ("head", "less", "tail") if command.startswith(path)]
            if len(words) == 1 and words[0] in ("head", "tail") and "-n".startswith(path):
                return [leading + prefix + "-n "]
            return []
        prefix, words, path, raw = completion_context(line)
        if not words:
            return [prefix + command + " " for command in self.COMMANDS if command.startswith(path)]
        command = words[0]
        if command == "bookmark":
            if len(words) == 1:
                return [prefix + action + " " for action in ("add", "go", "list", "rm") if action.startswith(path)]
            if len(words) == 2 and words[1] in ("go", "rm"):
                return [prefix + completion_quote(name, raw) + " " for name in sorted(self.get_bookmarks()) if name.startswith(path)]
            if not (len(words) == 3 and words[1] == "add"):
                return []
            words = ["cd"]
            command = "cd"
        if command not in ("cd", "cat", "get", "less", "ls", "lcd", "lls", "put", "head", "tail", "tree", "du", "stat"):
            return []
        local = command in ("lcd", "lls", "put")
        directory_only = command in ("cd", "lcd", "tree")
        if command in ("ls", "lls"):
            options = words[1:]
            _, paths = parse_ls_args(options)
            if paths:
                return []
            if "--" not in options and path.startswith("-"):
                return [prefix + option + " " for option in ("-l", "-a", "-t", "-r", "-h", "-la", "-al", "-lh", "-lt", "-ltr", "-ltrh", "-altrh", "--")
                        if option.startswith(path)]
        else:
            from shell_features import parse_options
            allowed = "rn" if command in ("get", "put") else "h" if command == "du" else ""
            valued = ("-n",) if command in ("head", "tail") else ("-L",) if command == "tree" else ()
            if len(words) > 1 and words[-1] in valued:
                return []
            _, _, positional = parse_options(words[1:], allowed=allowed, valued=valued, long_flags=("--dry-run",) if command in ("get", "put") else ())
            if "--" not in words[1:] and path.startswith("-"):
                flags = ["-r", "-n", "-rn", "--dry-run", "--"] if command in ("get", "put") else list(valued) + (["-h"] if command == "du" else [])
                return [prefix + flag + " " for flag in flags if flag.startswith(path)]
            if len(positional) > (1 if command in ("get", "put") else 0):
                return []
            if positional:
                local = command == "get"
                directory_only = False
        parent, separator, fragment = path.rpartition("/")
        typed_parent = parent + separator
        if local:
            return [prefix + completion_quote(name, raw) + ("" if directory else " ")
                    for name, directory in local_path_candidates(path, self.get_local(), directory_only)]
        key = shell_path(self.get_cwd(), typed_parent or ".")
        return [prefix + completion_quote(typed_parent + name, raw) + ("" if is_dir else " ")
                for name, is_dir in self.paths(key, fragment)
                if not directory_only or is_dir]

    def complete(self, text, state):
        if state == 0:
            self.matches = []
            try:
                # Empty delimiters make the entire line the replacement scope. This
                # keeps quoted/escaped paths intact on both GNU readline and libedit.
                self.matches = self.candidates(text)
            except (AWSError, ValueError, OSError) as exc:
                if isinstance(exc, AWSError):
                    print(f"\n자동완성 조회 오류: {exc}", file=sys.stderr)
                    self.readline.redisplay()
            except KeyboardInterrupt:
                pass
        return self.matches[state] if state < len(self.matches) else None


def setup_completion(get_cwd, get_local=Path.cwd, get_bookmarks=lambda: {}):
    try:
        import readline
    except ImportError:
        print("Tab 자동완성을 사용하려면 readline 지원 Python이 필요합니다.", file=sys.stderr)
        return
    completer = S3Completer(readline, get_cwd, get_local, get_bookmarks)
    readline.set_completer_delims("")
    readline.set_completer(completer.complete)
    bind_completion(readline)


def shell_main():
    cwd = ""
    features = ShellFeatures(sys.modules[__name__])
    setup_completion(lambda: features.cwd, lambda: features.local, lambda: features.bookmarks)
    print(f"S3 탐색: {ROOT}\n로컬 다운로드 위치: {Path.cwd()}\n명령 안내: help / 자동완성: Tab / 종료: exit")
    while True:
        try:
            line = input(f"s3:/transfer{'/' + cwd if cwd else ''}> ")
            stages = split_pipeline(line)
            if len(stages) > 1:
                features.history.append(line)
                features.pipeline(stages)
                continue
            words = shlex.split(line)
            if not words:
                continue
            command, *args = words
            features.history.append(line)
            if features.handle(command, args):
                cwd = features.cwd
                continue
            if command in ("exit", "quit"):
                if args:
                    raise ValueError("사용법: exit 또는 quit")
                return 0
            if command in ("pwd", "help"):
                if args:
                    raise ValueError(f"사용법: {command}")
                if command == "pwd":
                    print_location(features.local, features.cwd)
                else:
                    print(SHELL_HELP + "\n" + EXTRA_HELP)
            elif command == "cat":
                if len(args) != 1:
                    raise ValueError("사용법: cat 파일")
                key = shell_path(cwd, args[0])
                if not key or args[0].endswith("/"):
                    raise ValueError("cat에는 디렉터리 대신 파일 경로를 지정하세요.")
                if remote_exists(key, "file"):
                    sys.stdout.flush()
                    # Stream bytes directly; do not hold large S3 objects in Python memory.
                    result = aws("s3", "cp", ROOT + key, "-", "--only-show-errors", capture=False)
                    if result.returncode:
                        print(f"파일 읽기에 실패했습니다 (종료 코드 {result.returncode}).", file=sys.stderr)
                    print()  # Keep the next prompt separate when a file has no final newline.
                else:
                    shell_missing(key, "file")
            elif command == "less":
                if len(args) != 1 or not args[0]:
                    raise ValueError("사용법: less 파일")
                key = shell_path(cwd, args[0])
                if not key or args[0].endswith("/"):
                    raise ValueError("less에는 디렉터리 대신 파일 경로를 지정하세요.")
                if remote_exists(key, "file"):
                    shell_less(key)
                else:
                    shell_missing(key, "file", command="less")
            elif command == "search":
                if len(args) != 1 or not args[0]:
                    raise ValueError("사용법: search 문자열")
                search_main(["--", args[0]])
            else:
                raise ValueError(f"지원하지 않는 명령입니다: {command}. help로 명령을 확인하세요.")
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print("\n현재 명령을 중단했습니다. 종료하려면 exit를 입력하세요.")
        except (AWSError, ValueError, OSError) as exc:
            print(f"오류: {exc}", file=sys.stderr)


def menu_main():
    modes = {
        "1": ("shell", "S3 탐색"),
        "2": ("upload-file", "파일 업로드"),
        "3": ("upload-dir", "디렉터리 업로드"),
        "4": ("download-file", "파일 다운로드"),
        "5": ("download-dir", "디렉터리 다운로드"),
        "6": ("search", "이름 검색"),
    }
    while True:
        try:
            print(f"\nS3 Transfer — {ROOT}\n로컬 작업 위치: {Path.cwd()}")
            for number, (_, label) in modes.items():
                print(f"  {number}. {label}")
            print("  0. 종료")
            print("  pwd: 현재 로컬·S3 경로 확인 (모든 입력 단계에서 사용 가능)")
            choice = menu_input("모드 선택: ").strip().casefold()
            if choice in ("0", "exit", "quit"):
                return 0
            mode = modes.get(choice)
            if mode is None:
                mode = next((item for item in modes.values() if item[0] == choice), None)
            if mode is None:
                print("0~6 중 번호를 선택하세요.")
                continue
            action, _ = mode
            if action == "shell":
                shell_main()
                continue
            prompt = "검색 문자열" if action == "search" else "로컬 경로" if action.startswith("upload") else "transfer/ 아래 상대 경로"
            value = menu_input(f"{prompt} (빈 입력: 메뉴로): ",
                               local_kind=action.split("-")[1] if action.startswith("upload") else None)
            if not value:
                continue
            if action == "search":
                search_main(["--", value])
            else:
                operation, kind = action.split("-")
                main([operation, kind, "--", value])
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print("\n메뉴로 돌아갑니다. 종료하려면 0을 입력하세요.")
        except SystemExit as exc:
            # argparse can reject a path; keep the menu alive after a usage error.
            if exc.code not in (0, 2):
                raise


def cli_main(argv):
    if not argv:
        ensure_configuration()
        return menu_main()
    if argv == ["pwd"]:
        ensure_configuration()
        print_location()
        return 0
    parser = argparse.ArgumentParser(
        prog="s3_transfer", allow_abbrev=False,
        description=f"{ROOT} 업로드·다운로드·검색",
        epilog="옵션 없이 실행하면 모드 선택 메뉴가 열립니다. 대상이 없으면 비슷한 이름을 제안합니다.",
    )
    options = parser.add_mutually_exclusive_group(required=True)
    options.add_argument("--set-bucket", metavar="NAME", help="사용할 버킷 이름 저장·변경 후 종료")
    options.add_argument("--show-config", action="store_true", help="저장된 버킷과 설정 파일 경로 표시")
    options.add_argument("--pwd", action="store_true", help="현재 로컬 경로와 S3 기준 경로 표시")
    options.add_argument("--upload-file", metavar="PATH", help="로컬 파일 업로드")
    options.add_argument("--upload-dir", metavar="PATH", help="로컬 디렉터리 업로드 (sync)")
    options.add_argument("--download-file", metavar="PATH", help="transfer/ 아래 파일 다운로드")
    options.add_argument("--download-dir", metavar="PATH", help="transfer/ 아래 디렉터리 다운로드 (sync)")
    options.add_argument("--search", metavar="TEXT", help="하위 전체의 파일·디렉터리 이름 검색 (대소문자 무시)")
    options.add_argument("--shell", action="store_true", help="cd, ls, cat, less, get을 사용하는 대화형 S3 탐색")
    args = parser.parse_args(argv)
    if args.set_bucket is not None:
        activate_bucket(save_bucket(args.set_bucket))
        print(f"버킷 설정 저장: {config_path()}\nS3: {ROOT}")
        return 0
    if args.show_config:
        name = load_bucket()
        print(f"설정 파일: {config_path()}\n버킷: {name if name else '미설정'}")
        if name:
            print(f"S3: s3://{name}/{PREFIX}")
        return 0
    ensure_configuration()
    if args.pwd:
        print_location()
        return 0
    if args.shell:
        return shell_main()
    if args.search is not None:
        return search_main(["--", args.search])
    for action in ("upload", "download"):
        for kind in ("file", "dir"):
            path = getattr(args, f"{action}_{kind}")
            if path is not None:
                return main([action, kind, "--", path])


def upload_name(path):
    """Use the source basename, including for '.' and paths ending in '/' or '..'."""
    return Path(os.path.abspath(os.path.expanduser(str(path)))).name


def find_similar_local(source, kind):
    """Look beside the requested source, without recursively scanning local disks."""
    source = Path(os.path.abspath(source))
    best = []
    deadline = time.monotonic() + 3
    try:
        with os.scandir(source.parent) as entries:
            for index, item in enumerate(entries):
                if index >= 3000 or time.monotonic() >= deadline:
                    break
                try:
                    matches_kind = item.is_dir() if kind == "dir" else item.is_file()
                except OSError:
                    continue
                if not matches_kind:
                    continue
                score = similarity(source.name, item.name)
                if score >= 0.55:
                    best.append((score, Path(item.path)))
                    best.sort(key=lambda entry: (-entry[0], str(entry[1])))
                    del best[10:]
    except OSError:
        return []
    return [candidate for _, candidate in best]


def confirm_similar_upload(source, kind, destination_for, dry=False, location=None):
    print(f"대상을 찾을 수 없습니다: {source}", file=sys.stderr, flush=True)
    candidates = find_similar_local(source, kind)
    if not candidates:
        print("같은 로컬 디렉터리에 비슷한 원본이 없습니다.", file=sys.stderr)
        suggest(upload_name(source), kind, "upload")
        return None
    candidate = candidates[0]
    destination = destination_for(candidate)
    action = "업로드 미리보기" if dry else "업로드"
    print(f"가장 비슷한 로컬 {'디렉터리를' if kind == 'dir' else '파일을'} 찾았습니다.", file=sys.stderr)
    print(f"  원본: {candidate}\n  목적지: {destination}", file=sys.stderr, flush=True)
    while True:
        try:
            answer = menu_input(f"이 대상을 {action}할까요? [y/N]: ", location=location).strip().casefold()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("", "n"):
            print("업로드를 취소했습니다.", file=sys.stderr)
            return None
        if answer == "y":
            exists = candidate.is_dir() if kind == "dir" else candidate.is_file()
            if not exists:
                print("제안된 원본이 더 이상 없습니다. 입력으로 돌아갑니다.", file=sys.stderr)
                return None
            return candidate
        print("y 또는 n을 입력하세요.", file=sys.stderr)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "cli":
        return cli_main(argv[1:])
    if argv and argv[0] == "search":
        return search_main(argv[1:])
    parser = argparse.ArgumentParser(description="S3 transfer 업로드/다운로드 및 유사 이름 제안")
    parser.add_argument("action", choices=("upload", "download"))
    parser.add_argument("kind", choices=("file", "dir"))
    parser.add_argument("path", help="로컬 경로 또는 transfer/ 아래 상대 경로")
    args = parser.parse_args(argv)
    if not args.path:
        parser.error("파일 또는 디렉터리 경로를 지정하세요.")
    key = upload_name(args.path) if args.action == "upload" else args.path
    while key.startswith("./"):
        key = key[2:]
    if args.kind == "dir":
        key = key.rstrip("/")
    if not key:
        parser.error("파일 또는 디렉터리 경로를 지정하세요.")
    if args.action == "download" and ".." in key.split("/"):
        parser.error("다운로드 경로에는 '..'를 사용할 수 없습니다.")
    local = os.path.expanduser(args.path) if args.action == "upload" else "./" + key
    # Prefix local relative paths so a filename starting with '-' is not an AWS option.
    if not os.path.isabs(local):
        local = "./" + local
    try:
        if args.action == "upload":
            source = Path(local)
            exists = source.is_dir() if args.kind == "dir" else source.is_file()
            if not exists and source.exists():
                print(f"로컬 경로의 종류가 맞지 않습니다 ({args.kind} 필요): {args.path}",
                      file=sys.stderr)
                return 1
        else:
            exists = remote_exists(key, args.kind)
        if not exists:
            if args.action == "upload":
                candidate = confirm_similar_upload(source, args.kind, lambda path: ROOT + upload_name(path))
                if candidate is None:
                    return 1
                local, key = str(candidate), upload_name(candidate)
            else:
                print(f"대상을 찾을 수 없습니다: {ROOT}{key}", file=sys.stderr)
                suggest(key, args.kind, args.action)
                return 1
        remote = ROOT + key
        src, dst = (local, remote) if args.action == "upload" else (remote, local)
        operation = "sync" if args.kind == "dir" else "cp"
        return aws("s3", operation, src, dst, capture=False).returncode
    except AWSError as exc:
        print(f"S3 오류: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n작업을 중단했습니다.", file=sys.stderr)
        return 130


def entrypoint(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        # CLI parsing handles help and configuration options without prompting.
        if argv and argv[0] != "cli" and not any(arg in ("-h", "--help") for arg in argv):
            ensure_configuration()
        return main(argv)
    except EOFError:
        print("\n버킷 입력이 종료되었습니다. --set-bucket 이름으로 먼저 설정하세요.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n작업을 취소했습니다.", file=sys.stderr)
        return 130
    except (ValueError, OSError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(entrypoint())

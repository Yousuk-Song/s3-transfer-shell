"""Additional S3 shell commands; all AWS access goes through the shared backend."""

import codecs
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import sys
import tempfile


HELP = """추가 명령:
  lcd [로컬 경로] / lpwd / lls [-altrh] [경로]   로컬 위치·목록
  cd -                                          이전 S3 위치
  head [-n 줄수] 파일 / tail [-n 줄수] 파일      앞/뒤 일부 읽기 (기본 10줄)
  get [-r] [-n] [--dry-run] S3경로 [로컬경로]    다운로드
  put [-r] [-n] [--dry-run] 로컬경로 [S3경로]    업로드
  ls [-altrh] 패턴 / get 패턴 [로컬디렉터리]     *, ?, [] 패턴 지원
  tree [-L 깊이] [경로]                         하위 구조 (기본 2단계)
  du [-h] [경로]                                전체 파일 수·크기
  stat 파일                                     S3 객체 상세 정보
  bookmark add 이름 [경로] / list / go 이름 / rm 이름
  history [개수]                                이번 셸의 명령 기록
  cat 파일 | less / head [-n 줄수] / tail [-n 줄수]  내용 보기 파이프
전송 -r: 디렉터리, -n: 같은 이름의 대상 파일 건너뛰기, --dry-run: 미리보기.
put 목적지 생략: transfer/ 바로 아래에 원본 파일명·디렉터리명으로 업로드.
lcd는 이 셸의 저장 위치만 변경합니다. 전송은 대상에 없는 파일을 삭제하지 않습니다.
"""


def parse_options(args, allowed="", valued=(), long_flags=()):
    flags, values, paths = set(), {}, []
    index, options = 0, True
    while index < len(args):
        arg = args[index]
        if options and arg == "--":
            options = False
        elif options and arg in long_flags:
            flags.add(arg)
        elif options and arg in valued:
            index += 1
            if index >= len(args):
                raise ValueError(f"{arg} 뒤에 값을 입력하세요.")
            values[arg] = args[index]
        elif options and arg.startswith("-") and arg != "-":
            if arg.startswith("--") or not set(arg[1:]) <= set(allowed):
                raise ValueError(f"지원하지 않는 옵션: {arg}")
            flags.update(arg[1:])
        else:
            paths.append(arg)
        index += 1
    return flags, values, paths


def has_pattern(path):
    return any(char in path for char in "*?[")


def split_pipeline(line):
    """Split unquoted pipes only; preserve raw tokens for shlex and completion."""
    quote, escaped, start = None, False, 0
    stages = []
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
        elif char == "|":
            stages.append(line[start:index])
            start = index + 1
    stages.append(line[start:])
    return stages


def path_matches(key, pattern):
    """A wildcard matches a single path component, never a slash."""
    parts, patterns = key.split("/"), pattern.split("/")
    return len(parts) == len(patterns) and all(fnmatch.fnmatchcase(a, b) for a, b in zip(parts, patterns))


class ShellFeatures:
    def __init__(self, api):
        self.api = api
        self.cwd = ""
        self.previous = None
        self.local = Path.cwd()
        self.history = []
        self.state_dir = Path(os.environ.get("S3_TRANSFER_STATE_DIR", str(api.SCRIPT_DIR / "state")))
        self.bookmarks = {}
        self.bookmark_error = None
        path = self.state_dir / "bookmarks.json"
        if path.exists():
            try:
                data = json.loads(path.read_text())
                if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
                    raise ValueError("북마크 형식 오류")
                self.bookmarks = data
            except (OSError, ValueError) as exc:
                self.bookmark_error = str(exc)
                print(f"북마크를 읽지 못했습니다: {path}: {exc}", file=sys.stderr)

    def resolve(self, path):
        return self.api.shell_path(self.cwd, path)

    def local_path(self, path):
        expanded = Path(path).expanduser()
        return expanded if expanded.is_absolute() else self.local / expanded

    def objects(self, prefix):
        token = None
        while True:
            page = self.api.list_page(self.api.PREFIX + prefix, token=token)
            yield from page.get("Contents", [])
            if not page.get("IsTruncated"):
                return
            next_token = page.get("NextContinuationToken")
            if not next_token or next_token == token:
                raise self.api.AWSError("S3 목록의 다음 페이지를 읽을 수 없습니다.")
            token = next_token

    def directory_objects(self, key):
        prefix = key + "/" if key else ""
        found = False
        for entry in self.objects(prefix):
            found = True
            if not entry["Key"].endswith("/"):
                yield entry
        if key and not found:
            raise ValueError(f"디렉터리를 찾을 수 없습니다: {self.api.ROOT}{key}")

    def metadata(self, key, command):
        if not key:
            raise ValueError("파일 경로를 지정하세요.")
        result = self.api.aws("s3api", "head-object", "--bucket", self.api.BUCKET,
                              "--key", self.api.PREFIX + key, "--output", "json")
        if result.returncode and re.search(r"\((404|NoSuchKey|NotFound)\)", result.stderr):
            self.api.shell_missing(key, "file", command=command)
            return None
        return self.api.read_json(result)

    def expand_remote(self, pattern, dirs=False):
        prefix = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
        matches = {}
        for item in self.objects(prefix):
            key = item["Key"][len(self.api.PREFIX):]
            if not key.endswith("/") and path_matches(key, pattern):
                matches[key] = (key, item.get("Size", 0), item.get("LastModified", "-"))
            if dirs:
                parts = key.split("/")
                for index in range(1, len(parts)):
                    directory = "/".join(parts[:index])
                    if path_matches(directory, pattern):
                        matches[directory + "/"] = (directory + "/", None, None)
        return sorted(matches.values())

    def change_remote(self, path):
        if path == "-":
            if self.previous is None:
                raise ValueError("이전 S3 위치가 없습니다.")
            key = self.previous
        else:
            key = self.resolve(path)
        if not key or self.api.remote_exists(key, "dir"):
            self.previous, self.cwd = self.cwd, key
        else:
            self.api.shell_missing(key, "dir")

    def list_remote(self, args):
        flags, paths = self.api.parse_ls_args(args)
        key = self.resolve(paths[0] if paths else ".")
        options = dict(detailed="l" in flags, time_sort="t" in flags, reverse="r" in flags, human="h" in flags)
        if paths and has_pattern(paths[0]):
            entries = self.expand_remote(key, dirs=True)
            if not entries:
                print("일치하는 항목이 없습니다.")
            self.api.print_listing(entries, **options)
        else:
            self.api.shell_list(key, **options)

    def list_local(self, args):
        flags, paths = self.api.parse_ls_args(args)
        path = self.local_path(paths[0] if paths else ".")
        candidates = list(path.iterdir()) if path.is_dir() else [path]
        entries = []
        for item in candidates:
            stat = item.stat()
            directory = item.is_dir()
            modified = self.api.datetime.fromtimestamp(stat.st_mtime, self.api.timezone.utc).isoformat()
            entries.append((item.name + ("/" if directory else ""), None if directory else stat.st_size, modified))
        self.api.print_listing(entries, detailed="l" in flags, time_sort="t" in flags, reverse="r" in flags, human="h" in flags)

    def range_bytes(self, key, start, end, metadata):
        with tempfile.TemporaryDirectory(prefix="s3-preview-") as temp:
            output = Path(temp) / "chunk"
            args = ["s3api", "get-object", "--bucket", self.api.BUCKET, "--key", self.api.PREFIX + key,
                    "--range", f"bytes={start}-{end}"]
            if metadata.get("ETag"):
                args += ["--if-match", metadata["ETag"]]
            self.api.read_json(self.api.aws(*args, str(output), "--output", "json"))
            data = output.read_bytes()
            if len(data) != end - start + 1:
                raise self.api.AWSError("부분 조회 길이가 맞지 않습니다. 파일 변경 여부를 확인하세요.")
            return data

    def preview(self, command, args):
        _, values, paths = parse_options(args, valued=("-n",))
        if len(paths) != 1:
            raise ValueError(f"사용법: {command} [-n 줄수] 파일")
        count = int(values.get("-n", "10"))
        if count < 0:
            raise ValueError("줄수는 0 이상이어야 합니다.")
        key = self.resolve(paths[0])
        metadata = self.metadata(key, command)
        if metadata is None or not count:
            return
        size = metadata.get("ContentLength", 0)
        if not size:
            return
        block = 65536
        start = 0
        if command == "tail":
            # Locate the start by scanning backward without accumulating the file.
            end, remaining = size, count
            while end:
                offset = max(0, end - block)
                data = self.range_bytes(key, offset, end - 1, metadata)
                if end == size and data.endswith(b"\n"):
                    data = data[:-1]
                for position in range(len(data) - 1, -1, -1):
                    if data[position] == 10:
                        remaining -= 1
                        if remaining == 0:
                            start = offset + position + 1
                            break
                if not remaining:
                    break
                end = offset
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        remaining, last = count, b""
        for offset in range(start, size, block):
            data = self.range_bytes(key, offset, min(size - 1, offset + block - 1), metadata)
            if b"\0" in data:
                raise ValueError("바이너리 데이터가 포함되어 있습니다. get으로 내려받아 확인하세요.")
            if command == "head":
                position = 0
                while remaining:
                    newline = data.find(b"\n", position)
                    if newline < 0:
                        break
                    remaining -= 1
                    position = newline + 1
                if remaining == 0:
                    data = data[:position]
            print(decoder.decode(data), end="", flush=True)
            last = data[-1:]
            if command == "head" and remaining == 0:
                break
        print(decoder.decode(b"", final=True), end="")
        if last != b"\n":
            print()

    def describe(self, args):
        if len(args) != 1:
            raise ValueError("사용법: stat 파일")
        key = self.resolve(args[0])
        metadata = self.metadata(key, "stat")
        if metadata is None:
            return
        print("경로:", self.api.ROOT + key)
        fields = (("ContentLength", "크기 (bytes)"), ("LastModified", "수정 시각"), ("ContentType", "형식"),
                  ("ETag", "ETag"), ("VersionId", "버전"), ("StorageClass", "스토리지 클래스"),
                  ("ServerSideEncryption", "암호화"), ("Metadata", "사용자 메타데이터"))
        for field, label in fields:
            if field in metadata:
                print(f"{label}: {metadata[field]}")

    def tree(self, args):
        _, values, paths = parse_options(args, valued=("-L",))
        if len(paths) > 1:
            raise ValueError("사용법: tree [-L 깊이] [경로]")
        depth = int(values.get("-L", "2"))
        if not 1 <= depth <= 100:
            raise ValueError("깊이는 1~100으로 지정하세요.")
        key = self.resolve(paths[0] if paths else ".")
        if key and not self.api.remote_exists(key, "dir"):
            self.api.shell_missing(key, "dir")
            return
        print(self.api.ROOT + key + ("/" if key else ""))

        def walk(parent, indent, level):
            base = self.api.PREFIX + (parent + "/" if parent else "")
            entries, token = {}, None
            while True:
                page = self.api.list_page(base, token=token, delimiter="/")
                for item in page.get("CommonPrefixes", []):
                    entries[item["Prefix"][len(base):]] = True
                for item in page.get("Contents", []):
                    name = item["Key"][len(base):]
                    if name:
                        entries[name] = name.endswith("/")
                if not page.get("IsTruncated"):
                    break
                next_token = page.get("NextContinuationToken")
                if not next_token or next_token == token:
                    raise self.api.AWSError("트리 목록의 다음 페이지를 읽을 수 없습니다.")
                token = next_token
            ordered = sorted(entries.items(), key=lambda item: (not item[1], item[0]))
            for index, (name, directory) in enumerate(ordered):
                last = index == len(ordered) - 1
                print(indent + ("└── " if last else "├── ") + name)
                if directory and level < depth:
                    child = (parent + "/" if parent else "") + name.rstrip("/")
                    walk(child, indent + ("    " if last else "│   "), level + 1)
        walk(key, "", 1)

    def usage(self, args):
        flags, _, paths = parse_options(args, allowed="h")
        if len(paths) > 1:
            raise ValueError("사용법: du [-h] [경로]")
        key = self.resolve(paths[0] if paths else ".")
        if key and not self.api.remote_exists(key, "dir"):
            metadata = self.metadata(key, "stat")
            if metadata is None:
                return
            count, total = 1, metadata.get("ContentLength", 0)
        else:
            count, total = 0, 0
            for item in self.directory_objects(key):
                count += 1
                total += item.get("Size", 0)
        size = self.api.readable_size(total) if "h" in flags else str(total) + " bytes"
        print(f"{self.api.ROOT}{key}\n파일 {count}개, 전체 {size}")

    def save_bookmarks(self, updated):
        if self.bookmark_error:
            raise ValueError("기존 북마크 파일 오류를 먼저 해결하세요: " + self.bookmark_error)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.state_dir, delete=False) as output:
            temp = Path(output.name)
            json.dump(updated, output, ensure_ascii=False, indent=2)
            output.write("\n")
        try:
            temp.replace(self.state_dir / "bookmarks.json")
            self.bookmarks = updated
        finally:
            temp.unlink(missing_ok=True)

    def bookmark(self, args):
        if args in ([], ["list"]):
            for name, key in sorted(self.bookmarks.items()):
                print(f"{name}\t{self.api.ROOT}{key}")
            if not self.bookmarks:
                print("저장된 북마크가 없습니다.")
            return
        if args[0] == "add" and len(args) in (2, 3):
            name = args[1]
            if not name:
                raise ValueError("북마크 이름을 입력하세요.")
            key = self.resolve(args[2] if len(args) == 3 else ".")
            if key and not self.api.remote_exists(key, "dir"):
                self.api.shell_missing(key, "dir")
                return
            self.save_bookmarks(dict(self.bookmarks, **{name: key}))
            print(f"북마크 저장: {name} → {self.api.ROOT}{key}")
        elif args[0] == "go" and len(args) == 2:
            if args[1] not in self.bookmarks:
                raise ValueError("북마크가 없습니다: " + args[1])
            self.change_remote("/" + self.bookmarks[args[1]])
        elif args[0] == "rm" and len(args) == 2:
            updated = dict(self.bookmarks)
            if args[1] not in updated:
                raise ValueError("북마크가 없습니다: " + args[1])
            del updated[args[1]]
            self.save_bookmarks(updated)
        else:
            raise ValueError("사용법: bookmark add 이름 [경로] | list | go 이름 | rm 이름")

    def copy(self, src, dst, dry, skip):
        remote_destination = str(dst).startswith("s3://")
        if skip:
            exists = self.api.remote_exists(str(dst)[len(self.api.ROOT):], "file") if remote_destination else Path(dst).exists()
            if exists:
                print(f"건너뜀: {dst}")
                return
        print(f"{'미리보기' if dry else '전송'}: {src} → {dst}", flush=True)
        if dry:
            return
        if not remote_destination:
            target = Path(dst)
            if target.is_dir():
                raise ValueError(f"같은 이름의 로컬 디렉터리가 있습니다: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
        result = self.api.aws("s3", "cp", str(src), str(dst), capture=False)
        if result.returncode:
            raise self.api.AWSError(f"{'업로드' if remote_destination else '다운로드'}에 실패했습니다 (종료 코드 {result.returncode}).")
        print(f"{'업로드' if remote_destination else '다운로드'} 완료: {dst}")

    def safe_download_target(self, base, relative):
        parts = PurePosixPath(relative).parts
        if not parts or any(part in ("..", "/") for part in parts):
            raise ValueError(f"로컬 저장 경로로 사용할 수 없는 S3 키: {relative}")
        target = base.joinpath(*parts)
        root = base.resolve()
        resolved = target.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"다운로드 디렉터리 밖을 가리키는 경로: {target}")
        return target

    def transfer(self, command, args):
        flags, _, paths = parse_options(args, allowed="rn", long_flags=("--dry-run",))
        if not 1 <= len(paths) <= 2 or not paths[0]:
            raise ValueError(f"사용법: {command} [-r] [-n] [--dry-run] 원본 [대상]")
        recursive, dry, skip = "r" in flags, "--dry-run" in flags, "n" in flags
        source = paths[0]
        destination = paths[1] if len(paths) == 2 else None
        if command == "put":
            local = self.local_path(source)
            def upload_key(path):
                name = self.api.upload_name(path)
                key = self.resolve(destination) if destination is not None else name
                if not recursive and destination and destination.endswith("/"):
                    key = key + "/" + name if key else name
                if not key:
                    raise ValueError("업로드 대상 이름을 지정하세요.")
                return key
            if not local.exists():
                local = self.api.confirm_similar_upload(local, "dir" if recursive else "file",
                                                        lambda path: self.api.ROOT + upload_key(path), dry=dry,
                                                        location=(self.local, self.cwd))
                if local is None:
                    return
            if not (local.is_dir() if recursive else local.is_file()):
                raise ValueError("로컬 원본 종류를 확인하세요. 디렉터리는 put -r로 지정하세요.")
            key = upload_key(local)
            if recursive:
                for item in sorted(local.rglob("*")):
                    if item.is_symlink():
                        print(f"심볼릭 링크 건너뜀: {item}")
                    elif item.is_file():
                        remote = self.api.ROOT + key + "/" + item.relative_to(local).as_posix()
                        self.copy(item, remote, dry, skip)
            else:
                self.copy(local, self.api.ROOT + key, dry, skip)
            return
        key = self.resolve(source)
        if recursive:
            if has_pattern(source):
                raise ValueError("get -r에는 패턴 대신 디렉터리 경로를 지정하세요.")
            base = self.local_path(destination) if destination is not None else self.local / (key.rsplit("/", 1)[-1] if key else "transfer")
            prefix = self.api.PREFIX + (key + "/" if key else "")
            for item in self.directory_objects(key):
                relative = item["Key"][len(prefix):]
                target = self.safe_download_target(base, relative)
                self.copy("s3://" + self.api.BUCKET + "/" + item["Key"], target, dry, skip)
            return
        if has_pattern(source):
            entries = self.expand_remote(key)
            if not entries:
                print("일치하는 파일이 없습니다.")
                return
            base = self.local_path(destination) if destination is not None else self.local
            if base.exists() and not base.is_dir():
                raise ValueError("패턴 다운로드 대상은 로컬 디렉터리여야 합니다.")
            names = [entry[0].rsplit("/", 1)[-1] for entry in entries]
            if len(names) != len(set(names)):
                raise ValueError("같은 파일명이 여러 경로에 있습니다. 더 좁은 패턴이나 get -r을 사용하세요.")
            for entry, name in zip(entries, names):
                self.copy(self.api.ROOT + entry[0], self.safe_download_target(base, name), dry, skip)
            return
        if not key or source.endswith("/"):
            raise ValueError("파일 경로를 지정하세요. 디렉터리는 get -r로 받으세요.")
        if not self.api.remote_exists(key, "file"):
            self.api.shell_missing(key, "file", command="get")
            return
        target = self.local_path(destination) if destination is not None else self.local / key.rsplit("/", 1)[-1]
        if destination is not None and (target.is_dir() or destination.endswith("/")):
            target = target / key.rsplit("/", 1)[-1]
        self.copy(self.api.ROOT + key, target, dry, skip)

    def handle(self, command, args):
        if command == "cd":
            if len(args) > 1:
                raise ValueError("사용법: cd [경로|-]")
            self.change_remote(args[0] if args else "/")
        elif command == "lcd":
            if len(args) > 1:
                raise ValueError("사용법: lcd [로컬 경로]")
            path = self.local_path(args[0] if args else "~").resolve()
            if not path.is_dir():
                raise ValueError(f"로컬 디렉터리가 없습니다: {path}")
            self.local = path
            print(self.local)
        elif command == "lpwd":
            if args:
                raise ValueError("사용법: lpwd")
            print(self.local)
        elif command == "lls":
            self.list_local(args)
        elif command == "ls":
            self.list_remote(args)
        elif command in ("head", "tail"):
            self.preview(command, args)
        elif command == "stat":
            self.describe(args)
        elif command == "tree":
            self.tree(args)
        elif command == "du":
            self.usage(args)
        elif command == "bookmark":
            self.bookmark(args)
        elif command == "history":
            if len(args) > 1:
                raise ValueError("사용법: history [개수]")
            count = int(args[0]) if args else len(self.history)
            if count < 0:
                raise ValueError("개수는 0 이상이어야 합니다.")
            start = max(0, len(self.history) - count)
            for index in range(start, len(self.history)):
                print(f"{index + 1:>5}  {self.history[index]}")
        elif command in ("get", "put"):
            self.transfer(command, args)
        else:
            return False
        return True

    def pipeline(self, stages):
        if len(stages) != 2 or any(not stage.strip() for stage in stages):
            raise ValueError("사용법: cat 파일 | less 또는 cat 파일 | head/tail [-n 줄수]")
        source, target = (shlex.split(stage) for stage in stages)
        if len(source) != 2 or source[0] != "cat" or not target or target[0] not in ("less", "head", "tail"):
            raise ValueError("지원하는 파이프: cat 파일 | less, cat 파일 | head/tail [-n 줄수]")
        if target[0] in ("head", "tail"):
            # Parse filter options first so extra paths cannot override the source.
            _, values, paths = parse_options(target[1:], valued=("-n",))
            if paths:
                raise ValueError("파이프 뒤 head/tail에는 파일명 없이 -n 줄수만 지정하세요.")
            self.preview(target[0], ["-n", values.get("-n", "10"), "--", source[1]])
        else:
            if len(target) != 1:
                raise ValueError("사용법: cat 파일 | less")
            key = self.resolve(source[1])
            if not key or source[1].endswith("/"):
                raise ValueError("파일 경로를 지정하세요.")
            if self.api.remote_exists(key, "file"):
                self.api.shell_less(key)
            else:
                self.api.shell_missing(key, "file", command="less")

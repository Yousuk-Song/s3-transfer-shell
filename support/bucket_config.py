"""Persist the selected bucket without storing AWS credentials."""

import json
import os
from pathlib import Path
import re
import tempfile


def config_path():
    return Path(os.environ.get("S3_TRANSFER_STATE_DIR", str(Path(__file__).resolve().parent / "state"))).expanduser() / "config.json"


def validate_bucket(value):
    if not isinstance(value, str):
        raise ValueError("버킷 이름은 문자열이어야 합니다.")
    name = value.strip()
    if (not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", name)
            or ".." in name or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", name)
            or name.startswith(("xn--", "sthree-", "amzn-s3-demo-"))
            or name.endswith(("-s3alias", "--ol-s3", ".mrap", "--x-s3", "--table-s3"))):
        raise ValueError("일반 S3 버킷 이름을 입력하세요 (3~63자, 소문자·숫자·점·하이픈). s3:// 또는 /transfer/는 제외하세요.")
    return name


def load_bucket():
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("설정은 JSON 객체여야 합니다.")
        return validate_bucket(data.get("bucket"))
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as exc:
        raise ValueError(f"버킷 설정을 읽지 못했습니다: {path}. --set-bucket 이름으로 다시 설정하세요. ({exc})") from exc


def save_bucket(value):
    name = validate_bucket(value)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            staged = Path(output.name)
            json.dump({"bucket": name}, output, ensure_ascii=False, indent=2)
            output.write("\n")
        staged.replace(path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    return name

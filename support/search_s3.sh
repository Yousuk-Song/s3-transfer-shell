#!/usr/bin/env bash
# Usage: bash search_s3.sh '검색 문자열'
exec python3 "$(dirname -- "$0")/s3_transfer.py" search "$@"

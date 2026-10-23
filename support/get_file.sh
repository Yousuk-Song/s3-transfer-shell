#!/usr/bin/env bash
exec python3 "$(dirname -- "$0")/s3_transfer.py" download file "$@"

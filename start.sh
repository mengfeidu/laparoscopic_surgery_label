#!/usr/bin/env bash
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  printf '%s\n' "需要 Python 3.10 或更新版本。" >&2
  exit 1
fi
if ! "$PYTHON_BIN" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  printf '%s\n' "需要 Python 3.10 或更新版本；可用 PYTHON_BIN 指定解释器。" >&2
  exit 1
fi
for bin in ffmpeg ffprobe; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    printf '%s\n' "缺少 $bin。macOS：brew install ffmpeg；Ubuntu：sudo apt-get install ffmpeg。" >&2
    exit 1
  fi
done
if [[ ! -x .venv/bin/python ]]; then
  "$PYTHON_BIN" -m venv .venv
fi
# The first launch installs the project; later launches reuse the environment.
if ! .venv/bin/python -c 'import requests, PIL, jsonschema, dotenv; import importlib.metadata as m; from laparoscopy_5hz import __version__; assert m.version("laparoscopy-5hz") == __version__' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -e .
fi
if [[ $# -eq 0 ]]; then
  exec .venv/bin/python -m laparoscopy_5hz --help
fi
case "$1" in
  doctor|annotate|validate|schema|estimate|--help|-h|--version)
    exec .venv/bin/python -m laparoscopy_5hz "$@"
    ;;
  *)
    exec .venv/bin/python -m laparoscopy_5hz annotate "$@"
    ;;
esac

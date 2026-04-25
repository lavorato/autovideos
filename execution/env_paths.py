"""
Resolved folder paths for the video pipeline. Values come from the repo-root .env
(VIDEOS_*), defaulting to the conventional layout. Relative values are joined to
REPO_ROOT so the pipeline works from any current working directory.
"""
from __future__ import annotations

import os

_EXEC_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(_EXEC_DIR, ".."))


def _resolve(name: str, default: str) -> str:
    raw = (os.environ.get(name) or default).strip()
    if not raw:
        raw = default
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(REPO_ROOT, raw))


def input_dir() -> str:
    return _resolve("VIDEOS_INPUT_DIR", "input")


def output_dir() -> str:
    return _resolve("VIDEOS_OUTPUT_DIR", "output")


def tmp_dir() -> str:
    return _resolve("VIDEOS_TMP_DIR", ".tmp")


def logs_pipeline_dir() -> str:
    return _resolve("VIDEOS_LOG_DIR", "logs/pipeline")


def assets_dir() -> str:
    return _resolve("VIDEOS_ASSETS_DIR", "assets")


def music_dir() -> str:
    return _resolve("VIDEOS_MUSIC_DIR", "music")


def fx_dir() -> str:
    raw = (os.environ.get("VIDEOS_FX_DIR") or os.environ.get("FX_DIR") or "fxs").strip()
    if not raw:
        raw = "fxs"
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(REPO_ROOT, raw))


def bgs_dir() -> str:
    return _resolve("VIDEOS_BGS_DIR", "bgs")

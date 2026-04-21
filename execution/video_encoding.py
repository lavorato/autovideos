"""
Shared video encoding settings for quality/color preservation.
"""
from __future__ import annotations

import json
import os
import subprocess


def first_existing_nonempty_video(candidates: list[str]) -> str | None:
    """
    Return the first path that exists as a regular file with size > 0.
    Used to skip corrupt or interrupted outputs (e.g. empty *_hardcut.mp4).
    """
    for path in candidates:
        if not path:
            continue
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return path
        except OSError:
            continue
    return None


def _probe_color_metadata(video_path: str) -> dict:
    """Read source color metadata from the first video stream."""
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=color_space,color_transfer,color_primaries,color_range",
            "-of",
            "json",
            video_path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(probe.stdout or "{}")
    streams = data.get("streams") or [{}]
    return streams[0] if streams else {}


def _build_color_args(stream_info: dict) -> list[str]:
    color_args: list[str] = []
    for ff_key, src_key in [
        ("-colorspace", "color_space"),
        ("-color_trc", "color_transfer"),
        ("-color_primaries", "color_primaries"),
        ("-color_range", "color_range"),
    ]:
        value = stream_info.get(src_key)
        if value and value != "unknown":
            color_args.extend([ff_key, str(value)])
    return color_args


def build_lossless_x264_args(video_path: str) -> list[str]:
    """
    Build FFmpeg args for lossless x264 while preserving source color tags.
    """
    stream_info = _probe_color_metadata(video_path)
    return [
        "-c:v",
        "libx264",
        "-preset",
        "veryslow",
        "-crf",
        "0",
        *_build_color_args(stream_info),
    ]


def build_moviepy_lossless_params(video_path: str) -> list[str]:
    """
    Build MoviePy ffmpeg_params to match FFmpeg lossless/color settings.
    """
    stream_info = _probe_color_metadata(video_path)
    return [
        "-crf",
        "0",
        *_build_color_args(stream_info),
    ]

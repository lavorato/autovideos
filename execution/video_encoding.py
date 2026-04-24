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


def build_fast_hq_x264_args(
    video_path: str,
    crf: int = 17,
    preset: str = "veryfast",
) -> list[str]:
    """
    Build FFmpeg args for a visually-lossless x264 encode that is dramatically
    faster than lossless (crf 0 / veryslow). CRF 17 at veryfast is visually
    indistinguishable from the source for intermediate pipeline steps while
    running 20–50x faster on 4K footage.
    """
    stream_info = _probe_color_metadata(video_path)
    return [
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        *_build_color_args(stream_info),
    ]


def _videotoolbox_h264_available() -> bool:
    """Detect whether ffmpeg was built with the VideoToolbox h264 encoder."""
    try:
        probe = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    return "h264_videotoolbox" in (probe.stdout or "")


def build_videotoolbox_h264_args(
    video_path: str,
    quality: int = 75,
) -> list[str]:
    """
    Build FFmpeg args for macOS hardware-accelerated H.264 via VideoToolbox.
    Much faster than libx264 (offloads to the GPU/Media Engine). Quality is
    a 1-100 scale; 75 is visually lossless for intermediate pipeline steps.
    """
    stream_info = _probe_color_metadata(video_path)
    return [
        "-c:v",
        "h264_videotoolbox",
        "-q:v",
        str(quality),
        "-b:v",
        "0",
        "-pix_fmt",
        "yuv420p",
        *_build_color_args(stream_info),
    ]


def source_color_normalize_filter(video_path: str) -> str:
    """
    FFmpeg filter chain fragment: force yuv420p and re-apply the source stream's
    color metadata via setparams. Use on the main video before ``overlay`` so
    FFmpeg does not implicitly renegotiate colorspace against overlay inputs
    (e.g. Remotion/bt709 clips). Same behavior as step 08c b-roll composite.
    """
    stream_info = _probe_color_metadata(video_path)
    parts = ["format=yuv420p"]
    mapping = {
        "color_primaries": "color_primaries",
        "color_trc": "color_trc",
        "colorspace": "color_space",
        "range": "color_range",
    }
    setparams_kv: list[str] = []
    for ff_key, src_key in mapping.items():
        value = stream_info.get(src_key)
        if value and value != "unknown":
            setparams_kv.append(f"{ff_key}={value}")
    if setparams_kv:
        parts.append("setparams=" + ":".join(setparams_kv))
    return ",".join(parts)


def build_color_preserving_composite_encode_args(video_path: str) -> list[str]:
    """
    Encoder args for overlay/composite passes that must match step 08c output.

    Avoid ``h264_videotoolbox`` here: on macOS it can drop or mishandle
    color_primaries / color_trc / colorspace in a way that visibly shifts
    colors vs. the rest of the pipeline. libx264 at crf 16 (veryfast) matches
    the b-roll composite and keeps tagged color metadata consistent.
    """
    return build_fast_hq_x264_args(video_path, crf=16, preset="veryfast")


def build_fast_pipeline_encode_args(video_path: str) -> list[str]:
    """
    Preferred fast-but-high-quality encoder for intermediate pipeline steps.
    Uses VideoToolbox when available (macOS) and falls back to libx264
    veryfast + crf 17 otherwise. Both are visually lossless in practice but
    orders of magnitude faster than the true-lossless build used for archival.

    Set env VIDEO_ENCODER=x264 to force software libx264 even on macOS.
    """
    if os.environ.get("VIDEO_ENCODER", "").lower() == "x264":
        return build_fast_hq_x264_args(video_path)
    if _videotoolbox_h264_available():
        return build_videotoolbox_h264_args(video_path)
    return build_fast_hq_x264_args(video_path)


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

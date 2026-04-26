"""
Shared video encoding settings for quality/color preservation.
"""
from __future__ import annotations

import json
import math
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


def probe_first_audio_codec_name(video_path: str) -> str | None:
    """Return ``codec_name`` of the first audio stream, or ``None`` if missing."""
    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=nw=1:nk=1",
                video_path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        return None
    name = (probe.stdout or "").strip()
    return name or None


def ensure_mp4_aac_stereo_48k(video_path: str) -> bool:
    """
    If the first audio stream is not AAC, remux in place: copy video, re-encode
    audio to AAC-LC 192k stereo 48kHz, ``+faststart``. This matches what phones,
    Android, and Instagram expect in MP4 (ALAC and other lossless tracks often
    play on desktop only).

    Returns True if a remux was performed, False if the file was already AAC
    or had no audio. Raises on ffmpeg failure.
    """
    if not video_path or not os.path.isfile(video_path):
        return False
    codec = probe_first_audio_codec_name(video_path)
    if codec is None or codec == "aac":
        return False

    root, ext = os.path.splitext(video_path)
    temp_out = f"{root}.aac_stereo_48k_tmp{ext or '.mp4'}"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        video_path,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        temp_out,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        if os.path.exists(temp_out):
            try:
                os.remove(temp_out)
            except OSError:
                pass
        err = (e.stderr or "")[-800:]
        raise RuntimeError(
            f"ensure_mp4_aac_stereo_48k failed for {video_path!r} (was {codec}): {err}"
        ) from e

    os.replace(temp_out, video_path)
    print(
        f"[encode] Re-encoded audio to AAC 48kHz stereo (was {codec}): {video_path}"
    )
    return True


def whole_len_samples_48k(duration_sec: float) -> int:
    """
    Channel sample count to cover *duration_sec* at 48 kHz (ceiling)
    for FFmpeg ``apad=whole_len=...``.
    """
    return max(1, int(math.ceil(float(duration_sec) * 48000.0)))


def probe_stream_duration_seconds(video_path: str, stream_selector: str) -> float | None:
    """
    Return duration in seconds for ``v:0`` or ``a:0``, or None if the stream
    is missing or probe fails.
    """
    if not video_path or not os.path.isfile(video_path):
        return None
    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                stream_selector,
                "-show_entries",
                "stream=duration",
                "-of",
                "default=nw=1:nk=1",
                video_path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        return None
    raw = (p.stdout or "").strip()
    if not raw or raw in ("N/A", "0"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def probe_primary_video_length_seconds(video_path: str) -> float | None:
    """
    Best estimate of the first video track length in seconds. Uses stream
    *duration* when set; *format* duration; and ``nb_frames / r_frame_rate`` when
    stream duration is N/A in the container. Take the max of all candidates so
    we never under-estimate when ``format=duration`` incorrectly tracks a short
    audio stream (A/V desync in bad muxes).
    """
    if not video_path or not os.path.isfile(video_path):
        return None
    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=duration,nb_frames,r_frame_rate",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                video_path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        return None
    data = json.loads(p.stdout or "{}")
    st = (data.get("streams") or [{}])[0] or {}
    fmt = data.get("format") or {}
    cands: list[float] = []
    d = st.get("duration")
    if d not in (None, "", "N/A", "0"):
        try:
            cands.append(float(d))
        except (TypeError, ValueError):
            pass
    fd = fmt.get("duration")
    if fd not in (None, "", "N/A", "0"):
        try:
            cands.append(float(fd))
        except (TypeError, ValueError):
            pass
    nbf = st.get("nb_frames")
    rate = str(st.get("r_frame_rate") or st.get("avg_frame_rate") or "30/1")
    if nbf and str(nbf).isdigit() and int(nbf) > 0 and rate not in ("0/0", "unknown", ""):
        try:
            a, b = rate.split("/")
            fden = float(b) if float(b) else 1.0
            fps = float(a) / fden
            if fps > 0:
                cands.append(int(nbf) / fps)
        except (ValueError, ZeroDivisionError):
            pass
    if not cands:
        return None
    return max(cands)


def verify_mp4_av_streams(
    video_path: str, max_gap_sec: float = 0.25
) -> tuple[bool, str]:
    """
    Return (True, short_message) if OK; (False, reason) if video+audio exist
    and |v - a| > *max_gap_sec*.

    Non-MP4 or missing file is OK. No audio (music-only or silent) returns OK
    (cannot compare). V-only is OK. Both streams must be present to enforce.
    """
    if not video_path or not os.path.isfile(video_path):
        return (True, "")
    if not str(video_path).lower().endswith((".mp4", ".m4v", ".mov")):
        return (True, "")
    v = probe_stream_duration_seconds(video_path, "v:0")
    v_rob = probe_primary_video_length_seconds(video_path)
    if v_rob is not None:
        v = v_rob if v is None else max(v, v_rob)
    a = probe_stream_duration_seconds(video_path, "a:0")
    if v is None or a is None:
        return (True, "")
    diff = abs(v - a)
    if diff <= max_gap_sec:
        return (True, f"A/V OK: v={v:.3f}s a={a:.3f}s")
    return (
        False,
        f"A/V length mismatch: video={v:.3f}s audio={a:.3f}s (|Δ|={diff:.2f}s)",
    )

"""
Step 0: Transcode heavy source formats (.MOV / .MKV / .AVI) to a compact,
visually-lossless .mp4 so every downstream step runs on a much smaller file.

Why:
    iPhone .MOV sources are typically HEVC 10-bit 4K with bitrates near
    25 Mbps. Every downstream OpenCV/MoviePy/FFmpeg step has to decode that,
    and 10-bit HEVC decoding is 2–3x slower than 8-bit H.264. By transcoding
    once, up-front, to H.264 yuv420p + AAC at a visually-lossless setting,
    the rest of the pipeline runs dramatically faster on a smaller file.

Encoder choice — why libx264 veryfast CRF 20 instead of VideoToolbox:
    VideoToolbox H.264 is faster at the encode step itself, but for iPhone
    HEVC 10-bit sources it produces H.264 outputs that are often *larger*
    than the original (10-bit HEVC is much more efficient). Since this step
    runs once and every later step benefits from the smaller file, we favor
    output size over encode speed here. libx264 veryfast CRF 20 is
    visually transparent and typically ~20–30% smaller than the iPhone MOV.

Behavior:
    - Always runs on .mov/.mkv/.avi sources (heavy codecs).
    - For other formats (.mp4/.webm/...), the source is probed and transcoded
      only if its long edge exceeds 1920 px; otherwise the bytes are copied
      (no re-encode) to .tmp/{base}.mp4 so 00b and the rest of the pipeline
      always see a single working path under .tmp/.
    - Output: .tmp/{base}.mp4  (same base as the source → downstream tmp paths
      like .tmp/{base}_transcript.json remain unchanged)
    - Reuses an existing non-empty output (fast re-runs).
    - Resolution: output is normalized to 1920x1080 (landscape or square) or
      1080x1920 (portrait), preserving aspect with letter/pillar padding.
      Sources below that frame are upscaled; larger sources are scaled down
      to fit. The long-edge cap SOURCE_MAX_LONG_EDGE (default 1920) only
      affects when we skip the copy path for non-heavy files (unchanged).
    - Audio: re-encoded to AAC 256k (transparent for speech) to guarantee
      MP4 container compatibility regardless of source codec.

Environment overrides:
    SOURCE_TRANSCODE_CRF  libx264 CRF value (default: 20).
    SOURCE_TRANSCODE_PRESET libx264 preset (default: veryfast).
    SOURCE_MAX_LONG_EDGE  Long-edge cap in pixels (default: 1920).

Returns the path that downstream steps should use (always .tmp/{base}.mp4
when the input lived elsewhere, or the same path if the input is already
that file).
"""
import json
import os
import shutil
import sys
import subprocess

import env_paths
from video_encoding import build_fast_hq_x264_args


HEAVY_EXTENSIONS = {".mov", ".mkv", ".avi"}

# Standard HD working frame (landscape / portrait). Undersized sources are
# upscaled; larger sources are fit inside with pad.
TARGET_LW, TARGET_LH = 1920, 1080
TARGET_PW, TARGET_PH = 1080, 1920


def _size_mb(path: str) -> float:
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return 0.0


def _probe_dimensions(video_path: str) -> tuple[int, int]:
    """Return (width, height) of the first video stream, or (0, 0) on failure."""
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "json",
                video_path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return (0, 0)
    data = json.loads(probe.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        return (0, 0)
    return (int(streams[0].get("width") or 0), int(streams[0].get("height") or 0))


def _is_undersized_hd(width: int, height: int) -> bool:
    """True if the frame is smaller than the pipeline HD target (upscale)."""
    if width <= 0 or height <= 0:
        return False
    if width >= height:
        return width < TARGET_LW or height < TARGET_LH
    return width < TARGET_PW or height < TARGET_PH


def _fit_hd_scale_filter(width: int, height: int) -> str:
    """
    scale + pad to 1920x1080 (landscape) or 1080x1920 (portrait), aspect preserved.
    """
    if width >= height:
        tw, th = TARGET_LW, TARGET_LH
    else:
        tw, th = TARGET_PW, TARGET_PH
    return (
        f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
        f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )


def convert_source(video_path: str, tmp_dir: str | None = None) -> str:
    if tmp_dir is None:
        tmp_dir = env_paths.tmp_dir()
    ext = os.path.splitext(video_path)[1].lower()
    base = os.path.splitext(os.path.basename(video_path))[0]
    os.makedirs(tmp_dir, exist_ok=True)
    output_path = os.path.join(tmp_dir, f"{base}.mp4")
    in_abs = os.path.abspath(video_path)
    out_abs = os.path.abspath(output_path)

    # Input is already the canonical .tmp working file — nothing to create.
    if in_abs == out_abs:
        print(f"[00] Source already at working path: {video_path}")
        return video_path

    # Reuse a non-empty .tmp copy from a previous run.
    if os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
        in_mb = _size_mb(video_path)
        out_mb = _size_mb(output_path)
        print(
            f"[00] Reusing cached working source: {output_path} "
            f"({out_mb:.1f} MB; original {in_mb:.1f} MB)"
        )
        return output_path

    max_long_edge = int(os.getenv("SOURCE_MAX_LONG_EDGE", "1920"))
    width, height = _probe_dimensions(video_path)
    long_edge = max(width, height)
    oversized = long_edge > max_long_edge
    undersized = _is_undersized_hd(width, height)
    is_heavy = ext in HEAVY_EXTENSIONS

    if not is_heavy and not oversized and not undersized:
        if long_edge:
            print(
                f"[00] Source is {ext or 'unknown'} at {width}x{height} "
                f"(long edge {long_edge} <= {max_long_edge}); "
                f"copying to {output_path} (no re-encode)."
            )
        else:
            print(
                f"[00] Source is {ext or 'unknown'}; no transcode — copying to {output_path}."
            )
        shutil.copy2(video_path, output_path)
        return output_path

    in_mb = _size_mb(video_path)
    crf = int(os.getenv("SOURCE_TRANSCODE_CRF", "20"))
    preset = os.getenv("SOURCE_TRANSCODE_PRESET", "veryfast")
    if is_heavy:
        reason = "heavy format"
    elif oversized and undersized:
        reason = f"size adjust ({width}x{height}, long edge {long_edge}px vs {max_long_edge}px)"
    elif oversized:
        reason = f"oversized ({long_edge}px > {max_long_edge}px)"
    else:
        out_frame = f"{TARGET_LW}x{TARGET_LH}" if width >= height else f"{TARGET_PW}x{TARGET_PH}"
        reason = f"below HD target ({width}x{height} -> {out_frame})"
    print(
        f"[00] Transcoding source ({reason}) for faster pipeline processing: {video_path} "
        f"({in_mb:.1f} MB, {width}x{height}) -> libx264 preset={preset} crf={crf}"
    )

    video_args = build_fast_hq_x264_args(video_path, crf=crf, preset=preset)
    # Normalize to 1920x1080 or 1080x1920 (upscale if undersized, downscale if larger).
    scale_filter = _fit_hd_scale_filter(width, height)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats",
        "-i", video_path,
        "-vf", scale_filter,
        *video_args,
        "-c:a", "aac", "-b:a", "256k",
        "-movflags", "+faststart",
        output_path,
    ]

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        raise RuntimeError(f"[00] FFmpeg transcode failed with exit code {e.returncode}") from e

    out_mb = _size_mb(output_path)
    ratio = (out_mb / in_mb) if in_mb > 0 else 0.0
    savings = max(0.0, 1.0 - ratio)
    print(
        f"[00] Output: {output_path} "
        f"({out_mb:.1f} MB, {ratio:.0%} of original, {savings:.0%} smaller)"
    )
    return output_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 00_convert_source.py <video_path>")
        sys.exit(1)
    convert_source(sys.argv[1])

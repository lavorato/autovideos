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
    - Only runs on .mov/.mkv/.avi sources. Already-compact formats (.mp4/.webm)
      pass through untouched.
    - Output: .tmp/{base}.mp4  (same base as the source → downstream tmp paths
      like .tmp/{base}_transcript.json remain unchanged)
    - Reuses an existing non-empty output (fast re-runs).
    - Audio: re-encoded to AAC 256k (transparent for speech) to guarantee
      MP4 container compatibility regardless of source codec.

Environment overrides:
    SOURCE_TRANSCODE_CRF  libx264 CRF value (default: 20).
    SOURCE_TRANSCODE_PRESET libx264 preset (default: veryfast).

Returns the path that downstream steps should use (converted mp4 when
transcoding happened, original path otherwise).
"""
import os
import sys
import subprocess

from video_encoding import build_fast_hq_x264_args


HEAVY_EXTENSIONS = {".mov", ".mkv", ".avi"}


def _size_mb(path: str) -> float:
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return 0.0


def convert_source(video_path: str, tmp_dir: str = ".tmp") -> str:
    ext = os.path.splitext(video_path)[1].lower()
    base = os.path.splitext(os.path.basename(video_path))[0]
    os.makedirs(tmp_dir, exist_ok=True)
    output_path = os.path.join(tmp_dir, f"{base}.mp4")

    if ext not in HEAVY_EXTENSIONS:
        print(f"[00] Source is {ext or 'unknown'}; no transcode needed, using original.")
        return video_path

    if os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
        in_mb = _size_mb(video_path)
        out_mb = _size_mb(output_path)
        print(
            f"[00] Reusing cached transcoded source: {output_path} "
            f"({out_mb:.1f} MB; original {in_mb:.1f} MB)"
        )
        return output_path

    in_mb = _size_mb(video_path)
    crf = int(os.getenv("SOURCE_TRANSCODE_CRF", "20"))
    preset = os.getenv("SOURCE_TRANSCODE_PRESET", "veryfast")
    print(
        f"[00] Transcoding source for faster pipeline processing: {video_path} "
        f"({in_mb:.1f} MB) -> libx264 preset={preset} crf={crf}"
    )

    video_args = build_fast_hq_x264_args(video_path, crf=crf, preset=preset)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats",
        "-i", video_path,
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

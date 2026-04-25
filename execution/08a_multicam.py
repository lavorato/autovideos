"""
Step 8a: Multi-cam intercalation — alternates the main video with a second
camera angle (file named ``{base}_cam02.<ext>``) every ~5 seconds, using the
main video's audio throughout.

Lookup:
    - Main video: the latest non-empty pipeline output found among
      ``.tmp/{base}_effects.mp4`` (step 08), ``_color.mp4`` (07),
      ``_fixed_audio.mp4`` (05), ``_studio.mp4`` (04), then ``video_path``.
    - Cam02 video: ``input/{base}_cam02.<ext>`` (mp4/mov/mkv/avi/webm), with
      a fallback to the same folder the main video comes from.

Behavior:
    - Skips entirely (returns "") when no cam02 file exists — user has a
      single-camera setup.
    - Skips when the main video is shorter than ``2 * CUT_INTERVAL``, since
      there's no room to alternate with "at least 5 seconds each".
    - Enforces a minimum segment length of ``CUT_INTERVAL`` seconds; if the
      final remainder would be shorter, it is merged into the previous
      segment so no segment is shorter than 5s (except the final one if the
      whole video is shorter than two full segments, which is skipped).
    - Assumes cam02 is already time-aligned with the main video (simultaneous
      recording). When cam02 is shorter than the main video, any segment
      whose slot would exceed cam02's duration falls back to the main video
      for continuity.
    - Cam02 frames are letterboxed (scale + pad) to the main video's
      dimensions so concat can't mismatch resolutions.
    - Main video's audio track is preserved; cam02 audio is ignored.

Output: ``.tmp/{base}_multicam.mp4``
"""
import sys
import os
import json
import math
import platform
import subprocess

import env_paths
from video_encoding import build_fast_pipeline_encode_args, first_existing_nonempty_video


# Minimum seconds per segment for either camera. The user asked for "at
# least 5 seconds each video"; we treat this as the exact cut interval and
# absorb any short remainder into the previous segment.
CUT_INTERVAL = 5.0

CAM02_EXTENSIONS = (".mp4", ".mov", ".mkv", ".avi", ".webm")


def find_cam02(video_path: str) -> str | None:
    """Locate ``{base}_cam02.<ext>`` next to the source or in ``input/``."""
    base = os.path.splitext(os.path.basename(video_path))[0]
    search_dirs: list[str] = []
    src_dir = os.path.dirname(os.path.abspath(video_path)) or "."
    search_dirs.append(src_dir)
    input_root = env_paths.input_dir()
    if os.path.abspath(input_root) != os.path.abspath(src_dir):
        search_dirs.append(input_root)

    seen: set[str] = set()
    for d in search_dirs:
        if d in seen or not os.path.isdir(d):
            continue
        seen.add(d)
        for ext in CAM02_EXTENSIONS:
            candidate = os.path.join(d, f"{base}_cam02{ext}")
            if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                return candidate
    return None


def probe_video_size_duration(video_path: str) -> tuple[int, int, float]:
    """Return ``(width, height, duration_seconds)`` for the first video stream."""
    probe = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-show_entries", "format=duration",
            "-of", "json",
            video_path,
        ],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(probe.stdout)
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"[08a] ffprobe found no video stream in {video_path}")
    w = max(1, int(streams[0].get("width") or 1))
    h = max(1, int(streams[0].get("height") or 1))
    duration = float(data.get("format", {}).get("duration", 0) or 0)
    return w, h, duration


def build_segment_boundaries(duration: float, interval: float = CUT_INTERVAL) -> list[float]:
    """Split ``[0, duration]`` into boundaries so each slice is >= ``interval``.

    If the final slice would be shorter than ``interval``, it is merged into
    the previous slice (so the last slice can be up to ``2 * interval - 1e-3``).
    """
    boundaries = [0.0]
    t = interval
    while t < duration - 1e-6:
        boundaries.append(t)
        t += interval
    boundaries.append(duration)
    # Merge a too-short tail segment into the previous one.
    if len(boundaries) >= 3 and (boundaries[-1] - boundaries[-2]) < interval:
        del boundaries[-2]
    return boundaries


def apply_multicam(video_path: str, tmp_dir: str | None = None) -> str:
    if tmp_dir is None:
        tmp_dir = env_paths.tmp_dir()
    base = os.path.splitext(os.path.basename(video_path))[0]

    cam02 = find_cam02(video_path)
    if not cam02:
        print(f"[08a] No secondary camera ({base}_cam02.*) found, skipping.")
        return ""

    candidates = [
        os.path.join(tmp_dir, f"{base}_effects.mp4"),
        os.path.join(tmp_dir, f"{base}_color.mp4"),
        os.path.join(tmp_dir, f"{base}_fixed_audio.mp4"),
        os.path.join(tmp_dir, f"{base}_studio.mp4"),
        video_path,
    ]
    main_video = first_existing_nonempty_video(candidates)
    if not main_video:
        print("[08a] No readable main input video found, skipping.")
        return ""

    os.makedirs(tmp_dir, exist_ok=True)
    output_path = os.path.join(tmp_dir, f"{base}_multicam.mp4")

    main_w, main_h, main_dur = probe_video_size_duration(main_video)
    cam_w, cam_h, cam_dur = probe_video_size_duration(cam02)

    if main_dur <= 0:
        raise RuntimeError("[08a] Could not determine main video duration")

    if main_dur < 2 * CUT_INTERVAL:
        print(
            f"[08a] Main video only {main_dur:.1f}s (< {2 * CUT_INTERVAL:.0f}s); "
            "not enough room to intercalate, skipping."
        )
        return ""

    boundaries = build_segment_boundaries(main_dur)
    segment_count = len(boundaries) - 1

    print(f"[08a] Main:  {main_video}  ({main_w}x{main_h}, {main_dur:.1f}s)")
    print(f"[08a] Cam02: {cam02}  ({cam_w}x{cam_h}, {cam_dur:.1f}s)")
    print(
        f"[08a] Intercalating {segment_count} segment(s) at ~{CUT_INTERVAL:.0f}s each "
        f"(main = even index, cam02 = odd index, cam02 falls back to main when short)"
    )

    filter_parts: list[str] = []
    concat_labels: list[str] = []
    for seg_idx in range(segment_count):
        seg_start = boundaries[seg_idx]
        seg_end = boundaries[seg_idx + 1]
        seg_len = seg_end - seg_start

        # Odd segments prefer cam02; fall back to main if cam02 doesn't cover
        # this slot (assumes cam02 shares a zero-aligned timeline with main).
        want_cam02 = (seg_idx % 2 == 1) and (cam_dur - seg_start) >= seg_len - 1e-3
        label = f"v{seg_idx}"
        concat_labels.append(label)

        if want_cam02:
            # Letterbox cam02 to the main frame so concat resolutions match.
            filter_parts.append(
                f"[1:v]trim=start={seg_start:.6f}:end={seg_end:.6f},"
                f"setpts=PTS-STARTPTS,"
                f"scale={main_w}:{main_h}:force_original_aspect_ratio=decrease,"
                f"pad={main_w}:{main_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1,format=yuv420p[{label}]"
            )
        else:
            filter_parts.append(
                f"[0:v]trim=start={seg_start:.6f}:end={seg_end:.6f},"
                f"setpts=PTS-STARTPTS,setsar=1,format=yuv420p[{label}]"
            )

    filter_parts.append(
        "".join(f"[{lbl}]" for lbl in concat_labels)
        + f"concat=n={len(concat_labels)}:v=1:a=0[vout]"
    )
    filter_complex = ";".join(filter_parts)

    # FFmpeg picks the muxer from the extension; "foo.mp4.part" fails.
    root, ext = os.path.splitext(output_path)
    output_partial = f"{root}.part{ext}"

    hwaccel_args: list[str] = []
    if os.environ.get("FFMPEG_HWACCEL", "").lower() != "none" and platform.system() == "Darwin":
        hwaccel_args = ["-hwaccel", "videotoolbox"]

    encoder_args = build_fast_pipeline_encode_args(main_video)
    encoder_name = encoder_args[1] if len(encoder_args) > 1 else "unknown"
    print(f"[08a] Encoder: {encoder_name}  hwaccel: {hwaccel_args[-1] if hwaccel_args else 'none'}")

    ffmpeg_cmd = [
        "ffmpeg", "-y",
        *hwaccel_args, "-noautorotate", "-i", main_video,
        *hwaccel_args, "-noautorotate", "-i", cam02,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "0:a?",
        *encoder_args,
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-shortest",
        output_partial,
    ]
    run = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
    if run.returncode != 0 and hwaccel_args:
        # VideoToolbox decode occasionally chokes on unusual pixel formats;
        # retry in software before giving up.
        print("[08a] Hardware decode failed, retrying with software decode...")
        fallback = [a for a in ffmpeg_cmd if a not in hwaccel_args]
        run = subprocess.run(fallback, capture_output=True, text=True)
    if run.returncode != 0:
        if os.path.exists(output_partial):
            try:
                os.remove(output_partial)
            except OSError:
                pass
        err_tail = run.stderr[-1200:]
        raise RuntimeError(f"[08a] FFmpeg encode failed: {err_tail}")

    os.replace(output_partial, output_path)
    print(f"[08a] Output: {output_path}")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 08a_multicam.py <video_path>")
        sys.exit(1)
    apply_multicam(sys.argv[1])

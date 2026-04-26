"""
Step 11: Append a default outro clip (concat at end). Runs last; writes the same
output/{base}_final.mp4 as step 10, using the same x264 + AAC export as 09/10.

The ending file is VIDEOS_VIDEO_ENDING in .env, or assets/video_ending/default.mp4.
If that file is missing, the step is skipped and the input path is returned unchanged.
"""
from __future__ import annotations

import json
import os
import subprocess

import editor_gate
import env_paths
from video_encoding import (
    build_color_preserving_composite_encode_args,
    ensure_mp4_aac_stereo_48k,
    first_existing_nonempty_video,
    probe_primary_video_length_seconds,
    whole_len_samples_48k,
)


def _ffprobe_json(video_path: str) -> dict:
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            video_path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(r.stdout or "{}")


def _parse_fps(rate: str) -> float:
    if not rate or rate in ("0/0", "unknown"):
        return 30.0
    if "/" in rate:
        a, b = rate.split("/", 1)
        try:
            an, bn = float(a), float(b)
            if bn:
                return an / bn
        except (ValueError, ZeroDivisionError):
            pass
    try:
        return float(rate)
    except ValueError:
        return 30.0


def _probe_main_geometry(main_path: str) -> tuple[int, int, float]:
    data = _ffprobe_json(main_path)
    for st in data.get("streams") or []:
        if st.get("codec_type") == "video":
            w = int(st.get("width") or 0)
            h = int(st.get("height") or 0)
            fps = _parse_fps(
                str(st.get("avg_frame_rate") or st.get("r_frame_rate") or "")
            )
            if w > 0 and h > 0:
                return w, h, fps
    raise RuntimeError(f"Could not read video size from {main_path!r}")


def _has_audio_stream(path: str) -> bool:
    data = _ffprobe_json(path)
    for st in data.get("streams") or []:
        if st.get("codec_type") == "audio":
            return True
    return False


def _duration(path: str) -> float:
    p = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float((p.stdout or "0").strip() or 0.0)


def append_video_ending(
    video_path: str, tmp_dir: str | None = None, output_dir: str | None = None
) -> str:
    if tmp_dir is None:
        tmp_dir = env_paths.tmp_dir()
    if output_dir is None:
        output_dir = env_paths.output_dir()

    raw_stem = os.path.splitext(os.path.basename(video_path))[0]
    base = editor_gate.stem_for_editor_gate(raw_stem)

    candidates = [
        os.path.join(output_dir, f"{base}_final.mp4"),
        os.path.join(tmp_dir, f"{base}_dataviz.mp4"),
        os.path.join(tmp_dir, f"{base}_fx.mp4"),
        os.path.join(tmp_dir, f"{base}_broll.mp4"),
        os.path.join(tmp_dir, f"{base}_hardcut.mp4"),
        os.path.join(tmp_dir, f"{base}_fixed_audio.mp4"),
        video_path,
    ]
    vp_abs = os.path.abspath(video_path)
    if raw_stem != base and os.path.isfile(vp_abs) and os.path.getsize(vp_abs) > 0:
        input_video = vp_abs
    else:
        input_video = first_existing_nonempty_video(candidates)
    if not input_video:
        print("[11] No readable input video, skipping video ending.")
        return ""

    ending_path = env_paths.default_video_ending_path()
    if not os.path.isfile(ending_path) or os.path.getsize(ending_path) == 0:
        print(
            f"[11] No default ending at {ending_path!r} (set VIDEOS_VIDEO_ENDING or add the file), skipping."
        )
        return input_video

    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{base}_final.mp4")

    w, h, fps = _probe_main_geometry(input_video)
    t_end = _duration(ending_path)
    if t_end <= 0:
        print(f"[11] Ending file has no duration, skipping: {ending_path}")
        return input_video

    main_dur = max(
        _duration(input_video), probe_primary_video_length_seconds(input_video) or 0.0
    )
    if main_dur <= 0:
        print(f"[11] Main duration is 0, skipping: {input_video!r}")
        return input_video

    main_has_a = _has_audio_stream(input_video)
    end_has_a = _has_audio_stream(ending_path)
    wl_main = whole_len_samples_48k(main_dur)

    v_main = f"[0:v]setpts=PTS-STARTPTS,fps={fps},format=yuv420p[vm0]"
    v_end = (
        f"[1:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p,"
        f"setpts=PTS-STARTPTS[ve1]"
    )

    if main_has_a:
        # Pad 0:a to main_dur so concat matches vm0 when talk track is short.
        a_main = (
            f"[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"atrim=0:{main_dur},apad=whole_len={wl_main},asetpts=PTS-STARTPTS[am0]"
        )
    else:
        a_main = (
            f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=0:{main_dur},"
            f"asetpts=PTS-STARTPTS[am0]"
        )

    if end_has_a:
        a_end = (
            "[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            "asetpts=PTS-STARTPTS[ae1]"
        )
    else:
        a_end = (
            f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=0:{t_end},"
            f"asetpts=PTS-STARTPTS[ae1]"
        )

    concat = "[vm0][am0][ve1][ae1]concat=n=2:v=1:a=1[v][a]"
    filter_complex = ";".join([v_main, v_end, a_main, a_end, concat])

    use_temp = os.path.abspath(input_video) == os.path.abspath(output_path)
    actual_output = output_path + ".tmp_end.mp4" if use_temp else output_path

    encode_args = build_color_preserving_composite_encode_args(input_video)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_video,
        "-i",
        ending_path,
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-map_metadata",
        "0",
        *encode_args,
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
        actual_output,
    ]

    print(f"[11] Appending outro: {os.path.basename(ending_path)} → {output_path}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or "")[-800:]
        print(f"[11] FFmpeg error: {tail}")
        raise RuntimeError(f"Video ending concat failed (exit {result.returncode})")

    if use_temp:
        os.replace(actual_output, output_path)

    ensure_mp4_aac_stereo_48k(output_path)
    print(f"[11] Output: {output_path}")
    return output_path


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python 11_video_ending.py <video_path>")
        sys.exit(1)
    append_video_ending(sys.argv[1])

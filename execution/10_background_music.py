"""
Step 10: Add background music from a random track in music/ folder.
Loops the track if shorter than the video, fades out at the end.
Volume is configurable below.
"""
import sys
import os
import random
import subprocess

import editor_gate
import env_paths
from video_encoding import (
    build_color_preserving_composite_encode_args,
    ensure_mp4_aac_stereo_48k,
    first_existing_nonempty_video,
)

# --- Config ---
MUSIC_VOLUME = 0.20        # 0.0 to 1.0 — how loud the music is relative to original audio
FADE_OUT_DURATION = 0    # seconds of fade out at the end

AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".m4a", ".ogg", ".flac", ".opus"}


def pick_random_track(music_dir: str | None = None) -> str | None:
    """Pick a random audio file from the music directory (VIDEOS_MUSIC_DIR)."""
    if music_dir is None:
        music_dir = env_paths.music_dir()
    if not os.path.isdir(music_dir):
        return None
    tracks = [
        os.path.join(music_dir, f)
        for f in os.listdir(music_dir)
        if os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS
    ]
    if not tracks:
        return None
    chosen = random.choice(tracks)
    return chosen


def get_duration(path: str) -> float:
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(probe.stdout.strip())


def add_background_music(
    video_path: str, tmp_dir: str | None = None, output_dir: str | None = None
) -> str:
    if tmp_dir is None:
        tmp_dir = env_paths.tmp_dir()
    if output_dir is None:
        output_dir = env_paths.output_dir()
    # Match 09: strip pipeline suffixes (_final, _fx, …) so we look up
    # ``output/{base}_final.mp4`` — not ``{base}_final_final.mp4`` when 09+ just
    # passed ``…/IMG_123_final.mp4`` (which would miss and pick stale .tmp/ files).
    raw_stem = os.path.splitext(os.path.basename(video_path))[0]
    base = editor_gate.stem_for_editor_gate(raw_stem)

    # Resolve input: prefer final, then intermediates (skip empty files)
    candidates = [
        os.path.join(output_dir, f"{base}_final.mp4"),
        os.path.join(tmp_dir, f"{base}_dataviz.mp4"),
        os.path.join(tmp_dir, f"{base}_fx.mp4"),
        os.path.join(tmp_dir, f"{base}_broll.mp4"),
        os.path.join(tmp_dir, f"{base}_hardcut.mp4"),
        os.path.join(tmp_dir, f"{base}_effects.mp4"),
        os.path.join(tmp_dir, f"{base}_color.mp4"),
        os.path.join(tmp_dir, f"{base}_fixed_audio.mp4"),
        os.path.join(tmp_dir, f"{base}_studio.mp4"),
        video_path,
    ]
    vp_abs = os.path.abspath(video_path)
    if (
        raw_stem != base
        and os.path.isfile(vp_abs)
        and os.path.getsize(vp_abs) > 0
    ):
        # Pipeline passes the real previous step output; trust it over any stale
        # ``{base}_final.mp4`` on disk.
        input_video = vp_abs
    else:
        input_video = first_existing_nonempty_video(candidates)
    if not input_video:
        print("[10] No readable input video, skipping.")
        return ""

    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{base}_final.mp4")

    # Pick a track
    track = pick_random_track()
    if not track:
        print(f"[10] No music files found in {env_paths.music_dir()}/, skipping.")
        return ""

    print(f"[10] Selected track: {os.path.basename(track)}")
    print(f"[10] Music volume: {MUSIC_VOLUME} ({int(MUSIC_VOLUME * 100)}%)")

    video_duration = get_duration(input_video)
    track_duration = get_duration(track)
    print(f"[10] Video: {video_duration:.1f}s, Track: {track_duration:.1f}s")

    # Build audio filter:
    # Prepare music: loop if needed, trim, apply volume, fade, force stereo.
    # Then merge with original audio and sum channels explicitly via `pan`
    # so the original audio passes through at unit gain (no amix normalization).
    fade_start = max(0, video_duration - FADE_OUT_DURATION)

    if track_duration < video_duration:
        loops_needed = int(video_duration / track_duration) + 1
        music_input = f"[1:a]aloop=loop={loops_needed}:size={int(track_duration * 48000)},"
    else:
        music_input = "[1:a]"

    audio_filter = (
        f"{music_input}"
        f"atrim=0:{video_duration},"
        f"asetpts=PTS-STARTPTS,"
        f"volume={MUSIC_VOLUME},"
        f"afade=t=out:st={fade_start}:d={FADE_OUT_DURATION},"
        f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
        f"[music];"
        f"[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[orig];"
        f"[orig][music]amerge=inputs=2[merged];"
        f"[merged]pan=stereo|c0=c0+c2|c1=c1+c3,"
        f"atrim=0:{video_duration},asetpts=PTS-STARTPTS[outa]"
    )

    # If input and output are the same file, use a temp path
    use_temp = os.path.abspath(input_video) == os.path.abspath(output_path)
    actual_output = output_path + ".tmp.mp4" if use_temp else output_path

    # Re-encode video with the same libx264 settings as 08c/09 so the final
    # file matches the b-roll/captions chain (VideoToolbox can shift colors).
    encode_args = build_color_preserving_composite_encode_args(input_video)
    cmd = [
        "ffmpeg", "-y",
        "-i", input_video,
        "-i", track,
        "-filter_complex", audio_filter,
        "-map", "0:v", "-map", "[outa]",
        "-map_metadata", "0",
        *encode_args,
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",
        "-movflags", "+faststart",
        actual_output,
    ]

    print("[10] Mixing background music...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[10] FFmpeg error: {result.stderr[-600:]}")
        raise RuntimeError(f"Background music mixing failed (exit {result.returncode})")

    if use_temp:
        os.replace(actual_output, output_path)

    ensure_mp4_aac_stereo_48k(output_path)
    print(f"[10] Output: {output_path}")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 10_background_music.py <video_path>")
        sys.exit(1)
    add_background_music(sys.argv[1])

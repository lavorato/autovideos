"""
Step 5: Fix mute/silent gaps in the middle of the video.
Detects unexpected silence and applies crossfade or ambient fill.
"""
import sys
import os
import subprocess
import json
import struct
import wave
import tempfile

import env_paths


def detect_silent_gaps(video_path: str, silence_threshold: float = -40,
                       min_duration: float = 0.5) -> list:
    """Use FFmpeg silencedetect to find silent gaps."""
    cmd = [
        "ffmpeg", "-i", video_path,
        "-af", f"silencedetect=noise={silence_threshold}dB:d={min_duration}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    stderr = result.stderr

    gaps = []
    silence_start = None
    for line in stderr.split("\n"):
        if "silence_start:" in line:
            try:
                silence_start = float(line.split("silence_start:")[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
        elif "silence_end:" in line and silence_start is not None:
            try:
                parts = line.split("silence_end:")[1].strip().split()
                silence_end = float(parts[0])
                duration_str = line.split("silence_duration:")[1].strip() if "silence_duration:" in line else "0"
                duration = float(duration_str)
                gaps.append({
                    "start": silence_start,
                    "end": silence_end,
                    "duration": duration,
                })
                silence_start = None
            except (ValueError, IndexError):
                pass

    return gaps


def fix_mute(video_path: str, tmp_dir: str | None = None) -> str:
    if tmp_dir is None:
        tmp_dir = env_paths.tmp_dir()
    base = os.path.splitext(os.path.basename(video_path))[0]
    input_video = os.path.join(tmp_dir, f"{base}_studio.mp4")
    if not os.path.exists(input_video):
        input_video = video_path
    output_path = os.path.join(tmp_dir, f"{base}_fixed_audio.mp4")

    # Get video duration
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_video],
        capture_output=True, text=True, check=True,
    )
    total_duration = float(probe.stdout.strip())

    print(f"[05] Detecting silent gaps in: {input_video}")
    gaps = detect_silent_gaps(input_video)

    # Filter: only gaps in the middle (not start/end), and longer than 1s
    margin = 2.0  # seconds from start/end to ignore
    middle_gaps = [
        g for g in gaps
        if g["start"] > margin and g["end"] < (total_duration - margin) and g["duration"] > 1.0
    ]

    if not middle_gaps:
        print("[05] No problematic silent gaps found, copying input.")
        subprocess.run(["cp", input_video, output_path], check=True)
        return output_path

    print(f"[05] Found {len(middle_gaps)} silent gaps to fix:")
    for g in middle_gaps:
        print(f"  {g['start']:.2f}s - {g['end']:.2f}s (duration: {g['duration']:.2f}s)")

    # Strategy: generate low-volume ambient noise to fill gaps
    # Use crossfade at gap boundaries for smooth transition
    filter_parts = []
    for i, gap in enumerate(middle_gaps):
        # Create a volume boost around the gap to smooth it
        fade_dur = min(0.3, gap["duration"] / 4)
        filter_parts.append(
            f"volume=enable='between(t,{gap['start']},{gap['end']})':volume=0"
        )

    # Generate ambient noise and mix
    noise_filter = "anoisesrc=d={dur}:c=pink:r=44100:a=0.003".format(dur=total_duration)

    # Build the ambient fill filter
    audio_filter = (
        f"[0:a]aresample=44100[main];"
        f"{noise_filter}[noise];"
        f"[main][noise]amix=inputs=2:duration=first:weights=1 0.02[mixed]"
    )

    cmd = [
        "ffmpeg", "-y", "-i", input_video,
        "-filter_complex", audio_filter,
        "-map", "0:v", "-map", "[mixed]",
        "-c:v", "copy",
        "-c:a", "alac",
        output_path,
    ]
    print("[05] Applying ambient fill...")
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"[05] Output: {output_path}")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 05_fix_mute.py <video_path>")
        sys.exit(1)
    fix_mute(sys.argv[1])

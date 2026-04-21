"""
Step 2: Remove retakes from video.
Detects repeated phrases where the speaker starts over and keeps only the last take.
"""
import sys
import json
import os
import subprocess
from difflib import SequenceMatcher
from video_encoding import build_lossless_x264_args


def find_retakes(segments: list, similarity_threshold: float = 0.6) -> list:
    """Find segments that are retakes (repeated attempts at the same content)."""
    retake_indices = set()

    for i in range(len(segments) - 1):
        text_a = segments[i].get("text", "").strip().lower()
        text_b = segments[i + 1].get("text", "").strip().lower()

        if not text_a or not text_b:
            continue

        # Check if segments are similar (retake)
        ratio = SequenceMatcher(None, text_a, text_b).ratio()
        if ratio >= similarity_threshold:
            # Mark the earlier one as a retake (keep the later, usually better take)
            retake_indices.add(i)
            print(f"  Retake detected: [{i}] '{text_a[:60]}...' ~ [{i+1}] '{text_b[:60]}...' (similarity: {ratio:.2f})")

        # Also check if B starts the same way as A (partial retake)
        words_a = text_a.split()
        words_b = text_b.split()
        if len(words_a) >= 3 and len(words_b) >= 3:
            prefix_a = " ".join(words_a[:3])
            prefix_b = " ".join(words_b[:3])
            if SequenceMatcher(None, prefix_a, prefix_b).ratio() > 0.8 and i not in retake_indices:
                retake_indices.add(i)
                print(f"  Partial retake: [{i}] starts like [{i+1}]")

    return sorted(retake_indices)


def build_keep_intervals(segments: list, retake_indices: set) -> list:
    """Build list of time intervals to keep."""
    intervals = []
    for i, seg in enumerate(segments):
        if i not in retake_indices:
            intervals.append((seg["start"], seg["end"]))
    return intervals


def remove_retakes(video_path: str, tmp_dir: str = ".tmp") -> str:
    base = os.path.splitext(os.path.basename(video_path))[0]
    transcript_path = os.path.join(tmp_dir, f"{base}_transcript.json")
    output_path = os.path.join(tmp_dir, f"{base}_no_retakes.mp4")

    with open(transcript_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    segments = data.get("segments", [])
    if not segments:
        print("[02] No segments found, copying original.")
        subprocess.run(["cp", video_path, output_path], check=True)
        return output_path

    print(f"[02] Analyzing {len(segments)} segments for retakes...")
    retake_indices = find_retakes(segments)

    if not retake_indices:
        print("[02] No retakes detected, copying original.")
        subprocess.run(["cp", video_path, output_path], check=True)
        return output_path

    print(f"[02] Found {len(retake_indices)} retakes. Removing...")
    keep_intervals = build_keep_intervals(segments, set(retake_indices))

    # Build FFmpeg filter to concatenate kept segments
    filter_parts = []
    for idx, (start, end) in enumerate(keep_intervals):
        filter_parts.append(
            f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{idx}];"
            f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{idx}];"
        )

    concat_v = "".join(f"[v{i}]" for i in range(len(keep_intervals)))
    concat_a = "".join(f"[a{i}]" for i in range(len(keep_intervals)))
    n = len(keep_intervals)
    filter_complex = "".join(filter_parts) + f"{concat_v}{concat_a}concat=n={n}:v=1:a=1[outv][outa]"

    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        *build_lossless_x264_args(video_path),
        "-c:a", "alac",
        output_path,
    ]
    print(f"[02] Running FFmpeg...")
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"[02] Output: {output_path}")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 02_remove_retakes.py <video_path>")
        sys.exit(1)
    remove_retakes(sys.argv[1])

"""
Step 3: Remove filler words and long silences.
Preserves natural short pauses (~0.3s) for a human feel.
"""
import sys
import json
import os
import shutil
import subprocess
from video_encoding import build_lossless_x264_args
from transcript_remap import remap_transcript_to_keeps, write_transcript

# Filler words to detect (Portuguese + English)
FILLER_WORDS = {
    "éh", "eh", "ah", "uhm", "um", "hm", "ahn", "uh",
    "tipo", "né", "então", "assim", "basicamente", "literalmente",
    "like", "you know", "basically", "actually", "literally", "so",
}

MIN_PAUSE_KEEP = 0.6   # seconds - keep pauses shorter than this
MAX_SILENCE_GAP = 1.5  # seconds - trim silences longer than this


def detect_fillers_and_gaps(words: list) -> list:
    """Return list of time intervals to CUT (filler words + long silences)."""
    cuts = []

    for w in words:
        word_lower = w["word"].lower().strip(".,!?;:")
        if word_lower in FILLER_WORDS:
            cuts.append({"start": w["start"], "end": w["end"], "reason": f"filler: {word_lower}"})

    # Detect long silences between words
    for i in range(len(words) - 1):
        gap_start = words[i]["end"]
        gap_end = words[i + 1]["start"]
        gap_duration = gap_end - gap_start
        if gap_duration > MAX_SILENCE_GAP:
            # Keep a small natural pause
            trim_start = gap_start + MIN_PAUSE_KEEP
            trim_end = gap_end - MIN_PAUSE_KEEP
            if trim_end > trim_start:
                cuts.append({"start": trim_start, "end": trim_end, "reason": f"silence: {gap_duration:.1f}s"})

    return sorted(cuts, key=lambda x: x["start"])


def merge_cuts(cuts: list, margin: float = 0.05) -> list:
    """Merge overlapping or adjacent cut regions."""
    if not cuts:
        return []
    merged = [cuts[0].copy()]
    for c in cuts[1:]:
        if c["start"] <= merged[-1]["end"] + margin:
            merged[-1]["end"] = max(merged[-1]["end"], c["end"])
        else:
            merged.append(c.copy())
    return merged


def invert_to_keep(cuts: list, duration: float) -> list:
    """Convert cut intervals to keep intervals."""
    keeps = []
    pos = 0.0
    for c in cuts:
        if c["start"] > pos:
            keeps.append((pos, c["start"]))
        pos = c["end"]
    if pos < duration:
        keeps.append((pos, duration))
    return keeps


def _probe_duration(path: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(r.stdout.strip())


def _publish_fillers_output(
    *,
    tmp_dir: str,
    base: str,
    output_path: str,
    transcript_path: str,
    keeps: list[tuple[float, float]],
) -> str:
    """Copy ``_no_fillers`` into ``.tmp/{base}.mp4`` and remap transcript to the new timeline."""
    canonical = os.path.join(tmp_dir, f"{base}.mp4")
    shutil.copy2(output_path, canonical)
    new_dur = _probe_duration(canonical)
    new_size = os.path.getsize(canonical)

    with open(transcript_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    remapped = remap_transcript_to_keeps(
        data,
        keeps,
        new_video_path=canonical,
        new_video_size=new_size,
        new_video_duration=new_dur,
    )
    write_transcript(transcript_path, remapped)
    print(f"[03] Published {canonical} and remapped {transcript_path}")
    return canonical


def remove_fillers(video_path: str, tmp_dir: str = ".tmp") -> str:
    base = os.path.splitext(os.path.basename(video_path))[0]
    os.makedirs(tmp_dir, exist_ok=True)
    # Prefer retakes-removed video when step 02 already ran (legacy / --step order).
    input_video = os.path.join(tmp_dir, f"{base}_no_retakes.mp4")
    if not os.path.exists(input_video):
        input_video = video_path
    transcript_path = os.path.join(tmp_dir, f"{base}_transcript.json")
    output_path = os.path.join(tmp_dir, f"{base}_no_fillers.mp4")

    with open(transcript_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_video],
        capture_output=True, text=True, check=True,
    )
    duration = float(probe.stdout.strip())

    words = data.get("words", [])
    if not words:
        print("[03] No words in transcript, copying input.")
        subprocess.run(["cp", input_video, output_path], check=True)
        keeps = [(0.0, duration)]
        return _publish_fillers_output(
            tmp_dir=tmp_dir,
            base=base,
            output_path=output_path,
            transcript_path=transcript_path,
            keeps=keeps,
        )

    print(f"[03] Analyzing {len(words)} words for fillers...")
    cuts = detect_fillers_and_gaps(words)
    cuts = merge_cuts(cuts)

    if not cuts:
        print("[03] No fillers or long silences found, copying input.")
        subprocess.run(["cp", input_video, output_path], check=True)
        keeps = [(0.0, duration)]
        return _publish_fillers_output(
            tmp_dir=tmp_dir,
            base=base,
            output_path=output_path,
            transcript_path=transcript_path,
            keeps=keeps,
        )

    for c in cuts:
        print(f"  Cut: {c['start']:.2f}s - {c['end']:.2f}s ({c.get('reason', '')})")

    keeps = invert_to_keep(cuts, duration)
    keeps_t = [(float(a), float(b)) for a, b in keeps]
    print(f"[03] Cutting {len(cuts)} regions, keeping {len(keeps_t)} segments...")

    # Build FFmpeg concat filter
    filter_parts = []
    for idx, (start, end) in enumerate(keeps_t):
        filter_parts.append(
            f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{idx}];"
            f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{idx}];"
        )

    # The concat filter expects inputs interleaved per segment: [v0][a0][v1][a1]...
    # Grouping video pads before audio pads (e.g. [v0][v1]...[a0][a1]...) makes
    # ffmpeg link an audio output to a video input pad and fail with exit 234.
    concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(len(keeps_t)))
    n = len(keeps_t)
    filter_complex = "".join(filter_parts) + f"{concat_inputs}concat=n={n}:v=1:a=1[outv][outa]"

    cmd = [
        "ffmpeg", "-y", "-i", input_video,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        *build_lossless_x264_args(input_video),
        "-c:a", "alac",
        output_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        stderr_tail = "\n".join((e.stderr or "").strip().splitlines()[-20:])
        print(f"[03] FFmpeg failed (exit {e.returncode}):\n{stderr_tail}")
        raise
    print(f"[03] Output: {output_path}")
    return _publish_fillers_output(
        tmp_dir=tmp_dir,
        base=base,
        output_path=output_path,
        transcript_path=transcript_path,
        keeps=keeps_t,
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 03_remove_fillers.py <video_path>")
        sys.exit(1)
    remove_fillers(sys.argv[1])

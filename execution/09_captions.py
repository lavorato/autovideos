"""
Step 9: Add captions using captacity library (https://github.com/unconv/captacity).
Clean paragraph style with word-level highlighting, centered on face subject.
Uses pre-computed Whisper transcript from step 01 to skip re-transcription.
Final output goes to output/ directory.
"""
import sys
import json
import os
import subprocess
from contextlib import contextmanager

import captacity
from moviepy import CompositeVideoClip

from video_encoding import (
    build_moviepy_lossless_params,
    first_existing_nonempty_video,
)


@contextmanager
def _captacity_lossless_override(source_video: str):
    """
    captacity hardcodes `bitrate='8000k'` plus VP9/webm-style ffmpeg flags when
    it calls MoviePy's `write_videofile`, which re-encodes the final output
    lossily and drops the source color tags. Patch the composite clip's write
    method so captacity emits the same lossless x264 export (crf 0, veryslow,
    preserved colorspace/transfer/primaries/range, ALAC audio) used by the
    other pipeline steps.
    """
    original_write = CompositeVideoClip.write_videofile
    lossless_params = build_moviepy_lossless_params(source_video)

    def patched_write(self, filename, *args, **kwargs):
        kwargs["codec"] = "libx264"
        kwargs["preset"] = "veryslow"
        kwargs["audio_codec"] = "alac"
        kwargs.pop("bitrate", None)
        kwargs["ffmpeg_params"] = list(lossless_params)
        # MoviePy's extensions_dict has no entry for ALAC, so find_extension()
        # raises when it tries to derive a temp audio filename. Provide an
        # explicit .m4a temp path (ALAC's native container) to bypass lookup.
        base, _ = os.path.splitext(filename)
        kwargs.setdefault("temp_audiofile", f"{base}_captacityTEMP_audio.m4a")
        return original_write(self, filename, *args, **kwargs)

    CompositeVideoClip.write_videofile = patched_write
    try:
        yield
    finally:
        CompositeVideoClip.write_videofile = original_write

# --- Caption style: Clean Paragraph ---
FONT = "fonts/OpenSans.ttf"
FONT_BOLD = "fonts/OpenSans-Bold.ttf"  # Added bold font
FONT_SIZE = 40
FONT_COLOR = "white"
STROKE_WIDTH = 0
STROKE_COLOR = "black"
HIGHLIGHT_CURRENT_WORD = True
WORD_HIGHLIGHT_COLOR = "#FBBF23"  # primary brand color
LINE_COUNT = 1
PADDING = 250
SHADOW_STRENGTH = 0.4
SHADOW_BLUR = 0.08
MARGIN_BOTTOM = 1020  # pixels from bottom edge


def _sanitize_segments_for_captacity(segments):
    """
    Captacity's segment_parser requires every word dict to have both 'start' and 'end'
    (and a non-empty 'word'). WhisperX occasionally emits tokens without timestamps
    (unalignable digits/punctuation), which causes a KeyError deep inside captacity.
    Filter those out, drop empty segments, and ensure monotonic timing.
    """
    if not segments:
        return segments

    clean = []
    for seg in segments:
        words = seg.get("words") or []
        fixed = []
        for w in words:
            if not isinstance(w, dict):
                continue
            if "start" not in w or "end" not in w:
                continue
            try:
                s = float(w["start"])
                e = float(w["end"])
            except (TypeError, ValueError):
                continue
            text = str(w.get("word", ""))
            if not text.strip():
                continue
            if e <= s:
                e = s + 0.02
            fixed.append({"word": text, "start": s, "end": e})
        if not fixed:
            continue
        seg_copy = dict(seg)
        seg_copy["words"] = fixed
        seg_copy["start"] = float(seg.get("start", fixed[0]["start"]))
        seg_copy["end"] = float(seg.get("end", fixed[-1]["end"]))
        clean.append(seg_copy)
    return clean


def add_captions(video_path: str, tmp_dir: str = ".tmp", output_dir: str = "output") -> str:
    base = os.path.splitext(os.path.basename(video_path))[0]

    # Resolve input video (skip empty/corrupt intermediates)
    candidates = [
        os.path.join(tmp_dir, f"{base}_broll.mp4"),
        os.path.join(tmp_dir, f"{base}_hardcut.mp4"),
        os.path.join(tmp_dir, f"{base}_effects.mp4"),
        os.path.join(tmp_dir, f"{base}_color.mp4"),
        os.path.join(tmp_dir, f"{base}_fixed_audio.mp4"),
        os.path.join(tmp_dir, f"{base}_studio.mp4"),
        video_path,
    ]
    input_video = first_existing_nonempty_video(candidates)
    if not input_video:
        raise RuntimeError("[09] No readable input video found")

    transcript_path = os.path.join(tmp_dir, f"{base}_transcript.json")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{base}_final.mp4")

    # Load pre-computed transcript from step 01
    segments = None
    if os.path.exists(transcript_path):
        with open(transcript_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw_segments = data.get("segments")
        segments = _sanitize_segments_for_captacity(raw_segments)
        if segments:
            dropped = sum(len((s or {}).get("words") or []) for s in (raw_segments or [])) - sum(
                len(s["words"]) for s in segments
            )
            if dropped > 0:
                print(f"[09] Sanitized transcript: dropped {dropped} word(s) missing start/end")
            print(f"[09] Using pre-computed transcript ({len(segments)} segments)")
        else:
            segments = None
            print("[09] Transcript has no usable segments, captacity will transcribe from scratch")
    else:
        print("[09] No transcript found, captacity will transcribe from scratch")

    # Get video height to calculate bottom position
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "stream=height",
         "-select_streams", "v:0", "-of", "default=noprint_wrappers=1:nokey=1", input_video],
        capture_output=True, text=True, check=True,
    )
    vid_height = int(probe.stdout.strip())
    y_pos = vid_height - MARGIN_BOTTOM - (FONT_SIZE * LINE_COUNT)

    print(f"[09] Adding captions with captacity: {input_video}")
    print(f"[09] Style: font={FONT}, font_bold={FONT_BOLD}, size={FONT_SIZE}, highlight={WORD_HIGHLIGHT_COLOR}")
    print(f"[09] Position: center, y={y_pos}")

    with _captacity_lossless_override(input_video):
        captacity.add_captions(
            video_file=input_video,
            output_file=output_path,
            font=FONT_BOLD,
            font_size=FONT_SIZE,
            font_color=FONT_COLOR,
            stroke_width=STROKE_WIDTH,
            stroke_color=STROKE_COLOR,
            highlight_current_word=HIGHLIGHT_CURRENT_WORD,
            word_highlight_color=WORD_HIGHLIGHT_COLOR,
            line_count=LINE_COUNT,
            padding=PADDING,
            position=("center", y_pos),
            shadow_strength=SHADOW_STRENGTH,
            shadow_blur=SHADOW_BLUR,
            segments=segments,
            print_info=True,
        )

    print(f"[09] Final output: {output_path}")
    return output_path


def _build_test_video(tmp_dir: str = ".tmp", duration: float = 5.0) -> str:
    """
    Generate a 5-second 1080x1920 dummy video (dark gradient background + silent
    audio) and a matching fake word-level transcript so we can preview the
    caption style without running the full pipeline.
    """
    os.makedirs(tmp_dir, exist_ok=True)
    base = "captions_test"
    video_path = os.path.join(tmp_dir, f"{base}.mp4")
    transcript_path = os.path.join(tmp_dir, f"{base}_transcript.json")

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=0x101820:s=1080x1920:d={duration}:r=30",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t", str(duration),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            video_path,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    words = [
        "Testando", "a", "configuração", "das", "legendas",
        "com", "destaque", "de", "palavra", "atual",
    ]
    per_word = duration / len(words)
    word_dicts = []
    t = 0.0
    for w in words:
        start = round(t, 3)
        end = round(t + per_word * 0.9, 3)
        word_dicts.append({"word": w, "start": start, "end": end})
        t += per_word

    segments = [{
        "start": 0.0,
        "end": duration,
        "text": " ".join(words),
        "words": word_dicts,
    }]

    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump({"segments": segments}, f, ensure_ascii=False, indent=2)

    print(f"[09][test] Built dummy video: {video_path}")
    print(f"[09][test] Built dummy transcript: {transcript_path}")
    return video_path


def run_test(tmp_dir: str = ".tmp", output_dir: str = "output") -> str:
    """Preview current caption style against a 5s dummy video."""
    video_path = _build_test_video(tmp_dir=tmp_dir)
    return add_captions(video_path, tmp_dir=tmp_dir, output_dir=output_dir)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--test":
        run_test()
        sys.exit(0)
    if len(sys.argv) < 2:
        print("Usage: python 09_captions.py <video_path>")
        print("       python 09_captions.py --test   # 5s dummy video preview")
        sys.exit(1)
    add_captions(sys.argv[1])

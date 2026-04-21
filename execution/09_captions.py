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
import captacity

from video_encoding import first_existing_nonempty_video

# --- Caption style: Clean Paragraph ---
FONT = "/Library/Fonts/PlusJakartaSans[wght].ttf"
FONT_SIZE = 80
FONT_COLOR = "white"
STROKE_WIDTH =0
STROKE_COLOR = "black"
HIGHLIGHT_CURRENT_WORD = True
WORD_HIGHLIGHT_COLOR = "#1856FF"  # primary brand color
LINE_COUNT = 2
PADDING = 50
SHADOW_STRENGTH = 0.8
SHADOW_BLUR = 0.08
MARGIN_BOTTOM = 320  # pixels from bottom edge


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
        segments = data.get("segments")
        if segments:
            print(f"[09] Using pre-computed transcript ({len(segments)} segments)")
        else:
            print("[09] Transcript has no segments, captacity will transcribe from scratch")
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
    print(f"[09] Style: font={FONT}, size={FONT_SIZE}, highlight={WORD_HIGHLIGHT_COLOR}")
    print(f"[09] Position: center, y={y_pos}")

    captacity.add_captions(
        video_file=input_video,
        output_file=output_path,
        font=FONT,
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


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 09_captions.py <video_path>")
        sys.exit(1)
    add_captions(sys.argv[1])

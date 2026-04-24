"""
Step 9: Add captions using a Remotion composition (primary) or the captacity
library (fallback). Clean paragraph style with word-level highlighting,
centered on face subject. Uses pre-computed Whisper transcript from step 01
to skip re-transcription.

The Remotion path is 5-10x faster than the old MoviePy/captacity pipeline
because Chrome headless renders frames in parallel. When the Remotion project
has no `node_modules`, we transparently fall back to captacity so the step
still works on a fresh checkout.

Final output goes to output/ directory.
"""
import sys
import json
import os
import shutil
import subprocess
from contextlib import contextmanager

import captacity
from captacity import segment_parser
from moviepy import CompositeVideoClip

from video_encoding import (
    build_fast_pipeline_encode_args,
    build_moviepy_lossless_params,
    first_existing_nonempty_video,
)


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

REMOTION_DIR = os.path.join(os.path.dirname(__file__), "broll-renderer")
REMOTION_COMPOSITION_ID = "CaptionsComposition"
USE_REMOTION_DEFAULT = True


@contextmanager
def _captacity_lossless_override(source_video: str):
    """
    captacity hardcodes `bitrate='8000k'` plus VP9/webm-style ffmpeg flags when
    it calls MoviePy's `write_videofile`, which re-encodes the final output
    lossily and drops the source color tags. Patch the composite clip's write
    method so captacity emits the same lossless x264 export (crf 0, veryslow,
    preserved colorspace/transfer/primaries/range, ALAC audio) used by the
    other pipeline steps. Only used by the captacity fallback path.
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


def _probe_video_geometry(video_path: str) -> dict:
    """Return {fps, width, height, duration} for the given video."""
    probe = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,duration",
            "-show_entries", "format=duration",
            "-of", "json",
            video_path,
        ],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(probe.stdout or "{}")
    stream = (data.get("streams") or [{}])[0] or {}
    fmt = data.get("format") or {}

    # r_frame_rate is "num/den"; evaluate safely.
    fps_str = stream.get("r_frame_rate") or "30/1"
    try:
        num, den = fps_str.split("/")
        fps = float(num) / float(den) if float(den) else float(num)
    except (ValueError, ZeroDivisionError):
        fps = 30.0

    duration = stream.get("duration") or fmt.get("duration") or 0.0
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0.0

    return {
        "fps": fps,
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration": duration,
    }


def _build_captions_from_transcript(
    segments: list,
    font_path: str,
    video_width: int,
) -> list:
    """
    Run captacity's own line-wrapping logic to turn the WhisperX segments into
    caption blocks ready for rendering. Each returned caption has fields
    {text, start, end, words:[{word,start,end}]} — exactly what the Remotion
    component expects.
    """
    fit_function = captacity.fits_frame(
        LINE_COUNT, font_path, FONT_SIZE, STROKE_WIDTH, video_width - PADDING * 2,
    )
    captions = segment_parser.parse(segments=segments, fit_function=fit_function)

    cleaned = []
    for cap in captions:
        words = cap.get("words") or []
        if not words:
            continue
        cleaned.append({
            "text": (cap.get("text") or "").strip(),
            "start": float(cap.get("start") or words[0]["start"]),
            "end": float(cap.get("end") or words[-1]["end"]),
            "words": [
                {
                    "word": str(w["word"]),
                    "start": float(w["start"]),
                    "end": float(w["end"]),
                }
                for w in words
            ],
        })
    return cleaned


def _setup_remotion_public_dir(tmp_dir: str, base: str, font_path: str) -> tuple[str, str]:
    """
    Build a per-run public dir for Remotion's staticFile resolution. The
    overlay-only render path only needs the font file — the source video is
    never decoded inside Chrome; it's composited in via FFmpeg afterwards.

    We intentionally avoid symlinks: Remotion bundles the `--public-dir` into
    its webpack output without dereferencing, so symlinked entries show up as
    dangling links inside the bundle and fail with HTTP 404 at render time.
    Hardlink when possible (instant, no extra disk), fall back to copy on
    cross-device mounts.

    Returns (public_dir, font_basename).
    """
    public_dir = os.path.abspath(os.path.join(tmp_dir, "captions_public", base))
    if os.path.isdir(public_dir):
        shutil.rmtree(public_dir, ignore_errors=True)
    os.makedirs(public_dir, exist_ok=True)

    font_basename = os.path.basename(font_path)
    dest = os.path.join(public_dir, font_basename)
    src_abs = os.path.abspath(font_path)
    try:
        os.link(src_abs, dest)
    except OSError:
        shutil.copy2(src_abs, dest)

    return public_dir, font_basename


def _remotion_available() -> bool:
    """Remotion needs node_modules installed in the broll-renderer folder."""
    return os.path.isdir(os.path.join(REMOTION_DIR, "node_modules"))


def _remotion_concurrency() -> str:
    """
    Pick a Remotion worker count. Override via env CAPTIONS_REMOTION_CONCURRENCY.
    Default: CPU count - 2 (leaves headroom for the OS and the main Python
    process), clamped to at least 2. Each Chrome worker uses ~400MB RAM.
    """
    override = os.environ.get("CAPTIONS_REMOTION_CONCURRENCY", "").strip()
    if override:
        return override
    cpu = os.cpu_count() or 4
    return str(max(2, cpu - 2))


def _composite_overlay_with_ffmpeg(
    source_video: str, overlay_mov: str, output_path: str,
) -> None:
    """
    Overlay the transparent caption track (ProRes 4444 w/ alpha) onto the
    untouched source video in a single FFmpeg pass. Audio is stream-copied
    from the source; the video is re-encoded using the pipeline's standard
    fast-high-quality encoder (VideoToolbox H.264 on macOS) which preserves
    the source color tags.
    """
    encode_args = build_fast_pipeline_encode_args(source_video)
    cmd = [
        "ffmpeg", "-y",
        "-i", source_video,
        "-i", overlay_mov,
        "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto:shortest=0[v]",
        "-map", "[v]",
        "-map", "0:a?",
        "-map_metadata", "0",
        *encode_args,
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _render_with_remotion(
    input_video: str,
    captions: list,
    geometry: dict,
    font_path: str,
    output_path: str,
    tmp_dir: str,
    base: str,
) -> None:
    """
    Two-stage render that bypasses the slowest part of the old approach
    (Chrome headless decoding every frame of the full source video):

    1. Remotion renders ONLY the transparent caption overlay, as a ProRes
       4444 .mov with alpha. Chrome paints nearly-empty frames, so this is
       ~10x faster than compositing the whole video in-browser.
    2. FFmpeg overlays the caption track on the untouched source video in
       a single pass (audio stream-copied, video re-encoded with the
       pipeline's standard VideoToolbox H.264 path that preserves color
       tags). Because the main video's pixels are never decoded by Chrome,
       there is no quality loss on the source.
    """
    public_dir, font_basename = _setup_remotion_public_dir(
        tmp_dir, base, font_path,
    )

    duration_frames = max(1, int(round(geometry["duration"] * geometry["fps"])))
    y_from_bottom = MARGIN_BOTTOM - (FONT_SIZE * LINE_COUNT)

    props = {
        "fps": int(round(geometry["fps"])),
        "width": geometry["width"],
        "height": geometry["height"],
        "durationInFrames": duration_frames,
        # mainVideoSrc is intentionally empty: the composition's
        # renderMainVideo=false branch skips <OffthreadVideo> entirely.
        "mainVideoSrc": "",
        "fontSrc": font_basename,
        "captions": captions,
        "fontSize": FONT_SIZE,
        "fontColor": "#ffffff" if FONT_COLOR == "white" else FONT_COLOR,
        "highlightColor": WORD_HIGHLIGHT_COLOR,
        "shadowStrength": SHADOW_STRENGTH,
        "shadowBlurPx": float(FONT_SIZE) * SHADOW_BLUR,
        "yFromBottom": max(0, y_from_bottom),
        "padding": PADDING,
        "renderMainVideo": False,
    }

    props_path = os.path.abspath(os.path.join(tmp_dir, f"{base}_captions.props.json"))
    # ProRes 4444 natively carries alpha; h264/mp4 cannot, so the overlay is
    # written as a .mov.
    overlay_mov = os.path.abspath(os.path.join(tmp_dir, f"{base}_captions_overlay.mov"))

    with open(props_path, "w", encoding="utf-8") as f:
        json.dump(props, f, ensure_ascii=False)

    concurrency = _remotion_concurrency()
    cmd = [
        "npx", "remotion", "render",
        "src/index.tsx", REMOTION_COMPOSITION_ID,
        overlay_mov,
        "--props", props_path,
        "--codec", "prores",
        "--prores-profile", "4444",
        "--pixel-format", "yuva444p10le",
        # Alpha-bearing pixel formats require PNG frame intermediates;
        # Remotion's default (JPEG) has no alpha channel and is rejected.
        "--image-format", "png",
        "--concurrency", concurrency,
        "--public-dir", public_dir,
        "--log", "error",
    ]
    print(f"[09] Rendering captions overlay via Remotion "
          f"({len(captions)} blocks, {duration_frames} frames @ "
          f"{props['fps']}fps, concurrency={concurrency})...")
    subprocess.run(cmd, cwd=REMOTION_DIR, check=True)

    if not os.path.isfile(overlay_mov) or os.path.getsize(overlay_mov) == 0:
        raise RuntimeError(f"[09] Remotion produced no overlay at {overlay_mov}")

    print("[09] Compositing overlay onto source video via FFmpeg...")
    _composite_overlay_with_ffmpeg(input_video, overlay_mov, output_path)

    try:
        os.remove(overlay_mov)
        os.remove(props_path)
    except OSError:
        pass


def _render_with_captacity(
    input_video: str,
    segments: list | None,
    geometry: dict,
    output_path: str,
) -> None:
    """Legacy MoviePy/captacity path used as a fallback."""
    y_pos = geometry["height"] - MARGIN_BOTTOM - (FONT_SIZE * LINE_COUNT)

    print(f"[09] (fallback) Adding captions with captacity: {input_video}")
    print(f"[09] Style: font={FONT}, font_bold={FONT_BOLD}, "
          f"size={FONT_SIZE}, highlight={WORD_HIGHLIGHT_COLOR}")
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


def add_captions(video_path: str, tmp_dir: str = ".tmp", output_dir: str = "output") -> str:
    base = os.path.splitext(os.path.basename(video_path))[0]

    # Resolve input video (skip empty/corrupt intermediates)
    candidates = [
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

    geometry = _probe_video_geometry(input_video)
    print(f"[09] Video geometry: {geometry['width']}x{geometry['height']} "
          f"@ {geometry['fps']:.3f}fps, {geometry['duration']:.2f}s")

    use_remotion = (
        os.environ.get("CAPTIONS_USE_REMOTION", "1").strip().lower() in {"1", "true", "yes"}
        if USE_REMOTION_DEFAULT
        else os.environ.get("CAPTIONS_USE_REMOTION", "").strip().lower() in {"1", "true", "yes"}
    )

    if use_remotion and segments and _remotion_available():
        try:
            font_path = captacity.get_font_path(FONT_BOLD)
            captions = _build_captions_from_transcript(
                segments=segments,
                font_path=font_path,
                video_width=geometry["width"],
            )
            if not captions:
                raise RuntimeError("no caption blocks produced from transcript")
            _render_with_remotion(
                input_video=input_video,
                captions=captions,
                geometry=geometry,
                font_path=font_path,
                output_path=output_path,
                tmp_dir=tmp_dir,
                base=base,
            )
            print(f"[09] Final output: {output_path}")
            return output_path
        except subprocess.CalledProcessError as exc:
            print(f"[09] Remotion render failed (exit {exc.returncode}), "
                  f"falling back to captacity...")
        except Exception as exc:
            print(f"[09] Remotion render failed ({exc}), falling back to captacity...")
    elif use_remotion and not _remotion_available():
        print(f"[09] Remotion not installed at {REMOTION_DIR} (no node_modules), "
              f"using captacity fallback.")
    elif use_remotion and not segments:
        print("[09] No sanitized transcript available, using captacity fallback "
              "so it can transcribe from scratch.")

    _render_with_captacity(
        input_video=input_video,
        segments=segments,
        geometry=geometry,
        output_path=output_path,
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

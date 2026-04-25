"""
Step 8b: Hard Cut Zoom — alternates between wide and tight (face-centered)
shot every ~3 seconds with an instant cut (no transition/easing).
Creates a dynamic, TikTok/YouTube-style pacing.

Uses OpenCV Haar cascade for face detection (MediaPipe has protobuf issues).

Optional semantic zoom moments via OpenRouter:
  - OPENROUTER_API_KEY=<key>
  - OPENROUTER_MODEL=<provider/model> or OPENROUTER_MODELS=<m1,m2,m3>
When configured, the script asks the LLM to read the transcript and pick
impactful moments (punchlines, key facts, emotional beats, CTAs). Those
moments become the "tight zoom" windows; everything else stays wide. If
the LLM returns no moments, the script falls back to the current fixed
CUT_INTERVAL alternation.
"""
import sys
import os
import json
import math
import platform
import re
import numpy as np
import cv2
import subprocess
from editor_gate import stem_for_editor_gate
from openrouter_client import chat_completion, has_openrouter_api_key, parse_models_from_env
import env_paths
from video_encoding import build_fast_pipeline_encode_args

# --- Config ---
CUT_INTERVAL = 5.0       # seconds between each hard cut (fallback when no AI moments)
ZOOM_LEVEL = 1.4         # how much to zoom in on tight shots
AI_ZOOM_MIN_DURATION = 1.5   # clamp LLM moments below this
AI_ZOOM_MAX_DURATION = 6.0   # clamp LLM moments above this
AI_ZOOM_MAX_MOMENTS = 12     # hard cap on LLM-selected zoom windows
AI_ZOOM_MIN_GAP = 1.5        # minimum wide gap between two zoom moments
MAX_LLM_SEGMENTS = 120
MAX_LLM_SEGMENT_TEXT = 220
FACE_SAMPLE_FPS = 2.0    # frames/sec to sample for face detection (locked-per-segment
                         # only needs ~1 sample per segment; we keep a small margin).
FACE_FOLLOW = False      # True = camera follows face each frame (smooth tracking)
                         # False = zoom locks to face position at segment start (static zoom)
FACE_DETECT_MAX_DIM = 480  # downscale for Haar detection (was 640)


def detect_face_positions(video_path: str, sample_fps: float = FACE_SAMPLE_FPS) -> tuple:
    """Sample frames and detect face center positions using Haar cascade.

    Performance notes:
      - We only *decode* frames we actually sample. `cap.grab()` advances the
        decoder without doing a full frame decode/colour-convert (roughly 5-10x
        cheaper than `cap.read()` on 4K HEVC). This turns the detection pass from
        "decode every frame" into "decode only the ~sample_fps subset".
      - The sampled frame is downscaled once to FACE_DETECT_MAX_DIM before Haar.

    Returns first_frame_wh as (w, h) from the first decoded frame when available;
    this can disagree with container/stream metadata (e.g. some iPhone HEVC).
    """
    print("[08b] Detecting face positions (Haar cascade)...")
    face_cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    sample_interval = max(1, int(round(fps / max(0.1, sample_fps))))
    positions: list = []
    first_frame_wh: tuple[int, int] | None = None

    detect_scale = min(1.0, float(FACE_DETECT_MAX_DIM) / max(1, max(width, height)))

    frame_idx = 0
    while True:
        if frame_idx % sample_interval == 0:
            ret, frame = cap.read()
            if not ret:
                break
            if first_frame_wh is None:
                first_frame_wh = (int(frame.shape[1]), int(frame.shape[0]))
            t = frame_idx / fps
            if detect_scale < 1.0:
                small = cv2.resize(
                    frame, None, fx=detect_scale, fy=detect_scale,
                    interpolation=cv2.INTER_AREA,
                )
            else:
                small = frame
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(gray, 1.1, 5, minSize=(30, 30))

            cx, cy = 0.5, 0.5
            if len(faces) > 0:
                largest = max(faces, key=lambda f: f[2] * f[3])
                fx, fy, fw, fh = largest
                cx = (fx + fw / 2) / gray.shape[1]
                cy = (fy + fh / 2) / gray.shape[0]

            positions.append({"time": t, "cx": float(cx), "cy": float(cy)})
        else:
            if not cap.grab():
                break
        frame_idx += 1
        if total_frames and frame_idx >= total_frames:
            break

    cap.release()
    print(
        f"[08b] Sampled {len(positions)} face positions "
        f"(every {sample_interval} frame(s) at {fps:.2f} fps input)"
    )
    return positions, width, height, fps, first_frame_wh


def probe_video_stream_size_duration(video_path: str) -> tuple[int, int, float]:
    """Width/height as FFmpeg decodes them (may differ from OpenCV metadata)."""
    probe = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-show_entries", "format=duration",
            "-of", "json",
            video_path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(probe.stdout)
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError("[08b] ffprobe found no video stream")
    w = max(1, int(streams[0]["width"] or 1))
    h = max(1, int(streams[0]["height"] or 1))
    duration = float(data.get("format", {}).get("duration", 0) or 0)
    return w, h, duration


def smooth_positions(positions: list, window: int = 7) -> list:
    """Smooth face positions to avoid jitter between cuts."""
    if len(positions) < window:
        return positions
    smoothed = []
    for i in range(len(positions)):
        start = max(0, i - window // 2)
        end = min(len(positions), i + window // 2 + 1)
        avg_cx = np.mean([p["cx"] for p in positions[start:end]])
        avg_cy = np.mean([p["cy"] for p in positions[start:end]])
        smoothed.append({"time": positions[i]["time"], "cx": avg_cx, "cy": avg_cy})
    return smoothed


def zoom_crop_scale_ffmpeg(cx: float, cy: float, out_w: int, out_h: int, zoom: float) -> str:
    """Crop+zoom using expressions so w/h never exceed iw/ih (fixes probe vs decode mismatch)."""
    z = float(zoom)
    cx_s = f"{cx:.6f}"
    cy_s = f"{cy:.6f}"
    return (
        f",crop=w='min(floor(iw/{z})\\,iw)':h='min(floor(ih/{z})\\,ih)'"
        f":x='min(iw-ow\\,max(0\\,{cx_s}*iw-ow/2))'"
        f":y='min(ih-oh\\,max(0\\,{cy_s}*ih-oh/2))'"
        f",scale={out_w}:{out_h}:flags=lanczos"
    )


def get_face_at_time(positions: list, t: float) -> tuple:
    if not positions:
        return 0.5, 0.5
    closest = min(positions, key=lambda p: abs(p["time"] - t))
    return closest["cx"], closest["cy"]


def crop_and_resize(frame: np.ndarray, zoom: float, cx: float, cy: float) -> np.ndarray:
    """Crop frame around (cx, cy) at given zoom level and resize back to original dims."""
    h, w = frame.shape[:2]
    if zoom <= 1.0:
        return frame

    new_w = int(w / zoom)
    new_h = int(h / zoom)

    crop_cx = int(cx * w)
    crop_cy = int(cy * h)

    x1 = crop_cx - new_w // 2
    y1 = crop_cy - new_h // 2
    x2 = x1 + new_w
    y2 = y1 + new_h

    if x1 < 0:
        x1, x2 = 0, new_w
    if y1 < 0:
        y1, y2 = 0, new_h
    if x2 > w:
        x1, x2 = w - new_w, w
    if y2 > h:
        y1, y2 = h - new_h, h

    cropped = frame[max(0, y1):y2, max(0, x1):x2]
    resized = cv2.resize(cropped, (w, h), interpolation=cv2.INTER_LANCZOS4)
    return resized


def _extract_json_object(text: str) -> dict | None:
    """Extract and parse the first JSON object found in text."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        return None
    return None


def _load_transcript_segments(video_path: str, tmp_dir: str | None = None) -> list:
    """Load segments from the Whisper transcript if present.

    ``run_pipeline`` passes the current intermediate ``.mp4`` (e.g. …_studio.mp4);
    transcripts stay keyed by the trim/ingest stem (e.g. ``IMG_17922_transcript.json``).
    """
    if tmp_dir is None:
        tmp_dir = env_paths.tmp_dir()
    raw_stem = os.path.splitext(os.path.basename(video_path))[0]
    base = stem_for_editor_gate(raw_stem)
    transcript_path = os.path.join(tmp_dir, f"{base}_transcript.json")
    if raw_stem != base:
        print(
            f"[08b] Transcript stem: {raw_stem!r} → {base!r} "
            f"(using .tmp/{base}_transcript.json)",
            flush=True,
        )
    if not os.path.exists(transcript_path):
        print(
            f"[08b] No transcript file at {transcript_path}",
            flush=True,
        )
        return []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[08b] Could not read transcript: {exc}", flush=True)
        return []
    segs = data.get("segments") or []
    return segs if isinstance(segs, list) else []


def suggest_zoom_moments_with_openrouter(
    segments: list,
    models: list[str],
    duration: float,
) -> tuple[list, str | None]:
    """Ask OpenRouter to pick the most impactful zoom-in moments.

    Returns (moments, model_used). `moments` is a list of
    ``{"start": float, "end": float, "reason": str}`` sorted by start time.
    Empty list on failure.
    """
    if not segments or not models:
        return [], None

    capped = segments[:MAX_LLM_SEGMENTS]
    payload_segments = []
    for idx, seg in enumerate(capped):
        text = (seg.get("text") or "").strip()
        if len(text) > MAX_LLM_SEGMENT_TEXT:
            text = text[:MAX_LLM_SEGMENT_TEXT] + "..."
        payload_segments.append({
            "index": idx,
            "start": round(float(seg.get("start", 0.0)), 3),
            "end": round(float(seg.get("end", seg.get("start", 0.0))), 3),
            "text": text,
        })

    system_prompt = (
        "You are a video editor analyzing a spoken transcript to find the most "
        "impactful moments where a tight close-up zoom would add emphasis. "
        "Pick only moments that actually benefit from a punch-in: a punchline, "
        "a key insight or conclusion, a specific number/name/product, an "
        "emotional beat, a rhetorical question, or a clear call-to-action. "
        "Avoid filler chatter, generic transitions (so/then/and), small talk, "
        "intros/outros, and housekeeping. It is perfectly fine to return zero "
        "moments if nothing stands out. Return strict JSON only."
    )
    user_prompt = (
        "Choose the zoom-in moments for this video.\n"
        f"- Each moment must span {AI_ZOOM_MIN_DURATION}-{AI_ZOOM_MAX_DURATION} seconds "
        "and align to complete spoken phrases.\n"
        f"- Return at most {AI_ZOOM_MAX_MOMENTS} moments, spaced at least "
        f"{AI_ZOOM_MIN_GAP}s apart.\n"
        "- If no moments are strong enough, return an empty array.\n\n"
        "Return exactly this JSON schema:\n"
        '{ "moments": ['
        '{ "start": 0.0, "end": 0.0, "reason": "short justification", '
        '"confidence": 0.0 } ] }\n'
        "- start and end are seconds from the provided segment timeline.\n"
        "- end must be > start, both within [0, video_duration_sec].\n"
        "- confidence is between 0 and 1.\n\n"
        f"video_duration_sec: {round(float(duration), 3)}\n\n"
        f"Segments:\n{json.dumps(payload_segments, ensure_ascii=False)}"
    )

    print(
        f"[08b][openrouter] calling LLM: {len(models)} model(s), "
        f"up to {min(len(segments), MAX_LLM_SEGMENTS)} segment(s), "
        f"duration={duration:.3f}s",
        flush=True,
    )
    for model in models:
        print(
            f"[08b][openrouter] trying model={model!r}",
            flush=True,
        )
        try:
            response_text = chat_completion(
                model=model,
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                temperature=0.2,
                max_tokens=900,
            )
        except Exception as exc:
            print(f"[08b][openrouter] model={model!r} request_failed: {exc}", flush=True)
            continue

        parsed = _extract_json_object(response_text)
        if not parsed:
            preview = (response_text or "").strip().replace("\n", " ")
            if len(preview) > 500:
                preview = preview[:500] + "…"
            print(
                f"[08b][openrouter] model={model!r} parse_failed: raw preview: {preview!r}",
                flush=True,
            )
            continue

        raw_moments = parsed.get("moments", [])
        if not isinstance(raw_moments, list):
            print(f"[08b] OpenRouter '{model}' missing 'moments' array.")
            continue

        cleaned_moments = []
        for item in raw_moments:
            if not isinstance(item, dict):
                continue
            try:
                start = float(item.get("start"))
                end = float(item.get("end"))
            except (TypeError, ValueError):
                continue
            if not (end > start):
                continue
            start = max(0.0, min(start, duration))
            end = max(0.0, min(end, duration))
            dur = end - start
            if dur < AI_ZOOM_MIN_DURATION:
                end = min(duration, start + AI_ZOOM_MIN_DURATION)
            if end - start > AI_ZOOM_MAX_DURATION:
                end = start + AI_ZOOM_MAX_DURATION
            if end - start < 0.5:
                continue
            cleaned_moments.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "reason": str(item.get("reason", "")).strip()[:140],
            })

        cleaned_moments.sort(key=lambda m: m["start"])

        # Enforce minimum gap between moments (drop ones too close to predecessor).
        deduped = []
        for m in cleaned_moments:
            if deduped:
                last_end = deduped[-1]["end"]
                if m["start"] < last_end + AI_ZOOM_MIN_GAP:
                    continue
            deduped.append(m)
            if len(deduped) >= AI_ZOOM_MAX_MOMENTS:
                break

        if deduped:
            print(
                f"[08b][openrouter] ok model={model!r} moments={len(deduped)}",
                flush=True,
            )
            return deduped, model

        print(
            f"[08b][openrouter] model={model!r} returned no usable moments after validation.",
            flush=True,
        )

    print("[08b][openrouter] all models exhausted; no zoom moments.", flush=True)
    return [], None


def _segments_from_zoom_moments(moments: list, duration: float) -> list:
    """Interleave wide ranges between zoom moments, covering [0, duration].

    Returns a list of tuples (start, end, is_zoomed).
    """
    if not moments:
        return [(0.0, duration, False)]

    result = []
    cursor = 0.0
    min_wide = 0.05
    for m in moments:
        m_start = max(cursor, float(m["start"]))
        m_end = min(duration, float(m["end"]))
        if m_end <= m_start:
            continue
        if m_start - cursor >= min_wide:
            result.append((cursor, m_start, False))
        result.append((m_start, m_end, True))
        cursor = m_end
    if duration - cursor >= min_wide:
        result.append((cursor, duration, False))
    return result


def apply_hard_cut_zoom(video_path: str, tmp_dir: str | None = None) -> str:
    if tmp_dir is None:
        tmp_dir = env_paths.tmp_dir()
    base = os.path.splitext(os.path.basename(video_path))[0]

    input_video = os.path.join(tmp_dir, f"{base}_multicam.mp4")
    if not os.path.exists(input_video):
        input_video = os.path.join(tmp_dir, f"{base}_effects.mp4")
    if not os.path.exists(input_video):
        input_video = os.path.join(tmp_dir, f"{base}_color.mp4")
    if not os.path.exists(input_video):
        input_video = os.path.join(tmp_dir, f"{base}_fixed_audio.mp4")
    if not os.path.exists(input_video):
        input_video = video_path

    output_path = os.path.join(tmp_dir, f"{base}_hardcut.mp4")

    positions, cv_width, cv_height, fps, first_frame_wh = detect_face_positions(input_video)
    positions = smooth_positions(positions)

    width, height, duration = probe_video_stream_size_duration(input_video)
    if duration <= 0:
        raise RuntimeError("[08b] Could not determine input video duration")
    if first_frame_wh is not None:
        fw, fh = first_frame_wh
        if (fw, fh) != (width, height):
            # OpenCV may apply display rotation; FFmpeg filter graph uses stream
            # dimensions. Keep ffprobe WxH for scale/concat; expression crop clamps
            # to actual iw/ih so zoom never exceeds the frame.
            print(
                f"[08b] Note: first OpenCV frame is {fw}x{fh} but stream is "
                f"{width}x{height} (rotation/metadata); using stream size for output."
            )
    if (cv_width, cv_height) != (width, height):
        print(
            f"[08b] OpenCV reported {cv_width}x{cv_height} but FFmpeg stream is "
            f"{width}x{height}; crop/zoom uses FFmpeg size."
        )

    mode = "follow" if FACE_FOLLOW else "lock"
    if FACE_FOLLOW:
        print("[08b] FACE_FOLLOW=True is not supported in FFmpeg segment mode; using locked face per segment.")

    # ── Try LLM-driven zoom moments first ───────────────────────────────
    ai_zoom_segments: list = []
    ai_model: str | None = None
    ai_moments: list = []
    transcript_base = stem_for_editor_gate(
        os.path.splitext(os.path.basename(video_path))[0]
    )
    transcript_path_guess = os.path.join(tmp_dir, f"{transcript_base}_transcript.json")
    if has_openrouter_api_key():
        candidate_models = parse_models_from_env()
        print(
            f"[08b][openrouter] probe: api_key=set models={candidate_models!r} "
            f"transcript_expected={transcript_path_guess!r}",
            flush=True,
        )
        if candidate_models:
            transcript_segments = _load_transcript_segments(video_path, tmp_dir)
            print(
                f"[08b][openrouter] transcript_segments={len(transcript_segments)}",
                flush=True,
            )
            if transcript_segments:
                moments, ai_model = suggest_zoom_moments_with_openrouter(
                    segments=transcript_segments,
                    models=candidate_models,
                    duration=duration,
                )
                if moments:
                    for m in moments:
                        reason = f" — {m['reason']}" if m.get("reason") else ""
                        print(
                            f"[08b][openrouter] moment {m['start']:.2f}s → {m['end']:.2f}s{reason}",
                            flush=True,
                        )
                    ai_zoom_segments = _segments_from_zoom_moments(moments, duration)
                    ai_moments = moments
                    print(
                        f"[08b][openrouter] result: using_ai_zoom model={ai_model!r} "
                        f"moments={len(ai_moments)}",
                        flush=True,
                    )
                else:
                    print(
                        "[08b][openrouter] result: llm_ok_but_no_moments → fixed cut interval",
                        flush=True,
                    )
            else:
                print(
                    "[08b][openrouter] skip: no transcript segments "
                    "(check path above)",
                    flush=True,
                )
        else:
            print(
                "[08b][openrouter] skip: OPENROUTER_MODEL(S) not set",
                flush=True,
            )
    else:
        print("[08b][openrouter] skip: OPENROUTER_API_KEY not set", flush=True)

    # Persist AI moments (or empty list) so downstream steps — e.g. 08d FX
    # sounds — know where the impactful beats are without re-querying the LLM.
    moments_path = os.path.join(tmp_dir, f"{base}_zoom_moments.json")
    try:
        os.makedirs(tmp_dir, exist_ok=True)
        with open(moments_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "video": video_path,
                    "duration": round(float(duration), 3),
                    "model": ai_model,
                    "moments": ai_moments,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except OSError as exc:
        print(f"[08b] Could not write zoom moments sidecar: {exc}")

    # ── Build the FFmpeg segment list ───────────────────────────────────
    if ai_zoom_segments:
        segments_plan = ai_zoom_segments
        zoom_count = sum(1 for s in segments_plan if s[2])
        print(
            f"[08b] Using AI-selected zoom moments ({zoom_count} tight, "
            f"{len(segments_plan) - zoom_count} wide) via '{ai_model}'."
        )
    else:
        print(f"[08b] Falling back to fixed {CUT_INTERVAL}s alternating cuts.")
        segment_count = max(1, math.ceil(duration / CUT_INTERVAL))
        segments_plan = []
        for seg_idx in range(segment_count):
            seg_start = seg_idx * CUT_INTERVAL
            seg_end = min((seg_idx + 1) * CUT_INTERVAL, duration)
            is_zoomed = (seg_idx % 2 == 1)
            segments_plan.append((seg_start, seg_end, is_zoomed))

    total_cuts = max(0, len(segments_plan) - 1)
    print(f"[08b] Applying {total_cuts} hard cuts over {duration:.1f}s")
    print(f"[08b] Zoom: wide=1.0x, tight={ZOOM_LEVEL}x, mode={mode}, resolution={width}x{height}")

    filter_parts = []
    for seg_idx, (seg_start, seg_end, is_zoomed) in enumerate(segments_plan):
        segment_filter = f"[0:v]trim=start={seg_start:.6f}:end={seg_end:.6f},setpts=PTS-STARTPTS"
        if is_zoomed:
            cx, cy = get_face_at_time(positions, seg_start)
            segment_filter += zoom_crop_scale_ffmpeg(cx, cy, width, height, ZOOM_LEVEL)
        filter_parts.append(f"{segment_filter}[v{seg_idx}]")

    segment_count = len(segments_plan)
    concat_inputs = "".join([f"[v{idx}]" for idx in range(segment_count)])
    filter_parts.append(f"{concat_inputs}concat=n={segment_count}:v=1:a=0[vout]")
    filter_complex = ";".join(filter_parts)

    # FFmpeg picks muxer from the file extension; "foo.mp4.part" fails — use "foo.part.mp4".
    root, ext = os.path.splitext(output_path)
    output_partial = f"{root}.part{ext}"

    # Hardware-accelerated decode on macOS gives a large speedup on iPhone HEVC,
    # which is the dominant input format. Opt out with FFMPEG_HWACCEL=none.
    hwaccel_args: list[str] = []
    hwaccel_pref = os.environ.get("FFMPEG_HWACCEL", "").lower()
    if hwaccel_pref != "none" and platform.system() == "Darwin":
        hwaccel_args = ["-hwaccel", "videotoolbox"]

    encoder_args = build_fast_pipeline_encode_args(input_video)
    encoder_name = encoder_args[1] if len(encoder_args) > 1 else "unknown"
    print(f"[08b] Encoder: {encoder_name}  hwaccel: {hwaccel_args[-1] if hwaccel_args else 'none'}")

    ffmpeg_cmd = [
        "ffmpeg", "-y",
        *hwaccel_args,
        # Coded dimensions must match ffprobe WxH; otherwise display-matrix
        # rotation yields e.g. 2160x3840 in filters while we scale to 3840x2160,
        # and concat fails (wide vs zoomed branch size mismatch).
        "-noautorotate",
        "-i", input_video,
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
        # VideoToolbox decode can fail on unusual pixel formats; retry in software.
        print("[08b] Hardware decode failed, retrying with software decode...")
        fallback = [a for a in ffmpeg_cmd if a not in hwaccel_args]
        run = subprocess.run(fallback, capture_output=True, text=True)
    if run.returncode != 0:
        if os.path.exists(output_partial):
            try:
                os.remove(output_partial)
            except OSError:
                pass
        err_tail = run.stderr[-1200:]
        raise RuntimeError(f"[08b] FFmpeg encode failed: {err_tail}")

    os.replace(output_partial, output_path)

    print(f"[08b] Output: {output_path}")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 08b_hard_cut_zoom.py <video_path>")
        sys.exit(1)
    apply_hard_cut_zoom(sys.argv[1])

"""
Step 8c: B-Roll Overlay — scans for asset files (videos, images) next to the
original ingest under input/, then falls back to input/{base}/ when the source
sits directly in input/. Analyzes transcript for placement, renders animated
split-view overlays with Remotion, and composites onto the main video.

Assets folder (first match wins):
  - Same directory as the original source video under input/ (e.g.
    input/project/clip.MOV → scan input/project/)
  - If the source is directly in input/{base}.mov, use legacy input/{base}/
  - Supports: .mp4, .mov, .webm, .jpg, .jpeg, .png, .webp, .gif
  - Filenames can hint at content (e.g. "product.jpg", "store_front.mp4")

If no assets folder exists, this step is skipped.

Optional semantic matching via OpenRouter:
  - OPENROUTER_API_KEY=<key>
  - OPENROUTER_MODEL=<provider/model> or OPENROUTER_MODELS=<m1,m2,m3>
When configured, the script tries the provided model(s) in order and falls
back to local keyword matching if the API call fails.
"""
import sys
import os
import json
import subprocess
import re
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from openrouter_client import chat_completion, has_openrouter_api_key, parse_models_from_env
from video_encoding import (
    build_color_preserving_composite_encode_args,
    first_existing_nonempty_video,
    source_color_normalize_filter,
)
from editor_gate import stem_for_editor_gate

# --- Config ---
MIN_BROLL_DURATION = 3.0  # seconds minimum per b-roll
MAX_BROLL_DURATION = 6.0  # seconds max per b-roll
SPLIT_RATIO = 0.45        # how much of the frame the b-roll takes
# FFmpeg scale/crop require strictly positive width/height; margins shrink on small frames.
MIN_BROLL_PANEL = 0  # minimum split panel size (px) along the short axis
BROLL_MARGIN_CAP = 0
REMOTION_DIR = os.path.join(os.path.dirname(__file__), "broll-renderer")
USE_REMOTION_BY_DEFAULT = True

# How many Remotion clip renders to run concurrently. Each Remotion render
# spins up a Chrome headless instance (~400MB RAM), so keep this modest on
# laptops. Override via env BROLL_REMOTION_WORKERS.
DEFAULT_REMOTION_WORKERS = 3

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv"}
# Extensions used when locating the original file under input/ (for assets dir).
SOURCE_VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".avi", ".webm")

ANIMATIONS = [ "none"]
# POSITIONS = ["top", "bottom"]
POSITIONS = ["bottom"]
# Aspect ratio above which an asset is considered "vertical" (portrait).
# Small slack so ~square 1:1 doesn't get treated as vertical.
VERTICAL_ASPECT_THRESHOLD = 1.05
MAX_LLM_SEGMENTS = 80
MAX_LLM_SEGMENT_TEXT = 220
MAX_LLM_NEIGHBOR_TEXT = 100


def _probe_asset_dimensions(path: str) -> tuple[int, int] | None:
    """Return (width, height) for an image or video asset via ffprobe.

    ffprobe handles both still images and videos through a v:0 stream,
    so one codepath covers `.jpg`, `.png`, `.mp4`, `.mov`, etc.
    Returns None on failure so callers can fall back to split-panel layout.
    """
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet",
             "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "json", path],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(probe.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            return None
        w = int(streams[0].get("width") or 0)
        h = int(streams[0].get("height") or 0)
        if w <= 0 or h <= 0:
            return None
        return w, h
    except Exception:
        return None


def _is_vertical_asset(width: int, height: int) -> bool:
    if width <= 0:
        return False
    return (height / width) >= VERTICAL_ASPECT_THRESHOLD


def _find_input_source_paths_for_base(base: str, input_root: str) -> list[str]:
    """Return absolute paths to video files under input_root named {base}.<ext>."""
    if not base or not os.path.isdir(input_root):
        return []
    found: list[str] = []
    for root, _dirs, files in os.walk(input_root):
        for name in files:
            if name.startswith("."):
                continue
            stem, ext = os.path.splitext(name)
            if stem != base:
                continue
            if ext.lower() not in SOURCE_VIDEO_EXTENSIONS:
                continue
            p = os.path.join(root, name)
            try:
                if os.path.isfile(p) and os.path.getsize(p) > 0:
                    found.append(os.path.abspath(p))
            except OSError:
                continue
    return found


def _pick_input_source_path(candidates: list[str], input_abs: str) -> str | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Prefer shallowest path under input/, then shortest string (stable tie-break).
    def sort_key(p: str) -> tuple[int, int, str]:
        rel = os.path.relpath(p, input_abs)
        depth = 0 if rel in (".", os.curdir) else rel.count(os.sep) + 1
        return (depth, len(p), p)

    return sorted(candidates, key=sort_key)[0]


def _ingest_stem_from_video_path(video_path: str) -> str:
    """Stem of the original ingest (e.g. IMG_1792), not the latest pipeline .mp4 name."""
    raw = os.path.splitext(os.path.basename(video_path))[0]
    return stem_for_editor_gate(raw)


def resolve_broll_assets_directory(video_path: str) -> tuple[str | None, str]:
    """Directory to scan for B-roll files, and the video basename stem.

    Uses the folder that contains the original ingest under ``input/`` when
    that folder is deeper than ``input/`` (assets live beside the source).
    When the source file sits directly in ``input/``, uses legacy
    ``input/{base}/`` so we do not scan all of ``input/``.
    """
    base = _ingest_stem_from_video_path(video_path)
    input_abs = os.path.abspath("input")
    if not os.path.isdir(input_abs):
        return None, base

    abs_vp = os.path.abspath(video_path)
    candidates: list[str] = []
    seen_c: set[str] = set()

    def add_cand(p: str) -> None:
        p = os.path.abspath(p)
        if p not in seen_c:
            seen_c.add(p)
            candidates.append(p)

    if abs_vp.startswith(input_abs + os.sep):
        add_cand(abs_vp)

    for p in _find_input_source_paths_for_base(base, input_abs):
        add_cand(p)

    source_path = _pick_input_source_path(candidates, input_abs)
    if source_path:
        orig_dir = os.path.dirname(source_path)
        if os.path.abspath(orig_dir) == input_abs:
            legacy = os.path.join(input_abs, base)
            return (legacy if os.path.isdir(legacy) else None), base
        return orig_dir, base

    legacy = os.path.join(input_abs, base)
    return (legacy if os.path.isdir(legacy) else None), base


def _is_primary_source_asset(filename: str, base: str) -> bool:
    """True if this file is the main recording (same stem as pipeline base)."""
    stem, ext = os.path.splitext(filename)
    return stem == base and ext.lower() in SOURCE_VIDEO_EXTENSIONS


def find_assets(video_path: str) -> list:
    """Find images/videos for B-roll in the resolved assets directory."""
    assets_dir, base = resolve_broll_assets_directory(video_path)
    if not assets_dir:
        return []

    assets = []
    for f in sorted(os.listdir(assets_dir)):
        if _is_primary_source_asset(f, base):
            continue
        ext = os.path.splitext(f)[1].lower()
        full_path = os.path.abspath(os.path.join(assets_dir, f))
        if ext in IMAGE_EXTENSIONS:
            asset = {"path": full_path, "name": f, "type": "image"}
        elif ext in VIDEO_EXTENSIONS:
            try:
                probe = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", full_path],
                    capture_output=True, text=True, check=True,
                )
                dur = float(probe.stdout.strip())
                asset = {"path": full_path, "name": f, "type": "video", "duration": dur}
            except Exception:
                asset = {"path": full_path, "name": f, "type": "video", "duration": 5.0}
        else:
            continue

        dims = _probe_asset_dimensions(full_path)
        if dims:
            asset["width"], asset["height"] = dims
            asset["is_vertical"] = _is_vertical_asset(*dims)
        else:
            asset["is_vertical"] = False
        assets.append(asset)

    return assets


def extract_keywords_from_filename(filename: str) -> list:
    """Extract keywords from asset filename for matching."""
    name = os.path.splitext(filename)[0]
    # Split by common separators
    words = re.split(r"[-_ .]+", name.lower())
    # Filter out generic words and numbers
    stopwords = {"img", "image", "photo", "video", "clip", "broll",
                 "asset", "media", "content", "dsc", "mov", "screenshot"}
    return [w for w in words if len(w) > 2 and w not in stopwords and not w.isdigit()]


def _segment_text_for_keywords(seg: dict) -> str:
    """Segment text for keyword scoring: concat + space-joined word stream (clearer for Whisper)."""
    t = (seg.get("text") or "").lower()
    wlist = seg.get("words") or []
    if isinstance(wlist, list) and wlist:
        alt = " ".join(str(w.get("word", "")) for w in wlist if isinstance(w, dict)).lower()
        if alt.strip():
            return f"{t} {alt}"
    return t


def anchor_start_in_segment(segment: dict, keywords: list[str]) -> float:
    """First spoken moment within *segment* that matches *keywords*; else segment start.

    Uses per-word timestamps from the transcript. Clamped so the anchor is not
    in the last 0.25s of the segment (keeps a minimal alignment margin).
    """
    seg_s = float(segment.get("start", 0.0) or 0.0)
    seg_e = float(segment.get("end", seg_s) or seg_s)
    if seg_e < seg_s:
        seg_e = seg_s

    upper = max(seg_s, seg_e - 0.25)
    best: float | None = None

    if keywords:
        wrows = segment.get("words")
        if isinstance(wrows, list) and wrows:
            for w in wrows:
                if not isinstance(w, dict):
                    continue
                wtext = str(w.get("word", "")).lower()
                if not any(kw in wtext for kw in keywords if kw):
                    continue
                try:
                    ws = float(w.get("start", seg_s))
                except (TypeError, ValueError):
                    ws = seg_s
                if best is None or ws < best:
                    best = ws

    if best is None:
        anchor = seg_s
    else:
        anchor = best

    anchor = min(max(anchor, seg_s), upper)
    return anchor


def _resolve_broll_start_time(
    matched_seg: dict | None,
    asset: dict,
) -> float:
    """Ideal overlay start: word-anchored when segment + filename keywords allow."""
    if not matched_seg:
        return 0.0
    kws = extract_keywords_from_filename(asset.get("name", ""))
    return anchor_start_in_segment(matched_seg, kws)


def _pack_broll_no_overlap(
    ideal_start: float,
    broll_dur: float,
    video_duration: float,
    used: list[tuple[float, float]],
) -> tuple[float, float] | None:
    """Slide the [t, t+broll_dur) window forward until it does not overlap *used* (non-destructive to order)."""
    if video_duration <= 0.0:
        return None
    tail = max(0.0, video_duration - 0.5)
    t = max(0.0, float(ideal_start))
    for _ in range(len(used) * 2 + 128):
        end = min(t + broll_dur, tail)
        if end <= t:
            return None
        new_t = t
        for us, ue in used:
            if t < ue and end > us:
                new_t = max(new_t, ue)
        if new_t - t < 1e-9:
            return (t, end)
        t = new_t
    return None


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


def _snippet(s: str, max_len: int) -> str:
    t = (s or "").strip()
    if len(t) <= max_len:
        return t
    return t[:max_len].rstrip() + "..."


def suggest_segments_with_openrouter(
    assets: list,
    segments: list,
    models: list[str],
    video_duration_sec: float,
) -> tuple[dict, str | None]:
    """
    Use OpenRouter LLM to map asset filenames to transcript segment indices.
    Returns (asset_name -> segment_index, model_used). Empty map on failure.
    """
    if not assets or not segments or not models:
        return {}, None

    # Limit transcript payload to keep token usage predictable.
    payload_segments = []
    capped = segments[:MAX_LLM_SEGMENTS]
    n_cap = len(capped)
    for idx, seg in enumerate(capped):
        text = seg.get("text", "").strip()
        if len(text) > MAX_LLM_SEGMENT_TEXT:
            text = text[:MAX_LLM_SEGMENT_TEXT] + "..."

        seg_end = float(seg.get("end", seg.get("start", 0.0)))
        until_end = max(0.0, float(video_duration_sec) - seg_end)

        prev_text = ""
        if idx > 0:
            prev_text = _snippet(capped[idx - 1].get("text", ""), MAX_LLM_NEIGHBOR_TEXT)
        next_text = ""
        if idx + 1 < n_cap:
            next_text = _snippet(capped[idx + 1].get("text", ""), MAX_LLM_NEIGHBOR_TEXT)

        payload_segments.append({
            "index": idx,
            "start": round(float(seg.get("start", 0.0)), 3),
            "end": round(seg_end, 3),
            "text": text,
            "context_before": prev_text,
            "context_after": next_text,
            "seconds_until_video_end": round(until_end, 2),
        })

    asset_names = [a["name"] for a in assets]

    system_prompt = (
        "You map B-roll assets to transcript segments. Use each segment's text plus "
        "context_before and context_after to understand what is being said and what comes next. "
        "Use seconds_until_video_end to treat late-video speech (sign-offs, CTAs, thanks, "
        "subscribe prompts, housekeeping) as low-value for illustrative B-roll unless the "
        "asset clearly belongs there. Prefer segments where the asset visually supports the "
        "main idea; use null when no segment needs that asset or only weak filler matches exist. "
        "Return strict JSON only."
    )
    user_prompt = (
        "Choose the best transcript segment index for each asset.\n"
        "Match filename meaning to the spoken content; use neighbor context to avoid "
        "ambiguous picks. Do not force a match into outros or generic closing chatter.\n"
        "If no good segment exists, use null for segment_index.\n\n"
        "Return exactly this JSON schema:\n"
        '{ "matches": [ { "asset": "filename.ext", "segment_index": 0, "confidence": 0.0 } ] }\n'
        "- segment_index must be null or an integer from the provided segment list.\n"
        "- confidence must be between 0 and 1.\n\n"
        f"video_duration_sec: {round(float(video_duration_sec), 3)}\n\n"
        f"Assets:\n{json.dumps(asset_names, ensure_ascii=False)}\n\n"
        f"Segments:\n{json.dumps(payload_segments, ensure_ascii=False)}"
    )

    for model in models:
        print(f"[08c] OpenRouter: trying model '{model}' for asset matching...")
        try:
            response_text = chat_completion(
                model=model,
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                temperature=0.1,
                max_tokens=900,
            )
            parsed = _extract_json_object(response_text)
            if not parsed:
                print(f"[08c] OpenRouter '{model}' returned non-JSON output.")
                continue

            raw_matches = parsed.get("matches", [])
            if not isinstance(raw_matches, list):
                print(f"[08c] OpenRouter '{model}' missing matches array.")
                continue

            mapping = {}
            for item in raw_matches:
                if not isinstance(item, dict):
                    continue
                asset_name = str(item.get("asset", "")).strip()
                if asset_name not in asset_names:
                    continue
                seg_idx = item.get("segment_index")
                if isinstance(seg_idx, int) and 0 <= seg_idx < len(payload_segments):
                    mapping[asset_name] = seg_idx

            if mapping:
                print(f"[08c] OpenRouter selected {len(mapping)} matches with model '{model}'.")
                return mapping, model

            print(f"[08c] OpenRouter '{model}' returned no valid matches.")
        except Exception as exc:
            print(f"[08c] OpenRouter '{model}' failed: {exc}")

    return {}, None


def match_assets_to_segments(assets: list, segments: list, video_duration: float) -> list:
    """Match each asset to the best transcript segment based on content.
    Falls back to even distribution if no keyword matches found."""

    if not segments or not assets:
        return []

    placements = []
    used_time_ranges = []

    def find_best_segment(asset, available_segments):
        """Try keyword match first, then fall back to spacing."""
        keywords = extract_keywords_from_filename(asset["name"])
        best_score = 0
        best_seg = None

        if keywords:
            for seg in available_segments:
                text = _segment_text_for_keywords(seg)
                score = sum(1 for kw in keywords if kw in text)
                if score > best_score:
                    best_score = score
                    best_seg = seg
        return best_seg

    # Sort segments by start time
    sorted_segments = sorted(segments, key=lambda s: s.get("start", 0))

    llm_mapping = {}
    llm_model = None
    if has_openrouter_api_key():
        candidate_models = parse_models_from_env()
        if candidate_models:
            llm_mapping, llm_model = suggest_segments_with_openrouter(
                assets=assets,
                segments=sorted_segments,
                models=candidate_models,
                video_duration_sec=video_duration,
            )
        else:
            print("[08c] OPENROUTER_API_KEY found, but no OPENROUTER_MODEL(S) configured.")
    if llm_model:
        print(f"[08c] Using OpenRouter model '{llm_model}' for semantic placement hints.")

    for idx, asset in enumerate(assets):
        matched_seg = None

        # Prefer semantic mapping from OpenRouter when available.
        llm_seg_idx = llm_mapping.get(asset["name"])
        if isinstance(llm_seg_idx, int) and 0 <= llm_seg_idx < len(sorted_segments):
            matched_seg = sorted_segments[llm_seg_idx]

        # Fallback: local keyword matching.
        if matched_seg is None:
            matched_seg = find_best_segment(asset, sorted_segments)

        if matched_seg:
            start_time = _resolve_broll_start_time(matched_seg, asset)
        else:
            # Evenly distribute across video duration
            spacing = video_duration / (len(assets) + 1)
            ideal = spacing * (idx + 1)
            # Snap to nearest segment, then word-anchor within it
            closest = min(sorted_segments, key=lambda s: abs(s["start"] - ideal))
            start_time = anchor_start_in_segment(
                closest, extract_keywords_from_filename(asset["name"])
            )

        # Determine duration
        if asset["type"] == "video" and "duration" in asset:
            broll_dur = min(max(asset["duration"], MIN_BROLL_DURATION), MAX_BROLL_DURATION)
        else:
            broll_dur = random.uniform(MIN_BROLL_DURATION, MAX_BROLL_DURATION)

        # Slide forward in time only (keeps B-roll after its ideal moment vs arbitrary segment jumps)
        packed = _pack_broll_no_overlap(
            start_time, broll_dur, video_duration, used_time_ranges
        )
        if packed is None:
            continue
        start_time, end_time = packed

        used_time_ranges.append((start_time, end_time))

        # Cycle through animation/position styles
        anim = ANIMATIONS[idx % len(ANIMATIONS)]
        # Vertical assets take the entire screen; horizontal ones use the
        # split panel (bottom/top/left/right) as before.
        if asset.get("is_vertical"):
            pos = "fullscreen"
        else:
            pos = POSITIONS[idx % len(POSITIONS)]

        placements.append({
            "asset": asset,
            "start_time": round(start_time, 3),
            "end_time": round(end_time, 3),
            "duration": round(end_time - start_time, 3),
            "animation": anim,
            "position": pos,
        })

    placements.sort(key=lambda p: p["start_time"])
    return placements


def calc_broll_dimensions(pos: str, vid_w: int, vid_h: int) -> tuple:
    """Calculate B-roll inner dimensions and overlay position.

    `fullscreen` covers the entire main video (used for vertical assets on a
    vertical main video). Other positions keep the original split-panel layout.
    """
    vid_w = max(1, int(vid_w))
    vid_h = max(1, int(vid_h))
    if pos == "fullscreen":
        bw = vid_w
        bh = vid_h
    elif pos in ("left", "right"):
        bw = min(vid_w, max(MIN_BROLL_PANEL, int(vid_w * SPLIT_RATIO)))
        bh = vid_h
    else:
        bw = vid_w
        bh = min(vid_h, max(MIN_BROLL_PANEL, int(vid_h * SPLIT_RATIO)))

    # Keep inner_w / inner_h >= 1 for FFmpeg scale=crop=
    margin = min(
        BROLL_MARGIN_CAP,
        max(0, (bw - 1) // 2),
        max(0, (bh - 1) // 2),
    )
    inner_w = bw - margin * 2
    inner_h = bh - margin * 2

    if pos == "fullscreen":
        x, y = margin, margin
    elif pos == "left":
        x, y = margin, margin
    elif pos == "right":
        x, y = vid_w - bw + margin, margin
    elif pos == "top":
        x, y = margin, margin
    else:  # bottom
        x, y = margin, vid_h - bh + margin

    return inner_w, inner_h, x, y


def render_broll_clip(placement: dict, idx: int, vid_w: int, vid_h: int,
                      fps: float, tmp_dir: str) -> str | None:
    """Render a single B-roll clip with Remotion at the B-roll's own dimensions.

    Uses Remotion's `--public-dir` feature pointing at the asset's parent
    folder so `staticFile(assetName)` resolves to a URL Chrome can fetch
    (the previous implementation passed raw absolute paths, which Chrome
    could not load — that caused the composition to render a solid black
    background for the entire clip)."""
    inner_w, inner_h, _, _ = calc_broll_dimensions(
        placement["position"], vid_w, vid_h
    )

    asset_path = placement["asset"]["path"]
    asset_dir = os.path.dirname(os.path.abspath(asset_path))
    asset_name = os.path.basename(asset_path)

    duration_frames = max(1, int(round(placement["duration"] * fps)))

    config = {
        "fps": int(round(fps)),
        "width": inner_w,
        "height": inner_h,
        "durationInFrames": duration_frames,
        "mainVideoSrc": "",
        "segments": [{
            "assetName": asset_name,
            "assetType": placement["asset"]["type"],
            "startFrame": 0,
            "durationFrames": duration_frames,
            "animation": placement["animation"],
            "splitRatio": SPLIT_RATIO,
            "position": placement["position"],
        }],
    }

    props_path = os.path.abspath(os.path.join(tmp_dir, f"broll_clip_{idx}.props.json"))
    output_path = os.path.abspath(os.path.join(tmp_dir, f"broll_clip_{idx}.mp4"))

    # Pass the config via --props so it survives BOTH the Node composition
    # scan AND the in-browser render. Reading the file from disk inside
    # index.tsx silently fails inside Chrome headless (no `fs` module), which
    # is what caused every clip to render black before this change.
    with open(props_path, "w") as f:
        json.dump(config, f, indent=2)

    # Visually-lossless H.264 with a fast preset. The clip will be overlayed
    # by FFmpeg in composite_broll_clips() which re-encodes the final video,
    # so there's no point spending minutes on a high-effort preset here.
    # Keep Remotion's own concurrency at 1 because we parallelize clips from
    # Python via ThreadPoolExecutor (multiple Chrome instances in parallel).
    cmd = [
        "npx", "remotion", "render",
        "src/index.tsx", "BrollComposition",
        output_path,
        "--props", props_path,
        "--codec", "h264",
        "--crf", "20",
        "--x264-preset", "veryfast",
        "--concurrency", "3",
        "--public-dir", asset_dir,
        "--log", "error",
    ]
    try:
        result = subprocess.run(
            cmd, cwd=REMOTION_DIR,
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        print(f"  Remotion clip {idx} timed out")
        return None

    if result.returncode != 0:
        err = (result.stderr or "") + "\n" + (result.stdout or "")
        print(f"  Remotion clip {idx} failed (exit {result.returncode}):")
        for line in err.strip().splitlines()[-30:]:
            print(f"    {line}")
        return None

    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        print(f"  Remotion clip {idx} produced no output")
        return None

    try:
        os.remove(props_path)
    except OSError:
        pass
    return output_path


def _render_clips_parallel(placements: list, vid_w: int, vid_h: int,
                           fps: float, tmp_dir: str,
                           max_workers: int) -> list:
    """Render all B-roll clips with Remotion in parallel.

    Preserves input ordering in the returned list (clip N -> result[N]),
    but completes work out of order so progress prints reflect wall-clock
    finish order, not submission order."""
    total = len(placements)
    results: list = [None] * total
    if total == 0:
        return results

    workers = max(1, min(max_workers, total))
    print(f"[08c] Rendering {total} B-roll clips with Remotion "
          f"(parallel workers={workers})...")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_idx = {
            ex.submit(render_broll_clip, p, i, vid_w, vid_h, fps, tmp_dir): i
            for i, p in enumerate(placements)
        }
        done = 0
        for fut in as_completed(future_to_idx):
            i = future_to_idx[fut]
            name = placements[i]["asset"]["name"]
            try:
                results[i] = fut.result()
            except Exception as exc:
                print(f"  [{done + 1}/{total}] {name} ERROR: {exc}")
                results[i] = None
            else:
                status = "ok" if results[i] else "failed"
                done_dur = placements[i]["duration"]
                print(f"  [{done + 1}/{total}] {name} ({done_dur:.1f}s) {status}")
            done += 1
    return results


def composite_broll_clips(placements: list, clip_paths: list,
                           main_video: str, output_path: str,
                           main_w: int, main_h: int, fps: float) -> str:
    """Overlay rendered B-roll clips onto the main video using FFmpeg.
    Each clip is already rendered at the correct B-roll dimensions by Remotion."""
    inputs = ["-i", main_video]
    filter_parts = []

    # Lock the main video into yuv420p with its original color tags so the
    # overlay filter can't implicitly renegotiate colorspace between the
    # main video and the Remotion clips (which are bt709 yuv420p).
    src_fmt = source_color_normalize_filter(main_video)
    filter_parts.append(f"[0:v]{src_fmt}[bg]")
    current_label = "[bg]"

    clip_idx = 0
    for idx, p in enumerate(placements):
        clip = clip_paths[idx]
        if clip is None:
            continue
        inputs.extend(["-i", clip])
        clip_idx += 1
        inp = clip_idx

        _, _, x, y = calc_broll_dimensions(p["position"], main_w, main_h)
        start_t = p["start_time"]
        end_t = p["end_time"]

        scale_label = f"s{idx}"
        overlay_label = f"[ov{idx}]"

        # Force the clip to yuv420p too, then shift it to its timeline slot
        # so it's still alive at start_t.
        filter_parts.append(
            f"[{inp}:v]format=yuv420p,"
            f"setpts=PTS-STARTPTS+{start_t}/TB[{scale_label}]"
        )
        filter_parts.append(
            f"{current_label}[{scale_label}]overlay={x}:{y}:"
            f"enable='between(t,{start_t},{end_t})':eof_action=pass:"
            f"format=auto"
            f"{overlay_label}"
        )
        current_label = overlay_label

    if len(filter_parts) == 1:
        # Only the color-tag passthrough, no successful clips rendered.
        subprocess.run(["cp", main_video, output_path], check=True)
        return output_path

    filter_complex = ";".join(filter_parts)
    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex", filter_complex,
        "-map", current_label, "-map", "0:a",
        *build_color_preserving_composite_encode_args(main_video),
        "-c:a", "copy",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[08c] FFmpeg composite error: {result.stderr[-800:]}")
        raise RuntimeError(f"FFmpeg compositing failed (exit {result.returncode})")

    return output_path


def apply_broll_ffmpeg(placements: list, main_video: str,
                        output_path: str, main_w: int, main_h: int) -> str:
    """Direct FFmpeg compositing without Remotion (simpler animations)."""
    if not placements:
        subprocess.run(["cp", main_video, output_path], check=True)
        return output_path

    inputs = ["-i", main_video]
    for p in placements:
        inputs.extend(["-i", p["asset"]["path"]])

    filter_parts = []

    # Same color-preserving preamble as composite_broll_clips — force the
    # main video into a known pixel format with the source's color tags so
    # overlay cannot auto-negotiate into a slightly different colorspace.
    src_fmt = source_color_normalize_filter(main_video)
    filter_parts.append(f"[0:v]{src_fmt}[bg]")
    current_label = "[bg]"

    for idx, p in enumerate(placements):
        inp_idx = idx + 1
        start_t = p["start_time"]
        end_t = p["end_time"]
        pos = p["position"]

        inner_w, inner_h, x, y = calc_broll_dimensions(pos, main_w, main_h)

        scale_label = f"scaled{idx}"
        filter_parts.append(
            f"[{inp_idx}:v]scale={inner_w}:{inner_h}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={inner_w}:{inner_h},"
            f"format=yuv420p,"
            f"setpts=PTS-STARTPTS+{start_t}/TB"
            f"[{scale_label}]"
        )

        overlay_label = f"[ov{idx}]"
        filter_parts.append(
            f"{current_label}[{scale_label}]overlay={x}:{y}:"
            f"enable='between(t,{start_t},{end_t})':eof_action=pass:"
            f"format=auto"
            f"{overlay_label}"
        )
        current_label = overlay_label

    filter_complex = ";".join(filter_parts)
    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex", filter_complex,
        "-map", current_label, "-map", "0:a",
        *build_color_preserving_composite_encode_args(main_video),
        "-c:a", "copy",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[08c] FFmpeg error: {result.stderr[-800:]}")
        raise RuntimeError(f"FFmpeg compositing failed (exit {result.returncode})")

    return output_path


def apply_broll(video_path: str, tmp_dir: str = ".tmp") -> str:
    base = _ingest_stem_from_video_path(video_path)

    # Find assets
    assets_dir, _ = resolve_broll_assets_directory(video_path)
    assets = find_assets(video_path)
    if not assets:
        hint = (
            os.path.abspath(assets_dir)
            if assets_dir
            else f"input/ (no folder beside source or input/{base}/)"
        )
        print(f"[08c] No B-roll assets found (looked under {hint}), skipping.")
        return ""

    print(f"[08c] Found {len(assets)} assets in {os.path.abspath(assets_dir)}:")
    for a in assets:
        print(f"  - {a['name']} ({a['type']})")

    # Resolve input video (skip empty/corrupt intermediates, e.g. failed 08b)
    candidates = [
        os.path.join(tmp_dir, f"{base}_hardcut.mp4"),
        os.path.join(tmp_dir, f"{base}_multicam.mp4"),
        os.path.join(tmp_dir, f"{base}_effects.mp4"),
        os.path.join(tmp_dir, f"{base}_color.mp4"),
        os.path.join(tmp_dir, f"{base}_fixed_audio.mp4"),
        os.path.join(tmp_dir, f"{base}_studio.mp4"),
        video_path,
    ]
    input_video = first_existing_nonempty_video(candidates)
    if not input_video:
        print("[08c] No readable input video found, skipping.")
        return ""
    if input_video != candidates[0]:
        print(f"[08c] Using {input_video} (first choice missing or empty)")

    output_path = os.path.join(tmp_dir, f"{base}_broll.mp4")

    # Load transcript
    transcript_path = os.path.join(tmp_dir, f"{base}_transcript.json")
    segments = []
    transcript_video_duration: float | None = None
    if os.path.exists(transcript_path):
        with open(transcript_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        segments = data.get("segments", [])
        raw_dur = data.get("video_duration")
        if raw_dur is not None:
            try:
                transcript_video_duration = float(raw_dur)
            except (TypeError, ValueError):
                transcript_video_duration = None

    # Get video info
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries",
         "stream=width,height", "-show_entries", "format=duration",
         "-select_streams", "v:0", "-of", "json", input_video],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(probe.stdout)
    streams = info.get("streams") or []
    if not streams:
        print("[08c] ffprobe found no video stream, skipping.")
        return ""
    vid_w = max(1, int(streams[0].get("width") or 1))
    vid_h = max(1, int(streams[0].get("height") or 1))
    duration = float(info["format"]["duration"])
    fps_probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "stream=r_frame_rate",
         "-select_streams", "v:0", "-of", "default=noprint_wrappers=1:nokey=1",
         input_video],
        capture_output=True, text=True, check=True,
    )
    fps_str = fps_probe.stdout.strip()
    if "/" in fps_str:
        num, den = fps_str.split("/")
        fps = float(num) / float(den)
    else:
        fps = float(fps_str)

    print(f"[08c] Video: {vid_w}x{vid_h}, {duration:.1f}s, {fps:.2f}fps")
    if transcript_video_duration is not None and abs(duration - transcript_video_duration) > 0.5:
        print(
            "[08c] Warning: video duration differs from transcript video_duration "
            f"({duration:.3f}s vs {transcript_video_duration:.3f}s); "
            "B-roll timing may be wrong if the transcript is stale."
        )

    # Match assets to transcript
    placements = match_assets_to_segments(assets, segments, duration)
    if not placements:
        print("[08c] Could not place any B-rolls, skipping.")
        return ""

    print("[08c] B-roll placements:")
    for p in placements:
        print(f"  {p['start_time']:.1f}s-{p['end_time']:.1f}s: "
              f"{p['asset']['name']} ({p['animation']}, {p['position']})")

    # Strategy: render each B-roll clip individually with Remotion (animated),
    # then overlay all clips onto the main video with FFmpeg.
    # Falls back to direct FFmpeg overlay (simpler, no spring animations) if Remotion fails.
    remotion_enabled = (
        os.environ.get("BROLL_USE_REMOTION", "1").strip().lower() in {"1", "true", "yes"}
    )
    remotion_ok = (
        remotion_enabled
        and os.path.isdir(os.path.join(REMOTION_DIR, "node_modules"))
    )

    if not remotion_enabled and USE_REMOTION_BY_DEFAULT:
        remotion_ok = os.path.isdir(os.path.join(REMOTION_DIR, "node_modules"))

    if remotion_ok:
        try:
            max_workers = int(os.environ.get(
                "BROLL_REMOTION_WORKERS", str(DEFAULT_REMOTION_WORKERS)
            ))
        except ValueError:
            max_workers = DEFAULT_REMOTION_WORKERS

        clip_paths = _render_clips_parallel(
            placements, vid_w, vid_h, fps, tmp_dir, max_workers=max_workers,
        )

        if any(c is not None for c in clip_paths):
            try:
                print("[08c] Compositing clips onto main video...")
                composite_broll_clips(
                    placements, clip_paths, input_video, output_path,
                    vid_w, vid_h, fps,
                )
                # Clean up individual clips
                for c in clip_paths:
                    if c and os.path.exists(c):
                        os.remove(c)
                print(f"[08c] Output: {output_path}")
                return output_path
            except Exception as e:
                print(f"[08c] Remotion composite failed ({e}), falling back to FFmpeg...")
    elif os.path.isdir(os.path.join(REMOTION_DIR, "node_modules")):
        print("[08c] Remotion disabled (set BROLL_USE_REMOTION=1 to enable animated clips).")

    # FFmpeg fallback (direct overlay, no spring animations)
    print("[08c] Using FFmpeg direct overlay...")
    apply_broll_ffmpeg(placements, input_video, output_path, vid_w, vid_h)
    print(f"[08c] Output: {output_path}")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 08c_broll.py <video_path>")
        sys.exit(1)
    apply_broll(sys.argv[1])

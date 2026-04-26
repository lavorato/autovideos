"""
Step 8e: Data-Viz Overlay — detects numeric data points in the transcript
(currently percentages in PT-BR) and replaces the main video with a
fullscreen animated visualization (progress ring) for a couple of seconds
at each detection. Audio continues under the cutaway.

Pipeline:
  1. Parse .tmp/{trim_stem}_transcript.json (trim/ingest stem via stem_for_editor_gate) for percentage mentions (regex +
     PT-BR number words). Each hit gets its word-level start/end timestamps.
  2. Optional: send hits to OpenRouter for enrichment (label, duration,
     emphasis=growth/drop/neutral). Falls back to deterministic defaults
     if OpenRouter isn't configured or returns garbage.
  3. For each moment, materialize a HyperFrames project by substituting
     tokens into templates/percentage-ring.html.tpl, then call
     `npx hyperframes render` to produce an MP4 clip at the video's own
     dimensions/fps.
  4. Overlay every clip onto the input video via FFmpeg: each clip is opened
     with ``-itsoffset`` so its frames sit on the same clock as the main
     track, then ``overlay`` with enable='between(t,start,end)'. Audio is
     stream-copied so the voice keeps playing during the fullscreen cutaway.

Outputs:
  - .tmp/{base}_dataviz.mp4
  - .tmp/{base}_dataviz_moments.json   (sidecar for inspection / future steps)

Extensibility:
  Detection is pluggable via the DETECTORS list. To handle money/date/list
  cases in the future, add a new detector function that returns moments in
  the same shape and a new template.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict

from editor_gate import stem_for_editor_gate
from openrouter_client import (
    chat_completion,
    has_openrouter_api_key,
    parse_models_from_env,
)
import env_paths
from video_encoding import (
    build_fast_hq_x264_args,
    first_existing_nonempty_video,
)

# Locating the original ingest under input/ (same extensions as 08a/08c).
_SOURCE_VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".avi", ".webm")


def _find_input_source_paths_for_base(base: str, input_root: str) -> list[str]:
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
            if ext.lower() not in _SOURCE_VIDEO_EXTENSIONS:
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

    def sort_key(p: str) -> tuple[int, int, str]:
        rel = os.path.relpath(p, input_abs)
        depth = 0 if rel in (".", os.curdir) else rel.count(os.sep) + 1
        return (depth, len(p), p)

    return sorted(candidates, key=sort_key)[0]


def _resolve_original_source_under_input(video_path: str, base: str) -> str | None:
    """Absolute path to the ingest file under input/, if we can find it."""
    input_abs = env_paths.input_dir()
    if not os.path.isdir(input_abs):
        return None
    abs_vp = os.path.abspath(video_path)
    candidates: list[str] = []
    seen: set[str] = set()

    def add_cand(p: str) -> None:
        p = os.path.abspath(p)
        if p not in seen:
            seen.add(p)
            candidates.append(p)

    if abs_vp.startswith(input_abs + os.sep):
        add_cand(abs_vp)
    for p in _find_input_source_paths_for_base(base, input_abs):
        add_cand(p)
    return _pick_input_source_path(candidates, input_abs)


def _resolve_transcript_path(video_path: str, tmp_dir: str) -> tuple[str | None, list[str]]:
    """Return (path, tried_paths) for the first existing non-empty transcript JSON.

    ``run_pipeline`` passes the current intermediate ``.mp4`` (e.g. ``…_fx.mp4``);
    transcripts are keyed by the trim/ingest stem. Same as 08b/08c: use
    ``stem_for_editor_gate`` to map the filename stem to ``.tmp/{stem}_transcript.json``,
    then beside the original under ``input/`` (``{original_dir}/{orig_stem}_transcript.json``).
    """
    raw_stem = os.path.splitext(os.path.basename(video_path))[0]
    transcript_base = stem_for_editor_gate(raw_stem)
    tried: list[str] = []
    tmp_transcript = os.path.abspath(
        os.path.join(tmp_dir, f"{transcript_base}_transcript.json")
    )
    tried.append(tmp_transcript)
    if os.path.isfile(tmp_transcript) and os.path.getsize(tmp_transcript) > 0:
        return tmp_transcript, tried

    orig = _resolve_original_source_under_input(video_path, transcript_base)
    if orig:
        orig_stem = os.path.splitext(os.path.basename(orig))[0]
        beside = os.path.abspath(
            os.path.join(os.path.dirname(orig), f"{orig_stem}_transcript.json")
        )
        if beside not in tried:
            tried.append(beside)
        if os.path.isfile(beside) and os.path.getsize(beside) > 0:
            return beside, tried

    return None, tried


# --- Config ---
DATAVIZ_DIR = os.path.join(os.path.dirname(__file__), "dataviz-renderer")
TEMPLATE_PATH = os.path.join(DATAVIZ_DIR, "templates", "percentage-ring.html.tpl")
# We pin a local hyperframes install so Node's module resolution can't
# walk up to stale puppeteer packages in the user's home directory.
HYPERFRAMES_CLI = os.path.join(
    DATAVIZ_DIR, "node_modules", "hyperframes", "dist", "cli.js"
)
DEFAULT_DURATION = 2.0
MIN_DURATION = 1.5
MAX_DURATION = 3.0
DEFAULT_RENDER_WORKERS = 2  # Chrome headless is heavy; keep modest.
MIN_GAP_BETWEEN_MOMENTS = 1.5  # seconds — avoid stacking two stats back-to-back
MAX_LLM_MOMENTS = 12          # cap how many moments we ask the LLM to enrich

# Emphasis -> palette tokens (CLAUDE.md design tokens).
EMPHASIS_PALETTES = {
    "growth": {
        "accent": "#07CA6B",   # success
        "bg_base": "#05110c",
        "bg_gradient": "rgba(7, 202, 107, 0.22)",
        "glow": "rgba(7, 202, 107, 0.55)",
    },
    "drop": {
        "accent": "#EA2143",   # danger
        "bg_base": "#12060a",
        "bg_gradient": "rgba(234, 33, 67, 0.22)",
        "glow": "rgba(234, 33, 67, 0.55)",
    },
    "neutral": {
        "accent": "#1856FF",   # primary
        "bg_base": "#07091a",
        "bg_gradient": "rgba(24, 86, 255, 0.22)",
        "glow": "rgba(24, 86, 255, 0.55)",
    },
}

# PT-BR spelled-out numbers. Only single-word values; compound numbers
# ("vinte e cinco por cento") are rare in speech for stat drops and we
# skip them deliberately to avoid false positives.
PT_NUMBER_WORDS = {
    "zero": 0, "um": 1, "uma": 1, "dois": 2, "duas": 2,
    "três": 3, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6,
    "sete": 7, "oito": 8, "nove": 9, "dez": 10, "onze": 11,
    "doze": 12, "treze": 13, "quatorze": 14, "catorze": 14,
    "quinze": 15, "dezesseis": 16, "dezessete": 17, "dezoito": 18,
    "dezenove": 19, "vinte": 20, "trinta": 30, "quarenta": 40,
    "cinquenta": 50, "sessenta": 60, "setenta": 70, "oitenta": 80,
    "noventa": 90, "cem": 100, "cento": 100, "meio": 50, "metade": 50,
}

# "por cento" / "porcento" / "%" — the unit marker that turns a number
# into a percentage. We accept both the glued and spaced form.
PT_PERCENT_UNIT_WORDS = {"porcento", "porcentos"}
# A raw integer/decimal followed by "%" anywhere in the word token.
RX_NUMBER_WITH_PERCENT = re.compile(r"^\s*(\d{1,3}(?:[.,]\d+)?)\s*%\s*$")
RX_BARE_NUMBER = re.compile(r"^\s*(\d{1,3}(?:[.,]\d+)?)\s*$")


@dataclass
class Moment:
    """A single data point to visualize."""
    kind: str                 # "percentage" — future: "money", "date", ...
    value: float              # 0..100 for percentages, clamped
    start: float              # seconds in the source timeline
    end: float                # seconds in the source timeline (word-level end)
    text: str                 # transcript fragment that triggered the match
    label: str = ""           # human-readable caption under the ring
    duration: float = DEFAULT_DURATION  # on-screen duration (seconds)
    emphasis: str = "neutral" # palette key: growth | drop | neutral
    context_before: str = ""
    context_after: str = ""
    clip_path: str = field(default="", repr=False)


# --- Helpers ---------------------------------------------------------------

def _strip_diacritics(s: str) -> str:
    """Fold PT-BR accents so 'três' matches 'tres', etc."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    ).lower()


def _clean_token(raw: str) -> str:
    """Lowercase a token and drop surrounding punctuation."""
    return re.sub(r"[^\wà-ÿ%\.,-]+", "", (raw or "").strip().lower())


def _parse_word_time(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _word_span_valid(w: dict) -> bool:
    """True when start/end look like real alignment (WhisperX uses 0,0 as placeholder)."""
    s = _parse_word_time(w.get("start"))
    e = _parse_word_time(w.get("end"))
    if s is None or e is None:
        return False
    if abs(s) < 1e-5 and abs(e) < 1e-5:
        return False
    if e < s - 1e-5:
        return False
    return True


def _word_times_need_fill(w: dict) -> bool:
    return not _word_span_valid(w)


def _prev_valid_end(seg_words: list[dict], idx: int) -> float | None:
    for j in range(idx - 1, -1, -1):
        if _word_span_valid(seg_words[j]):
            return float(seg_words[j]["end"])
    return None


def _next_valid_start(seg_words: list[dict], idx: int) -> float | None:
    for j in range(idx + 1, len(seg_words)):
        if _word_span_valid(seg_words[j]):
            return float(seg_words[j]["start"])
    return None


def _segment_time_bounds(seg: dict, seg_words: list[dict]) -> tuple[float, float]:
    """Bounds for filling unaligned tokens: prefer real word spans over segment metadata."""
    raw_s = float(seg.get("start", 0.0) or 0.0)
    raw_e = float(seg.get("end", raw_s) or raw_s)
    valid_s = [
        float(w["start"]) for w in seg_words if _word_span_valid(w)
    ]
    valid_e = [float(w["end"]) for w in seg_words if _word_span_valid(w)]
    if valid_s:
        # If metadata is still 0,0, min(raw, wmin) would wrongly pin to 0.
        return min(valid_s), max(valid_e)
    return raw_s, raw_e


def _backfill_flat_from_top_level(flat: list[dict], top_words: list) -> None:
    """When token lists line up, copy timings from top-level words (usually better aligned)."""
    if not top_words or len(flat) != len(top_words):
        return
    for i, w in enumerate(flat):
        tw = top_words[i]
        if not isinstance(tw, dict):
            continue
        if _word_span_valid(tw) and _word_times_need_fill(w):
            w["start"] = tw["start"]
            w["end"] = tw["end"]


def _fill_missing_word_times_in_list(words: list[dict], *, lo: float, hi: float) -> None:
    """Same interpolation as per-segment, for a flat global word list."""
    n = len(words)
    for idx, w in enumerate(words):
        if not _word_times_need_fill(w):
            continue
        prev_end = _prev_valid_end(words, idx)
        next_start = _next_valid_start(words, idx)
        if prev_end is not None and next_start is not None:
            mid = (prev_end + next_start) / 2.0
            w["start"] = mid
            w["end"] = next_start
        elif prev_end is not None:
            w["start"] = prev_end
            w["end"] = min(prev_end + 0.25, hi)
        elif next_start is not None:
            w["start"] = max(lo, next_start - 0.25)
            w["end"] = next_start
        else:
            w["start"] = lo
            w["end"] = min(lo + 0.25, hi)


def _load_transcript(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def _flatten_words(data: dict) -> list[dict]:
    """
    Transcript files in this project come in two shapes depending on which
    whisper variant produced them:
      - a flat top-level `words` list, typically aligned word-by-word
      - `segments[*].words`, which is usually richer but can have words
        with missing start/end timestamps (e.g. numeric symbols like
        `100%` that WhisperX couldn't align to an audio position).

    We prefer segments[*].words when it exists because it contains every
    token in the sentence — including ones the top-level list filters
    out — and fill in missing timestamps by interpolating from adjacent
    words within the same segment.
    """
    segments = data.get("segments") or []
    top_level = [w for w in (data.get("words") or []) if isinstance(w, dict)]
    dur = data.get("video_duration")
    hi_fallback = float(dur) if isinstance(dur, (int, float)) and float(dur) > 0 else 86400.0

    if segments:
        flat: list[dict] = []
        for seg in segments:
            seg_words = [w for w in (seg.get("words") or []) if isinstance(w, dict)]
            if not seg_words:
                continue
            seg_start, seg_end = _segment_time_bounds(seg, seg_words)
            if seg_end < seg_start:
                seg_end = seg_start + 0.25
            for idx, w in enumerate(seg_words):
                if not _word_times_need_fill(w):
                    flat.append(w)
                    continue
                prev_end = _prev_valid_end(seg_words, idx)
                next_start = _next_valid_start(seg_words, idx)
                if prev_end is not None and next_start is not None:
                    mid = (prev_end + next_start) / 2.0
                    w["start"] = mid
                    w["end"] = next_start
                elif prev_end is not None:
                    w["start"] = prev_end
                    w["end"] = min(prev_end + 0.25, seg_end)
                elif next_start is not None:
                    w["start"] = max(seg_start, next_start - 0.25)
                    w["end"] = next_start
                else:
                    w["start"] = seg_start
                    w["end"] = max(seg_end, seg_start + 0.05)
                flat.append(w)
        if flat:
            _backfill_flat_from_top_level(flat, top_level)
            return flat

    words = list(top_level)
    _fill_missing_word_times_in_list(words, lo=0.0, hi=hi_fallback)
    return words


def _segments_for_context(data: dict) -> list[dict]:
    """Return `segments` array (possibly empty) for building LLM context."""
    return data.get("segments") or []


def _segment_for_time(segments: list[dict], t: float) -> dict | None:
    """Locate the segment that wraps a given timestamp."""
    for seg in segments:
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", 0.0))
        if start <= t <= end + 0.01:
            return seg
    return None


def _neighbor_text(segments: list[dict], t: float) -> tuple[str, str]:
    """Return (text_before, text_after) for the segments around `t`."""
    if not segments:
        return "", ""
    for idx, seg in enumerate(segments):
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", 0.0))
        if start <= t <= end + 0.01:
            before = segments[idx - 1].get("text", "") if idx > 0 else ""
            after = segments[idx + 1].get("text", "") if idx + 1 < len(segments) else ""
            return before.strip(), after.strip()
    return "", ""


# --- Detectors -------------------------------------------------------------

def detect_percentages(data: dict) -> list[Moment]:
    """
    Scan word-level transcript for percentage mentions in PT-BR.
    Supported patterns:
      A) Single token containing the % sign:  "100%", "12,5%"
      B) Two consecutive tokens:              "50" "%"
      C) Three-word chains:                   "50" "por" "cento"
                                              "vinte" "por" "cento"
      D) Two-word chains:                     "50" "porcento"
                                              "vinte" "porcento"
    We require the percent-unit marker to fire the detection so plain
    numbers ("eu tenho 10 carros") don't trigger the overlay.
    """
    words = _flatten_words(data)
    segments = _segments_for_context(data)
    moments: list[Moment] = []

    def _numeric_token(tok: str) -> float | None:
        tok = _clean_token(tok)
        if not tok:
            return None
        m = RX_BARE_NUMBER.match(tok)
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except ValueError:
                return None
        folded = _strip_diacritics(tok)
        if folded in PT_NUMBER_WORDS:
            return float(PT_NUMBER_WORDS[folded])
        return None

    i = 0
    n = len(words)
    while i < n:
        w = words[i]
        tok = _clean_token(w.get("word", ""))
        start = float(w.get("start", 0.0) or 0.0)
        end = float(w.get("end", start) or start)

        value: float | None = None
        span_end = end

        # Pattern A: single token like "100%" or "12,5%"
        m = RX_NUMBER_WITH_PERCENT.match(tok)
        if m:
            try:
                value = float(m.group(1).replace(",", "."))
            except ValueError:
                value = None

        # Pattern B: "50" followed by "%"
        if value is None and i + 1 < n:
            num = _numeric_token(tok)
            next_tok = _clean_token(words[i + 1].get("word", ""))
            if num is not None and "%" in next_tok:
                value = num
                span_end = float(words[i + 1].get("end", end) or end)
                i += 1  # consume the %

        # Pattern D: "50" / "vinte" + "porcento"
        if value is None and i + 1 < n:
            num = _numeric_token(tok)
            next_tok = _strip_diacritics(_clean_token(words[i + 1].get("word", "")))
            if num is not None and next_tok in PT_PERCENT_UNIT_WORDS:
                value = num
                span_end = float(words[i + 1].get("end", end) or end)
                i += 1

        # Pattern C: "50"/"vinte" + "por" + "cento"
        if value is None and i + 2 < n:
            num = _numeric_token(tok)
            w1 = _strip_diacritics(_clean_token(words[i + 1].get("word", "")))
            w2 = _strip_diacritics(_clean_token(words[i + 2].get("word", "")))
            if num is not None and w1 == "por" and w2.startswith("cent"):
                value = num
                span_end = float(words[i + 2].get("end", end) or end)
                i += 2

        if value is not None:
            value = max(0.0, min(100.0, value))
            seg = _segment_for_time(segments, start)
            text = (seg or {}).get("text", "").strip() if seg else w.get("word", "").strip()
            before, after = _neighbor_text(segments, start)
            moments.append(Moment(
                kind="percentage",
                value=value,
                start=round(start, 3),
                end=round(span_end, 3),
                text=text[:240],
                context_before=before[:160],
                context_after=after[:160],
            ))
        i += 1

    # De-dup and enforce minimum gap between moments.
    moments.sort(key=lambda m: m.start)
    filtered: list[Moment] = []
    last_end = -1e9
    for m in moments:
        if m.start - last_end < MIN_GAP_BETWEEN_MOMENTS:
            continue
        filtered.append(m)
        last_end = m.end
    return filtered


# Pluggable list — future detectors (money, dates, comparisons) go here.
DETECTORS = [detect_percentages]


# --- OpenRouter enrichment -------------------------------------------------

def _extract_json_object(text: str) -> dict | None:
    """Lift the first {...} blob out of an LLM response string."""
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
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def enrich_moments_with_openrouter(moments: list[Moment]) -> str | None:
    """
    Mutates `moments` in place with label/duration/emphasis from the LLM.
    Returns the model name that succeeded, or None if we didn't call the
    LLM / every attempt failed.
    """
    if not moments or not has_openrouter_api_key():
        return None
    models = parse_models_from_env()
    if not models:
        print("[08e] OPENROUTER_API_KEY found, but no OPENROUTER_MODEL(S) configured.")
        return None

    payload = []
    for idx, m in enumerate(moments[:MAX_LLM_MOMENTS]):
        payload.append({
            "index": idx,
            "value": m.value,
            "text": m.text,
            "context_before": m.context_before,
            "context_after": m.context_after,
            "start": m.start,
        })

    system_prompt = (
        "You label numeric data points found in Brazilian-Portuguese speech so "
        "they can be shown as on-screen stat drops. For each moment return a "
        "short label in PT-BR (3-6 words, ALL CAPS optional — we uppercase in CSS), "
        "an on-screen duration in seconds (1.5-3.0), and an emphasis key.\n\n"
        "emphasis values:\n"
        "  - \"growth\"  when the number represents increase/positive outcome\n"
        "  - \"drop\"    when it represents decrease/negative outcome\n"
        "  - \"neutral\" when it's a descriptive fact (default)\n\n"
        "Return strict JSON only."
    )
    user_prompt = (
        "Enrich each moment below. Return EXACTLY this schema:\n"
        '{ "moments": [ { "index": 0, "label": "Taxa de retenção", '
        '"duration": 2.0, "emphasis": "neutral" } ] }\n'
        "- label must be PT-BR, concise, no trailing punctuation.\n"
        "- duration must be a number between 1.5 and 3.0.\n"
        "- emphasis must be one of: growth, drop, neutral.\n\n"
        f"Moments:\n{json.dumps(payload, ensure_ascii=False)}"
    )

    for model in models:
        print(f"[08e] OpenRouter: trying model '{model}' for data-viz enrichment...")
        try:
            raw = chat_completion(
                model=model,
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                temperature=0.2,
                max_tokens=700,
            )
        except Exception as exc:
            print(f"[08e] OpenRouter '{model}' failed: {exc}")
            continue

        parsed = _extract_json_object(raw)
        if not parsed or not isinstance(parsed.get("moments"), list):
            print(f"[08e] OpenRouter '{model}' returned non-JSON or missing 'moments'.")
            continue

        applied = 0
        for item in parsed["moments"]:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(moments):
                continue
            m = moments[idx]
            label = str(item.get("label") or "").strip()
            if label:
                m.label = label[:60]
            try:
                dur = float(item.get("duration") or DEFAULT_DURATION)
                m.duration = max(MIN_DURATION, min(MAX_DURATION, dur))
            except (TypeError, ValueError):
                pass
            emph = str(item.get("emphasis") or "").strip().lower()
            if emph in EMPHASIS_PALETTES:
                m.emphasis = emph
            applied += 1

        if applied:
            print(f"[08e] OpenRouter enriched {applied}/{len(moments)} moments "
                  f"with model '{model}'.")
            return model

    return None


def _apply_defaults(moments: list[Moment]) -> None:
    """Fill in sensible defaults for anything the LLM didn't touch."""
    for m in moments:
        if not m.label:
            # Use a short snippet of the speech as a fallback label.
            snippet = (m.text or "").strip().rstrip(".,!?;:")
            m.label = snippet[:40] if snippet else "Dado"
        if not m.emphasis:
            m.emphasis = "neutral"
        if not m.duration or m.duration <= 0:
            m.duration = DEFAULT_DURATION
        m.duration = max(MIN_DURATION, min(MAX_DURATION, m.duration))


# --- Rendering -------------------------------------------------------------

def _load_template() -> str:
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _render_html(template: str, *, width: int, height: int,
                 value: float, label: str, duration: float,
                 emphasis: str) -> str:
    palette = EMPHASIS_PALETTES.get(emphasis, EMPHASIS_PALETTES["neutral"])
    # HTML-escape the label to keep < > & safe — it's user/LLM-generated text.
    safe_label = (label
                  .replace("&", "&amp;")
                  .replace("<", "&lt;")
                  .replace(">", "&gt;")
                  .replace('"', "&quot;"))
    substitutions = {
        "__WIDTH__": str(int(width)),
        "__HEIGHT__": str(int(height)),
        "__DURATION__": f"{duration:.3f}",
        "__VALUE__": f"{value:.3f}".rstrip("0").rstrip("."),
        "__LABEL__": safe_label,
        "__COLOR_ACCENT__": palette["accent"],
        "__COLOR_BG_BASE__": palette["bg_base"],
        "__COLOR_BG_GRADIENT__": palette["bg_gradient"],
        "__COLOR_GLOW__": palette["glow"],
    }
    out = template
    for token, value_str in substitutions.items():
        out = out.replace(token, value_str)
    return out


def _render_single_clip(moment: Moment, idx: int, *,
                        width: int, height: int, fps: float,
                        template: str, tmp_dir: str) -> str | None:
    """
    Materialize a one-off HyperFrames project for this moment and render
    it to an MP4 clip. Returns the clip path on success, None on failure.
    """
    project_dir = os.path.abspath(os.path.join(tmp_dir, f"dataviz_{idx:02d}"))
    os.makedirs(project_dir, exist_ok=True)

    html = _render_html(
        template,
        width=width,
        height=height,
        value=moment.value,
        label=moment.label,
        duration=moment.duration,
        emphasis=moment.emphasis,
    )
    index_path = os.path.join(project_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html)

    # Mirror the repo-level hyperframes.json so `npx hyperframes` doesn't
    # fall back to global defaults while rendering.
    for cfg_name in ("hyperframes.json",):
        src_cfg = os.path.join(DATAVIZ_DIR, cfg_name)
        if os.path.isfile(src_cfg):
            shutil.copy2(src_cfg, os.path.join(project_dir, cfg_name))

    output_path = os.path.join(project_dir, "render.mp4")
    # Use the project-local hyperframes CLI so Node picks up the modern
    # puppeteer/puppeteer-core sitting next to it. If we fell back to
    # `npx --yes hyperframes`, Node's upward module search would resolve
    # any stale puppeteer in the user's home and break createCDPSession.
    if not os.path.isfile(HYPERFRAMES_CLI):
        print("  [08e] local hyperframes not found. Run:")
        print(f"        cd {DATAVIZ_DIR} && npm install")
        return None

    env = os.environ.copy()
    env.setdefault("PUPPETEER_SKIP_DOWNLOAD", "1")
    env.setdefault("PUPPETEER_SKIP_CHROMIUM_DOWNLOAD", "1")

    # draft is noisy/jerky for a 2-second stat drop; standard is the
    # right sweet spot on an M-series laptop (~8-12s per 2s clip @ 4K).
    quality = os.environ.get("DATAVIZ_QUALITY", "standard")
    cmd = [
        "node", HYPERFRAMES_CLI, "render",
        "--output", output_path,
        "--fps", str(int(round(fps))),
        "--quality", quality,
        "--workers", "1",
        project_dir,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
    except subprocess.TimeoutExpired:
        print(f"  [08e] clip {idx} timed out after 600s")
        return None
    except FileNotFoundError:
        print("  [08e] `node` not found — install Node 22+ to use step 08e")
        return None

    if result.returncode != 0:
        tail = (result.stderr or "") + "\n" + (result.stdout or "")
        print(f"  [08e] clip {idx} render failed (exit {result.returncode}):")
        for line in tail.strip().splitlines()[-20:]:
            print(f"    {line}")
        return None

    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        print(f"  [08e] clip {idx} produced no output")
        return None

    return output_path


def _render_clips_parallel(moments: list[Moment], *,
                            width: int, height: int, fps: float,
                            tmp_dir: str, max_workers: int) -> None:
    """Render every moment in parallel, storing the clip path on each Moment."""
    if not moments:
        return
    template = _load_template()
    workers = max(1, min(max_workers, len(moments)))
    print(f"[08e] Rendering {len(moments)} data-viz clips with HyperFrames "
          f"(parallel workers={workers})...")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                _render_single_clip, m, idx,
                width=width, height=height, fps=fps,
                template=template, tmp_dir=tmp_dir,
            ): (idx, m)
            for idx, m in enumerate(moments)
        }
        done = 0
        for fut in as_completed(futures):
            idx, m = futures[fut]
            try:
                m.clip_path = fut.result() or ""
            except Exception as exc:
                print(f"  [{done + 1}/{len(moments)}] {m.value}% ERROR: {exc}")
                m.clip_path = ""
            else:
                status = "ok" if m.clip_path else "failed"
                print(f"  [{done + 1}/{len(moments)}] {m.value}% @ "
                      f"{m.start:.1f}s ({m.duration:.1f}s) {status}")
            done += 1


# --- Compositing -----------------------------------------------------------

def _composite_clips(input_video: str, output_path: str,
                     moments: list[Moment]) -> str:
    """
    Overlay each rendered clip onto the input video for
    [start, start+duration]. Audio is stream-copied (fullscreen cutaway).

    Timeline alignment uses ``-itsoffset`` on each overlay input so decoded
    clip PTS matches the main video clock. A pure ``setpts=...+offset/TB`` chain
    often leaves overlays stuck at t≈0 for MP4 clips from HyperFrames/Chrome
    where STARTPTS/TB handling does not land where ``overlay`` expects.
    """
    valid = [m for m in moments if m.clip_path]
    if not valid:
        subprocess.run(["cp", input_video, output_path], check=True)
        return output_path

    inputs: list[str] = ["-i", input_video]
    for m in valid:
        # Apply offset to the *next* input only (all streams of that file).
        inputs.extend(["-itsoffset", f"{float(m.start):.6f}", "-i", m.clip_path])

    filter_parts: list[str] = []
    current_label = "[0:v]"
    for idx, m in enumerate(valid):
        clip_idx = idx + 1
        scale_label = f"[s{idx}]"
        overlay_label = f"[ov{idx}]"
        end_t = float(m.start) + float(m.duration)

        # yuv420p only — timestamps come from demuxer (-itsoffset).
        filter_parts.append(f"[{clip_idx}:v]format=yuv420p{scale_label}")
        filter_parts.append(
            f"{current_label}{scale_label}overlay=0:0:"
            f"enable='between(t,{float(m.start):.6f},{end_t:.6f})':"
            f"eof_action=pass:format=auto{overlay_label}"
        )
        current_label = overlay_label

    filter_complex = ";".join(filter_parts)
    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex", filter_complex,
        "-map", current_label,
        "-map", "0:a?",
        *build_fast_hq_x264_args(input_video, crf=17, preset="veryfast"),
        "-c:a", "copy",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[08e] FFmpeg composite error: {result.stderr[-800:]}")
        raise RuntimeError(f"FFmpeg compositing failed (exit {result.returncode})")
    return output_path


# --- Probing ---------------------------------------------------------------

def _probe_video(path: str) -> tuple[int, int, float, float]:
    """Return (width, height, fps, duration) for the input video."""
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet",
         "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-show_entries", "format=duration",
         "-of", "json", path],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(probe.stdout or "{}")
    stream = (info.get("streams") or [{}])[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    r_frame_rate = stream.get("r_frame_rate") or "30/1"
    if "/" in r_frame_rate:
        num, den = r_frame_rate.split("/")
        try:
            fps = float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            fps = 30.0
    else:
        try:
            fps = float(r_frame_rate)
        except ValueError:
            fps = 30.0
    duration = float((info.get("format") or {}).get("duration") or 0.0)
    return width, height, fps, duration


def _write_sidecar(path: str, moments: list[Moment], model: str | None) -> None:
    payload = {
        "moments": [
            {k: v for k, v in asdict(m).items() if k != "clip_path"}
            for m in moments
        ],
        "model": model,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# --- Public entry point ----------------------------------------------------

def apply_data_viz(video_path: str, tmp_dir: str | None = None) -> str:
    """
    Orchestrate the 08e step. Safe no-op return ("") when there's nothing
    to overlay — run_pipeline.py tolerates empty returns.
    """
    if tmp_dir is None:
        tmp_dir = env_paths.tmp_dir()
    base = os.path.splitext(os.path.basename(video_path))[0]
    sidecar_path = os.path.join(tmp_dir, f"{base}_dataviz_moments.json")
    output_path = os.path.join(tmp_dir, f"{base}_dataviz.mp4")

    if os.environ.get("DATAVIZ_DISABLE", "").strip() == "1":
        print("[08e] DATAVIZ_DISABLE=1, skipping.")
        return ""

    transcript_base = stem_for_editor_gate(base)
    if base != transcript_base:
        print(
            f"[08e] Transcript stem: {base!r} → {transcript_base!r} "
            f"(using .tmp/{transcript_base}_transcript.json)",
            flush=True,
        )

    transcript_path, tried = _resolve_transcript_path(video_path, tmp_dir)
    if not transcript_path:
        print("[08e] No transcript found; checked:")
        for p in tried:
            print(f"    - {p}")
        print("[08e] Skipping.")
        return ""
    if transcript_path != os.path.abspath(
        os.path.join(tmp_dir, f"{transcript_base}_transcript.json")
    ):
        print(f"[08e] Using transcript beside original: {transcript_path}")

    # Resolve input video. Run after 08d (fx_sounds) so we overlay onto the
    # polished track; fall back through earlier intermediates when 08d is
    # missing (e.g. user invoked --only 08e).
    candidates = [
        os.path.join(tmp_dir, f"{base}_fx.mp4"),
        os.path.join(tmp_dir, f"{base}_broll.mp4"),
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
        print("[08e] No readable input video found, skipping.")
        return ""
    if input_video != candidates[0]:
        print(f"[08e] Using {input_video} (first choice missing or empty)")

    data = _load_transcript(transcript_path)
    if not data:
        print(f"[08e] Empty/invalid transcript, skipping.")
        return ""

    # Run every detector, merge results.
    moments: list[Moment] = []
    for detector in DETECTORS:
        moments.extend(detector(data))
    moments.sort(key=lambda m: m.start)

    if not moments:
        print("[08e] No data-viz moments detected in transcript, passthrough.")
        # Preserve the contract: always write _dataviz.mp4 if possible so the
        # next step can pick it up via first_existing_nonempty_video().
        _write_sidecar(sidecar_path, [], None)
        subprocess.run(["cp", input_video, output_path], check=True)
        return output_path

    print(f"[08e] Detected {len(moments)} candidate moment(s):")
    for m in moments:
        print(f"  {m.start:.2f}s  {m.value}%  \"{(m.text or '')[:60]}\"")

    model_used = enrich_moments_with_openrouter(moments)
    if model_used:
        print(f"[08e] Enriched via OpenRouter model '{model_used}'.")
    _apply_defaults(moments)

    # Probe video for dimensions/fps.
    try:
        width, height, fps, _dur = _probe_video(input_video)
    except Exception as exc:
        print(f"[08e] ffprobe failed ({exc}), skipping.")
        return ""
    if width <= 0 or height <= 0:
        print("[08e] Invalid video dimensions, skipping.")
        return ""

    print(f"[08e] Video: {width}x{height} @ {fps:.2f}fps")

    # Render clips in parallel.
    try:
        workers = int(os.environ.get(
            "DATAVIZ_RENDER_WORKERS", str(DEFAULT_RENDER_WORKERS)
        ))
    except ValueError:
        workers = DEFAULT_RENDER_WORKERS

    _render_clips_parallel(
        moments,
        width=width, height=height, fps=fps,
        tmp_dir=tmp_dir, max_workers=workers,
    )

    # Drop moments that failed to render; still write sidecar with all
    # attempted moments so debugging is easy.
    _write_sidecar(sidecar_path, moments, model_used)

    rendered = [m for m in moments if m.clip_path]
    if not rendered:
        print("[08e] No clips rendered successfully, passthrough.")
        subprocess.run(["cp", input_video, output_path], check=True)
        return output_path

    print(f"[08e] Compositing {len(rendered)} clip(s) onto main video...")
    _composite_clips(input_video, output_path, rendered)

    # Clean up per-moment render folders.
    for idx, _ in enumerate(moments):
        project_dir = os.path.join(tmp_dir, f"dataviz_{idx:02d}")
        if os.path.isdir(project_dir):
            try:
                shutil.rmtree(project_dir)
            except OSError:
                pass

    print(f"[08e] Output: {output_path}")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 08e_data_viz.py <video_path>")
        sys.exit(1)
    apply_data_viz(sys.argv[1])

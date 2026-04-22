"""
Step 1: Transcribe video audio using WhisperX.
Outputs a JSON transcript with word-level timestamps.

Trailing cleanup: after alignment, removes words that typically appear after long
silence at the end of the file (Whisper hallucinations / music / no-speech).
Tune with TRANSCRIPT_TAIL_GAP_SEC, TRANSCRIPT_TAIL_MIN_START_FRAC,
TRANSCRIPT_TAIL_HALLUCINATION_REGEX, TRANSCRIPT_NO_HALLUCINATION_STRIP.
"""
import re
import sys
import json
import os
import subprocess
import torch
import whisperx


def _probe_duration_seconds(video_path: str) -> float | None:
    """Return container duration in seconds, or None if ffprobe fails."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", video_path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return float(r.stdout.strip())
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None


def _file_size_bytes(video_path: str) -> int | None:
    """Return file size in bytes, or None if the file is missing."""
    try:
        return os.path.getsize(video_path)
    except OSError:
        return None


def _existing_transcript_matches(
    output_path: str,
    video_path: str,
    size: int | None,
    duration: float | None,
    duration_tol: float = 0.2,
) -> bool:
    """Return True if a cached transcript at output_path matches this video."""
    if not os.path.isfile(output_path):
        return False
    try:
        with open(output_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False

    cached_name = os.path.basename(str(data.get("video", "")))
    if cached_name != os.path.basename(video_path):
        return False

    cached_size = data.get("video_size")
    if size is not None and isinstance(cached_size, int) and cached_size != size:
        return False

    cached_duration = data.get("video_duration")
    if (
        duration is not None
        and isinstance(cached_duration, (int, float))
        and abs(float(cached_duration) - float(duration)) > duration_tol
    ):
        return False

    return bool(data.get("words"))


def _trim_tail_after_long_silence(
    words: list,
    duration: float,
    gap_sec: float,
    min_post_gap_start_frac: float,
) -> tuple[list, int]:
    """
    If a long silence is followed by a short burst of words late in the timeline,
    drop that burst (classic end-of-file hallucination).

    Returns (possibly shortened words list, number of words removed).
    """
    if len(words) < 2 or duration <= 0:
        return words, 0

    min_start = duration * min_post_gap_start_frac
    cut_after = -1
    for j in range(len(words) - 2, -1, -1):
        gap = words[j + 1]["start"] - words[j]["end"]
        if gap >= gap_sec and words[j + 1]["start"] >= min_start:
            cut_after = j
            break

    if cut_after < 0:
        return words, 0

    removed = len(words) - (cut_after + 1)
    if removed <= 0:
        return words, 0
    return words[: cut_after + 1], removed


def _strip_trailing_hallucination_words(words: list, pattern: re.Pattern | None) -> tuple[list, int]:
    """Remove trailing words whose text matches known no-speech / subtitle hallucinations."""
    if not words or pattern is None:
        return words, 0
    n0 = len(words)
    out = list(words)
    while out and pattern.search(str(out[-1].get("word", "")).strip()):
        out = out[:-1]
    return out, n0 - len(out)


def _align_segments_to_words(segments: list, words: list) -> list:
    """Drop or clip segments so they do not extend past the last kept word."""
    if not words:
        return []
    t_end = float(words[-1]["end"])
    out = []
    for seg in segments:
        s = float(seg.get("start", 0))
        e = float(seg.get("end", 0))
        if s >= t_end + 0.12:
            continue
        if e <= t_end + 0.05:
            out.append(seg)
            continue
        seg = dict(seg)
        seg["end"] = min(e, t_end)
        sub = seg.get("words") or []
        if isinstance(sub, list) and sub:
            kept = [w for w in sub if float(w.get("end", 0)) <= t_end + 0.02]
            seg["words"] = kept
            if kept:
                seg["text"] = "".join(str(w.get("word", "")) for w in kept).strip()
            elif seg.get("text"):
                seg["text"] = str(seg["text"])[:120].rsplit(" ", 1)[0] + "…"
        out.append(seg)
    return out


def _extract_words(aligned_result: dict) -> list:
    """Normalize aligned output into the pipeline's flat words list."""
    words = []

    # WhisperX may return words either nested in each segment or in word_segments.
    segments = aligned_result.get("segments", []) or []
    for segment in segments:
        for w in segment.get("words", []) or []:
            if "start" in w and "end" in w:
                words.append({
                    "word": str(w.get("word", "")).strip(),
                    "start": round(float(w["start"]), 3),
                    "end": round(float(w["end"]), 3),
                })

    if words:
        return words

    for w in aligned_result.get("word_segments", []) or []:
        if "start" in w and "end" in w:
            words.append({
                "word": str(w.get("word", "")).strip(),
                "start": round(float(w["start"]), 3),
                "end": round(float(w["end"]), 3),
            })
    return words


def transcribe(video_path: str, output_dir: str = ".tmp") -> str:
    os.makedirs(output_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(video_path))[0]
    output_path = os.path.join(output_dir, f"{base}_transcript.json")

    video_size = _file_size_bytes(video_path)
    video_duration = _probe_duration_seconds(video_path)

    if _existing_transcript_matches(output_path, video_path, video_size, video_duration):
        print(f"[01] Reusing cached transcript (same name, size & duration): {output_path}")
        return output_path

    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    model_size = os.getenv("WHISPERX_MODEL", "medium")
    batch_size = int(os.getenv("WHISPERX_BATCH_SIZE", "16"))

    print(f"[01] Loading WhisperX model ({model_size}) on {device} ({compute_type})...")
    model = whisperx.load_model(model_size, device=device, compute_type=compute_type)

    print(f"[01] Transcribing: {video_path}")
    audio = whisperx.load_audio(video_path)
    result = model.transcribe(audio, batch_size=batch_size)
    language = result.get("language", "unknown")

    print(f"[01] Aligning word timestamps ({language})...")
    align_model, metadata = whisperx.load_align_model(language_code=language, device=device)
    aligned = whisperx.align(
        result.get("segments", []),
        align_model,
        metadata,
        audio,
        device,
        return_char_alignments=False,
    )

    words = _extract_words(aligned)
    segments = aligned.get("segments", result.get("segments", []))

    duration = video_duration
    if duration is None:
        duration = float(words[-1]["end"]) if words else 0.0

    gap_sec = float(os.getenv("TRANSCRIPT_TAIL_GAP_SEC", "2.6"))
    min_frac = float(os.getenv("TRANSCRIPT_TAIL_MIN_START_FRAC", "0.78"))
    removed_silence = 0
    words, removed_silence = _trim_tail_after_long_silence(
        words, duration=duration, gap_sec=gap_sec, min_post_gap_start_frac=min_frac
    )
    if removed_silence:
        print(
            f"[01] Trimmed {removed_silence} tail word(s) after "
            f"≥{gap_sec}s silence (post-gap start ≥{min_frac:.0%} of duration)."
        )

    if os.getenv("TRANSCRIPT_NO_HALLUCINATION_STRIP", "").strip().lower() in ("1", "true", "yes"):
        pattern = None
    else:
        custom = os.getenv("TRANSCRIPT_TAIL_HALLUCINATION_REGEX")
        if custom is not None and custom.strip():
            pattern = re.compile(custom.strip())
        else:
            pattern = re.compile(
                r"(?is)^\s*(\[[^\]]*(música|music|noise|applause|laughter|silence)[^\]]*\]|"
                r"subtitles?\b|legendas?\b|amara\.org)\s*$"
            )
    words, removed_hall = _strip_trailing_hallucination_words(words, pattern)
    if removed_hall:
        print(f"[01] Stripped {removed_hall} trailing token(s) matching no-speech / subtitle pattern.")

    segments = _align_segments_to_words(segments, words)
    full_text = " ".join(w["word"].strip() for w in words if w.get("word"))

    transcript_data = {
        "video": video_path,
        "video_size": video_size,
        "video_duration": round(float(duration), 3) if duration else None,
        "language": language,
        "text": full_text,
        "segments": segments,
        "words": words,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(transcript_data, f, ensure_ascii=False, indent=2)

    print(f"[01] Transcript saved: {output_path}")
    print(f"[01] Detected language: {transcript_data['language']}")
    print(f"[01] Total words: {len(words)}")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 01_transcribe.py <video_path>")
        sys.exit(1)
    transcribe(sys.argv[1])

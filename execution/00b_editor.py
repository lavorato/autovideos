"""
Step 00b: Unified editor (video trimmer + transcript fixer).

Single local web UI that merges the two previous interactive tools:

  - Video trim   (formerly ``00b_trim_video.py``)       — trim start/end, cut
                                                          middle scenes from
                                                          the converted video.
  - Transcript   (formerly ``01b_fix_transcript.py``)   — word-level find and
                                                          replace on the
                                                          WhisperX JSON.

On launch, a file-picker sidebar lists everything editable under ``.tmp/``:
raw converted videos (``.tmp/{base}.mp4``) and transcripts
(``.tmp/{base}_transcript.json``). Click one and the matching editor panel
appears on the right. Click the current file again (or press Close) to return
to the picker.

Typical flow:
  1. Run ``00_convert_source.py`` — or ``watch_input.py`` (convert only).
  2. Run this script; **trim** then **Mark trim done** — transcription (step 01)
     runs automatically unless ``EDITOR_AUTO_TRANSCRIBE=0``.
  3. Edit the transcript → **Save** (after a short debounce, steps 02+ run
     automatically unless ``EDITOR_AUTO_PIPELINE=0``), or use **Mark transcript
     review done** to confirm + run immediately.

Usage:
  python execution/00b_editor.py                  # default port 5058
  python execution/00b_editor.py --port 5060
  python execution/00b_editor.py --no-browser
  python execution/00b_editor.py .tmp/IMG_1792.mp4              # pre-open
  python execution/00b_editor.py .tmp/IMG_1792_transcript.json  # pre-open
  python execution/00b_editor.py --mark-trim-done .tmp/IMG_1792.mp4
  python execution/00b_editor.py --mark-done .tmp/IMG_1792.mp4   # transcript review gate
"""
from __future__ import annotations

import argparse
import copy
import glob
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import traceback
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv  # noqa: E402

    _ENV_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".env"))
    load_dotenv(_ENV_PATH, override=False)
except ImportError:
    pass

import editor_gate  # noqa: E402
from video_encoding import build_lossless_x264_args  # noqa: E402


def _editor_auto_transcribe_enabled() -> bool:
    return os.environ.get("EDITOR_AUTO_TRANSCRIBE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _run_transcribe_trimmed(base: str, tmp_dir: str) -> None:
    from importlib import import_module

    out_dir = os.path.abspath(tmp_dir)
    mp4 = os.path.join(out_dir, f"{base}.mp4")
    if not os.path.isfile(mp4) or os.path.getsize(mp4) <= 0:
        print(f"[00b-editor] Auto-transcribe skipped: missing or empty {mp4}", file=sys.stderr)
        return
    mod = import_module("01_transcribe")
    mod.transcribe(mp4, output_dir=out_dir)


def schedule_transcribe_after_trim(base: str, tmp_dir: str, *, background: bool) -> bool:
    """Run step 01 on ``.tmp/{base}.mp4`` if ``EDITOR_AUTO_TRANSCRIBE`` is on.

    Returns True if a transcribe job was started or run.
    """
    if not _editor_auto_transcribe_enabled():
        return False

    def job() -> None:
        try:
            print(f"[00b-editor] Auto-transcribe starting for {base!r}…", flush=True)
            _run_transcribe_trimmed(base, tmp_dir)
            print(f"[00b-editor] Auto-transcribe finished for {base!r}", flush=True)
        except Exception as e:
            print(f"[00b-editor] Auto-transcribe failed for {base!r}: {e}", file=sys.stderr)
            traceback.print_exc()

    if background:
        threading.Thread(target=job, name=f"auto-transcribe-{base}", daemon=True).start()
    else:
        job()
    return True


def _editor_auto_pipeline_enabled() -> bool:
    return os.environ.get("EDITOR_AUTO_PIPELINE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _pipeline_debounce_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("EDITOR_AUTO_PIPELINE_DEBOUNCE", "3")))
    except ValueError:
        return 3.0


_pipeline_timers_lock = threading.Lock()
_pipeline_debounce_timers: dict[str, threading.Timer] = {}
_pipeline_active_lock = threading.Lock()
_pipeline_active_bases: set[str] = set()


def _cancel_pipeline_debounce_for_base(base: str) -> None:
    with _pipeline_timers_lock:
        old = _pipeline_debounce_timers.pop(base, None)
    if old is not None:
        old.cancel()


def _run_pipeline_steps_post_01(base: str, tmp_dir: str) -> None:
    import run_pipeline as rp

    out_dir = os.path.abspath(tmp_dir)
    video_path = os.path.join(out_dir, f"{base}.mp4")
    if not os.path.isfile(video_path) or os.path.getsize(video_path) <= 0:
        print(f"[00b-editor] Pipeline skipped: no video at {video_path}", file=sys.stderr)
        return

    with _pipeline_active_lock:
        if base in _pipeline_active_bases:
            print(
                f"[00b-editor] Pipeline already running for {base!r}, skipping duplicate start",
                file=sys.stderr,
            )
            return
        _pipeline_active_bases.add(base)

    try:
        all_steps = rp.get_steps()
        active = [s for s in all_steps if s["enabled"] and s["id"] not in ("00", "01")]
        if not active:
            print("[00b-editor] Pipeline: no enabled steps after 01", file=sys.stderr)
            return
        with rp.pipeline_run_log():
            print(f"[00b-editor] Pipeline starting (steps 02+) for {base!r}…", flush=True)
            ok = rp.process_video(
                video_path,
                active,
                do_clean=False,
                verify_outputs=False,
                fail_fast=False,
                skip_editor_gate=False,
                tmp_dir=tmp_dir,
            )
            if ok:
                print(f"[00b-editor] Pipeline finished OK for {base!r}", flush=True)
            else:
                print(
                    f"[00b-editor] Pipeline finished with errors for {base!r}",
                    file=sys.stderr,
                )
    finally:
        with _pipeline_active_lock:
            _pipeline_active_bases.discard(base)


def _complete_review_and_run_pipeline(base: str, tmp_dir: str) -> None:
    try:
        editor_gate.write_editor_review_for_base(base, tmp_dir)
    except ValueError as e:
        print(f"[00b-editor] Post-save pipeline skipped: {e}", file=sys.stderr)
        return
    except OSError as e:
        print(f"[00b-editor] Review marker failed: {e}", file=sys.stderr)
        return
    _run_pipeline_steps_post_01(base, tmp_dir)


def schedule_pipeline_after_transcript_save(base: str, tmp_dir: str) -> bool:
    """Debounce: after the last Save, wait then write review marker and run 02+."""
    if not _editor_auto_pipeline_enabled():
        return False
    delay = _pipeline_debounce_seconds()

    def fire() -> None:
        with _pipeline_timers_lock:
            _pipeline_debounce_timers.pop(base, None)
        try:
            _complete_review_and_run_pipeline(base, tmp_dir)
        except Exception as e:
            print(f"[00b-editor] Post-save pipeline failed: {e}", file=sys.stderr)
            traceback.print_exc()

    with _pipeline_timers_lock:
        old = _pipeline_debounce_timers.pop(base, None)
        if old is not None:
            old.cancel()
        t = threading.Timer(delay, fire)
        t.daemon = True
        _pipeline_debounce_timers[base] = t
        t.start()
    return True


def run_pipeline_after_review_confirm(base: str, tmp_dir: str) -> bool:
    """Review marker was just written (sidebar button). Run 02+ in background."""
    if not _editor_auto_pipeline_enabled():
        return False
    _cancel_pipeline_debounce_for_base(base)

    def job() -> None:
        try:
            _run_pipeline_steps_post_01(base, tmp_dir)
        except Exception as e:
            print(f"[00b-editor] Pipeline failed: {e}", file=sys.stderr)
            traceback.print_exc()

    threading.Thread(target=job, name=f"pipeline-{base}", daemon=True).start()
    return True


# ============================================================================ #
# Video trim helpers (merged from 00b_trim_video.py)
# ============================================================================ #


def _probe_duration(path: str) -> float:
    """Return container duration in seconds (0.0 on failure)."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True, text=True, check=True,
        )
        return float((r.stdout or "0").strip())
    except (subprocess.CalledProcessError, ValueError, OSError):
        return 0.0


def _probe_has_audio(path: str) -> bool:
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-select_streams", "a",
                "-show_entries", "stream=index",
                "-of", "csv=p=0",
                path,
            ],
            capture_output=True, text=True, check=True,
        )
        return bool((r.stdout or "").strip())
    except (subprocess.CalledProcessError, OSError):
        return False


def _normalize_cuts(cuts: list[dict], duration: float) -> list[tuple[float, float]]:
    """Sort + clamp + merge overlapping cuts. Returns a list of (start, end)."""
    if duration <= 0:
        return []
    norm: list[tuple[float, float]] = []
    for c in cuts or []:
        try:
            s = max(0.0, float(c.get("start", 0)))
            e = min(duration, float(c.get("end", 0)))
        except (TypeError, ValueError):
            continue
        if e - s < 0.02:
            continue
        norm.append((s, e))
    if not norm:
        return []
    norm.sort()
    merged: list[tuple[float, float]] = [norm[0]]
    for s, e in norm[1:]:
        ps, pe = merged[-1]
        if s <= pe + 0.01:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


def _keep_segments(cuts: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    """Complement of ``cuts`` inside [0, duration]."""
    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in cuts:
        if s > cursor + 0.005:
            keeps.append((cursor, s))
        cursor = max(cursor, e)
    if duration - cursor > 0.005:
        keeps.append((cursor, duration))
    return keeps


def _build_trim_filter(keeps: list[tuple[float, float]], has_audio: bool) -> tuple[str, list[str]]:
    """Build an ffmpeg ``-filter_complex`` expression + output map args."""
    parts: list[str] = []
    labels_v: list[str] = []
    labels_a: list[str] = []
    for i, (s, e) in enumerate(keeps):
        parts.append(
            f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]"
        )
        labels_v.append(f"[v{i}]")
        if has_audio:
            parts.append(
                f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]"
            )
            labels_a.append(f"[a{i}]")

    n = len(keeps)
    if n == 1:
        maps = ["-map", "[v0]"]
        if has_audio:
            maps += ["-map", "[a0]"]
        return ";".join(parts), maps

    concat_inputs = "".join(
        v + (a if has_audio else "") for v, a in zip(labels_v, labels_a + [""] * n)
    )
    concat = f"{concat_inputs}concat=n={n}:v=1:a={1 if has_audio else 0}"
    if has_audio:
        parts.append(f"{concat}[v][a]")
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        parts.append(f"{concat}[v]")
        maps = ["-map", "[v]"]
    return ";".join(parts), maps


def apply_trim_to_file(video_path: str, cuts: list[dict]) -> dict[str, Any]:
    """Run FFmpeg to apply ``cuts`` to ``video_path`` (in-place + .bak)."""
    abs_path = os.path.abspath(video_path)
    duration = _probe_duration(abs_path)
    if duration <= 0:
        raise RuntimeError(f"Could not determine duration of {abs_path}")

    norm_cuts = _normalize_cuts(cuts, duration)
    if not norm_cuts:
        return {"ok": True, "changed": False, "reason": "no cuts", "path": abs_path}

    keeps = _keep_segments(norm_cuts, duration)
    if not keeps:
        raise RuntimeError("Refusing to save: every second of the video is marked for removal.")

    has_audio = _probe_has_audio(abs_path)
    filter_complex, map_args = _build_trim_filter(keeps, has_audio)

    video_args = build_lossless_x264_args(abs_path)
    audio_args = ["-c:a", "alac"] if has_audio else ["-an"]

    root, ext = os.path.splitext(abs_path)
    if not ext:
        ext = ".mp4"
    tmp_out = f"{root}.trimming{ext}"
    if os.path.exists(tmp_out):
        try:
            os.remove(tmp_out)
        except OSError:
            pass

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats",
        "-i", abs_path,
        "-filter_complex", filter_complex,
        *map_args,
        *video_args,
        *audio_args,
        "-movflags", "+faststart",
        tmp_out,
    ]

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass
        raise RuntimeError(f"FFmpeg exited with code {e.returncode}") from e

    backup_path = abs_path + ".bak"
    created_backup = False
    if not os.path.exists(backup_path):
        shutil.copy2(abs_path, backup_path)
        created_backup = True

    os.replace(tmp_out, abs_path)

    new_duration = _probe_duration(abs_path)
    return {
        "ok": True,
        "changed": True,
        "path": abs_path,
        "backup": created_backup,
        "backup_path": backup_path,
        "cuts": [{"start": s, "end": e} for s, e in norm_cuts],
        "keeps": [{"start": s, "end": e} for s, e in keeps],
        "new_duration": round(new_duration, 3),
        "old_duration": round(duration, 3),
    }


# ============================================================================ #
# Transcript fixer helpers (merged from 01b_fix_transcript.py)
# ============================================================================ #


_PUNCT = set('.,!?¿¡"\'`´“”„‘’«»()[]{}:;…—–-•·')


def _split_token(token: str) -> tuple[str, str, str]:
    i, n = 0, len(token)
    while i < n and token[i] in _PUNCT:
        i += 1
    j = n
    while j > i and token[j - 1] in _PUNCT:
        j -= 1
    return token[:i], token[i:j], token[j:]


def _core(token: str) -> str:
    return _split_token(token)[1]


def _replace_token(token: str, old: str, new: str, case_insensitive: bool) -> tuple[str, bool]:
    lead, core, trail = _split_token(token)
    if not core:
        return token, False
    a, b = (core.lower(), old.lower()) if case_insensitive else (core, old)
    if a != b:
        return token, False
    return f"{lead}{new}{trail}", True


def _rebuild_segment_text(words: list[dict]) -> str:
    return " ".join(str(w.get("word", "")).strip() for w in words if str(w.get("word", "")).strip())


def _phrase_match_at(words: list[dict], i: int, phrase: list[str], ci: bool) -> int:
    """Return the number of token slots consumed if ``words[i:]`` starts with
    ``phrase`` (ignoring empty/cleared slots in between), else 0.

    Comparison is done on token *cores* so punctuation stuck to a token (e.g.
    ``"Francisco,"``) still matches. Empty tokens (left over from a prior
    multi-word replacement) are skipped so they don't break a match.
    """
    n = len(words)
    j = i
    for ptok in phrase:
        while j < n and not _core(str(words[j].get("word", ""))):
            j += 1
        if j >= n:
            return 0
        core = _core(str(words[j].get("word", "")))
        a, b = (core.lower(), ptok.lower()) if ci else (core, ptok)
        if a != b:
            return 0
        j += 1
    return j - i


def _apply_phrase_replacement(
    words: list[dict], phrase: list[str], new: str, ci: bool
) -> int:
    """Replace every run of ``words`` that matches ``phrase`` with ``new``.

    If ``new`` has the same token count as ``phrase``, each token's core is
    replaced 1:1 so per-word timing + punctuation survive. Otherwise the full
    replacement is placed in the first matched slot and subsequent matched
    slots are cleared (their timing is preserved but the ``word`` string is
    emptied so rebuilt text skips them).
    """
    if not phrase:
        return 0
    new_tokens = new.split()
    count = 0
    i = 0
    while i < len(words):
        span = _phrase_match_at(words, i, phrase, ci)
        if not span:
            i += 1
            continue
        # Collect the indices of the non-empty tokens inside the matched span.
        slots: list[int] = []
        j = i
        while len(slots) < len(phrase):
            if _core(str(words[j].get("word", ""))):
                slots.append(j)
            j += 1
        first = slots[0]
        last = slots[-1]
        lead_first, _, _ = _split_token(str(words[first].get("word", "")))
        _, _, trail_last = _split_token(str(words[last].get("word", "")))
        if len(new_tokens) == len(phrase):
            for k, idx in enumerate(slots):
                lead, _, trail = _split_token(str(words[idx].get("word", "")))
                words[idx]["word"] = f"{lead}{new_tokens[k]}{trail}"
        else:
            if new:
                words[first]["word"] = f"{lead_first}{new}{trail_last}"
            else:
                words[first]["word"] = f"{lead_first}{trail_last}"
            for idx in slots[1:]:
                words[idx]["word"] = ""
        count += 1
        i = last + 1
    return count


def apply_replacement(
    transcript: dict[str, Any],
    old: str,
    new: str,
    case_insensitive: bool = False,
) -> int:
    """In-place word-level replacement across ``words``/``segments``/``text``.

    Supports both single-word and multi-word ``old`` values. Multi-word
    searches match a run of consecutive tokens so phrases like
    ``"San Francisco"`` find the sequence ``San`` + ``Francisco``.
    """
    phrase = old.split()
    count = 0

    if len(phrase) <= 1:
        for w in transcript.get("words", []) or []:
            tok = str(w.get("word", ""))
            new_tok, changed = _replace_token(tok, old, new, case_insensitive)
            if changed:
                w["word"] = new_tok
                count += 1

        for seg in transcript.get("segments", []) or []:
            sub = seg.get("words") or []
            seg_changed = False
            for w in sub:
                tok = str(w.get("word", ""))
                new_tok, changed = _replace_token(tok, old, new, case_insensitive)
                if changed:
                    w["word"] = new_tok
                    seg_changed = True
                    count += 1
            if seg_changed and sub:
                seg["text"] = _rebuild_segment_text(sub)
    else:
        top = transcript.get("words", []) or []
        count += _apply_phrase_replacement(top, phrase, new, case_insensitive)

        for seg in transcript.get("segments", []) or []:
            sub = seg.get("words") or []
            c = _apply_phrase_replacement(sub, phrase, new, case_insensitive)
            if c and sub:
                seg["text"] = _rebuild_segment_text(sub)
            count += c

    words = transcript.get("words", []) or []
    transcript["text"] = " ".join(
        str(w.get("word", "")).strip() for w in words if str(w.get("word", "")).strip()
    )
    return count


def unique_word_counts(transcript: dict[str, Any]) -> list[tuple[str, int]]:
    cnt: Counter[str] = Counter()
    for w in transcript.get("words", []) or []:
        core = _core(str(w.get("word", "")))
        if core:
            cnt[core] += 1
    return sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0].lower()))


# ============================================================================ #
# File discovery
# ============================================================================ #


# Intermediate artefacts produced by downstream pipeline steps — we never want
# the editor to list these as "editable videos".
_DOWNSTREAM_SUFFIXES = (
    "_no_retakes", "_no_fillers", "_voice", "_studio", "_fixed_audio",
    "_color", "_effects", "_hardcut", "_broll", "_final", "_multicam",
    "_zoompan", "_scenes",
)


def _is_editable_video(path: str) -> bool:
    if not path.lower().endswith(".mp4"):
        return False
    if path.endswith(".bak") or ".trimming." in path or ".trimmed.tmp" in path:
        return False
    name = os.path.basename(path)
    stem = os.path.splitext(name)[0]
    return not any(suf in stem for suf in _DOWNSTREAM_SUFFIXES)


def _transcript_base(path: str) -> str:
    """``IMG_1792_transcript.json`` → ``IMG_1792``."""
    name = os.path.splitext(os.path.basename(path))[0]
    if name.endswith("_transcript"):
        return name[: -len("_transcript")]
    return name


def _video_base(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _transcript_word_count(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return 0
    return len(data.get("words") or [])


def list_editable_files(tmp_dir: str = ".tmp") -> list[dict[str, Any]]:
    """List all videos + transcripts under ``tmp_dir`` that the editor can open."""
    if not os.path.isdir(tmp_dir):
        return []

    items: dict[str, dict[str, Any]] = {}

    for path in sorted(glob.glob(os.path.join(tmp_dir, "*.mp4"))):
        if not _is_editable_video(path):
            continue
        base = _video_base(path)
        slot = items.setdefault(base, {"base": base, "video": None, "transcript": None})
        try:
            dur = _probe_duration(path)
        except Exception:  # noqa: BLE001
            dur = 0.0
        slot["video"] = {
            "path": os.path.abspath(path),
            "name": os.path.basename(path),
            "duration": round(dur, 2),
            "has_backup": os.path.exists(path + ".bak"),
            "size": os.path.getsize(path) if os.path.exists(path) else 0,
        }

    for path in sorted(glob.glob(os.path.join(tmp_dir, "*_transcript.json"))):
        base = _transcript_base(path)
        slot = items.setdefault(base, {"base": base, "video": None, "transcript": None})
        slot["transcript"] = {
            "path": os.path.abspath(path),
            "name": os.path.basename(path),
            "words": _transcript_word_count(path),
            "has_backup": os.path.exists(path + ".bak"),
            "size": os.path.getsize(path) if os.path.exists(path) else 0,
        }

    out = [items[k] for k in sorted(items.keys())]
    for slot in out:
        slot["trim_review"] = editor_gate.is_trim_complete_for_base(slot["base"], tmp_dir)
        slot["editor_review"] = editor_gate.is_editor_review_complete_for_base(
            slot["base"], tmp_dir
        )
    return out


# ============================================================================ #
# Session (current editor target)
# ============================================================================ #


class VideoEditor:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        if not os.path.isfile(self.path):
            raise FileNotFoundError(self.path)
        self.duration = _probe_duration(self.path)
        self.has_audio = _probe_has_audio(self.path)
        self._lock = threading.Lock()

    def reload(self) -> None:
        self.duration = _probe_duration(self.path)
        self.has_audio = _probe_has_audio(self.path)

    def state(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": os.path.basename(self.path),
            "duration": self.duration,
            "has_audio": self.has_audio,
            "has_backup": os.path.exists(self.path + ".bak"),
        }

    def save(self, cuts: list[dict]) -> dict[str, Any]:
        with self._lock:
            info = apply_trim_to_file(self.path, cuts)
            self.reload()
            return info

    def restore_from_backup(self) -> dict[str, Any]:
        with self._lock:
            backup = self.path + ".bak"
            if not os.path.isfile(backup):
                raise FileNotFoundError(f"No backup file at {backup}")
            shutil.copy2(backup, self.path)
            self.reload()
            return {"ok": True, "path": self.path, "duration": self.duration}


class TranscriptEditor:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        with open(self.path, "r", encoding="utf-8") as f:
            self.transcript: dict[str, Any] = json.load(f)
        self._original: dict[str, Any] = copy.deepcopy(self.transcript)
        self._lock = threading.Lock()
        self.media_path: str | None = self._resolve_media_path()

    def _resolve_media_path(self) -> str | None:
        """Locate the video/audio that goes with this transcript.

        Priority:
          1. ``transcript["video"]`` if it points to an existing file
             (WhisperX stores it as a path relative to the project root).
          2. ``{dirname}/{base}.mp4`` where ``base`` is the transcript filename
             with the ``_transcript`` suffix stripped.
        """
        hinted = self.transcript.get("video")
        if isinstance(hinted, str) and hinted.strip():
            for cand in (hinted, os.path.join(os.path.dirname(self.path), os.path.basename(hinted))):
                cand_abs = os.path.abspath(cand)
                if os.path.isfile(cand_abs):
                    return cand_abs
        base = os.path.splitext(os.path.basename(self.path))[0]
        if base.endswith("_transcript"):
            base = base[: -len("_transcript")]
        sibling = os.path.join(os.path.dirname(self.path), base + ".mp4")
        if os.path.isfile(sibling):
            return os.path.abspath(sibling)
        return None

    def reload(self) -> None:
        with self._lock:
            with open(self.path, "r", encoding="utf-8") as f:
                self.transcript = json.load(f)
            self._original = copy.deepcopy(self.transcript)
            self.media_path = self._resolve_media_path()

    def state(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": os.path.basename(self.path),
            "transcript": self.transcript,
            "words": unique_word_counts(self.transcript),
            "has_backup": os.path.exists(self.path + ".bak"),
            "has_media": self.media_path is not None,
            "media_name": os.path.basename(self.media_path) if self.media_path else None,
        }

    def replace(self, old: str, new: str, case_insensitive: bool) -> int:
        with self._lock:
            return apply_replacement(self.transcript, old, new, case_insensitive)

    def save(self) -> dict[str, Any]:
        with self._lock:
            backup_path = self.path + ".bak"
            created_backup = False
            if not os.path.exists(backup_path):
                shutil.copy2(self.path, backup_path)
                created_backup = True
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.transcript, f, ensure_ascii=False, indent=2)
            self._original = copy.deepcopy(self.transcript)
            return {"path": self.path, "backup": created_backup, "backup_path": backup_path}


class Session:
    """Thread-safe holder for the currently-open editor."""

    def __init__(self, tmp_dir: str = ".tmp") -> None:
        self.tmp_dir = tmp_dir
        self._lock = threading.Lock()
        self.kind: str | None = None
        self.video: VideoEditor | None = None
        self.transcript: TranscriptEditor | None = None

    def _kind_for_path(self, path: str) -> str:
        if path.lower().endswith(".mp4"):
            return "video"
        if path.lower().endswith("_transcript.json"):
            return "transcript"
        if path.lower().endswith(".json"):
            return "transcript"
        raise ValueError(f"Unsupported file type: {path}")

    def open(self, path: str) -> dict[str, Any]:
        with self._lock:
            kind = self._kind_for_path(path)
            self.close_locked()
            if kind == "video":
                self.video = VideoEditor(path)
                self.kind = "video"
                return {"kind": "video", **self.video.state()}
            self.transcript = TranscriptEditor(path)
            self.kind = "transcript"
            return {"kind": "transcript", **self.transcript.state()}

    def close(self) -> None:
        with self._lock:
            self.close_locked()

    def close_locked(self) -> None:
        self.kind = None
        self.video = None
        self.transcript = None

    def state(self) -> dict[str, Any]:
        with self._lock:
            if self.kind == "video" and self.video:
                return {"kind": "video", **self.video.state()}
            if self.kind == "transcript" and self.transcript:
                return {"kind": "transcript", **self.transcript.state()}
            return {"kind": None}

    def current_video_path(self) -> str | None:
        """Path that the ``/video`` endpoint should stream right now.

        - In ``video`` mode: the file being trimmed.
        - In ``transcript`` mode: the companion video/audio file if one exists
          (so the transcript editor can play it back in sync with the words).
        """
        with self._lock:
            if self.kind == "video" and self.video:
                return self.video.path
            if self.kind == "transcript" and self.transcript and self.transcript.media_path:
                return self.transcript.media_path
            return None


# ============================================================================ #
# Web UI
# ============================================================================ #


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Lavora Editor</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
  <style>
    :root {
      --primary: #1856FF;
      --secondary: #3A344E;
      --success: #07CA6B;
      --warning: #E89558;
      --danger:  #EA2143;
      --surface: #FFFFFF;
      --text:    #141414;
      --muted:   #5a5668;
      --line:    rgba(58, 52, 78, 0.14);
      --glass:   rgba(255, 255, 255, 0.55);
      --glass-strong: rgba(255, 255, 255, 0.78);
      --shadow:  0 20px 60px rgba(24, 86, 255, 0.12), 0 2px 8px rgba(20, 20, 20, 0.06);
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; }
    body {
      font-family: 'Plus Jakarta Sans', system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      color: var(--text);
      min-height: 100vh;
      background:
        radial-gradient(1200px 600px at 10% -10%, rgba(24, 86, 255, 0.18), transparent 60%),
        radial-gradient(900px 500px at 110% 10%, rgba(7, 202, 107, 0.14), transparent 60%),
        radial-gradient(800px 500px at 50% 120%, rgba(232, 149, 88, 0.14), transparent 60%),
        linear-gradient(180deg, #F4F6FB 0%, #EAEEF7 100%);
    }
    header {
      padding: 16px 24px;
      display: flex;
      align-items: center;
      gap: 12px;
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(10px);
      background: rgba(255,255,255,0.55);
      position: sticky; top: 0; z-index: 10;
    }
    header .dot {
      width: 10px; height: 10px; border-radius: 50%; background: var(--primary);
      box-shadow: 0 0 0 4px rgba(24,86,255,0.18);
    }
    header h1 { font-size: 16px; margin: 0; font-weight: 700; letter-spacing: -0.01em; }
    header .mode {
      font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
      padding: 3px 8px; border-radius: 999px; color: var(--primary);
      background: rgba(24, 86, 255, 0.10);
      border: 1px solid rgba(24, 86, 255, 0.25);
    }
    header .mode.transcript { color: var(--warning); background: rgba(232, 149, 88, 0.10); border-color: rgba(232, 149, 88, 0.30); }
    header .mode.video { color: var(--primary); background: rgba(24, 86, 255, 0.10); border-color: rgba(24, 86, 255, 0.25); }
    header .file {
      font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px; color: var(--muted);
      padding: 4px 10px; border-radius: 8px;
      background: rgba(58, 52, 78, 0.06);
      border: 1px solid var(--line);
      max-width: 40vw; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    header .spacer { flex: 1; }
    header .status {
      font-size: 12px; color: var(--muted);
      font-family: 'JetBrains Mono', monospace;
    }

    main {
      max-width: 1400px; margin: 20px auto; padding: 0 20px 60px;
      display: grid; grid-template-columns: 320px minmax(0, 1fr); gap: 20px;
    }
    @media (max-width: 1024px) {
      main { grid-template-columns: 1fr; }
    }

    .card {
      background: var(--glass);
      backdrop-filter: blur(16px) saturate(1.1);
      -webkit-backdrop-filter: blur(16px) saturate(1.1);
      border: 1px solid var(--line);
      border-radius: 16px;
      box-shadow: var(--shadow);
      padding: 16px 18px;
    }
    .card h2 {
      font-size: 13px; font-weight: 700; letter-spacing: 0.04em;
      text-transform: uppercase; color: var(--secondary);
      margin: 0 0 12px;
    }
    .card h2 .pill {
      font-size: 10px; padding: 2px 8px; border-radius: 999px;
      background: rgba(58, 52, 78, 0.08); color: var(--muted);
      border: 1px solid var(--line);
      margin-left: 6px;
    }

    /* ---------- File picker (sidebar) ---------- */
    aside.sidebar { position: sticky; top: 84px; align-self: start; }
    .file-groups { display: grid; gap: 10px; max-height: 72vh; overflow-y: auto; padding-right: 2px; }
    .group {
      border-radius: 12px;
      background: var(--glass-strong);
      border: 1px solid var(--line);
      overflow: hidden;
    }
    .group .group-title {
      padding: 8px 12px;
      font-size: 12px; font-weight: 700;
      font-family: 'JetBrains Mono', monospace;
      color: var(--secondary);
      background: rgba(24, 86, 255, 0.05);
      border-bottom: 1px solid var(--line);
      display: flex; align-items: center; gap: 6px;
    }
    .group .group-title .base {
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .group .group-gate {
      display: flex; align-items: center; flex-wrap: wrap; gap: 8px;
      padding: 8px 12px;
      border-bottom: 1px solid var(--line);
      background: rgba(7, 202, 107, 0.05);
    }
    .group .group-gate .pill.ok {
      background: rgba(7, 202, 107, 0.15);
      color: var(--success);
      border: 1px solid rgba(7, 202, 107, 0.35);
      font-size: 10px;
    }
    .group .group-gate-split { flex-direction: column; align-items: stretch; gap: 8px; }
    .group .gate-row {
      display: flex; align-items: center; flex-wrap: wrap; gap: 8px;
    }
    .group .gate-label {
      font-family: 'JetBrains Mono', monospace;
      font-size: 10px;
      font-weight: 700;
      color: var(--muted);
      min-width: 7.5em;
    }
    .group .row {
      display: flex; align-items: center; gap: 10px;
      padding: 10px 12px;
      border-top: 1px solid var(--line);
      cursor: pointer;
      transition: background 0.12s ease;
    }
    .group .row:first-child { border-top: none; }
    .group .row:hover { background: rgba(24, 86, 255, 0.06); }
    .group .row.active {
      background: rgba(24, 86, 255, 0.12);
      box-shadow: inset 3px 0 0 var(--primary);
    }
    .row .kind-tag {
      font-size: 10px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;
      padding: 3px 7px; border-radius: 999px;
      font-family: 'JetBrains Mono', monospace;
    }
    .row .kind-tag.video { background: rgba(24, 86, 255, 0.12); color: var(--primary); border: 1px solid rgba(24, 86, 255, 0.25); }
    .row .kind-tag.transcript { background: rgba(232, 149, 88, 0.14); color: var(--warning); border: 1px solid rgba(232, 149, 88, 0.30); }
    .row .meta {
      font-size: 11px; color: var(--muted);
      font-family: 'JetBrains Mono', monospace;
      margin-left: auto;
      white-space: nowrap;
    }
    .row .bak {
      font-size: 10px; padding: 2px 6px; border-radius: 999px;
      background: rgba(7, 202, 107, 0.12); color: var(--success);
      border: 1px solid rgba(7, 202, 107, 0.25);
      font-family: 'JetBrains Mono', monospace;
    }
    .empty {
      padding: 20px; text-align: center; color: var(--muted); font-size: 13px;
    }
    .sidebar-actions {
      display: flex; gap: 8px; margin-top: 12px;
    }

    /* ---------- Editor area: welcome ---------- */
    .welcome {
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      padding: 80px 40px; text-align: center;
      min-height: 60vh;
    }
    .welcome .big {
      font-size: 28px; font-weight: 800; letter-spacing: -0.02em;
      background: linear-gradient(135deg, var(--primary) 0%, var(--warning) 100%);
      -webkit-background-clip: text; background-clip: text; color: transparent;
      margin-bottom: 10px;
    }
    .welcome p { color: var(--muted); max-width: 440px; margin: 6px 0; }

    /* ---------- Transcript editor ---------- */
    .editor-grid {
      display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 16px;
    }
    @media (max-width: 1200px) { .editor-grid { grid-template-columns: 1fr; } }

    .transcript {
      line-height: 1.75;
      font-size: 15px;
      max-height: 50vh; overflow-y: auto;
      padding: 14px 16px;
      border-radius: 12px;
      background: var(--glass-strong);
      border: 1px solid var(--line);
      scroll-behavior: smooth;
    }
    .transcript .w {
      display: inline-block;
      padding: 1px 3px;
      margin: 0 1px;
      border-radius: 5px;
      cursor: pointer;
      transition: background 0.10s ease, color 0.10s ease, transform 0.10s ease;
    }
    .transcript .w:hover {
      background: rgba(24, 86, 255, 0.10);
      color: var(--primary);
    }
    .transcript .w.active {
      background: var(--primary);
      color: white;
      transform: translateY(-1px);
      box-shadow: 0 3px 10px rgba(24,86,255,0.35);
    }
    .transcript .gap {
      opacity: 0.35; font-size: 11px;
      padding: 0 2px; color: var(--muted);
    }

    .media-strip {
      display: flex; align-items: center; gap: 10px;
      padding: 10px 12px;
      border-radius: 12px;
      background: var(--glass-strong);
      border: 1px solid var(--line);
      margin-bottom: 12px;
    }
    .media-strip audio { flex: 1; width: 100%; min-width: 0; }
    .media-strip .media-meta {
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px; color: var(--muted);
      white-space: nowrap;
    }
    .media-missing {
      padding: 10px 12px; margin-bottom: 12px;
      border-radius: 10px;
      background: rgba(232, 149, 88, 0.10);
      border: 1px dashed rgba(232, 149, 88, 0.40);
      font-size: 12px; color: var(--muted);
    }
    .toolbar {
      display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
      margin-top: 14px;
    }
    input[type="text"], input[type="number"] {
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid var(--line);
      background: var(--glass-strong);
      font: inherit;
      color: var(--text);
      min-width: 160px;
    }
    input[type="text"]:focus, input[type="number"]:focus {
      outline: 2px solid var(--primary);
      outline-offset: 1px;
    }
    label.check {
      display: inline-flex; gap: 6px; align-items: center;
      font-size: 13px; color: var(--muted);
    }
    button {
      padding: 10px 14px;
      border-radius: 10px;
      border: 1px solid var(--line);
      background: var(--glass-strong);
      font: inherit; font-weight: 600;
      cursor: pointer;
      transition: transform 0.05s ease, box-shadow 0.15s ease;
      color: var(--text);
    }
    button:hover { box-shadow: 0 4px 14px rgba(24,86,255,0.12); }
    button:active { transform: translateY(1px); }
    button:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
    button.primary { background: var(--primary); color: white; border-color: transparent; }
    button.primary:hover { box-shadow: 0 6px 20px rgba(24,86,255,0.35); }
    button.success { background: var(--success); color: white; border-color: transparent; }
    button.warning { background: var(--warning); color: white; border-color: transparent; }
    button.danger  { background: var(--danger);  color: white; border-color: transparent; }
    button.ghost { background: transparent; }
    button[disabled] { opacity: 0.5; cursor: not-allowed; }
    button.small { padding: 8px 10px; font-size: 12px; }

    .history { margin-top: 12px; display: grid; gap: 6px; }
    .history .row {
      display: flex; align-items: center; gap: 10px;
      padding: 8px 12px;
      border-radius: 10px;
      background: rgba(24, 86, 255, 0.06);
      border: 1px solid rgba(24, 86, 255, 0.15);
      font-size: 13px;
    }
    .history .row code {
      font-family: 'JetBrains Mono', monospace;
      background: rgba(255,255,255,0.7);
      padding: 2px 6px; border-radius: 6px;
    }
    .history .row .count {
      margin-left: auto; color: var(--muted); font-size: 12px;
      font-family: 'JetBrains Mono', monospace;
    }

    .word-list {
      max-height: 58vh; overflow-y: auto;
      display: flex; flex-wrap: wrap; gap: 6px;
      padding: 10px;
      border-radius: 12px;
      background: var(--glass-strong);
      border: 1px solid var(--line);
    }
    .chip {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 5px 10px;
      border-radius: 999px;
      background: rgba(255,255,255,0.85);
      border: 1px solid var(--line);
      font-size: 13px;
      cursor: pointer;
      user-select: none;
      transition: background 0.15s ease, border-color 0.15s ease;
    }
    .chip:hover { background: rgba(24, 86, 255, 0.08); border-color: rgba(24,86,255,0.35); }
    .chip .n { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--muted); }
    .chip.rare { background: rgba(232, 149, 88, 0.12); border-color: rgba(232,149,88,0.35); }
    .search {
      width: 100%;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid var(--line);
      background: var(--glass-strong);
      font: inherit;
      margin-bottom: 10px;
    }

    /* ---------- Video editor ---------- */
    .video-wrap {
      border-radius: 12px;
      overflow: hidden;
      background: #000;
      border: 1px solid var(--line);
      aspect-ratio: 16 / 9;
      display: flex; align-items: center; justify-content: center;
    }
    .video-wrap video { width: 100%; height: 100%; object-fit: contain; background: #000; }

    .timeline {
      position: relative;
      margin-top: 14px;
      height: 46px;
      border-radius: 10px;
      background: var(--glass-strong);
      border: 1px solid var(--line);
      cursor: pointer; user-select: none;
      overflow: hidden;
    }
    .timeline .track {
      position: absolute; inset: 0;
      background: linear-gradient(90deg, rgba(24,86,255,0.05), rgba(24,86,255,0.10));
    }
    .timeline .cut {
      position: absolute; top: 0; bottom: 0;
      background: repeating-linear-gradient(
        45deg,
        rgba(234, 33, 67, 0.35) 0 6px,
        rgba(234, 33, 67, 0.55) 6px 12px
      );
      border-left: 2px solid var(--danger);
      border-right: 2px solid var(--danger);
    }
    .timeline .playhead {
      position: absolute; top: -3px; bottom: -3px;
      width: 2px; background: var(--primary);
      box-shadow: 0 0 0 3px rgba(24,86,255,0.18);
      pointer-events: none;
    }
    .timeline .marker {
      position: absolute; top: 0; bottom: 0;
      width: 2px; background: var(--warning);
      pointer-events: none;
    }
    .timeline .tick {
      position: absolute; top: 0; bottom: 0;
      width: 1px; background: rgba(58,52,78,0.10);
      pointer-events: none;
    }
    .ruler {
      position: relative; height: 18px; margin-top: 4px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 10px; color: var(--muted);
    }
    .ruler span { position: absolute; transform: translateX(-50%); }

    .time-readout {
      font-family: 'JetBrains Mono', monospace;
      font-size: 13px;
      padding: 8px 12px;
      border-radius: 10px;
      background: rgba(24, 86, 255, 0.08);
      border: 1px solid rgba(24, 86, 255, 0.18);
      color: var(--secondary);
      min-width: 150px; text-align: center;
    }

    .cuts-list { display: grid; gap: 8px; max-height: 58vh; overflow-y: auto; padding-right: 4px; }
    .cut-row {
      display: grid;
      grid-template-columns: auto 1fr auto 1fr auto;
      gap: 6px; align-items: center;
      padding: 10px;
      border-radius: 12px;
      background: rgba(234, 33, 67, 0.06);
      border: 1px solid rgba(234, 33, 67, 0.22);
    }
    .cut-row .tag {
      font-size: 10px; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.04em;
      padding: 3px 7px; border-radius: 999px;
      background: rgba(234, 33, 67, 0.12); color: var(--danger);
      border: 1px solid rgba(234, 33, 67, 0.25);
      font-family: 'JetBrains Mono', monospace;
    }
    .cut-row .sep { color: var(--muted); font-size: 12px; }
    .cut-row .row-actions { display: flex; gap: 4px; }
    .cut-row input[type="number"] { min-width: 0; padding: 8px 10px; font-family: 'JetBrains Mono', monospace; font-size: 13px; }

    .kbd {
      display: inline-block; min-width: 14px; text-align: center;
      padding: 1px 6px; border-radius: 5px;
      background: rgba(58, 52, 78, 0.08);
      border: 1px solid var(--line);
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
    }
    .hint { font-size: 12px; color: var(--muted); }
    .footer-row {
      display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
      margin-top: 16px; padding-top: 14px;
      border-top: 1px solid var(--line);
    }

    .toast {
      position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
      background: var(--text); color: white; padding: 10px 16px;
      border-radius: 10px; font-size: 13px; opacity: 0; pointer-events: none;
      transition: opacity 0.2s ease, transform 0.2s ease;
      max-width: 80vw;
    }
    .toast.show { opacity: 1; transform: translateX(-50%) translateY(-4px); }
    .toast.error { background: var(--danger); }
    .toast.success { background: var(--success); }

    .overlay {
      position: fixed; inset: 0; background: rgba(20,20,20,0.35);
      display: none; align-items: center; justify-content: center;
      backdrop-filter: blur(4px); z-index: 20;
    }
    .overlay.show { display: flex; }
    .overlay .panel {
      background: rgba(255,255,255,0.95);
      border: 1px solid var(--line);
      border-radius: 14px; padding: 18px 22px;
      font-size: 13px; color: var(--secondary);
      box-shadow: var(--shadow);
      font-family: 'JetBrains Mono', monospace;
    }

    .view { display: none; }
    .view.active { display: block; }
  </style>
</head>
<body>
  <header>
    <span class="dot"></span>
    <h1>Lavora Editor</h1>
    <span id="mode-badge" class="mode" style="display:none"></span>
    <span class="file" id="file-label">No file open</span>
    <span class="spacer"></span>
    <button id="btn-close" class="small ghost" type="button" style="display:none">Close file</button>
    <span class="status" id="status">Ready</span>
  </header>

  <main>
    <!-- ========== Sidebar: file picker ========== -->
    <aside class="sidebar">
      <div class="card">
        <h2>Files <span class="pill" id="file-count">0</span></h2>
        <input id="file-search" class="search" type="text" placeholder="Filter files…" />
        <div id="file-groups" class="file-groups"></div>
        <div class="sidebar-actions">
          <button type="button" class="small" id="btn-refresh">Refresh</button>
          <span class="hint" style="margin-left:auto">from <code>.tmp/</code></span>
        </div>
      </div>
    </aside>

    <!-- ========== Editor area (welcome / video / transcript) ========== -->
    <section>
      <!-- Welcome -->
      <div id="view-welcome" class="view active card">
        <div class="welcome">
          <div class="big">Pick a file to edit</div>
          <p>The sidebar lists files in <code>.tmp/</code>. <strong>Order:</strong> trim → <strong>Mark trim done</strong> (auto-transcribe) → edit transcript → <strong>Save</strong> (after ~3s idle, the rest of the pipeline runs; tune with <code>EDITOR_AUTO_PIPELINE_DEBOUNCE</code>) or <strong>Mark transcript review done</strong> for an immediate run. Set <code>EDITOR_AUTO_PIPELINE=0</code> to only run <code>run_pipeline.py --skip 00,01</code> manually. New trim clears gates until you confirm again.</p>
        </div>
      </div>

      <!-- Video trimmer -->
      <div id="view-video" class="view">
        <div class="card">
          <h2>Preview</h2>
          <div class="video-wrap">
            <video id="player" preload="metadata" controls></video>
          </div>
          <div class="timeline" id="timeline" title="Click to seek"></div>
          <div class="ruler" id="ruler"></div>

          <div class="toolbar">
            <span class="time-readout" id="time-readout">00:00.00 / 00:00.00</span>
            <button class="small" type="button" data-jump="-1">−1s</button>
            <button class="small" type="button" data-jump="-0.1">−0.1s</button>
            <button class="small" type="button" id="play-btn">Play</button>
            <button class="small" type="button" data-jump="0.1">+0.1s</button>
            <button class="small" type="button" data-jump="1">+1s</button>
            <span class="spacer" style="flex:1"></span>
            <label class="check"><input id="preview-skip" type="checkbox" checked /> skip cuts on play</label>
          </div>

          <div class="toolbar" style="margin-top:10px">
            <button class="warning" type="button" id="btn-trim-start">Trim start &lt;- here</button>
            <button class="warning" type="button" id="btn-trim-end">Trim end -&gt; here</button>
            <span class="spacer" style="flex:1"></span>
            <button type="button" id="btn-mark-in">Mark cut-in (I)</button>
            <button class="danger" type="button" id="btn-mark-out">Mark cut-out (O)</button>
          </div>

          <div class="editor-grid" style="margin-top:16px">
            <div>
              <div class="card" style="padding:14px">
                <h2 style="margin-bottom:8px">Keyboard</h2>
                <div class="hint">
                  <span class="kbd">Space</span> play/pause ·
                  <span class="kbd">←</span>/<span class="kbd">→</span> -/+ 0.1s ·
                  <span class="kbd">Shift+←/→</span> -/+ 1s ·
                  <span class="kbd">I</span> cut-in ·
                  <span class="kbd">O</span> cut-out ·
                  <span class="kbd">[</span> trim start ·
                  <span class="kbd">]</span> trim end
                </div>
              </div>
            </div>
            <div>
              <div class="card" style="padding:14px">
                <h2>Cuts to remove (<span id="cut-count">0</span>)</h2>
                <div id="cuts" class="cuts-list"></div>
                <div class="footer-row" style="margin-top:10px">
                  <span class="hint" id="keep-summary">Kept duration: —</span>
                </div>
              </div>
            </div>
          </div>

          <div class="footer-row">
            <span class="spacer"></span>
            <button type="button" id="btn-restore" class="ghost">Restore .bak</button>
            <button type="button" id="btn-clear">Clear cuts</button>
            <button type="button" id="btn-save-video" class="success">Save trim</button>
          </div>
        </div>
      </div>

      <!-- Transcript fixer -->
      <div id="view-transcript" class="view">
        <div class="editor-grid">
          <div class="card">
            <h2>Transcript</h2>
            <div id="transcript-media-slot"></div>
            <div id="transcript" class="transcript"></div>

            <div class="toolbar">
              <input id="find" type="text" placeholder="Find (e.g. uenos)" aria-label="Find" />
              <input id="replace" type="text" placeholder="Replace with (e.g. Ubers)" aria-label="Replace" />
              <label class="check">
                <input id="ci" type="checkbox" /> case-insensitive
              </label>
              <button id="apply" class="primary" type="button">Apply</button>
              <button id="revert-transcript" class="ghost" type="button">Reload</button>
              <span class="spacer" style="flex:1"></span>
              <button id="save-transcript" class="success" type="button">Save</button>
            </div>

            <div class="history">
              <h2 style="margin-top:14px">History</h2>
              <div id="history"></div>
            </div>

            <div class="footer-row">
              <span class="hint">Tip: click any word on the right to copy it into “Find”.</span>
            </div>
          </div>

          <div class="card">
            <h2>Unique words (<span id="word-count">0</span>)</h2>
            <input id="word-search" class="search" type="text" placeholder="Filter words…" />
            <div id="words" class="word-list"></div>
          </div>
        </div>
      </div>
    </section>
  </main>

  <div id="toast" class="toast" role="status" aria-live="polite"></div>
  <div id="overlay" class="overlay"><div class="panel" id="overlay-text">Working…</div></div>

  <script>
    //////////////////////////////////////////////////////////////////////////
    // Shared helpers
    //////////////////////////////////////////////////////////////////////////
    const $ = (id) => document.getElementById(id);
    const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
    const fmt = (t) => {
      if (!isFinite(t) || t < 0) t = 0;
      const m = Math.floor(t / 60);
      const s = t - m * 60;
      return `${String(m).padStart(2, "0")}:${s.toFixed(2).padStart(5, "0")}`;
    };
    const escapeHtml = (s) => (s || "").replace(/[&<>"']/g, (c) => ({
      "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"
    }[c]));
    const humanBytes = (n) => {
      if (!n) return "0B";
      const u = ["B","KB","MB","GB","TB"]; let i = 0; let x = n;
      while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
      return `${x.toFixed(x < 10 && i ? 1 : 0)}${u[i]}`;
    };

    function toast(msg, kind = "") {
      const t = $("toast");
      t.className = "toast show " + kind;
      t.textContent = msg;
      clearTimeout(toast._t);
      toast._t = setTimeout(() => { t.className = "toast"; }, 2400);
    }
    const setStatus = (x) => { $("status").textContent = x; };
    const showOverlay = (m) => { $("overlay-text").textContent = m; $("overlay").classList.add("show"); };
    const hideOverlay = () => { $("overlay").classList.remove("show"); };

    function showView(name) {
      for (const v of ["welcome","video","transcript"]) {
        $("view-" + v).classList.toggle("active", v === name);
      }
    }

    //////////////////////////////////////////////////////////////////////////
    // App state
    //////////////////////////////////////////////////////////////////////////
    const app = {
      files: [],
      currentPath: null,
      kind: null,  // "video" | "transcript" | null
      video: {
        duration: 0, hasAudio: true,
        cuts: [], pendingIn: null, dirty: false, nextId: 1,
      },
      transcript: {
        data: null,          // full transcript JSON
        words: [],           // unique-word counts [[word, n], ...]
        timed: [],           // flat [{word, start, end}, ...] for playback
        history: [],
        dirty: false,
        hasMedia: false,
        mediaName: null,
        activeIdx: -1,       // currently-highlighted word
      },
    };
    // Separate <audio> element for the transcript editor; created on hydrate.
    let transcriptPlayer = null;

    //////////////////////////////////////////////////////////////////////////
    // File picker (sidebar)
    //////////////////////////////////////////////////////////////////////////
    async function refreshFiles() {
      const r = await fetch("/api/files");
      const data = await r.json();
      app.files = data.files || [];
      renderFiles();
    }

    function renderFiles() {
      const filter = ($("file-search").value || "").trim().toLowerCase();
      const groups = app.files.filter((g) => !filter || g.base.toLowerCase().includes(filter));
      $("file-count").textContent = groups.reduce((n, g) => n + (g.video ? 1 : 0) + (g.transcript ? 1 : 0), 0);
      const host = $("file-groups");
      if (!groups.length) {
        host.innerHTML = '<div class="empty">No editable files in <code>.tmp/</code>.<br />Run <code>00_convert_source.py</code> first.</div>';
        return;
      }
      host.innerHTML = groups.map((g) => {
        const rows = [];
        if (g.video) {
          const active = g.video.path === app.currentPath ? " active" : "";
          rows.push(`
            <div class="row${active}" data-path="${escapeHtml(g.video.path)}">
              <span class="kind-tag video">video</span>
              <span class="meta">${g.video.duration.toFixed(1)}s · ${humanBytes(g.video.size)}</span>
              ${g.video.has_backup ? '<span class="bak">bak</span>' : ''}
            </div>`);
        }
        if (g.transcript) {
          const active = g.transcript.path === app.currentPath ? " active" : "";
          rows.push(`
            <div class="row${active}" data-path="${escapeHtml(g.transcript.path)}">
              <span class="kind-tag transcript">transcript</span>
              <span class="meta">${g.transcript.words} words</span>
              ${g.transcript.has_backup ? '<span class="bak">bak</span>' : ''}
            </div>`);
        }
        const trimOk = g.trim_review
          ? `<span class="pill ok">trim ✓</span>`
          : `<button type="button" class="small warning gate-trim" data-base="${escapeHtml(g.base)}">Mark trim done</button>`;
        const reviewHint = !g.transcript
          ? `<span class="hint">Transcribe runs after “Mark trim done” (or <code>--only 01</code> if auto is off)</span>`
          : "";
        const reviewOk = g.editor_review
          ? `<span class="pill ok">review ✓</span>`
          : `<button type="button" class="small success gate-review" data-base="${escapeHtml(g.base)}" ${!g.transcript ? "disabled" : ""}>Mark transcript review done</button>`;
        const gate = `
          <div class="group-gate group-gate-split">
            <div class="gate-row"><span class="gate-label">1 · Trim</span>${trimOk}${reviewHint}</div>
            <div class="gate-row"><span class="gate-label">2 · Transcript</span>${reviewOk}<span class="hint">Then <code>--skip 00,01</code></span></div>
          </div>`;
        return `
          <div class="group">
            <div class="group-title"><span class="base">${escapeHtml(g.base)}</span></div>
            ${gate}
            ${rows.join("")}
          </div>`;
      }).join("");

      host.querySelectorAll(".row[data-path]").forEach((el) => {
        el.addEventListener("click", () => openFile(el.dataset.path));
      });
      async function postGate(url, base, okMsg = null) {
        showOverlay("Saving…");
        try {
          const r = await fetch(url, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ base }),
          });
          const data = await r.json().catch(() => ({}));
          if (!r.ok) {
            toast("Could not confirm: " + (data.error || r.statusText), "error");
            return;
          }
          let msg = okMsg;
          if (!msg && url.includes("editor-review/trim")) {
            msg = data.auto_transcribe
              ? "Trim confirmed — transcribing in background (watch the terminal)"
              : "Trim confirmed";
          }
          if (!msg && url.includes("editor-review/confirm")) {
            msg = data.auto_pipeline
              ? "Review confirmed — pipeline starting (watch the terminal)"
              : "Transcript review confirmed";
          }
          toast(msg || "Saved", "success");
          await refreshFiles();
        } catch (e) {
          toast("Request failed: " + e, "error");
        } finally {
          hideOverlay();
        }
      }
      host.querySelectorAll(".gate-trim").forEach((btn) => {
        btn.addEventListener("click", async (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          const base = btn.getAttribute("data-base");
          if (!base) return;
          await postGate("/api/editor-review/trim", base);
        });
      });
      host.querySelectorAll(".gate-review").forEach((btn) => {
        btn.addEventListener("click", async (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          const base = btn.getAttribute("data-base");
          if (!base || btn.disabled) return;
          await postGate("/api/editor-review/confirm", base);
        });
      });
    }

    async function openFile(path) {
      if (app.currentPath === path) return;
      if (isDirty() && !confirm("You have unsaved changes. Discard and open another file?")) return;
      // Pause any in-flight transcript playback so we don't stream the old
      // media while the new file is being loaded.
      if (transcriptPlayer) { try { transcriptPlayer.pause(); } catch (e) { /* ignore */ } }
      setStatus("Opening…");
      const r = await fetch("/api/open", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ path }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({error: "open failed"}));
        toast("Open failed: " + (err.error || r.statusText), "error");
        setStatus("Error"); return;
      }
      const state = await r.json();
      app.currentPath = state.path;
      app.kind = state.kind;
      $("file-label").textContent = state.name;
      const badge = $("mode-badge");
      badge.style.display = "";
      badge.textContent = state.kind === "video" ? "Video trim" : "Transcript";
      badge.className = "mode " + state.kind;
      $("btn-close").style.display = "";
      if (state.kind === "video") {
        hydrateVideo(state);
        showView("video");
      } else {
        hydrateTranscript(state);
        showView("transcript");
      }
      renderFiles();
      setStatus("Ready");
    }

    async function closeFile() {
      if (isDirty() && !confirm("Discard unsaved changes?")) return;
      if (transcriptPlayer) { try { transcriptPlayer.pause(); } catch (e) { /* ignore */ } }
      await fetch("/api/close", { method: "POST" });
      app.currentPath = null;
      app.kind = null;
      $("file-label").textContent = "No file open";
      $("mode-badge").style.display = "none";
      $("btn-close").style.display = "none";
      showView("welcome");
      setStatus("Ready");
      renderFiles();
    }

    function isDirty() {
      if (app.kind === "video") return app.video.dirty;
      if (app.kind === "transcript") return app.transcript.dirty;
      return false;
    }

    $("btn-refresh").addEventListener("click", refreshFiles);
    $("file-search").addEventListener("input", renderFiles);
    $("btn-close").addEventListener("click", closeFile);

    //////////////////////////////////////////////////////////////////////////
    // Video editor
    //////////////////////////////////////////////////////////////////////////
    const player = $("player");

    function hydrateVideo(state) {
      app.video.duration = +state.duration || 0;
      app.video.hasAudio = !!state.has_audio;
      app.video.cuts = [];
      app.video.pendingIn = null;
      app.video.dirty = false;
      app.video.nextId = 1;
      player.src = `/video?v=${Date.now()}`;
      player.load();
      player.addEventListener("loadedmetadata", () => {
        if (!app.video.duration || Math.abs(app.video.duration - player.duration) > 0.5) {
          app.video.duration = player.duration || app.video.duration;
        }
        renderTimeline(); renderCuts();
      }, { once: true });
    }

    function sortedNormalizedCuts() {
      const dur = app.video.duration;
      return app.video.cuts
        .map((c) => ({ id: c.id, start: clamp(+c.start || 0, 0, dur), end: clamp(+c.end || 0, 0, dur) }))
        .filter((c) => c.end - c.start > 0.02)
        .sort((a, b) => a.start - b.start);
    }

    function keptDuration() {
      const cuts = sortedNormalizedCuts();
      let removed = 0, cursor = 0;
      for (const c of cuts) {
        const s = Math.max(c.start, cursor);
        const e = Math.max(c.end, s);
        if (e > s) { removed += e - s; cursor = e; }
      }
      return Math.max(0, app.video.duration - removed);
    }

    function renderTimeline() {
      const tl = $("timeline");
      const dur = app.video.duration || 1;
      const cuts = sortedNormalizedCuts();
      tl.innerHTML = '<div class="track"></div>';
      const steps = 10;
      for (let i = 1; i < steps; i++) {
        const d = document.createElement("div");
        d.className = "tick";
        d.style.left = (i / steps) * 100 + "%";
        tl.appendChild(d);
      }
      for (const c of cuts) {
        const d = document.createElement("div");
        d.className = "cut";
        d.title = `${fmt(c.start)} → ${fmt(c.end)}  (remove)`;
        d.style.left = (c.start / dur * 100) + "%";
        d.style.width = ((c.end - c.start) / dur * 100) + "%";
        d.dataset.id = c.id;
        d.addEventListener("click", (e) => {
          e.stopPropagation();
          player.currentTime = c.start;
        });
        tl.appendChild(d);
      }
      if (app.video.pendingIn != null) {
        const d = document.createElement("div");
        d.className = "marker";
        d.style.left = (app.video.pendingIn / dur * 100) + "%";
        tl.appendChild(d);
      }
      const ph = document.createElement("div");
      ph.className = "playhead"; ph.id = "playhead";
      tl.appendChild(ph);
      renderRuler();
      updatePlayhead();
    }

    function renderRuler() {
      const r = $("ruler"); r.innerHTML = "";
      const dur = app.video.duration || 0;
      if (dur <= 0) return;
      const steps = 10;
      for (let i = 0; i <= steps; i++) {
        const s = document.createElement("span");
        s.style.left = (i / steps) * 100 + "%";
        s.textContent = fmt(dur * (i / steps));
        r.appendChild(s);
      }
    }

    function updatePlayhead() {
      const ph = $("playhead");
      if (!ph) return;
      const dur = app.video.duration || 1;
      const t = player.currentTime || 0;
      ph.style.left = (t / dur * 100) + "%";
      $("time-readout").textContent = `${fmt(t)} / ${fmt(dur)}`;
    }

    function renderCuts() {
      const host = $("cuts");
      const cuts = sortedNormalizedCuts();
      $("cut-count").textContent = cuts.length;
      if (!cuts.length) {
        host.innerHTML = '<div class="hint">No cuts yet. Mark <b>I</b>n / <b>O</b>ut on the player, or click “Trim start/end”.</div>';
      } else {
        host.innerHTML = cuts.map((c) => {
          const tag = (c.start <= 0.05) ? "start-trim"
                   : (c.end >= app.video.duration - 0.05) ? "end-trim" : "cut";
          return `
            <div class="cut-row" data-id="${c.id}">
              <span class="tag">${tag}</span>
              <input type="number" step="0.01" min="0" max="${app.video.duration.toFixed(3)}" data-id="${c.id}" data-field="start" value="${c.start.toFixed(2)}" />
              <span class="sep">→</span>
              <input type="number" step="0.01" min="0" max="${app.video.duration.toFixed(3)}" data-id="${c.id}" data-field="end" value="${c.end.toFixed(2)}" />
              <div class="row-actions">
                <button class="small" type="button" data-go="${c.id}" title="Seek to start">▶</button>
                <button class="small danger" type="button" data-del="${c.id}" title="Remove cut">✕</button>
              </div>
            </div>`;
        }).join("");
        host.querySelectorAll("input[data-id]").forEach((inp) => {
          inp.addEventListener("change", (e) => {
            const id = +e.target.dataset.id;
            const f = e.target.dataset.field;
            const v = parseFloat(e.target.value);
            const cut = app.video.cuts.find((c) => c.id === id);
            if (cut && isFinite(v)) {
              cut[f] = clamp(v, 0, app.video.duration);
              if (cut.end < cut.start) [cut.start, cut.end] = [cut.end, cut.start];
              markVideoDirty(); renderCuts(); renderTimeline();
            }
          });
        });
        host.querySelectorAll("[data-del]").forEach((b) => {
          b.addEventListener("click", () => {
            app.video.cuts = app.video.cuts.filter((c) => c.id !== +b.dataset.del);
            markVideoDirty(); renderCuts(); renderTimeline();
          });
        });
        host.querySelectorAll("[data-go]").forEach((b) => {
          b.addEventListener("click", () => {
            const cut = app.video.cuts.find((c) => c.id === +b.dataset.go);
            if (cut) player.currentTime = cut.start;
          });
        });
      }
      const kept = keptDuration();
      $("keep-summary").textContent = `Kept duration: ${fmt(kept)}  (−${fmt(app.video.duration - kept)})`;
    }

    function markVideoDirty() {
      app.video.dirty = sortedNormalizedCuts().length > 0;
      setStatus(app.video.dirty ? "Unsaved cuts" : "Ready");
    }

    function addCut(start, end) {
      const s = clamp(Math.min(start, end), 0, app.video.duration);
      const e = clamp(Math.max(start, end), 0, app.video.duration);
      if (e - s < 0.05) { toast("Cut too short (< 50ms).", "error"); return; }
      app.video.cuts.push({ id: app.video.nextId++, start: s, end: e });
      markVideoDirty(); renderCuts(); renderTimeline();
    }

    function upsertStartTrim(to) {
      app.video.cuts = app.video.cuts.filter((c) => !(c.start <= 0.05));
      if (to > 0.05) addCut(0, to);
      else { markVideoDirty(); renderCuts(); renderTimeline(); }
    }
    function upsertEndTrim(from) {
      app.video.cuts = app.video.cuts.filter((c) => !(c.end >= app.video.duration - 0.05));
      if (from < app.video.duration - 0.05) addCut(from, app.video.duration);
      else { markVideoDirty(); renderCuts(); renderTimeline(); }
    }

    async function saveVideo() {
      const cuts = sortedNormalizedCuts().map((c) => ({ start: c.start, end: c.end }));
      if (!cuts.length) { toast("No cuts to save.", "error"); return; }
      if (!confirm(`Apply ${cuts.length} cut(s) and overwrite the video?\nA .bak copy will be created on first save.`)) return;
      showOverlay("Rendering with FFmpeg. This may take a while…");
      setStatus("Rendering…");
      try {
        const r = await fetch("/api/video/save", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ cuts }),
        });
        const data = await r.json();
        if (!r.ok || !data.ok) throw new Error(data.error || "save failed");
        toast(`Saved: ${cuts.length} cut(s) applied  (kept ${data.new_duration.toFixed(1)}s of ${data.old_duration.toFixed(1)}s)${data.backup ? " · .bak created" : ""}`, "success");
        await openFile(app.currentPath);  // reload
        refreshFiles();
      } catch (err) {
        toast("Save failed: " + (err.message || err), "error");
        setStatus("Error");
      } finally {
        hideOverlay();
      }
    }

    async function restoreVideoBackup() {
      if (!confirm("Restore the original video from the .bak backup? This overwrites the current file.")) return;
      showOverlay("Restoring from backup…");
      try {
        const r = await fetch("/api/video/restore", { method: "POST" });
        const data = await r.json();
        if (!r.ok || !data.ok) throw new Error(data.error || "restore failed");
        toast("Restored from backup.", "success");
        await openFile(app.currentPath);
        refreshFiles();
      } catch (err) {
        toast("Restore failed: " + (err.message || err), "error");
      } finally {
        hideOverlay();
      }
    }

    function clearVideoCuts() {
      if (app.video.dirty && !confirm("Discard unsaved cuts?")) return;
      app.video.cuts = [];
      app.video.pendingIn = null;
      markVideoDirty(); renderCuts(); renderTimeline();
    }

    // Player wiring
    player.addEventListener("timeupdate", () => {
      if (app.kind !== "video") return;
      updatePlayhead();
      if ($("preview-skip").checked && !player.paused) {
        const t = player.currentTime;
        for (const c of sortedNormalizedCuts()) {
          if (t >= c.start - 0.01 && t < c.end - 0.01) {
            if (c.end >= app.video.duration - 0.02) { player.pause(); player.currentTime = app.video.duration; }
            else player.currentTime = c.end;
            break;
          }
        }
      }
    });
    player.addEventListener("play", () => $("play-btn").textContent = "Pause");
    player.addEventListener("pause", () => $("play-btn").textContent = "Play");
    $("play-btn").addEventListener("click", () => player.paused ? player.play() : player.pause());
    document.querySelectorAll("[data-jump]").forEach((b) => {
      b.addEventListener("click", () => {
        player.currentTime = clamp(player.currentTime + parseFloat(b.dataset.jump), 0, app.video.duration);
      });
    });
    $("timeline").addEventListener("click", (e) => {
      const rect = e.currentTarget.getBoundingClientRect();
      const pct = (e.clientX - rect.left) / rect.width;
      player.currentTime = clamp(pct * app.video.duration, 0, app.video.duration);
    });
    $("btn-trim-start").addEventListener("click", () => upsertStartTrim(player.currentTime));
    $("btn-trim-end").addEventListener("click", () => upsertEndTrim(player.currentTime));
    $("btn-mark-in").addEventListener("click", () => {
      app.video.pendingIn = player.currentTime;
      renderTimeline();
      toast(`Cut-in @ ${fmt(app.video.pendingIn)} — now press O.`);
    });
    $("btn-mark-out").addEventListener("click", () => {
      if (app.video.pendingIn == null) { toast("Press Mark cut-in (I) first.", "error"); return; }
      addCut(app.video.pendingIn, player.currentTime);
      app.video.pendingIn = null; renderTimeline();
    });
    $("btn-save-video").addEventListener("click", saveVideo);
    $("btn-restore").addEventListener("click", restoreVideoBackup);
    $("btn-clear").addEventListener("click", clearVideoCuts);

    //////////////////////////////////////////////////////////////////////////
    // Transcript editor
    //////////////////////////////////////////////////////////////////////////
    function flattenTimedWords(transcript) {
      // Prefer segment-level words (richer / complete). Fall back to the
      // flat top-level ``words`` array if segments are missing. Skip tokens
      // whose ``word`` was cleared (e.g. by a multi-word replacement).
      const out = [];
      const segs = transcript?.segments || [];
      if (segs.length) {
        for (const seg of segs) {
          for (const w of (seg.words || [])) {
            if (!w || typeof w.word !== "string") continue;
            const txt = String(w.word);
            if (!txt.trim()) continue;
            out.push({
              word: txt,
              start: Number.isFinite(+w.start) ? +w.start : null,
              end:   Number.isFinite(+w.end)   ? +w.end   : null,
            });
          }
        }
      }
      if (!out.length) {
        for (const w of (transcript?.words || [])) {
          if (!w || typeof w.word !== "string") continue;
          const txt = String(w.word);
          if (!txt.trim()) continue;
          out.push({
            word: txt,
            start: Number.isFinite(+w.start) ? +w.start : null,
            end:   Number.isFinite(+w.end)   ? +w.end   : null,
          });
        }
      }
      return out;
    }

    function hydrateTranscript(state) {
      app.transcript.data = state.transcript;
      app.transcript.words = state.words || [];
      app.transcript.timed = flattenTimedWords(state.transcript);
      app.transcript.history = [];
      app.transcript.dirty = false;
      app.transcript.hasMedia = !!state.has_media;
      app.transcript.mediaName = state.media_name || null;
      app.transcript.activeIdx = -1;
      renderMediaStrip();
      renderTranscript();
      renderTranscriptWords();
      renderHistory();
    }

    function renderMediaStrip() {
      const slot = $("transcript-media-slot");
      // Detach previous listeners by simply replacing the element.
      if (transcriptPlayer) {
        try { transcriptPlayer.pause(); } catch (e) { /* ignore */ }
        transcriptPlayer = null;
      }
      if (!app.transcript.hasMedia) {
        slot.innerHTML = `
          <div class="media-missing">
            No matching media found for this transcript. Drop a
            <code>.mp4</code> next to the JSON (same basename) to enable playback.
          </div>`;
        return;
      }
      slot.innerHTML = `
        <div class="media-strip">
          <audio id="transcript-player" preload="metadata" controls src="/video?v=${Date.now()}"></audio>
          <span class="media-meta">${escapeHtml(app.transcript.mediaName || "media")}</span>
        </div>`;
      transcriptPlayer = $("transcript-player");
      transcriptPlayer.addEventListener("timeupdate", onTranscriptTimeUpdate);
      transcriptPlayer.addEventListener("seeking", onTranscriptTimeUpdate);
      transcriptPlayer.addEventListener("ended", () => {
        app.transcript.activeIdx = -1;
        setActiveWord(-1);
      });
    }

    function renderTranscript() {
      const host = $("transcript");
      const timed = app.transcript.timed;
      if (!timed.length) {
        // No timed words — fall back to the plain ``text`` field so the user
        // still sees something editable.
        host.textContent = app.transcript.data?.text || "";
        return;
      }
      // Build word spans. Use document fragments for a fast single insert.
      const frag = document.createDocumentFragment();
      for (let i = 0; i < timed.length; i++) {
        const w = timed[i];
        if (i > 0) frag.appendChild(document.createTextNode(" "));
        const span = document.createElement("span");
        span.className = "w";
        span.dataset.idx = i;
        if (w.start != null) span.dataset.start = w.start.toFixed(3);
        if (w.end != null)   span.dataset.end = w.end.toFixed(3);
        span.textContent = w.word;
        frag.appendChild(span);
      }
      host.innerHTML = "";
      host.appendChild(frag);
      app.transcript.activeIdx = -1;
    }

    // Binary search for the word whose [start, end] contains ``t``. If no word
    // is active at ``t`` (between words), we pick the word that is about to
    // start (closest upcoming) so scrolling still tracks the playhead.
    function findActiveWordIdx(t) {
      const arr = app.transcript.timed;
      if (!arr.length) return -1;
      let lo = 0, hi = arr.length - 1, best = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const w = arr[mid];
        if (w.start == null) { lo = mid + 1; continue; }
        if (t < w.start) { hi = mid - 1; }
        else if (w.end != null && t > w.end) { best = mid; lo = mid + 1; }
        else { return mid; }  // t is within [start, end]
      }
      // If we fell through, ``best`` is the last finished word; the next one
      // is the upcoming word — prefer highlighting that if it is close.
      const next = best + 1;
      if (next < arr.length && arr[next].start != null && arr[next].start - t < 0.25) {
        return next;
      }
      return best;
    }

    function setActiveWord(idx) {
      const prev = app.transcript.activeIdx;
      if (prev === idx) return;
      const host = $("transcript");
      if (prev >= 0) {
        const el = host.querySelector(`.w[data-idx="${prev}"]`);
        if (el) el.classList.remove("active");
      }
      app.transcript.activeIdx = idx;
      if (idx < 0) return;
      const el = host.querySelector(`.w[data-idx="${idx}"]`);
      if (!el) return;
      el.classList.add("active");
      // Keep the active word visible without fighting the user's scroll.
      const hostRect = host.getBoundingClientRect();
      const elRect = el.getBoundingClientRect();
      if (elRect.top < hostRect.top + 40 || elRect.bottom > hostRect.bottom - 40) {
        el.scrollIntoView({ block: "center", behavior: "smooth" });
      }
    }

    function onTranscriptTimeUpdate() {
      if (!transcriptPlayer) return;
      const idx = findActiveWordIdx(transcriptPlayer.currentTime || 0);
      setActiveWord(idx);
    }

    function renderHistory() {
      const h = $("history");
      if (!app.transcript.history.length) {
        h.innerHTML = '<div class="hint">No replacements yet.</div>';
        return;
      }
      h.innerHTML = app.transcript.history.map((r) => `
        <div class="row">
          <code>${escapeHtml(r.old)}</code>
          <span>→</span>
          <code>${escapeHtml(r.new)}</code>
          ${r.ci ? '<span class="hint">(case-insensitive)</span>' : ''}
          <span class="count">${r.count} replaced</span>
        </div>
      `).join("");
    }

    function renderTranscriptWords() {
      const filter = ($("word-search").value || "").trim().toLowerCase();
      const list = app.transcript.words || [];
      const filtered = filter ? list.filter(([w]) => w.toLowerCase().includes(filter)) : list;
      $("word-count").textContent = list.length;
      $("words").innerHTML = filtered.slice(0, 600).map(([w, n]) => `
        <span class="chip ${n <= 2 ? 'rare' : ''}" data-word="${escapeHtml(w)}">
          ${escapeHtml(w)} <span class="n">${n}</span>
        </span>
      `).join("");
      document.querySelectorAll(".chip").forEach((c) => {
        c.addEventListener("click", () => {
          $("find").value = c.dataset.word;
          $("find").focus(); $("find").select();
        });
      });
    }

    async function applyReplacement() {
      const old = $("find").value;
      const nw = $("replace").value;
      const ci = $("ci").checked;
      if (!old) { toast("Enter the word to find.", "error"); return; }
      setStatus("Applying…");
      const r = await fetch("/api/transcript/replace", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ old, new: nw, case_insensitive: ci }),
      });
      if (!r.ok) { toast("Replace failed.", "error"); setStatus("Error"); return; }
      const data = await r.json();
      app.transcript.data = data.transcript;
      app.transcript.words = data.words;
      app.transcript.timed = flattenTimedWords(data.transcript);
      app.transcript.history.push({ old, new: nw, ci, count: data.count });
      app.transcript.dirty = app.transcript.dirty || data.count > 0;
      renderTranscript(); renderTranscriptWords(); renderHistory();
      // Re-sync the active-word highlight with the current playhead, if any.
      onTranscriptTimeUpdate();
      setStatus(app.transcript.dirty ? "Unsaved changes" : "Ready");
      toast(`${data.count} replacement${data.count === 1 ? "" : "s"}.`, data.count ? "success" : "");
      $("find").value = ""; $("replace").value = ""; $("find").focus();
    }

    async function saveTranscript() {
      setStatus("Saving…");
      const r = await fetch("/api/transcript/save", { method: "POST" });
      if (!r.ok) { toast("Save failed.", "error"); setStatus("Error"); return; }
      const data = await r.json();
      app.transcript.dirty = false;
      setStatus("Saved");
      let saveMsg = `Saved → ${data.path}${data.backup ? " (backup created)" : ""}`;
      if (data.auto_pipeline_scheduled) {
        saveMsg += ` — pipeline in ~${data.pipeline_debounce_sec}s (watch terminal)`;
      }
      toast(saveMsg, "success");
      refreshFiles();
    }

    async function reloadTranscript() {
      if (app.transcript.dirty && !confirm("Discard unsaved changes and reload from disk?")) return;
      await fetch("/api/transcript/reload", { method: "POST" });
      await openFile(app.currentPath);
    }

    $("apply").addEventListener("click", applyReplacement);
    $("save-transcript").addEventListener("click", saveTranscript);
    $("revert-transcript").addEventListener("click", reloadTranscript);
    $("word-search").addEventListener("input", renderTranscriptWords);
    $("find").addEventListener("keydown", (e) => { if (e.key === "Enter") $("replace").focus(); });
    $("replace").addEventListener("keydown", (e) => { if (e.key === "Enter") applyReplacement(); });

    // Delegated click on word spans: seek the player + start playing, and
    // also copy the word's core into the Find box so a replacement is one
    // keystroke away. Modifier keys copy the word without seeking.
    $("transcript").addEventListener("click", (e) => {
      const span = e.target.closest(".w");
      if (!span) return;
      const word = span.textContent || "";
      const core = _core(word);
      if (e.shiftKey || e.metaKey || e.ctrlKey) {
        $("find").value = core;
        $("find").focus(); $("find").select();
        return;
      }
      if (core) $("find").value = core;
      const start = parseFloat(span.dataset.start || "");
      if (transcriptPlayer && isFinite(start)) {
        try {
          transcriptPlayer.currentTime = Math.max(0, start);
          transcriptPlayer.play().catch(() => { /* user gesture may be needed */ });
        } catch (err) { /* ignore */ }
      }
    });

    // Lightweight mirror of Python's _core() — strip leading/trailing
    // punctuation so Shift-click copies "uenos" from "uenos,".
    function _core(token) {
      const PUNCT = new Set(['.', ',', '!', '?', '"', "'", '`', '´', '“', '”', '„', '‘', '’', '«', '»',
                             '(', ')', '[', ']', '{', '}', ':', ';', '…', '—', '–', '-', '•', '·',
                             '¿', '¡']);
      let i = 0, n = token.length;
      while (i < n && PUNCT.has(token[i])) i++;
      let j = n;
      while (j > i && PUNCT.has(token[j - 1])) j--;
      return token.slice(i, j);
    }

    //////////////////////////////////////////////////////////////////////////
    // Keyboard (video editor only)
    //////////////////////////////////////////////////////////////////////////
    document.addEventListener("keydown", (e) => {
      if (app.kind !== "video") return;
      if (["INPUT", "TEXTAREA"].includes(e.target.tagName)) return;
      if (e.key === " ") { e.preventDefault(); player.paused ? player.play() : player.pause(); }
      else if (e.key === "ArrowLeft")  player.currentTime = clamp(player.currentTime + (e.shiftKey ? -1 : -0.1), 0, app.video.duration);
      else if (e.key === "ArrowRight") player.currentTime = clamp(player.currentTime + (e.shiftKey ?  1 :  0.1), 0, app.video.duration);
      else if (e.key === "i" || e.key === "I") {
        app.video.pendingIn = player.currentTime; renderTimeline();
        toast(`Cut-in @ ${fmt(app.video.pendingIn)}`);
      }
      else if (e.key === "o" || e.key === "O") {
        if (app.video.pendingIn == null) { toast("Press I first.", "error"); return; }
        addCut(app.video.pendingIn, player.currentTime);
        app.video.pendingIn = null; renderTimeline();
      }
      else if (e.key === "[") upsertStartTrim(player.currentTime);
      else if (e.key === "]") upsertEndTrim(player.currentTime);
    });

    window.addEventListener("beforeunload", (e) => {
      if (isDirty()) { e.preventDefault(); e.returnValue = ""; }
    });

    //////////////////////////////////////////////////////////////////////////
    // Boot
    //////////////////////////////////////////////////////////////////////////
    (async function boot() {
      await refreshFiles();
      // If the server already has a file opened (e.g. launched with a path arg),
      // reflect it in the UI on load.
      const r = await fetch("/api/state");
      const st = await r.json();
      if (st && st.kind) {
        app.currentPath = st.path; app.kind = st.kind;
        $("file-label").textContent = st.name;
        const badge = $("mode-badge");
        badge.style.display = ""; badge.textContent = st.kind === "video" ? "Video trim" : "Transcript";
        badge.className = "mode " + st.kind;
        $("btn-close").style.display = "";
        if (st.kind === "video") { hydrateVideo(st); showView("video"); }
        else { hydrateTranscript(st); showView("transcript"); }
        renderFiles();
      }
    })().catch((err) => { console.error(err); toast("Boot failed: " + err.message, "error"); });
  </script>
</body>
</html>
"""


# ============================================================================ #
# HTTP handler
# ============================================================================ #


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    if not header or not header.startswith("bytes="):
        return None
    spec = header[len("bytes="):].split(",", 1)[0].strip()
    if "-" not in spec:
        return None
    a, b = spec.split("-", 1)
    try:
        if a == "":
            n = int(b)
            if n <= 0:
                return None
            start = max(0, size - n)
            end = size - 1
        else:
            start = int(a)
            end = int(b) if b else size - 1
    except ValueError:
        return None
    if start < 0 or end >= size or start > end:
        return None
    return start, end


def _build_handler(session: Session):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("[00b-editor] " + (fmt % args) + "\n")

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_video(self) -> None:
            path = session.current_video_path()
            if not path:
                self.send_response(404); self.end_headers(); return
            try:
                size = os.path.getsize(path)
            except OSError:
                self.send_response(404); self.end_headers(); return
            ctype = mimetypes.guess_type(path)[0] or "video/mp4"
            rng = _parse_range(self.headers.get("Range", ""), size)
            if rng is None:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with open(path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, length=1024 * 1024)
                return
            start, end = rng
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                chunk = 1024 * 256
                while remaining > 0:
                    data = f.read(min(chunk, remaining))
                    if not data:
                        break
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    remaining -= len(data)

        # ---- Routing ---- #
        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send_html(INDEX_HTML); return
            if path == "/api/files":
                self._send_json(200, {"files": list_editable_files(session.tmp_dir)}); return
            if path == "/api/state":
                self._send_json(200, session.state()); return
            if path == "/video":
                self._send_video(); return
            self.send_response(404); self.end_headers()

        def do_HEAD(self):  # noqa: N802
            if urlparse(self.path).path == "/video":
                path = session.current_video_path()
                if not path:
                    self.send_response(404); self.end_headers(); return
                try:
                    size = os.path.getsize(path)
                except OSError:
                    self.send_response(404); self.end_headers(); return
                ctype = mimetypes.guess_type(path)[0] or "video/mp4"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return
            self.send_response(404); self.end_headers()

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON"}); return

            if path == "/api/open":
                target = str(body.get("path", "")).strip()
                if not target or not os.path.isfile(target):
                    self._send_json(400, {"error": f"not a file: {target}"}); return
                try:
                    state = session.open(target)
                except (ValueError, FileNotFoundError, OSError, json.JSONDecodeError) as e:
                    self._send_json(400, {"error": str(e)}); return
                self._send_json(200, state); return

            if path == "/api/close":
                session.close()
                self._send_json(200, {"ok": True}); return

            if path == "/api/video/save":
                if session.kind != "video" or session.video is None:
                    self._send_json(400, {"error": "no video open"}); return
                cuts = body.get("cuts") or []
                if not isinstance(cuts, list):
                    self._send_json(400, {"error": "'cuts' must be a list"}); return
                try:
                    info = session.video.save(cuts)
                except (RuntimeError, OSError, FileNotFoundError) as e:
                    self._send_json(500, {"error": str(e)}); return
                base = _video_base(session.video.path)
                editor_gate.reset_gates_after_video_trim(base, session.tmp_dir)
                self._send_json(200, info); return

            if path == "/api/video/restore":
                if session.kind != "video" or session.video is None:
                    self._send_json(400, {"error": "no video open"}); return
                try:
                    info = session.video.restore_from_backup()
                except (FileNotFoundError, OSError) as e:
                    self._send_json(500, {"error": str(e)}); return
                base = _video_base(session.video.path)
                editor_gate.reset_gates_after_video_trim(base, session.tmp_dir)
                self._send_json(200, info); return

            if path == "/api/transcript/replace":
                if session.kind != "transcript" or session.transcript is None:
                    self._send_json(400, {"error": "no transcript open"}); return
                old = str(body.get("old", "")).strip()
                new = str(body.get("new", ""))
                ci = bool(body.get("case_insensitive", False))
                if not old:
                    self._send_json(400, {"error": "missing 'old'"}); return
                count = session.transcript.replace(old, new, ci)
                self._send_json(200, {
                    "count": count,
                    "transcript": session.transcript.transcript,
                    "words": unique_word_counts(session.transcript.transcript),
                }); return

            if path == "/api/transcript/save":
                if session.kind != "transcript" or session.transcript is None:
                    self._send_json(400, {"error": "no transcript open"}); return
                try:
                    info = session.transcript.save()
                except OSError as e:
                    self._send_json(500, {"error": f"save failed: {e}"}); return
                base = _transcript_base(session.transcript.path)
                debounce = _pipeline_debounce_seconds()
                scheduled = schedule_pipeline_after_transcript_save(base, session.tmp_dir)
                info = {
                    **info,
                    "auto_pipeline_scheduled": scheduled,
                    "pipeline_debounce_sec": debounce if scheduled else 0.0,
                }
                self._send_json(200, info); return

            if path == "/api/transcript/reload":
                if session.kind != "transcript" or session.transcript is None:
                    self._send_json(400, {"error": "no transcript open"}); return
                try:
                    session.transcript.reload()
                except (OSError, json.JSONDecodeError) as e:
                    self._send_json(500, {"error": f"reload failed: {e}"}); return
                self._send_json(200, {"ok": True}); return

            if path == "/api/editor-review/trim":
                base = str(body.get("base", "")).strip()
                if not base:
                    self._send_json(400, {"error": "missing base"}); return
                if not editor_gate.tmp_base_has_video(session.tmp_dir, base):
                    self._send_json(400, {"error": f"no .mp4 in .tmp for base {base!r}"}); return
                try:
                    out = editor_gate.write_trim_confirm_for_base(base, session.tmp_dir)
                except (OSError, ValueError) as e:
                    self._send_json(500, {"error": str(e)}); return
                auto = schedule_transcribe_after_trim(base, session.tmp_dir, background=True)
                self._send_json(
                    200,
                    {"ok": True, "path": out, "auto_transcribe": auto},
                )
                return

            if path == "/api/editor-review/confirm":
                base = str(body.get("base", "")).strip()
                if not base:
                    self._send_json(400, {"error": "missing base"}); return
                if not editor_gate.tmp_base_has_transcript(session.tmp_dir, base):
                    self._send_json(400, {"error": f"no transcript in .tmp for base {base!r}"}); return
                try:
                    out = editor_gate.write_editor_review_for_base(base, session.tmp_dir)
                except (OSError, ValueError) as e:
                    self._send_json(400, {"error": str(e)}); return
                auto_p = run_pipeline_after_review_confirm(base, session.tmp_dir)
                self._send_json(200, {"ok": True, "path": out, "auto_pipeline": auto_p}); return

            self.send_response(404); self.end_headers()

    return Handler


# ============================================================================ #
# Entry point
# ============================================================================ #


def serve(
    initial_path: str | None = None,
    port: int = 5058,
    open_browser: bool = True,
    tmp_dir: str = ".tmp",
) -> None:
    session = Session(tmp_dir=tmp_dir)
    if initial_path:
        try:
            session.open(initial_path)
        except (ValueError, FileNotFoundError, OSError, json.JSONDecodeError) as e:
            print(f"[00b-editor] Could not open {initial_path!r}: {e}", file=sys.stderr)

    handler = _build_handler(session)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"[00b-editor] Serving {os.path.abspath(tmp_dir)} on {url}  (Ctrl+C to stop)")
    if _editor_auto_transcribe_enabled():
        print("[00b-editor] Auto-transcribe after trim: on (set EDITOR_AUTO_TRANSCRIBE=0 to disable)")
    else:
        print("[00b-editor] Auto-transcribe after trim: off")
    if _editor_auto_pipeline_enabled():
        print(
            f"[00b-editor] Auto-pipeline after transcript save: on, debounce {_pipeline_debounce_seconds():g}s "
            "(EDITOR_AUTO_PIPELINE=0 to disable; EDITOR_AUTO_PIPELINE_DEBOUNCE to change delay)"
        )
    else:
        print("[00b-editor] Auto-pipeline after transcript save: off")
    if session.kind:
        print(f"[00b-editor] Pre-opened ({session.kind}): {session.state().get('path')}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[00b-editor] Shutting down.")
    finally:
        server.server_close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Unified local editor: video trim + transcript find/replace."
    )
    ap.add_argument(
        "path", nargs="?",
        help="Optional file to pre-open (.mp4 for trim, *_transcript.json for fix).",
    )
    ap.add_argument("--port", type=int, default=5058, help="Local port (default: 5058).")
    ap.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser.")
    ap.add_argument("--tmp-dir", default=".tmp", help="Directory to browse (default: .tmp).")
    g_mark = ap.add_mutually_exclusive_group()
    g_mark.add_argument(
        "--mark-trim-done",
        action="store_true",
        help="Write trim gate in {base}_editor_review.json and exit.",
    )
    g_mark.add_argument(
        "--mark-done",
        action="store_true",
        help="Write transcript review gate (needs trim confirmed + transcript on disk).",
    )
    args = ap.parse_args()

    if args.mark_trim_done:
        if not args.path:
            print("Error: --mark-trim-done requires a path or base", file=sys.stderr)
            return 1
        base = editor_gate.resolve_base_from_cli_arg(args.path)
        try:
            out = editor_gate.write_trim_confirm_for_base(base, args.tmp_dir)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(f"[00b-editor] Wrote {out}")
        schedule_transcribe_after_trim(base, args.tmp_dir, background=False)
        return 0

    if args.mark_done:
        if not args.path:
            print("Error: --mark-done requires a path or base (e.g. .tmp/IMG_1234.mp4)", file=sys.stderr)
            return 1
        base = editor_gate.resolve_base_from_cli_arg(args.path)
        try:
            out = editor_gate.write_editor_review_for_base(base, args.tmp_dir)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(f"[00b-editor] Wrote {out}")
        return 0

    if args.path and not os.path.isfile(args.path):
        print(f"Not a file: {args.path}")
        return 1

    serve(
        initial_path=args.path,
        port=args.port,
        open_browser=not args.no_browser,
        tmp_dir=args.tmp_dir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

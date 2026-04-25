"""
Pipeline gates for 00b: trim must be confirmed before transcribe (01);
transcript review before steps 02+.

Marker: ``.tmp/{base}_editor_review.json``
  - v2: ``trim_confirmed_at``, ``review_confirmed_at`` (ISO UTC)
  - v1 legacy: only ``confirmed_at`` → counts as both trim + review (old workflows)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import env_paths


def video_basename(video_path: str) -> str:
    return os.path.splitext(os.path.basename(video_path))[0]


# `run_pipeline` advances the working path to each step's .mp4 (e.g. …_voice.mp4 after 03b).
# Trim/transcript gates and markers always use the original 00/01 stem (e.g. IMG_1792), not
# intermediate names — strip these suffixes when resolving from a current video path.
_STEM_STRIP_TO_TRIM_BASE: tuple[str, ...] = tuple(
    sorted(
        (
            "_no_retakes",
            "_no_fillers",
            "_fixed_audio",
            "_dataviz",
            "_hardcut",
            "_zoompan",
            "_effects",
            "_multicam",
            "_studio",
            "_voice",
            "_broll",
            "_color",
            "_scenes",
            "_fx",
            "_final",
        ),
        key=len,
        reverse=True,
    )
)


def stem_for_editor_gate(stem: str) -> str:
    """Map a .mp4 stem (possibly …_voice, …_studio) back to the trim/transcript base."""
    for _ in range(len(_STEM_STRIP_TO_TRIM_BASE) + 5):
        for suf in _STEM_STRIP_TO_TRIM_BASE:
            if stem.endswith(suf):
                stem = stem[: -len(suf)]
                break
        else:
            return stem
    return stem


def _resolve_tmp_dir(tmp_dir: str | None) -> str:
    return tmp_dir if tmp_dir is not None else env_paths.tmp_dir()


def editor_review_path(video_path: str, tmp_dir: str | None = None) -> str:
    return editor_review_path_for_base(stem_for_editor_gate(video_basename(video_path)), tmp_dir)


def editor_review_path_for_base(base: str, tmp_dir: str | None = None) -> str:
    d = _resolve_tmp_dir(tmp_dir)
    return os.path.join(d, f"{base}_editor_review.json")


def _load_marker(base: str, tmp_dir: str) -> dict[str, Any] | None:
    p = editor_review_path_for_base(base, tmp_dir)
    if not os.path.isfile(p) or os.path.getsize(p) == 0:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _is_legacy_both_done(data: dict[str, Any]) -> bool:
    return bool(data.get("confirmed_at")) and "trim_confirmed_at" not in data and "review_confirmed_at" not in data


def is_trim_complete_for_base(base: str, tmp_dir: str | None = None) -> bool:
    tmp_dir = _resolve_tmp_dir(tmp_dir)
    data = _load_marker(base, tmp_dir)
    if not data:
        return False
    if data.get("trim_confirmed_at"):
        return True
    return _is_legacy_both_done(data)


def is_trim_complete(video_path: str, tmp_dir: str | None = None) -> bool:
    return is_trim_complete_for_base(stem_for_editor_gate(video_basename(video_path)), tmp_dir)


def is_editor_review_complete_for_base(base: str, tmp_dir: str | None = None) -> bool:
    tmp_dir = _resolve_tmp_dir(tmp_dir)
    data = _load_marker(base, tmp_dir)
    if not data:
        return False
    if data.get("review_confirmed_at"):
        return True
    return _is_legacy_both_done(data)


def is_editor_review_complete(video_path: str, tmp_dir: str | None = None) -> bool:
    return is_editor_review_complete_for_base(
        stem_for_editor_gate(video_basename(video_path)), tmp_dir
    )


def editor_gate_step_ids(all_steps: list[dict[str, Any]]) -> frozenset[str]:
    """Steps that require transcript review (post-01); excludes 00 and 01."""
    return frozenset(s["id"] for s in all_steps if s["id"] not in ("00", "01"))


TRANSCRIBE_GATE_STEP_IDS: frozenset[str] = frozenset({"01"})


def tmp_base_has_video(tmp_dir: str, base: str) -> bool:
    v = os.path.join(tmp_dir, f"{base}.mp4")
    try:
        return os.path.isfile(v) and os.path.getsize(v) > 0
    except OSError:
        return False


def tmp_base_has_transcript(tmp_dir: str, base: str) -> bool:
    t = os.path.join(tmp_dir, f"{base}_transcript.json")
    try:
        return os.path.isfile(t) and os.path.getsize(t) > 0
    except OSError:
        return False


def tmp_base_has_assets(tmp_dir: str, base: str) -> bool:
    return tmp_base_has_video(tmp_dir, base) or tmp_base_has_transcript(tmp_dir, base)


def delete_marker_for_base(base: str, tmp_dir: str | None = None) -> None:
    p = editor_review_path_for_base(base, tmp_dir)
    try:
        os.remove(p)
    except OSError:
        pass


def reset_gates_after_video_trim(base: str, tmp_dir: str | None = None) -> None:
    """After saving a new trim, timestamps/transcript review are invalid — clear marker."""
    delete_marker_for_base(base, tmp_dir)


def write_trim_confirm_for_base(base: str, tmp_dir: str | None = None) -> str:
    tmp_dir = _resolve_tmp_dir(tmp_dir)
    if not tmp_base_has_video(tmp_dir, base):
        raise ValueError(f"No .mp4 in {tmp_dir!r} for base {base!r}")
    out = editor_review_path_for_base(base, tmp_dir)
    os.makedirs(os.path.abspath(os.path.dirname(out) or "."), exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {"version": 2, "trim_confirmed_at": now}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return out


def write_editor_review_for_base(base: str, tmp_dir: str | None = None) -> str:
    tmp_dir = _resolve_tmp_dir(tmp_dir)
    if not tmp_base_has_transcript(tmp_dir, base):
        raise ValueError(
            f"No transcript at {tmp_dir}/{base}_transcript.json — run step 01 after trim first."
        )
    if not is_trim_complete_for_base(base, tmp_dir):
        raise ValueError(
            "Trim is not confirmed yet — use “Mark trim done” in 00b_editor before transcript review."
        )
    out = editor_review_path_for_base(base, tmp_dir)
    os.makedirs(os.path.abspath(os.path.dirname(out) or "."), exist_ok=True)
    prev = _load_marker(base, tmp_dir) or {}
    trim_at = prev.get("trim_confirmed_at")
    if not trim_at and _is_legacy_both_done(prev):
        trim_at = prev.get("confirmed_at")
    if not trim_at:
        trim_at = datetime.now(timezone.utc).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "version": 2,
        "trim_confirmed_at": trim_at,
        "review_confirmed_at": now,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return out


def write_editor_review_complete(video_path: str, tmp_dir: str | None = None) -> str:
    return write_editor_review_for_base(stem_for_editor_gate(video_basename(video_path)), tmp_dir)


def write_trim_confirm_complete(video_path: str, tmp_dir: str | None = None) -> str:
    return write_trim_confirm_for_base(stem_for_editor_gate(video_basename(video_path)), tmp_dir)


def resolve_base_from_cli_arg(arg: str) -> str:
    """Accept ``.tmp/IMG.mp4``, ``IMG_transcript.json``, or bare ``IMG``."""
    bn = os.path.basename(arg.strip().rstrip("/"))
    if bn.endswith("_transcript.json"):
        return stem_for_editor_gate(bn[: -len("_transcript.json")])
    low = bn.lower()
    if low.endswith(".mp4"):
        return stem_for_editor_gate(os.path.splitext(bn)[0])
    return stem_for_editor_gate(bn)

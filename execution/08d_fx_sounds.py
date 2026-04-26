"""
Step 8d: FX Sounds — overlay short sound effects at each "impactful moment"
that step 08b's AI analysis identified in the transcript.

The AI moments are read from `.tmp/{editor_gate_stem}_zoom_moments.json`
(produced by 08b), with fallbacks for older sidecar names.
For every moment, we pick a sound from `fxs/` and mix it into the audio
track at the moment's start time. Video is copied through untouched.

Optional keyword layer: if `fxs/fx_keywords.json` exists (or FX_KEYWORD_MAP),
we match the moment's *context* (LLM "reason" plus transcript text around the
same time window) against regex `rules` and/or a `keywords` map, and use
the first matching file instead of a random effect. When nothing matches, the
existing random (non–back-to-back) pool is used.

If no moments exist (empty sidecar, no transcript, or OpenRouter disabled),
the step is a no-op: the previous intermediate is re-used as-is by downstream
steps via their usual fallback candidate list.

Tunables (env vars):
  - FX_VOLUME            (default 0.6)  — gain applied to each FX before mixing
  - FX_MAX_DURATION      (default 2.5)  — seconds; longer FX are trimmed to this
  - FX_DISABLE=1                        — skip the step entirely
  - FX_KEYWORD_DISABLE=1                — ignore fx_keywords.json (random FX only)
  - FX_KEYWORD_MAP=<path>               — override path to the keyword JSON
  - VIDEOS_FX_DIR / FX_DIR — folder to pick FX files from (default: fxs/)
"""
import sys
import os
import re
import json
import random
import subprocess

import env_paths
from editor_gate import stem_for_editor_gate
from video_encoding import first_existing_nonempty_video
FX_VOLUME = float(os.environ.get("FX_VOLUME", "0.6"))
FX_MAX_DURATION = float(os.environ.get("FX_MAX_DURATION", "2.5"))
FX_MOMENT_TEXT_PAD = float(os.environ.get("FX_MOMENT_TEXT_PAD", "0.35"))
FX_EXTENSIONS = {".wav", ".mp3", ".aac", ".m4a", ".ogg", ".flac"}
# Everything is resampled to this shared sample rate before mixing so the
# filter graph can sum samples deterministically regardless of how each FX
# file happens to be encoded. Channel layout is detected from the input
# video so we preserve its voice track at full amplitude (upmixing mono to
# stereo would attenuate it by ~3 dB which subtly "changes" the voice).
MIX_SAMPLE_RATE = 48000


def _probe_channel_layout(path: str) -> str:
    """Return the input video's audio channel layout ("mono"/"stereo"/...).

    Falls back to "stereo" if probing fails or the file has no audio stream.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=channel_layout,channels",
                "-of", "default=noprint_wrappers=1:nokey=0",
                path,
            ],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "stereo"
    layout = ""
    channels = 0
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "channel_layout":
            layout = value
        elif key == "channels":
            try:
                channels = int(value)
            except ValueError:
                channels = 0
    if layout and layout != "unknown":
        return layout
    if channels == 1:
        return "mono"
    if channels >= 2:
        return "stereo"
    return "stereo"


def _list_fx_files(fx_dir: str | None = None) -> list[str]:
    if fx_dir is None:
        fx_dir = env_paths.fx_dir()
    if not os.path.isdir(fx_dir):
        return []
    return [
        os.path.join(fx_dir, f)
        for f in sorted(os.listdir(fx_dir))
        if os.path.splitext(f)[1].lower() in FX_EXTENSIONS
    ]


def _transcript_path_for_gate(gate: str, tmp_dir: str) -> str:
    return os.path.join(tmp_dir, f"{gate}_transcript.json")


def _load_transcript_segments(gate: str, tmp_dir: str) -> list:
    path = _transcript_path_for_gate(gate, tmp_dir)
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[08d] Could not read transcript {path}: {exc}")
        return []
    segs = data.get("segments") or []
    if not isinstance(segs, list):
        segs = []
    if (not segs) and isinstance(data.get("words"), list):
        for w in data["words"]:
            if not isinstance(w, dict):
                continue
            txt = (w.get("word") or w.get("text") or "").strip()
            if not txt:
                continue
            try:
                s = float(w.get("start", 0.0))
                e = float(w.get("end", s))
            except (TypeError, ValueError):
                continue
            segs.append({"start": s, "end": e, "text": txt})
    return segs


def _moment_overlap_text(
    start: float,
    end: float | None,
    segments: list,
    pad: float = FX_MOMENT_TEXT_PAD,
) -> str:
    """Concatenate segment texts that overlap the moment window (for keyword FX)."""
    if not segments:
        return ""
    t0 = max(0.0, start - pad)
    t1 = end if end is not None and end > start else start + 2.0
    t1 = t1 + pad
    parts: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        try:
            s = float(seg.get("start", 0.0))
            e = float(seg.get("end", s))
        except (TypeError, ValueError):
            continue
        if e < t0 or s > t1:
            continue
        text = (seg.get("text") or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def _fx_keyword_map_path(fx_dir: str) -> str:
    override = (os.environ.get("FX_KEYWORD_MAP") or "").strip()
    if override:
        p = override if os.path.isabs(override) else os.path.normpath(
            os.path.join(env_paths.REPO_ROOT, override)
        )
        return p
    return os.path.join(fx_dir, "fx_keywords.json")


def _load_fx_keyword_rules(fx_dir: str) -> list[tuple[re.Pattern, str]]:
    """Load ordered (compiled regex, audio basename) pairs from JSON."""
    if os.environ.get("FX_KEYWORD_DISABLE") == "1":
        return []
    path = _fx_keyword_map_path(fx_dir)
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[08d] Could not read keyword map {path}: {exc}")
        return []
    if not isinstance(data, dict):
        return []
    out: list[tuple[re.Pattern, str]] = []
    for r in data.get("rules") or []:
        if not isinstance(r, dict):
            continue
        pat = (r.get("pattern") or r.get("match") or "").strip()
        fn = (r.get("file") or "").strip()
        if not pat or not fn:
            continue
        try:
            out.append((re.compile(pat, re.IGNORECASE | re.DOTALL), fn))
        except re.error as exc:
            print(f"[08d] fx_keywords rule skipped (bad regex) {pat!r}: {exc}")
    kwd = data.get("keywords")
    if isinstance(kwd, dict):
        for key, val in sorted(kwd.items(), key=lambda kv: len(kv[0]), reverse=True):
            if not isinstance(key, str) or not isinstance(val, str):
                continue
            k = key.strip()
            v = val.strip()
            if not k or not v:
                continue
            out.append((re.compile(re.escape(k), re.IGNORECASE), v))
    return out


def _index_fx_by_basename(fx_files: list[str]) -> dict[str, str]:
    return {os.path.basename(p): p for p in fx_files}


def _resolve_fx_audio_path(
    fx_dir: str, file_spec: str, by_base: dict[str, str],
) -> str | None:
    spec = (file_spec or "").strip()
    if not spec:
        return None
    if os.path.isabs(spec) and os.path.isfile(spec):
        return spec
    b = os.path.basename(spec)
    if b in by_base:
        return by_base[b]
    joined = os.path.join(fx_dir, b)
    if os.path.isfile(joined):
        return os.path.normpath(joined)
    return None


def _match_keyword_fx(
    context: str,
    rules: list[tuple[re.Pattern, str]],
    fx_dir: str,
    by_base: dict[str, str],
) -> str | None:
    if not context.strip() or not rules:
        return None
    for pat, file_spec in rules:
        if not pat.search(context):
            continue
        path = _resolve_fx_audio_path(fx_dir, file_spec, by_base)
        if path:
            return path
        print(
            f"[08d] Keyword matched but audio missing: {file_spec!r} "
            f"(in {fx_dir!r})"
        )
    return None


def _resolve_zoom_moments_path(raw_stem: str, tmp_dir: str) -> str | None:
    """Locate the 08b sidecar: same editor-gate base as the current .mp4 stem.

    08b used to name the file after whatever intermediate it received (e.g.
    ``…_voice_studio_fixed_audio_zoom_moments.json``) while 08d often runs on
    ``…_broll.mp4``. We try the normalized stem, the raw stem, then any
    ``*_zoom_moments.json`` whose stem matches ``stem_for_editor_gate``."""
    gate = stem_for_editor_gate(raw_stem)
    primary = os.path.join(tmp_dir, f"{gate}_zoom_moments.json")
    alt = os.path.join(tmp_dir, f"{raw_stem}_zoom_moments.json")
    for p in (primary, alt):
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
    if os.path.isdir(tmp_dir):
        suffix = "_zoom_moments.json"
        matches: list[str] = []
        for name in os.listdir(tmp_dir):
            if not name.endswith(suffix):
                continue
            stem = name[: -len(suffix)]
            if stem_for_editor_gate(stem) == gate:
                p = os.path.join(tmp_dir, name)
                if os.path.isfile(p) and os.path.getsize(p) > 0:
                    matches.append(p)
        if matches:
            matches.sort()
            return matches[0]
    return None


def _load_moments(raw_stem: str, tmp_dir: str) -> list:
    """Return the AI-selected zoom moments for this pipeline base or []."""
    path = _resolve_zoom_moments_path(raw_stem, tmp_dir)
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[08d] Could not read {path}: {exc}")
        return []
    moments = data.get("moments") or []
    if not isinstance(moments, list):
        return []
    cleaned = []
    for m in moments:
        try:
            start = float(m.get("start"))
        except (TypeError, ValueError):
            continue
        if start < 0:
            continue
        end_raw = m.get("end")
        end: float | None
        if end_raw is not None and end_raw != "":
            try:
                end = float(end_raw)
            except (TypeError, ValueError):
                end = None
        else:
            end = None
        cleaned.append({
            "start": start,
            "end": end,
            "reason": str(m.get("reason", "")),
        })
    cleaned.sort(key=lambda m: m["start"])
    return cleaned


def _iter_fx_picks(fx_files: list[str]):
    """Yield paths from `fx_files` forever, shuffling to avoid back-to-back repeats."""
    if not fx_files:
        return
    last: str | None = None
    pool: list[str] = []
    while True:
        if not pool:
            pool = list(fx_files)
            random.shuffle(pool)
            if last is not None and len(pool) > 1 and pool[0] == last:
                pool[0], pool[1] = pool[1], pool[0]
        nxt = pool.pop(0)
        last = nxt
        yield nxt


def _passthrough(input_video: str, output_path: str) -> str:
    """Stream-copy `input_video` to `output_path` so downstream steps always
    find a `.tmp/{base}_fx.mp4` regardless of whether FX were actually mixed."""
    root, ext = os.path.splitext(output_path)
    output_partial = f"{root}.part{ext}"
    cmd = [
        "ffmpeg", "-y",
        "-i", input_video,
        "-c", "copy",
        "-movflags", "+faststart",
        output_partial,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        if os.path.exists(output_partial):
            try:
                os.remove(output_partial)
            except OSError:
                pass
        raise RuntimeError(
            f"[08d] Passthrough copy failed: {result.stderr[-600:]}"
        )
    os.replace(output_partial, output_path)
    print(f"[08d] Output (passthrough): {output_path}")
    return output_path


def add_fx_sounds(video_path: str, tmp_dir: str | None = None) -> str:
    if tmp_dir is None:
        tmp_dir = env_paths.tmp_dir()
    fx_root = env_paths.fx_dir()
    base = os.path.splitext(os.path.basename(video_path))[0]

    # Prefer the latest available intermediate so captions see a single,
    # self-consistent video with FX already baked into the audio.
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
        print("[08d] No readable input video found; skipping.")
        return ""

    output_path = os.path.join(tmp_dir, f"{base}_fx.mp4")

    if os.environ.get("FX_DISABLE") == "1":
        print("[08d] FX_DISABLE=1 set; skipping FX sound overlay.")
        return _passthrough(input_video, output_path)

    moments = _load_moments(base, tmp_dir)
    if not moments:
        gate = stem_for_editor_gate(base)
        print(
            "[08d] No AI zoom moments found "
            f"(no .tmp/*_zoom_moments.json for editor-gate base {gate!r}; "
            "see 08b); passing audio through."
        )
        return _passthrough(input_video, output_path)

    fx_files = _list_fx_files()
    if not fx_files:
        print(f"[08d] No FX files found in '{fx_root}/'; passing audio through.")
        return _passthrough(input_video, output_path)

    gate = stem_for_editor_gate(base)
    transcript_segments = _load_transcript_segments(gate, tmp_dir)
    keyword_rules = _load_fx_keyword_rules(fx_root)
    by_base = _index_fx_by_basename(fx_files)
    if keyword_rules:
        print(
            f"[08d] Keyword FX map: {len(keyword_rules)} rule(s) "
            f"({_fx_keyword_map_path(fx_root)!r})"
        )

    fx_fallback = _iter_fx_picks(fx_files)
    assignments: list[tuple[dict, str, str]] = []
    for moment in moments:
        end = moment.get("end")
        overlap = _moment_overlap_text(
            float(moment["start"]),
            float(end) if isinstance(end, (int, float)) else None,
            transcript_segments,
        )
        reason = (moment.get("reason") or "").strip()
        context = f"{reason} {overlap}".strip()
        chosen = _match_keyword_fx(context, keyword_rules, fx_root, by_base)
        if chosen:
            tag = "keyword"
        else:
            chosen = next(fx_fallback)
            tag = "random"
        assignments.append((moment, chosen, tag))

    print(f"[08d] Overlaying {len(assignments)} FX sound(s) from '{fx_root}/'")
    for moment, fx, tag in assignments:
        reason = f" — {moment['reason']}" if moment.get("reason") else ""
        print(
            f"  {moment['start']:6.2f}s  ←  {os.path.basename(fx)} "
            f"[{tag}]{reason}"
        )

    # Build an FFmpeg command that overlays each FX onto the original voice
    # track at its moment's timestamp. To guarantee the voice is NOT altered
    # outside of FX moments (and to avoid `amix`'s implicit averaging quirks
    # when many inputs overlap in time), we normalize every stream to a
    # shared format and then sum them explicitly via `amerge + pan` — the
    # same unity-gain pattern used by step 10 for background music.
    inputs: list[str] = ["-i", input_video]
    for _, fx_path, _ in assignments:
        inputs.extend(["-i", fx_path])

    # Match the input video's channel layout so the voice is preserved at
    # full amplitude (a mono→stereo upmix would bake in a ~3 dB attenuation).
    channel_layout = _probe_channel_layout(input_video)
    fmt = (
        f"aformat=sample_fmts=fltp:"
        f"sample_rates={MIX_SAMPLE_RATE}:"
        f"channel_layouts={channel_layout}"
    )

    filter_parts: list[str] = []
    # Voice track: just resample/reformat so it matches the FX layout for the
    # final amerge. No volume change, no filtering — the voice is preserved
    # sample-accurately and simply re-encoded.
    filter_parts.append(f"[0:a]{fmt}[voice]")

    fx_labels: list[str] = []
    for idx, (moment, _, _tag) in enumerate(assignments, start=1):
        delay_ms = max(0, int(round(float(moment["start"]) * 1000)))
        label = f"fx{idx}"
        filter_parts.append(
            f"[{idx}:a]"
            f"atrim=0:{FX_MAX_DURATION:.3f},"
            f"asetpts=PTS-STARTPTS,"
            f"volume={FX_VOLUME:.3f},"
            f"{fmt},"
            f"adelay={delay_ms}:all=1"
            f"[{label}]"
        )
        fx_labels.append(f"[{label}]")

    # Step 1: collapse every FX into a single track. They're scheduled at
    # distinct moments and each is ≤ FX_MAX_DURATION seconds long, so `amix`
    # with normalize=0 just passes each one through at its own gain — no
    # surprise attenuation from overlap-count scaling.
    n_fx = len(fx_labels)
    filter_parts.append(
        f"{''.join(fx_labels)}"
        f"amix=inputs={n_fx}:duration=longest:dropout_transition=0:normalize=0"
        f"[fx_bed]"
    )

    # Step 2: sum the voice and the FX bed at unity gain via amerge+pan.
    # `amerge` concatenates the two streams' channels; pan then maps them
    # back to the input's layout by summing voice channel i with FX bed
    # channel i (c0+cN for the first N channels, where N = channel count).
    # This is an exact arithmetic addition, so the voice signal is preserved
    # bit-for-bit wherever FX is silent.
    if channel_layout == "mono":
        pan_expr = "mono|c0=c0+c1"
    else:  # stereo or anything wider — handle first two channels as L/R
        pan_expr = f"{channel_layout}|c0=c0+c2|c1=c1+c3"
    filter_parts.append(
        f"[voice][fx_bed]amerge=inputs=2[merged];"
        f"[merged]pan={pan_expr}[outa]"
    )

    filter_complex = ";".join(filter_parts)

    root, ext = os.path.splitext(output_path)
    output_partial = f"{root}.part{ext}"

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", "[outa]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", str(MIX_SAMPLE_RATE),
        "-movflags", "+faststart",
        output_partial,
    ]

    print("[08d] Mixing FX into audio track via FFmpeg...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        if os.path.exists(output_partial):
            try:
                os.remove(output_partial)
            except OSError:
                pass
        err_tail = result.stderr[-1200:]
        raise RuntimeError(f"[08d] FFmpeg FX mix failed: {err_tail}")

    os.replace(output_partial, output_path)
    print(f"[08d] Output: {output_path}")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 08d_fx_sounds.py <video_path>")
        sys.exit(1)
    add_fx_sounds(sys.argv[1])

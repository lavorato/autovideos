"""
Step 8d: FX Sounds — overlay short sound effects at each "impactful moment"
that step 08b's AI analysis identified in the transcript.

The AI moments are read from `.tmp/{base}_zoom_moments.json` (produced by 08b).
For every moment, we pick a random .wav from `fxs/` and mix it into the
audio track at the moment's start time. Video is copied through untouched.

If no moments exist (empty sidecar, no transcript, or OpenRouter disabled),
the step is a no-op: the previous intermediate is re-used as-is by downstream
steps via their usual fallback candidate list.

Tunables (env vars):
  - FX_VOLUME            (default 0.6)  — gain applied to each FX before mixing
  - FX_MAX_DURATION      (default 2.5)  — seconds; longer FX are trimmed to this
  - FX_DISABLE=1                        — skip the step entirely
  - FX_DIR               (default fxs/) — folder to pick FX files from
"""
import sys
import os
import json
import random
import subprocess

from video_encoding import first_existing_nonempty_video


FX_DIR = os.environ.get("FX_DIR", "fxs")
FX_VOLUME = float(os.environ.get("FX_VOLUME", "0.6"))
FX_MAX_DURATION = float(os.environ.get("FX_MAX_DURATION", "2.5"))
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


def _list_fx_files(fx_dir: str = FX_DIR) -> list[str]:
    if not os.path.isdir(fx_dir):
        return []
    return [
        os.path.join(fx_dir, f)
        for f in sorted(os.listdir(fx_dir))
        if os.path.splitext(f)[1].lower() in FX_EXTENSIONS
    ]


def _load_moments(base: str, tmp_dir: str) -> list:
    """Return the AI-selected zoom moments for `base` or [] if unavailable."""
    path = os.path.join(tmp_dir, f"{base}_zoom_moments.json")
    if not os.path.exists(path):
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
        cleaned.append({"start": start, "reason": str(m.get("reason", ""))})
    cleaned.sort(key=lambda m: m["start"])
    return cleaned


def _pick_fx_cycle(fx_files: list[str], count: int) -> list[str]:
    """Pick `count` FX files, shuffling to avoid immediate repeats.

    If `count` exceeds the pool, the shuffled pool repeats (reshuffled each
    cycle) so we still never play the same FX twice back-to-back.
    """
    if not fx_files:
        return []
    picks: list[str] = []
    pool: list[str] = []
    while len(picks) < count:
        if not pool:
            pool = list(fx_files)
            random.shuffle(pool)
            if picks and pool[0] == picks[-1] and len(pool) > 1:
                pool[0], pool[1] = pool[1], pool[0]
        picks.append(pool.pop(0))
    return picks


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


def add_fx_sounds(video_path: str, tmp_dir: str = ".tmp") -> str:
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
        print(
            "[08d] No AI zoom moments found "
            f"(.tmp/{base}_zoom_moments.json missing or empty); passing audio through."
        )
        return _passthrough(input_video, output_path)

    fx_files = _list_fx_files()
    if not fx_files:
        print(f"[08d] No FX files found in '{FX_DIR}/'; passing audio through.")
        return _passthrough(input_video, output_path)

    picks = _pick_fx_cycle(fx_files, len(moments))
    assignments = list(zip(moments, picks))

    print(f"[08d] Overlaying {len(assignments)} FX sound(s) from '{FX_DIR}/'")
    for moment, fx in assignments:
        reason = f" — {moment['reason']}" if moment.get("reason") else ""
        print(f"  {moment['start']:6.2f}s  ←  {os.path.basename(fx)}{reason}")

    # Build an FFmpeg command that overlays each FX onto the original voice
    # track at its moment's timestamp. To guarantee the voice is NOT altered
    # outside of FX moments (and to avoid `amix`'s implicit averaging quirks
    # when many inputs overlap in time), we normalize every stream to a
    # shared format and then sum them explicitly via `amerge + pan` — the
    # same unity-gain pattern used by step 10 for background music.
    inputs: list[str] = ["-i", input_video]
    for _, fx_path in assignments:
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
    for idx, (moment, _) in enumerate(assignments, start=1):
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

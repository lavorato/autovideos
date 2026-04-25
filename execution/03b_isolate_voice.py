"""
Step 3b: Isolate voice from background noise using Demucs source separation.

Demucs is Meta's hybrid-transformer source-separation model. Running it with
`--two-stems=vocals` cleanly separates the speaker's voice from everything else
(HVAC hum, traffic, keyboard, music, room tone, etc.), producing a far cleaner
track than the FFT-based denoiser used later in Step 4 (Studio Sound).

Pipeline placement
------------------
Runs AFTER filler/retake removal (Step 3) so Demucs only processes the trimmed,
post-cut audio — not the raw take. Output feeds Step 4, which still applies EQ,
compression and loudness normalization on top of the now-clean voice.

Input : `.tmp/{base}_no_fillers.mp4`  (falls back to the raw video path)
Output: `.tmp/{base}_voice.mp4`       (video stream copied, audio = vocals stem)

Environment overrides
---------------------
- VOICE_ISOLATION_MODEL   default `htdemucs`  (`mdx_extra` also works well)
- VOICE_ISOLATION_DEVICE  default auto (mps → cuda → cpu)
- VOICE_ISOLATION_SHIFTS  default `1`  (higher = better quality, slower; 2-5 is typical)
- VOICE_ISOLATION_DISABLE set to `1`/`true` to bypass and reuse the input as-is

Requires: `demucs>=4`, `torch`, `ffmpeg` on PATH.
"""
import os
import sys
import shutil
import subprocess
import tempfile

import env_paths


def _pick_device() -> str:
    override = os.getenv("VOICE_ISOLATION_DEVICE", "").strip().lower()
    if override:
        return override
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _extract_audio_wav(video_path: str, wav_path: str) -> None:
    """Pull the video's audio track into a 44.1 kHz stereo float WAV for Demucs."""
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn",
        "-ac", "2",
        "-ar", "44100",
        "-c:a", "pcm_f32le",
        wav_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _mux_video_with_audio(video_path: str, vocals_wav: str, output_path: str) -> None:
    """Copy the video stream from `video_path` and replace audio with `vocals_wav`."""
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", vocals_wav,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "alac",
        "-shortest",
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _video_has_audio(video_path: str) -> bool:
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_type",
                "-of", "default=nokey=1:noprint_wrappers=1",
                video_path,
            ],
            capture_output=True, text=True, check=False,
        )
        return "audio" in r.stdout
    except OSError:
        return False


def _run_demucs(input_wav: str, out_dir: str, model: str, device: str, shifts: int) -> str:
    """Run Demucs two-stem (vocals) separation. Returns the vocals WAV path."""
    from demucs.separate import main as demucs_cli

    args = [
        "--two-stems=vocals",
        "-n", model,
        "-d", device,
        "--shifts", str(shifts),
        "-o", out_dir,
        input_wav,
    ]
    print(f"[03b] Running Demucs: model={model} device={device} shifts={shifts}")
    demucs_cli(args)

    base_no_ext = os.path.splitext(os.path.basename(input_wav))[0]
    vocals_path = os.path.join(out_dir, model, base_no_ext, "vocals.wav")
    if not os.path.isfile(vocals_path):
        raise FileNotFoundError(
            f"Demucs finished but vocals stem not found at {vocals_path}"
        )
    return vocals_path


def isolate_voice(video_path: str, tmp_dir: str | None = None) -> str:
    if tmp_dir is None:
        tmp_dir = env_paths.tmp_dir()
    os.makedirs(tmp_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(video_path))[0]

    input_video = video_path
    for candidate in (
        os.path.join(tmp_dir, f"{base}_no_retakes.mp4"),
        os.path.join(tmp_dir, f"{base}_no_fillers.mp4"),
    ):
        if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
            input_video = candidate
            break

    output_path = os.path.join(tmp_dir, f"{base}_voice.mp4")

    if os.getenv("VOICE_ISOLATION_DISABLE", "").strip().lower() in ("1", "true", "yes"):
        print("[03b] VOICE_ISOLATION_DISABLE set, copying input through")
        shutil.copyfile(input_video, output_path)
        return output_path

    if not _video_has_audio(input_video):
        print(f"[03b] Input has no audio stream, skipping: {input_video}")
        shutil.copyfile(input_video, output_path)
        return output_path

    model = os.getenv("VOICE_ISOLATION_MODEL", "mdx_extra")
    device = _pick_device()
    shifts = max(1, int(os.getenv("VOICE_ISOLATION_SHIFTS", "5")))

    print(f"[03b] Isolating voice in: {input_video}")

    work_dir = tempfile.mkdtemp(prefix="voice_iso_", dir=tmp_dir)
    try:
        audio_wav = os.path.join(work_dir, f"{base}.wav")
        _extract_audio_wav(input_video, audio_wav)

        separated_dir = os.path.join(work_dir, "separated")
        vocals_wav = _run_demucs(
            audio_wav,
            out_dir=separated_dir,
            model=model,
            device=device,
            shifts=shifts,
        )

        print(f"[03b] Muxing isolated voice back with video...")
        _mux_video_with_audio(input_video, vocals_wav, output_path)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    print(f"[03b] Output: {output_path}")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 03b_isolate_voice.py <video_path>")
        sys.exit(1)
    isolate_voice(sys.argv[1])

"""
Step 4: Apply Studio Sound - professional audio enhancement.
Noise reduction, compression, EQ for voice clarity, loudness normalization.
"""
import sys
import os
import subprocess


def apply_studio_sound(video_path: str, tmp_dir: str = ".tmp") -> str:
    base = os.path.splitext(os.path.basename(video_path))[0]
    # Prefer the voice-isolated track from Step 3b if available, so EQ/compression
    # operate on a clean vocals stem instead of noisy raw audio. Fall back through
    # the normal chain if the isolation step was skipped.
    for candidate in (
        os.path.join(tmp_dir, f"{base}_voice.mp4"),
        os.path.join(tmp_dir, f"{base}_no_retakes.mp4"),
        os.path.join(tmp_dir, f"{base}_no_fillers.mp4"),
    ):
        if os.path.exists(candidate):
            input_video = candidate
            break
    else:
        input_video = video_path
    output_path = os.path.join(tmp_dir, f"{base}_studio.mp4")

    print(f"[04] Applying Studio Sound to: {input_video}")

    # Audio filter chain:
    # 1. highpass: remove rumble below 80Hz
    # 2. lowpass: remove hiss above 14kHz
    # 3. afftdn: FFT-based noise reduction
    # 4. acompressor: dynamic compression for even volume
    # 5. equalizer: boost voice presence (2-5kHz)
    # 6. equalizer: slight warmth boost (200-400Hz)
    # 7. loudnorm: normalize to -16 LUFS (podcast/video standard)
    audio_filter = (
        "highpass=f=80,"
        "lowpass=f=14000,"
        "afftdn=nf=-25:nt=w:om=o,"
        "acompressor=threshold=-20dB:ratio=3:attack=5:release=50:makeup=2dB,"
        "equalizer=f=3000:t=q:w=1.5:g=3,"
        "equalizer=f=300:t=q:w=1.0:g=1.5,"
        "loudnorm=I=-16:TP=-1.5:LRA=11"
    )

    cmd = [
        "ffmpeg", "-y", "-i", input_video,
        "-af", audio_filter,
        "-c:v", "copy",
        "-c:a", "alac",
        output_path,
    ]
    print("[04] Processing audio...")
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"[04] Output: {output_path}")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 04_studio_sound.py <video_path>")
        sys.exit(1)
    apply_studio_sound(sys.argv[1])

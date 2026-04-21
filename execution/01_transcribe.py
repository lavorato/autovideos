"""
Step 1: Transcribe video audio using WhisperX.
Outputs a JSON transcript with word-level timestamps.
"""
import sys
import json
import os
import torch
import whisperx


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

    transcript_data = {
        "video": video_path,
        "language": language,
        "text": result.get("text", ""),
        "segments": aligned.get("segments", result.get("segments", [])),
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

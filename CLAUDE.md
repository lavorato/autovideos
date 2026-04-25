# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 3-Layer Architecture

This project uses a 3-layer architecture that separates concerns to maximize reliability. LLMs are probabilistic; business logic is deterministic. This system bridges that gap.

### Layer 1: Directives (What to do)
- SOPs written in Markdown, located in `directives/`
- Define objectives, inputs, tools/scripts to use, outputs, and edge cases

### Layer 2: Orchestration (Decision-making)
- That's you. Your role: intelligent routing.
- Read directives, call execution tools in the correct order, handle errors, ask for clarification, update directives with learnings
- You don't do work manually — you read the directive, formulate inputs/outputs, then run the appropriate execution script

### Layer 3: Execution (Do the work)
- Deterministic Python scripts in `execution/`
- Environment variables and API tokens live in `.env`
- Handles API calls, data processing, file operations, database interactions

## Operating Principles

1. **Check tools first** — Before writing a new script, check `execution/` following the directive. Only create new scripts if they truly don't exist.
2. **Self-anneal on failure** — Read error/stack trace, fix the script and re-test (unless it consumes paid credits — ask the user first), then update the directive with learnings.
3. **Update directives as you learn** — Directives are living documents. Update them with API limitations, better approaches, common errors, timing expectations. Do NOT create new directives or overwrite existing ones without user permission.

## Directory Structure

```
input/          # Raw video files to process
output/         # Final edited videos
directives/     # Markdown SOPs
execution/      # Deterministic Python scripts (9-step video pipeline + runner)
.tmp/           # Intermediate files (always regenerable, can be deleted anytime)
.env            # Environment variables and API keys
credentials.json
token.json      # Google OAuth credentials
```

## Video Editing Pipeline

Run the full pipeline on all videos in `input/`:
```bash
python execution/run_pipeline.py
```

Or process a specific video:
```bash
python execution/run_pipeline.py input/my_video.mp4
```

### Always-on pre-processing (recommended at session start)

The watcher runs **step 00 only** when a new video lands in `input/`. Then: **trim** in `00b_editor.py` → **Mark trim done** — Whisper step 01 runs **automatically** in the background (disable with `EDITOR_AUTO_TRANSCRIBE=0`, then run `run_pipeline.py .tmp/BASE.mp4 --only 01`) → fix transcript in 00b → **Save and continue** (or **Mark transcript review done**) to run 02+:

```bash
python execution/watch_input.py                   # or: python execution/run_pipeline.py --watch
# After “Save and continue” (debounced when EDITOR_AUTO_PIPELINE=1) or “Mark transcript review done”, steps 02+ run from the editor; plain “Save” only persists the JSON
python execution/run_pipeline.py input/BASE.mov --skip 00,01   # manual 02+ if auto-pipeline is off
```

See `directives/video_editing_pipeline.md` for the full always-on workflow.

The pipeline runs 9 sequential steps (see `directives/video_editing_pipeline.md` for full SOP):
1. Transcribe (Whisper) → 2. Remove retakes → 3. Remove fillers → 4. Studio Sound (EQ/compression/normalize) → 5. Fix mute gaps → 6. Scene detection → 7. Color correction → 8. Zoom/PAN face-tracking effects (every 6s) → 9. Captions (Clean Paragraph + fade)

**Dependencies**: FFmpeg 8+, Python 3.11+, whisper, moviepy, scenedetect, opencv-python, mediapipe

## Key Concepts

- **Deliverables** live in the cloud (Google Sheets, Google Slides, etc.) — local files are only for processing
- Everything in `.tmp/` is ephemeral and regenerable
- Directives are your instruction set — preserve them

## Design System

The project uses a **Glassmorphism** design system (frosted glass effect with translucent layers, subtle blur, luminous borders). Key tokens:

- **Fonts**: Plus Jakarta Sans (primary/display), JetBrains Mono (mono)
- **Colors**: primary=#1856FF, secondary=#3A344E, success=#07CA6B, warning=#E89558, danger=#EA2143, surface=#FFFFFF, text=#141414
- **Accessibility**: WCAG 2.2 AA, keyboard-first, visible focus states
- Prefer semantic tokens over raw values; prioritize accessibility over aesthetics

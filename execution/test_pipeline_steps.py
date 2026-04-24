"""
Developer harness for exercising pipeline steps in isolation.

  python execution/test_pipeline_steps.py list
  python execution/test_pipeline_steps.py smoke
  python execution/test_pipeline_steps.py run 08b input/my_video.mp4 [--verify] [--clean] [--skip-editor-gate]

The main runner also supports:
  python execution/run_pipeline.py input/video.mp4 --step 08b --verify
  python execution/run_pipeline.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys

# Same path setup as run_pipeline.py
sys.path.insert(0, os.path.dirname(__file__))

# Load root .env before any step modules are imported.
try:
    from dotenv import load_dotenv
    _ENV_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".env"))
    load_dotenv(_ENV_PATH, override=False)
except ImportError:
    pass

import run_pipeline as rp
from importlib import import_module


def cmd_list() -> int:
    rp.print_steps_table(rp.get_steps())
    return 0


def cmd_smoke() -> int:
    """Import every registered step module and resolve its entry function."""
    print("Smoke test: importing all step modules...\n")
    failed = False
    for step in rp.ALL_STEPS:
        sid, module, func = step["id"], step["module"], step["func"]
        try:
            mod = import_module(module)
            fn = getattr(mod, func)
            if not callable(fn):
                print(f"  FAIL [{sid}] {module}.{func} is not callable")
                failed = True
            else:
                print(f"  OK   [{sid}] {module}.{func}")
        except Exception as e:
            print(f"  FAIL [{sid}] {module}: {e}")
            failed = True
    print()
    if failed:
        print("Smoke test failed.")
        return 1
    print("Smoke test passed.")
    return 0


def cmd_run(
    step_query: str,
    video: str,
    verify: bool,
    clean: bool,
    skip_editor_gate: bool,
) -> int:
    steps_meta = rp.get_steps()
    step = rp.match_step(step_query, steps_meta)
    if not step:
        print(f"Error: step {step_query!r} not found")
        rp.print_steps_table(steps_meta)
        return 1
    if not os.path.isfile(video):
        print(f"Error: video not found: {video}")
        return 1
    active = [step]
    if clean:
        rp.clean_tmp(video)
    ok = rp.process_video(
        video,
        active,
        do_clean=False,
        verify_outputs=verify,
        fail_fast=True,
        skip_editor_gate=skip_editor_gate,
    )
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test individual video pipeline steps (imports, run, verify).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Show pipeline steps and enabled/disabled state")

    p_smoke = sub.add_parser("smoke", help="Import every step module (no FFmpeg / no GPU)")

    p_run = sub.add_parser("run", help="Run a single step on one video file")
    p_run.add_argument("step", help="Step id, module name, or label fragment (same as --step)")
    p_run.add_argument("video", help="Path to input video")
    p_run.add_argument("--verify", action="store_true", help="Check expected outputs after the step")
    p_run.add_argument("--clean", action="store_true", help="Remove .tmp intermediates for this basename first")
    p_run.add_argument(
        "--skip-editor-gate",
        action="store_true",
        help="Run without .tmp/{base}_editor_review.json (same as run_pipeline.py).",
    )

    args = parser.parse_args()
    if args.command == "list":
        return cmd_list()
    if args.command == "smoke":
        return cmd_smoke()
    if args.command == "run":
        return cmd_run(
            args.step,
            args.video,
            args.verify,
            args.clean,
            args.skip_editor_gate,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

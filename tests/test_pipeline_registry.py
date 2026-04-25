"""
Fast registry checks: every step resolves to an importable module and callable.
Run from repo root:

  python -m unittest tests.test_pipeline_registry -v
"""
import os
import sys
import unittest
from importlib import import_module

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EXEC = os.path.join(ROOT, "execution")
if EXEC not in sys.path:
    sys.path.insert(0, EXEC)

import run_pipeline as rp  # noqa: E402


class TestPipelineRegistry(unittest.TestCase):
    def test_all_steps_have_output_pattern_or_documented(self):
        ids = {s["id"] for s in rp.ALL_STEPS}
        missing = ids - set(rp.step_output_patterns().keys())
        self.assertFalse(
            missing,
            f"step_output_patterns() missing keys: {sorted(missing)}",
        )

    def test_each_step_module_exports_callable(self):
        for step in rp.ALL_STEPS:
            with self.subTest(step=step["id"]):
                mod = import_module(step["module"])
                fn = getattr(mod, step["func"])
                self.assertTrue(callable(fn), f"{step['module']}.{step['func']} must be callable")

    def test_match_step_accepts_id(self):
        steps = rp.get_steps()
        first = rp.ALL_STEPS[0]
        found = rp.match_step(first["id"], steps)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], first["id"])


class TestSkip00UsesTmpSource(unittest.TestCase):
    def test_skip_00_resolves_to_tmp_mp4(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            base = "clip_test_abc"
            tmp_mp4 = os.path.join(td, f"{base}.mp4")
            with open(tmp_mp4, "wb") as f:
                f.write(b"xyz")
            clip_input = os.path.join(td, "input", f"{base}.mp4")
            os.makedirs(os.path.dirname(clip_input), exist_ok=True)
            with open(clip_input, "wb") as f:
                f.write(b"full")

            out = rp._resolve_prepared_working_path(clip_input, set(), td)
            self.assertEqual(os.path.abspath(out), os.path.abspath(tmp_mp4))

    def test_does_not_resolve_when_00_in_run(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "x.mp4")
            with open(p, "wb") as f:
                f.write(b"a")
            out = rp._resolve_prepared_working_path(p, {"00"}, td)
            self.assertEqual(out, p)


if __name__ == "__main__":
    unittest.main()

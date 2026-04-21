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
        missing = ids - set(rp.STEP_OUTPUT_PATTERNS.keys())
        self.assertFalse(
            missing,
            f"STEP_OUTPUT_PATTERNS missing keys: {sorted(missing)}",
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


if __name__ == "__main__":
    unittest.main()

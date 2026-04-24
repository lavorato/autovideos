"""
Live integration check for the OpenRouter chat client.

This test is NOT part of the normal pipeline flow. It performs a real
network call to the OpenRouter API to verify credentials, model access,
and the minimal request/response contract in `openrouter_client`.

Requires a valid OPENROUTER_API_KEY in the environment (loaded from .env
when python-dotenv is installed). If the key is missing, the test is
skipped rather than failed.

Run from repo root:

  python -m unittest tests.test_openrouter_client -v
"""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EXEC = os.path.join(ROOT, "execution")
if EXEC not in sys.path:
    sys.path.insert(0, EXEC)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"), override=False)
except ImportError:
    pass

from openrouter_client import (  # noqa: E402
    chat_completion,
    has_openrouter_api_key,
    parse_models_from_env,
)


@unittest.skipUnless(
    has_openrouter_api_key(),
    "OPENROUTER_API_KEY not set; skipping live OpenRouter test.",
)
class TestOpenRouterLive(unittest.TestCase):
    def setUp(self) -> None:
        models = parse_models_from_env()
        self.model = models[0] if models else "openai/gpt-4o-mini"

    def test_chat_completion_returns_non_empty_string(self) -> None:
        content = chat_completion(
            model=self.model,
            user_prompt="Reply with exactly the single word: pong",
            system_prompt="You are a terse test probe. Reply with one word only.",
            temperature=0.0,
            max_tokens=32,
            timeout_seconds=30,
        )
        self.assertIsInstance(content, str)
        self.assertTrue(content.strip(), "OpenRouter returned an empty response.")

    def test_chat_completion_follows_simple_instruction(self) -> None:
        content = chat_completion(
            model=self.model,
            user_prompt="Respond with only the number 2 (no punctuation, no words).",
            system_prompt="You are a deterministic calculator. Answer with digits only.",
            temperature=0.0,
            max_tokens=32,
            timeout_seconds=30,
        )
        self.assertIn("2", content)


class TestOpenRouterEnvHelpers(unittest.TestCase):
    def test_parse_models_prefers_models_over_model(self) -> None:
        prior_models = os.environ.get("OPENROUTER_MODELS")
        prior_model = os.environ.get("OPENROUTER_MODEL")
        try:
            os.environ["OPENROUTER_MODELS"] = "a/one, b/two"
            os.environ["OPENROUTER_MODEL"] = "c/three"
            self.assertEqual(parse_models_from_env(), ["a/one", "b/two"])
        finally:
            if prior_models is None:
                os.environ.pop("OPENROUTER_MODELS", None)
            else:
                os.environ["OPENROUTER_MODELS"] = prior_models
            if prior_model is None:
                os.environ.pop("OPENROUTER_MODEL", None)
            else:
                os.environ["OPENROUTER_MODEL"] = prior_model

    def test_parse_models_falls_back_to_single_model(self) -> None:
        prior_models = os.environ.get("OPENROUTER_MODELS")
        prior_model = os.environ.get("OPENROUTER_MODEL")
        try:
            os.environ.pop("OPENROUTER_MODELS", None)
            os.environ["OPENROUTER_MODEL"] = "solo/model"
            self.assertEqual(parse_models_from_env(), ["solo/model"])
        finally:
            if prior_models is not None:
                os.environ["OPENROUTER_MODELS"] = prior_models
            if prior_model is None:
                os.environ.pop("OPENROUTER_MODEL", None)
            else:
                os.environ["OPENROUTER_MODEL"] = prior_model


if __name__ == "__main__":
    unittest.main()

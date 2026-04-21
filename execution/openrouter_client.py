"""
Small OpenRouter chat client for deterministic script usage.
"""
import json
import os
import urllib.error
import urllib.request


DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_APP_NAME = "lavora.to videos pipeline"
DEFAULT_HTTP_REFERER = "http://localhost"


def has_openrouter_api_key() -> bool:
    """Return True if OPENROUTER_API_KEY is available."""
    return bool(os.getenv("OPENROUTER_API_KEY", "").strip())


def parse_models_from_env() -> list[str]:
    """
    Parse candidate models from env.
    Priority:
      1) OPENROUTER_MODELS (comma-separated)
      2) OPENROUTER_MODEL (single)
    """
    raw_models = os.getenv("OPENROUTER_MODELS", "").strip()
    if raw_models:
        models = [m.strip() for m in raw_models.split(",") if m.strip()]
        if models:
            return models

    single = os.getenv("OPENROUTER_MODEL", "").strip()
    if single:
        return [single]

    return []


def chat_completion(
    model: str,
    user_prompt: str,
    system_prompt: str = "",
    temperature: float = 0.0,
    max_tokens: int = 800,
    timeout_seconds: int = 45,
) -> str:
    """Call OpenRouter chat completions and return assistant content."""
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set.")

    base_url = os.getenv("OPENROUTER_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    http_referer = os.getenv("OPENROUTER_HTTP_REFERER", DEFAULT_HTTP_REFERER)
    app_name = os.getenv("OPENROUTER_APP_NAME", DEFAULT_APP_NAME)

    payload = {
        "model": model,
        "messages": [],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if system_prompt:
        payload["messages"].append({"role": "system", "content": system_prompt})
    payload["messages"].append({"role": "user", "content": user_prompt})

    req = urllib.request.Request(
        url=f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": http_referer,
            "X-Title": app_name,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
            data = json.loads(body)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenRouter request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("OpenRouter returned invalid JSON.") from exc

    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError("OpenRouter response has no choices.")
    message = choices[0].get("message", {})
    content = message.get("content")
    if not content:
        raise RuntimeError("OpenRouter response message is empty.")
    return str(content).strip()

"""Isolation test for OpenAI provider with NVIDIA NIM."""

from __future__ import annotations

import os
import sys
import time

from dotenv import load_dotenv

# Ensure repo root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

from ai.openai_provider import OpenAIProvider  # noqa: E402


def main() -> None:
    base_url = os.getenv("OPENAI_BASE_URL", "https://integrate.api.nvidia.com/v1")
    api_key = os.getenv("OPENAI_API_KEY", "")
    model = os.getenv("OPENAI_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")

    print(f"Base URL: {base_url}")
    print(f"Model: {model}")
    print(f"API key set: {bool(api_key)} (len={len(api_key)})")
    print()

    provider = OpenAIProvider(base_url=base_url, api_key=api_key, model=model)

    print("Checking availability...")
    available = provider.is_available()
    print(f"Available: {available}")
    if not available:
        print(f"Reason: {provider.reason}")
        return

    print("\nSending message: 'say hello in one sentence'")
    started = time.perf_counter()
    try:
        response = provider.chat(
            messages=[{"role": "user", "content": "say hello in one sentence"}],
            model=model,
        )
        elapsed = time.perf_counter() - started
        print(f"\nResponse: {response.text}")
        print(f"Model: {response.model}")
        print(f"Time taken: {elapsed:.2f} seconds")
    except Exception as exc:
        elapsed = time.perf_counter() - started
        print(f"\nERROR after {elapsed:.2f}s: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
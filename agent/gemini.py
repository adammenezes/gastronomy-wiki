"""
Cooking Brain — Gemini Client
==============================
Thin wrapper around google-genai used by all sub-agents.
Supports a round-robin key pool for rate-limit distribution across multiple API keys.
"""

import os
import logging
import threading

from google import genai
from google.genai import types as genai_types

log = logging.getLogger("cooking-brain.gemini")


class GeminiPool:
    """Thread-safe round-robin pool of genai.Client instances."""

    def __init__(self, clients: list):
        if not clients:
            raise ValueError("GeminiPool requires at least one client.")
        self._clients = clients
        self._idx     = 0
        self._lock    = threading.Lock()

    def next(self) -> genai.Client:
        with self._lock:
            client = self._clients[self._idx]
            self._idx = (self._idx + 1) % len(self._clients)
        return client

    def __len__(self):
        return len(self._clients)


def init_gemini(cfg: dict) -> GeminiPool:
    gemini_cfg = cfg["gemini"]

    primary_key = os.environ.get(gemini_cfg["api_key_env"])
    if not primary_key:
        raise RuntimeError(
            f"Environment variable '{gemini_cfg['api_key_env']}' is not set."
        )

    keys = [primary_key]
    for env_name in (gemini_cfg.get("extra_api_key_envs") or []):
        val = os.environ.get(env_name, "").strip()
        if val:
            keys.append(val)
        else:
            log.debug(f"Extra key env '{env_name}' not set or empty — skipping.")

    log.info(f"Gemini model: {gemini_cfg['model']} | key pool size: {len(keys)}")
    return GeminiPool([genai.Client(api_key=k) for k in keys])


def call_gemini(
    pool: GeminiPool,
    gemini_cfg: dict,
    system_prompt: str,
    user_content: str,
) -> str:
    """Single blocking Gemini call. Thread-safe (round-robin pool + independent HTTP request)."""
    client = pool.next()
    full_prompt = f"{system_prompt}\n\n---\n\nCONTENT TO PROCESS:\n\n{user_content}"

    cfg_kwargs: dict = dict(
        temperature=gemini_cfg["temperature"],
        max_output_tokens=gemini_cfg["max_output_tokens"],
    )
    if "response_mime_type" in gemini_cfg:
        cfg_kwargs["response_mime_type"] = gemini_cfg["response_mime_type"]

    # thinking_budget: 0 disables internal reasoning (saves tokens for structured JSON tasks)
    if "thinking_budget" in gemini_cfg:
        cfg_kwargs["thinking_config"] = genai_types.ThinkingConfig(
            thinking_budget=gemini_cfg["thinking_budget"]
        )

    response = client.models.generate_content(
        model=gemini_cfg["model"],
        contents=full_prompt,
        config=genai_types.GenerateContentConfig(**cfg_kwargs),
    )
    if not response.text:
        log.warning("Gemini returned empty or blocked response.")
        return ""
    return response.text.strip()


def call_gemini_video(
    pool: GeminiPool,
    gemini_cfg: dict,
    system_prompt: str,
    video_url: str,
) -> str:
    """
    Gemini native video processing. Passes a YouTube URL directly to the model
    as a FileData part so Gemini can read the video + audio.
    Used as fallback when no transcript is available.
    """
    client = pool.next()
    response = client.models.generate_content(
        model=gemini_cfg["model"],
        contents=genai_types.Content(
            parts=[
                genai_types.Part(
                    file_data=genai_types.FileData(file_uri=video_url)
                ),
                genai_types.Part(
                    text=f"{system_prompt}\n\nExtract all content from this video."
                ),
            ]
        ),
        config=genai_types.GenerateContentConfig(
            temperature=gemini_cfg["temperature"],
            max_output_tokens=gemini_cfg["max_output_tokens"],
        ),
    )
    return response.text.strip()

"""
Cooking Brain — LLM Client (LangChain-based)
=============================================
Provider-agnostic LLM wrapper. Supports OpenAI, Anthropic, Google Gemini,
and Ollama out of the box. Adding a new provider is a single block in
``_make_client``.

Two functions cover ~99% of usage:
  - ``call_llm(pool, llm_cfg, system_prompt, user_content) -> str``
  - ``call_llm_video(pool, llm_cfg, system_prompt, video_url) -> str``

The video function is **Gemini-only** — it uses google-genai's file_data
mechanism to pass YouTube URLs directly to the model. When the provider is
not Google, callers should fall back to transcript extraction.

Multi-key round-robin pooling is preserved across all providers via ``LLMPool``.
"""

import os
import logging
import threading

log = logging.getLogger("cooking-brain.llm")


# ── Pool ──────────────────────────────────────────────────────────────────────

class LLMPool:
    """Thread-safe round-robin pool of LangChain chat model instances."""

    def __init__(self, clients: list, provider: str = "google"):
        if not clients:
            raise ValueError("LLMPool requires at least one client.")
        self._clients = clients
        self._idx     = 0
        self._lock    = threading.Lock()
        self.provider = provider

    def next(self):
        with self._lock:
            client = self._clients[self._idx]
            self._idx = (self._idx + 1) % len(self._clients)
        return client

    def __len__(self):
        return len(self._clients)


# ── Init ──────────────────────────────────────────────────────────────────────

def init_llm(cfg: dict) -> LLMPool:
    """Read cfg['llm'] and build an LLMPool with one client per API key."""
    llm_cfg  = cfg["llm"]
    provider = llm_cfg["provider"].lower()

    primary_key = os.environ.get(llm_cfg["api_key_env"]) if provider != "ollama" else "n/a"
    if not primary_key:
        raise RuntimeError(
            f"Environment variable '{llm_cfg['api_key_env']}' is not set."
        )

    keys = [primary_key]
    for env_name in (llm_cfg.get("extra_api_key_envs") or []):
        val = os.environ.get(env_name, "").strip()
        if val:
            keys.append(val)

    clients = [_make_client(provider, k, llm_cfg) for k in keys]
    log.info(
        f"LLM provider: {provider} | model: {llm_cfg['model']} | "
        f"key pool size: {len(keys)}"
    )
    return LLMPool(clients, provider=provider)


def _make_client(provider: str, api_key: str, cfg: dict):
    """Instantiate a LangChain chat model for the given provider."""
    model       = cfg["model"]
    temperature = cfg.get("temperature", 0.3)
    max_tokens  = cfg.get("max_output_tokens", 8192)

    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        kwargs = dict(
            model=model,
            google_api_key=api_key,
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        # Gemini-specific: 0 disables internal reasoning (saves output tokens for JSON tasks)
        if "thinking_budget" in cfg:
            kwargs["thinking_budget"] = cfg["thinking_budget"]
        return ChatGoogleGenerativeAI(**kwargs)

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=model,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    if provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=model,
            temperature=temperature,
        )

    raise ValueError(
        f"Unknown LLM provider: {provider!r}. "
        f"Supported: google, openai, anthropic, ollama."
    )


# ── Calls ─────────────────────────────────────────────────────────────────────

def call_llm(
    pool: LLMPool,
    llm_cfg: dict,
    system_prompt: str,
    user_content: str,
) -> str:
    """
    Single blocking LLM call. Thread-safe (round-robin pool + independent HTTP).

    Returns the response text stripped of leading/trailing whitespace.
    """
    from langchain_core.messages import SystemMessage, HumanMessage  # noqa: PLC0415

    client = pool.next()
    full_user = f"CONTENT TO PROCESS:\n\n{user_content}"

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=full_user),
    ]

    response = client.invoke(messages)
    text = (response.content or "")
    if isinstance(text, list):
        text = "".join(part.get("text", str(part)) if isinstance(part, dict) else str(part)
                       for part in text)
    text = text.strip()
    if not text:
        log.warning("LLM returned empty response.")
    return text


def call_llm_video(
    pool: LLMPool,
    llm_cfg: dict,
    system_prompt: str,
    video_url: str,
) -> str:
    """
    Native video processing — **Gemini only**.

    Uses google-genai's file_data mechanism to pass a YouTube URL directly to
    the model. When the provider is not Google, raises NotImplementedError so
    callers can fall back to transcript extraction.
    """
    if pool.provider != "google":
        raise NotImplementedError(
            f"call_llm_video is only supported for the 'google' provider "
            f"(current: {pool.provider!r}). Fall back to transcript extraction."
        )

    # Use google-genai directly — LangChain doesn't expose Gemini's file_data
    # for arbitrary URIs through ChatGoogleGenerativeAI.
    from google import genai as google_genai
    from google.genai import types as genai_types

    api_key = os.environ.get(llm_cfg["api_key_env"], "")
    client  = google_genai.Client(api_key=api_key)

    response = client.models.generate_content(
        model=llm_cfg["model"],
        contents=genai_types.Content(
            parts=[
                genai_types.Part(file_data=genai_types.FileData(file_uri=video_url)),
                genai_types.Part(text=f"{system_prompt}\n\nExtract all content from this video."),
            ]
        ),
        config=genai_types.GenerateContentConfig(
            temperature=llm_cfg.get("temperature", 0.3),
            max_output_tokens=llm_cfg.get("max_output_tokens", 8192),
        ),
    )
    return (response.text or "").strip()

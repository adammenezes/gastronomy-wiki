# Implementation Plan: LangChain Agnostic Pipeline

The goal of this refactor is to strip out the hardcoded `google-genai` SDK and replace it with LangChain, allowing the Cooking Brain pipeline to use any LLM provider (OpenAI, Anthropic, local models, etc.) while preserving the robust multi-agent architecture.

## Open Questions for Next Session
> [!WARNING]
> **Video Processing Fallback**
> Currently, the `CleanerAgent` relies on Gemini's native multimodal capabilities to process raw YouTube URLs (extracting information even without transcripts). Most other LLMs (like GPT-4) cannot do this natively. 
> *Question: Should we implement a LangChain `YoutubeLoader` fallback to grab transcripts for non-Gemini models, or should we strictly require a Gemini key for video processing?*

> [!WARNING]
> **Rate Limiting Pool**
> Currently, `gemini.py` uses a custom `GeminiPool` to round-robin multiple API keys to avoid rate limits. LangChain does not have a native API key pooler out-of-the-box.
> *Question: Should we port this custom key-pooling logic over to the LangChain implementation, or remove it and assume the user has a high-tier paid API key?*

---

## Proposed Changes

### 1. `agent/gemini.py` → `agent/llm.py`
We will rename the file and completely replace the `google-genai` code with LangChain's `BaseChatModel` interface.
- Create an `init_llm(cfg)` factory function that reads the provider from `config.yaml` and instantiates the correct LangChain model (e.g., `ChatOpenAI`, `ChatAnthropic`, `ChatGoogleGenAI`).
- Replace `call_gemini` with a generic `call_llm` function that takes a `BaseChatModel` and uses the standard `model.invoke(prompt)` pattern.

### 2. `agent/config.yaml` overhaul
We will replace the `gemini:` block with a universal `llm:` block that supports provider definitions.
```yaml
llm:
  provider: openai                # "google", "openai", "anthropic", "ollama"
  model: gpt-4o-mini
  temperature: 0.3
  api_key_env: OPENAI_API_KEY
  agents:
    writer:
      model: gpt-4o               # Can mix-and-match models per agent
    cross_linker:
      model: gpt-4o-mini
```

### 3. Agent Refactoring
Every agent currently imports `init_gemini` and `call_gemini`. We will need to update the imports and method signatures across the entire `agents/` folder.
- Update imports: `from llm import init_llm, call_llm`
- Update the `client` parameter passed by the `Orchestrator` to be a `langchain_core.language_models.BaseChatModel`.

### 4. Dependency Updates
- Add `langchain`, `langchain-core`, `langchain-openai`, `langchain-google-genai`, `langchain-anthropic` to the environment.

## Verification Plan
1. Set the config to use `openai` with a dummy `gpt-4o-mini` key.
2. Run `python agent/cli/compile.py --url <test_url>`.
3. Verify that the `RouterAgent` correctly outputs structured JSON using the OpenAI model.
4. Verify that the `WriterAgent` correctly formats the markdown and the `CrossLinker` accurately isolates and links paragraphs using the new LLM wrapper.

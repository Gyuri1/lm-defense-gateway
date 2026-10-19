"""Simple OpenAI-SDK test client for llm-defense-gateway (pip install openai)."""
import os
import time

from openai import OpenAI

BASE_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8100/v1")
API_KEY  = os.getenv("GATEWAY_API_KEY", "change-me")
MODEL    = os.getenv("MODEL", "your-model-name")
PROMPT   = os.getenv("PROMPT", "Write a 300-word essay about the ocean.")

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)

t0 = time.perf_counter()
resp = client.chat.completions.create(
    model=MODEL,
    messages=[{"role": "user", "content": PROMPT}],
    max_tokens=300,
    temperature=0.7,
)
elapsed = time.perf_counter() - t0

choice = resp.choices[0]
out_tokens = resp.usage.completion_tokens if resp.usage else 0
print(f"Finish reason: {choice.finish_reason}")
if choice.finish_reason == "content_filter":
    print(f"BLOCKED: {choice.message.content}")
    print(f"AI Defense: {(resp.model_extra or {}).get('ai_defense')}")
else:
    print(f"Answer: {choice.message.content[:200]}...")
print(f"Output tokens: {out_tokens}")
print(f"Elapsed: {elapsed:.2f}s")
if elapsed > 0:
    print(f"Generation throughput: {out_tokens / elapsed:.1f} tok/s")
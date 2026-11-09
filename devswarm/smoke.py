"""A two-turn conversation that proves the LLM client, caching and tracing work together."""

from __future__ import annotations

from dataclasses import dataclass

from anthropic.types import MessageParam

from devswarm.llm.types import LLMClient, LLMError, LLMRequest, LLMResponse
from devswarm.storage.trace import TraceStore

# Must exceed the model's minimum cacheable size (512 tokens on Haiku 5.5),
# otherwise the API silently skips caching and this check could never pass.
_SYSTEM_PROMPT = "\n".join(
    f"Rule {number}: answer with one short word and nothing else." for number in range(1, 151)
)
_PROMPTS = ("Reply with the word one.", "Now reply with the word two.")


@dataclass(frozen=True)
class SmokeResult:
    run_id: str
    turns: list[LLMResponse]

    @property
    def cache_hit(self) -> bool:
        return self.turns[-1].usage.cache_read_input_tokens > 0

    @property
    def cost_usd(self) -> float:
        return sum(turn.cost_usd for turn in self.turns)


def run_smoke(client: LLMClient, store: TraceStore, model: str) -> SmokeResult:
    run_id = store.start_run("LLM smoke test", config={"model": model})
    step_id = store.start_step(run_id, "smoke")
    messages: list[MessageParam] = []
    turns: list[LLMResponse] = []
    for prompt in _PROMPTS:
        messages.append({"role": "user", "content": prompt})
        request = LLMRequest(
            model=model,
            system=_SYSTEM_PROMPT,
            messages=list(messages),
            max_tokens=2_000,
            effort="low",
        )
        try:
            response = client.create(request)
        except LLMError as exc:
            store.record_llm_error(step_id, request, exc)
            store.finish_step(step_id, "failed", summary=str(exc))
            store.finish_run(run_id, "failed", error=str(exc))
            raise
        store.record_llm_call(step_id, request, response)
        # Appended unchanged, thinking blocks included: the API rejects edited history.
        messages.append({"role": "assistant", "content": response.content})
        turns.append(response)

    store.finish_step(step_id, "done")
    store.finish_run(run_id, "done")
    return SmokeResult(run_id=run_id, turns=turns)

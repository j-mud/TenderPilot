"""Thin wrapper around the model API.

Supports two providers, chosen by the LLM_PROVIDER env var:
  - "anthropic" (default): Claude via the Anthropic API
  - "openai": any OpenAI-compatible endpoint (OpenAI, or a hackathon-provided
    endpoint such as HUMAIN's, by setting OPENAI_BASE_URL)

Two capabilities are exposed:
  - structured(): ask for JSON matching a Pydantic model, validated + retried
  - tool_turn():  one step of a tool-calling conversation (used by the orchestrator)
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class TurnResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_assistant: Any = None  # provider-specific message to append to history


def _extract_json(text: str) -> str:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        return fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in model output")
    return text[start : end + 1]


class LLM:
    def __init__(self, provider: str | None = None, model: str | None = None):
        self.provider = (provider or os.getenv("LLM_PROVIDER", "anthropic")).lower()
        if self.provider == "anthropic":
            import anthropic

            self.client = anthropic.Anthropic()
            self.model = model or os.getenv("LLM_MODEL", "claude-sonnet-5-5")
        elif self.provider == "openai":
            from openai import OpenAI

            self.client = OpenAI(base_url=os.getenv("OPENAI_BASE_URL") or None)
            self.model = model or os.getenv("LLM_MODEL", "gpt-4o")
        else:
            raise ValueError(f"Unknown LLM_PROVIDER: {self.provider}")

    # ---------- plain completion ----------
    def complete(self, system: str, prompt: str, max_tokens: int = 4096) -> str:
        if self.provider == "anthropic":
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(b.text for b in resp.content if b.type == "text")
        resp = self.client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""

    # ---------- structured output ----------
    def structured(self, system: str, prompt: str, schema: type[T], retries: int = 2) -> T:
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        full_prompt = (
            f"{prompt}\n\nReturn ONLY one JSON object that matches this JSON schema, "
            f"with no other text:\n{schema_json}"
        )
        last_err: Exception | None = None
        for _ in range(retries + 1):
            text = self.complete(system, full_prompt, max_tokens=8000)
            try:
                return schema.model_validate_json(_extract_json(text))
            except (ValueError, ValidationError) as err:
                last_err = err
                full_prompt += f"\n\nYour previous answer was invalid: {err}. Fix it and return valid JSON only."
        raise RuntimeError(f"Model did not return valid {schema.__name__}: {last_err}")

    # ---------- tool calling ----------
    def tool_turn(self, system: str, messages: list, tools: list[dict]) -> TurnResult:
        """tools use a neutral format: {name, description, parameters (JSON schema)}."""
        if self.provider == "anthropic":
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=system,
                messages=messages,
                tools=[
                    {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                    for t in tools
                ],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            calls = [ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"]
            content = []
            for b in resp.content:
                if b.type == "text":
                    content.append({"type": "text", "text": b.text})
                elif b.type == "tool_use":
                    content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": dict(b.input)})
            return TurnResult(text, calls, {"role": "assistant", "content": content})

        resp = self.client.chat.completions.create(
            model=self.model,
            max_tokens=2048,
            messages=[{"role": "system", "content": system}, *messages],
            tools=[{"type": "function", "function": t} for t in tools],
        )
        msg = resp.choices[0].message
        calls = [
            ToolCall(tc.id, tc.function.name, json.loads(tc.function.arguments or "{}"))
            for tc in (msg.tool_calls or [])
        ]
        return TurnResult(msg.content or "", calls, msg.model_dump(exclude_none=True))

    def tool_results_message(self, results: list[tuple[ToolCall, str]]) -> list[dict]:
        """Format tool outputs to append to the conversation."""
        if self.provider == "anthropic":
            return [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": call.id, "content": output}
                        for call, output in results
                    ],
                }
            ]
        return [{"role": "tool", "tool_call_id": call.id, "content": output} for call, output in results]

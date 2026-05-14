# -*- coding: utf-8 -*-
"""
Reasoning Cell — стандартная обёртка LLM-вызовов с детерминизмом.

Контракт:
- input → структурированный JSON (через pydantic-схему)
- output → структурированный JSON (через pydantic-схему)
- кеш: одинаковый input → одинаковый output (без вызова LLM)
- self-verify: модель сама проверяет свой ответ (опционально)
- constraint check: pure-python проверка инвариантов
- provenance: всё пишется в БД (llm_calls table)

Принципы:
1. LLM никогда не вызывается напрямую — только через ReasoningCell
2. Каждая клетка имеет имя (cell_type) для аудита
3. Промпт-шаблон версионируется (prompt_version)
4. Модель версионируется (model_version)
5. Кеш-ключ = sha256(prompt_template_version + model_version + input_json)
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Anthropic SDK импортируем лениво — позволит работать в офлайн-режиме (cache only)
_anthropic_client = None


def get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic
        _anthropic_client = Anthropic()
    return _anthropic_client


# ---------------------------------------------------------------------
# Модели и стоимость
# ---------------------------------------------------------------------
DEFAULT_MODEL = "claude-sonnet-4-6"
PRICING = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "thinking": 15.0},
    "claude-opus-4-6":   {"input": 15.0, "output": 75.0, "thinking": 75.0},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "thinking": 15.0},
    "claude-opus-4-5":   {"input": 15.0, "output": 75.0, "thinking": 75.0},
}


def estimate_cost_usd(model: str, tokens_in: int, tokens_out: int, thinking_tokens: int = 0) -> float:
    """Оценка стоимости вызова в USD."""
    p = PRICING.get(model, {"input": 3.0, "output": 15.0, "thinking": 15.0})
    return (
        tokens_in * p["input"] / 1_000_000
        + tokens_out * p["output"] / 1_000_000
        + thinking_tokens * p["thinking"] / 1_000_000
    )


# ---------------------------------------------------------------------
# Cell Result
# ---------------------------------------------------------------------
@dataclass
class CellResult:
    """Результат одного reasoning-вызова."""
    cell_type: str
    input_json: dict
    output_json: dict
    confidence: float                  # 0..1
    cached: bool                       # пришло ли из кеша
    constraint_check_passed: bool
    self_verify_passed: bool | None    # None если не проверяли
    reasoning_trace: str = ""          # extended thinking content
    prompt_full: str = ""              # для аудита
    response_full: str = ""            # для аудита
    tokens_in: int = 0
    tokens_out: int = 0
    thinking_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    model_version: str = ""
    prompt_version: str = ""
    call_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    cached_from_id: str | None = None


# ---------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------
class ReasoningCache:
    """SQLite-бэкенд кеша. Ключ = sha256(prompt_version + model + input)."""

    def __init__(self, db_conn):
        self._conn = db_conn

    @staticmethod
    def make_key(cell_type: str, prompt_version: str, model: str, input_json: dict) -> str:
        canonical = json.dumps(
            {"t": cell_type, "pv": prompt_version, "m": model, "in": input_json},
            sort_keys=True, ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT response_json, model_version FROM reasoning_cache "
            "WHERE prompt_hash=? AND invalidated_at IS NULL",
            (key,),
        )
        row = cur.fetchone()
        if row:
            self._conn.execute(
                "UPDATE reasoning_cache SET hit_count = hit_count + 1 WHERE prompt_hash=?",
                (key,),
            )
            self._conn.commit()
            return json.loads(row[0])
        return None

    def put(self, key: str, response: dict, model: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO reasoning_cache "
            "(prompt_hash, response_json, model_version, created_at) VALUES (?, ?, ?, ?)",
            (key, json.dumps(response, ensure_ascii=False), model, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()


# ---------------------------------------------------------------------
# Cell base class
# ---------------------------------------------------------------------
class ReasoningCell:
    """
    Базовый класс reasoning-клетки. Наследники:
    - определяют cell_type, prompt_version, model
    - реализуют render_prompt(input) → str
    - реализуют parse_response(text) → dict
    - реализуют check_constraints(input, output) → bool
    """

    cell_type: str = "base"
    prompt_version: str = "v1"
    model: str = DEFAULT_MODEL
    use_thinking: bool = True
    thinking_budget: int = 4000        # токенов на reasoning
    max_tokens: int = 4096
    self_verify: bool = False          # делаем ли второй вызов с проверкой

    def __init__(self, cache: ReasoningCache | None = None, llm_log_writer: Callable | None = None):
        self.cache = cache
        self.llm_log_writer = llm_log_writer  # колбэк для записи в llm_calls

    # ------- to be overridden -------
    def render_prompt(self, input_data: dict) -> str:
        raise NotImplementedError

    def parse_response(self, text: str) -> dict:
        """Извлекает структурированный JSON из ответа модели.
        По умолчанию ожидает что ответ — это валидный JSON в блоке ```json ... ```."""
        # 1. Попытка как чистый JSON
        try:
            return json.loads(text.strip())
        except Exception:
            pass
        # 2. Поиск блока ```json
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
        if m:
            return json.loads(m.group(1))
        # 3. Грубый fallback — найти первый { ... }
        m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if m:
            return json.loads(m.group(1))
        raise ValueError(f"Cannot parse response as JSON:\n{text[:500]}")

    def check_constraints(self, input_data: dict, output: dict) -> tuple[bool, str]:
        """Проверка инвариантов (sum=1, no negatives, etc). Возвращает (passed, reason)."""
        return True, ""

    def get_confidence(self, output: dict) -> float:
        """Извлекает confidence из ответа модели (если она его выставила)."""
        return float(output.get("confidence", 0.5))

    # ------- main entry -------
    def run(self, input_data: dict, *, force_recompute: bool = False, run_id: str | None = None) -> CellResult:
        prompt = self.render_prompt(input_data)

        # Cache lookup
        if self.cache is not None and not force_recompute:
            key = ReasoningCache.make_key(self.cell_type, self.prompt_version, self.model, input_data)
            cached = self.cache.get(key)
            if cached:
                output = cached
                ok, _ = self.check_constraints(input_data, output)
                result = CellResult(
                    cell_type=self.cell_type,
                    input_json=input_data,
                    output_json=output,
                    confidence=self.get_confidence(output),
                    cached=True,
                    constraint_check_passed=ok,
                    self_verify_passed=None,
                    model_version=self.model,
                    prompt_version=self.prompt_version,
                )
                if self.llm_log_writer:
                    self.llm_log_writer(result, run_id=run_id, prompt_full=prompt, was_cache_hit=True)
                return result

        # Live call
        client = get_anthropic_client()
        t0 = time.time()

        api_kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        if self.use_thinking:
            api_kwargs["thinking"] = {"type": "enabled", "budget_tokens": self.thinking_budget}
            # При extended thinking max_tokens должен быть > thinking_budget
            if api_kwargs["max_tokens"] <= self.thinking_budget:
                api_kwargs["max_tokens"] = self.thinking_budget + 2048

        msg = client.messages.create(**api_kwargs)
        latency_ms = int((time.time() - t0) * 1000)

        # Извлекаем содержимое
        thinking_text = ""
        text_response = ""
        for block in msg.content:
            if hasattr(block, "type"):
                if block.type == "thinking":
                    thinking_text += getattr(block, "thinking", "")
                elif block.type == "text":
                    text_response += block.text
            elif hasattr(block, "text"):
                text_response += block.text

        tokens_in = msg.usage.input_tokens
        tokens_out = msg.usage.output_tokens
        thinking_tokens = 0  # API не разделяет thinking токены отдельно в большинстве случаев
        # Если в usage есть cache_*_input_tokens или extended thinking метрики — добавим
        cost = estimate_cost_usd(self.model, tokens_in, tokens_out, thinking_tokens)

        # Парсим ответ
        try:
            output = self.parse_response(text_response)
        except Exception as e:
            output = {"error": f"parse_failed: {e}", "raw": text_response}

        ok, reason = self.check_constraints(input_data, output)
        if not ok and "error" not in output:
            output["constraint_failed_reason"] = reason

        # Self-verify (опционально)
        sv_passed = None
        if self.self_verify and "error" not in output and ok:
            sv_passed = self._self_verify_call(input_data, output, prompt, text_response)

        confidence = self.get_confidence(output) if "error" not in output else 0.0

        # Кешируем
        if self.cache is not None and ok and "error" not in output:
            key = ReasoningCache.make_key(self.cell_type, self.prompt_version, self.model, input_data)
            self.cache.put(key, output, self.model)

        result = CellResult(
            cell_type=self.cell_type,
            input_json=input_data,
            output_json=output,
            confidence=confidence,
            cached=False,
            constraint_check_passed=ok,
            self_verify_passed=sv_passed,
            reasoning_trace=thinking_text,
            prompt_full=prompt,
            response_full=text_response,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            thinking_tokens=thinking_tokens,
            latency_ms=latency_ms,
            cost_usd=cost,
            model_version=self.model,
            prompt_version=self.prompt_version,
        )
        if self.llm_log_writer:
            self.llm_log_writer(result, run_id=run_id, prompt_full=prompt, was_cache_hit=False)
        return result

    def _self_verify_call(self, input_data: dict, output: dict, original_prompt: str, original_response: str) -> bool:
        """Второй вызов: спрашивает модель проверить свой же ответ."""
        client = get_anthropic_client()
        verify_prompt = (
            "Ниже исходная задача и ответ. Проверь ответ на: "
            "(1) арифметическую корректность, (2) соответствие инструкции, "
            "(3) разумность значений. Ответь строго JSON: "
            '{\"verdict\":\"PASS\"|\"FAIL\", \"issues\": [список проблем]}.\n\n'
            f"=== ИСХОДНЫЙ ВВОД ===\n{json.dumps(input_data, ensure_ascii=False, indent=2)}\n\n"
            f"=== ОТВЕТ ===\n{json.dumps(output, ensure_ascii=False, indent=2)}"
        )
        msg = client.messages.create(
            model=self.model,
            max_tokens=512,
            messages=[{"role": "user", "content": verify_prompt}],
        )
        text = ""
        for block in msg.content:
            if hasattr(block, "text"):
                text += block.text
        try:
            data = self.parse_response(text)
            return data.get("verdict", "FAIL") == "PASS"
        except Exception:
            return False


# ---------------------------------------------------------------------
# llm_calls writer
# ---------------------------------------------------------------------
def make_llm_log_writer(db_conn):
    """Возвращает колбэк, пишущий результат в таблицу llm_calls."""

    def writer(result: CellResult, run_id: str | None = None,
               prompt_full: str = "", was_cache_hit: bool = False):
        prompt_hash = ReasoningCache.make_key(
            result.cell_type, result.prompt_version, result.model_version, result.input_json
        )
        db_conn.execute(
            """INSERT INTO llm_calls (
                id, run_id, cell_type, prompt_hash, model_version,
                prompt_full, reasoning_trace, response_full,
                self_verify_passed, constraint_check_passed,
                confidence, tokens_in, tokens_out, thinking_tokens,
                latency_ms, cost_usd, cached_from_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result.call_id, run_id, result.cell_type, prompt_hash, result.model_version,
                json.dumps({"prompt": prompt_full, "input": result.input_json}, ensure_ascii=False),
                result.reasoning_trace,
                result.response_full,
                int(result.self_verify_passed) if result.self_verify_passed is not None else None,
                int(result.constraint_check_passed),
                result.confidence,
                result.tokens_in, result.tokens_out, result.thinking_tokens,
                result.latency_ms, result.cost_usd,
                "cache" if was_cache_hit else None,
                result.created_at,
            ),
        )
        db_conn.commit()

    return writer

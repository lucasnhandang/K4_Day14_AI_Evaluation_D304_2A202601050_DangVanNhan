"""Run RAGAS and DeepEval on the same OrbitTech benchmark traces.

This adapter deliberately does not call the RAG application. It evaluates the
recorded answers in artifacts/actual_answers.json against golden_dataset.json so
both frameworks see the same question, answer, reference, and retrieved text.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import types
import warnings
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]


def _load_ragas_imports() -> tuple[Any, Any, list[Any], bool]:
    """Import RAGAS 0.4.x and tolerate its stale optional VertexAI import."""

    compatibility_shim = False
    try:
        from ragas import EvaluationDataset, SingleTurnSample, evaluate
        from ragas.metrics import (
            AnswerRelevancy,
            ContextPrecision,
            ContextRecall,
            Faithfulness,
        )
    except ModuleNotFoundError as exc:
        missing = "langchain_community.chat_models.vertexai"
        if missing not in str(exc):
            raise
        # RAGAS imports VertexAI classes for type dispatch even when this run
        # uses OpenAI. langchain-community 0.4.x no longer ships that module.
        module = types.ModuleType(missing)
        module.ChatVertexAI = type("ChatVertexAI", (), {})
        sys.modules[missing] = module
        compatibility_shim = True
        from ragas import EvaluationDataset, SingleTurnSample, evaluate
        from ragas.metrics import (
            AnswerRelevancy,
            ContextPrecision,
            ContextRecall,
            Faithfulness,
        )

    metrics = [Faithfulness, AnswerRelevancy, ContextRecall, ContextPrecision]
    return evaluate, (EvaluationDataset, SingleTurnSample), metrics, compatibility_shim


def _load_inputs(golden_path: Path, actual_path: Path) -> list[dict[str, Any]]:
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    actual_root = json.loads(actual_path.read_text(encoding="utf-8"))
    actual = actual_root["answers"]
    actual_by_id = {item["id"]: item for item in actual}

    rows = []
    for pair in golden["qa_pairs"]:
        item = actual_by_id.get(pair["id"])
        if item is None:
            raise ValueError(f"Missing actual answer for {pair['id']}")
        retrieved = [context["text"] for context in item["retrieved_contexts"]]
        reference_contexts = [context["text"] for context in pair["contexts"]]
        rows.append(
            {
                "id": pair["id"],
                "question": pair["question"],
                "expected_answer": pair["expected_answer"],
                "actual_answer": item["actual_answer"],
                "retrieved_contexts": retrieved,
                "reference_contexts": reference_contexts,
            }
        )
    return rows


def _aggregate(rows: list[dict[str, Any]], metric_names: list[str]) -> dict[str, Any]:
    aggregate: dict[str, Any] = {}
    for name in metric_names:
        values = [
            float(row[name])
            for row in rows
            if isinstance(row.get(name), (int, float))
            and math.isfinite(float(row[name]))
        ]
        aggregate[f"avg_{name}"] = mean(values) if values else None
        aggregate[f"scored_{name}"] = len(values)
    return aggregate


def _finite_or_none(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return float(value)


def _run_ragas(rows: list[dict[str, Any]], model_name: str) -> dict[str, Any]:
    evaluate, dataset_types, metric_classes, compatibility_shim = _load_ragas_imports()
    EvaluationDataset, SingleTurnSample = dataset_types

    from langchain_openai import ChatOpenAI
    from ragas.llms import LangchainLLMWrapper

    llm = LangchainLLMWrapper(
        ChatOpenAI(model=model_name, temperature=0, max_tokens=2048)
    )
    metrics = [metric(llm=llm) for metric in metric_classes]
    samples = [
        SingleTurnSample(
            user_input=row["question"],
            response=row["actual_answer"],
            reference=row["expected_answer"],
            retrieved_contexts=row["retrieved_contexts"],
            reference_contexts=row["reference_contexts"],
        )
        for row in rows
    ]

    dataset = EvaluationDataset(samples=samples)
    result = evaluate(
        dataset=dataset,
        metrics=metrics,
        raise_exceptions=False,
        show_progress=True,
    )
    frame = result.to_pandas()
    result_rows = []
    for index, row in enumerate(frame.to_dict(orient="records")):
        result_rows.append(
            {
                "id": rows[index]["id"],
                "faithfulness": _finite_or_none(row.get("faithfulness")),
                "answer_relevancy": _finite_or_none(row.get("answer_relevancy")),
                "context_recall": _finite_or_none(row.get("context_recall")),
                "context_precision": _finite_or_none(row.get("context_precision")),
            }
        )
    metric_names = [
        "faithfulness",
        "answer_relevancy",
        "context_recall",
        "context_precision",
    ]
    return {
        "version": "0.4.3",
        "model": model_name,
        "compatibility_shim_used": compatibility_shim,
        "results": result_rows,
        "summary": _aggregate(result_rows, metric_names),
    }


class _OpenAICompatibleJudge:
    """DeepEval judge using the configured OpenAI-compatible endpoint.

    DeepEval's string model path requested 16,384 output tokens from the
    OpenRouter endpoint. This adapter keeps the same model and endpoint while
    capping each judge response at 1,024 tokens, which is ample for metric
    reasoning and avoids a provider-side credit rejection.
    """

    def __init__(self, model_name: str):
        from deepeval.models import DeepEvalBaseLLM

        self._base_class = DeepEvalBaseLLM
        self._model_name = model_name
        self._client = None
        self._async_client = None

    def as_deepeval_model(self):
        base_class = self._base_class
        owner = self

        class Judge(base_class):
            def load_model(self):
                return owner

            def get_model_name(self, *args, **kwargs):
                return owner._model_name

            def generate(self, prompt, schema=None, **kwargs):
                return owner._generate(prompt, schema=schema, async_mode=False)

            async def a_generate(self, prompt, schema=None, **kwargs):
                return await owner._generate(prompt, schema=schema, async_mode=True)

            def supports_structured_outputs(self):
                return False

            def supports_json_mode(self):
                return True

        return Judge(model=self._model_name)

    def _response_format(self, schema: Any) -> dict[str, Any] | None:
        return {"type": "json_object"} if schema is not None else None

    def _request_kwargs(self, prompt: str, schema: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._model_name,
            "messages": [{"role": "user", "content": str(prompt)}],
            "temperature": 0,
            "max_tokens": 1024,
        }
        response_format = self._response_format(schema)
        if response_format is not None:
            kwargs["response_format"] = response_format
        return kwargs

    def _make_client(self):
        from openai import OpenAI

        if self._client is None:
            self._client = OpenAI(
                api_key=os.environ["OPENAI_API_KEY"],
                base_url=os.getenv("OPENAI_BASE_URL") or None,
            )
        return self._client

    def _make_async_client(self):
        from openai import AsyncOpenAI

        if self._async_client is None:
            self._async_client = AsyncOpenAI(
                api_key=os.environ["OPENAI_API_KEY"],
                base_url=os.getenv("OPENAI_BASE_URL") or None,
            )
        return self._async_client

    def _extract_content(self, response: Any) -> str:
        content = response.choices[0].message.content
        return content if isinstance(content, str) else str(content)

    def _generate(self, prompt: str, schema: Any, async_mode: bool):
        kwargs = self._request_kwargs(prompt, schema)
        if async_mode:
            async def request():
                response = await self._make_async_client().chat.completions.create(
                    **kwargs
                )
                return self._extract_content(response)

            return request()
        response = self._make_client().chat.completions.create(**kwargs)
        return self._extract_content(response)


def _run_deepeval(rows: list[dict[str, Any]], model_name: str) -> dict[str, Any]:
    from deepeval import evaluate as deepeval_evaluate
    from deepeval.evaluate.configs import (
        AsyncConfig,
        CacheConfig,
        DisplayConfig,
        ErrorConfig,
    )
    from deepeval.metrics import (
        AnswerRelevancyMetric,
        ContextualPrecisionMetric,
        ContextualRecallMetric,
        FaithfulnessMetric,
    )
    from deepeval.test_case import LLMTestCase

    metric_factories = {
        "Faithfulness": FaithfulnessMetric,
        "Answer Relevancy": AnswerRelevancyMetric,
        "Contextual Recall": ContextualRecallMetric,
        "Contextual Precision": ContextualPrecisionMetric,
    }
    test_cases = [
        LLMTestCase(
            input=row["question"],
            actual_output=row["actual_answer"],
            expected_output=row["expected_answer"],
            retrieval_context=row["retrieved_contexts"],
        )
        for row in rows
    ]
    judge = _OpenAICompatibleJudge(model_name).as_deepeval_model()
    metrics = [
        metric_class(
            threshold=0.5,
            model=judge,
            include_reason=True,
            async_mode=True,
            verbose_mode=False,
        )
        for metric_class in metric_factories.values()
    ]
    evaluation = deepeval_evaluate(
        test_cases=test_cases,
        metrics=metrics,
        async_config=AsyncConfig(run_async=True, max_concurrent=10),
        display_config=DisplayConfig(
            show_indicator=True,
            print_results=False,
            inspect_after_run=False,
        ),
        cache_config=CacheConfig(write_cache=False, use_cache=False),
        error_config=ErrorConfig(ignore_errors=True),
    )

    metric_key_by_name = {
        "faithfulness": "faithfulness",
        "answer relevancy": "answer_relevancy",
        "contextual recall": "contextual_recall",
        "contextual precision": "contextual_precision",
    }
    # DeepEval executes cases concurrently and returns TestResult objects in
    # completion order. Use TestResult.index, never enumerate(), to preserve
    # the question/answer ID alignment in the artifact.
    result_by_index = {}
    for test_result in evaluation.test_results:
        index = test_result.index
        if index is None or not 0 <= index < len(rows):
            raise RuntimeError(
                f"DeepEval returned an invalid test result index: {index!r}"
            )
        output = {"id": rows[index]["id"]}
        for metric_data in test_result.metrics_data or []:
            key = metric_key_by_name.get(metric_data.name.lower())
            if key is None:
                continue
            output[key] = metric_data.score
            output[f"{key}_reason"] = metric_data.reason
            if metric_data.error:
                output[f"{key}_error"] = metric_data.error
        result_by_index[index] = output

    result_rows = [result_by_index[index] for index in sorted(result_by_index)]

    metric_names = list(metric_key_by_name.values())
    return {
        "version": "4.1.7",
        "model": model_name,
        "results": result_rows,
        "summary": _aggregate(result_rows, metric_names),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=ROOT / "golden_dataset.json")
    parser.add_argument(
        "--actual", type=Path, default=ROOT / "artifacts" / "actual_answers.json"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts" / "framework_comparison.json",
    )
    parser.add_argument("--model", default=None)
    parser.add_argument(
        "--framework",
        choices=("both", "ragas", "deepeval"),
        default="both",
        help="Run one framework or both; a single-framework run preserves the other existing result.",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    model_name = args.model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required for RAGAS/DeepEval LLM metrics")

    rows = _load_inputs(args.golden, args.actual)
    print(f"Loaded {len(rows)} shared evaluation cases")
    existing = None
    if args.output.exists():
        existing = json.loads(args.output.read_text(encoding="utf-8"))

    if args.framework in ("both", "ragas"):
        print("Running RAGAS...")
        ragas_result = _run_ragas(rows, model_name)
    elif existing and "ragas" in existing.get("frameworks", {}):
        ragas_result = existing["frameworks"]["ragas"]
    else:
        raise SystemExit("--framework deepeval requires an existing RAGAS result")

    if args.framework in ("both", "deepeval"):
        print("Running DeepEval...")
        deepeval_result = _run_deepeval(rows, model_name)
    elif existing and "deepeval" in existing.get("frameworks", {}):
        deepeval_result = existing["frameworks"]["deepeval"]
    else:
        raise SystemExit("--framework ragas requires an existing DeepEval result")

    output = {
        "schema_version": "1.0",
        "corpus_id": "orbittech-customer-support-v1",
        "input": {
            "golden": str(args.golden),
            "actual": str(args.actual),
            "case_count": len(rows),
            "model": model_name,
        },
        "frameworks": {"ragas": ragas_result, "deepeval": deepeval_result},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved framework comparison: {args.output}")
    print("RAGAS summary:", json.dumps(ragas_result["summary"], indent=2))
    print("DeepEval summary:", json.dumps(deepeval_result["summary"], indent=2))
    return 0


if __name__ == "__main__":
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        raise SystemExit(main())

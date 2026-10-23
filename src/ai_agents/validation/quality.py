"""고정 가상 입력으로 실제 평가 모델만 호출한다. MCP/DB/기준 생성/운영 저장 없음."""

from copy import deepcopy
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from typing import Literal
from urllib.parse import urlsplit

from openai import AsyncOpenAI
from pydantic import Field, model_validator

from ..evaluation_agent.feedback.agent import FeedbackAgent
from ..evaluation_agent.feedback.prompts import SYSTEM_PROMPT as FEEDBACK_PROMPT
from ..evaluation_agent.feedback.validation import validate_messages
from ..evaluation_agent.final.agent import SYSTEM_PROMPT as FINAL_PROMPT
from ..evaluation_agent.final.agent import OpenAIEvaluator
from ..evaluation_agent.final.materials import EvaluationMaterials
from ..evaluation_agent.final.stored_rubric import StoredEvaluationInput
from ..evaluation_agent.final.validation import validate_criteria, validate_materials
from ..shared.evidence_models import EvaluationEvidence
from ..shared.live_feedback_contracts import LiveEvaluationContext, MessageBatch
from ..shared.models import NonEmpty, StrictModel
from .models import Check, Dataset
from .runner import digest, run_experiment


class QualityConfig(StrictModel):
    model: NonEmpty
    base_url: NonEmpty
    model_revision: NonEmpty = "unspecified"
    timeout_seconds: int = Field(default=120, ge=1, le=600, strict=True)

    @model_validator(mode="after")
    def explicit_endpoint(self):
        url = urlsplit(self.base_url)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise ValueError("explicit HTTP(S) endpoint without credentials/query required")
        return self


class FrozenFinalInput(StrictModel):
    request: StoredEvaluationInput
    evidence: EvaluationEvidence
    materials: EvaluationMaterials

    @model_validator(mode="after")
    def bound(self):
        validate_criteria(self.request)
        validate_materials(self.request, self.evidence, self.materials)
        return self


def check_quality_dataset(dataset: Dataset):
    if dataset.purpose not in {"feedback", "final"}:
        raise ValueError("only feedback/final quality datasets supported")
    for case in dataset.examples:
        if dataset.purpose == "final":
            FrozenFinalInput.model_validate(case.inputs)
        else:
            if set(case.inputs) != {"context", "batch"}:
                raise ValueError("feedback inputs must contain only context and batch")
            context = LiveEvaluationContext.model_validate(case.inputs["context"])
            batch = MessageBatch.model_validate(case.inputs["batch"])
            validate_messages(context, batch)
    return {"purpose": dataset.purpose, "cases": len(dataset.examples), "network_calls": 0}


class MeasuredChat:
    """검증 재생성을 포함한 호출 수·시간·토큰만 수집. 인증/프롬프트는 metrics에 넣지 않는다."""

    def __init__(self, client):
        self.client, self.attempts = client, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        record = {"status": "error", "input_tokens": None, "output_tokens": None}
        started = perf_counter()
        try:
            result = await self.client.chat.completions.create(**kwargs)
            record["status"] = "returned"
            usage = getattr(result, "usage", None)
            for source, dest in (("prompt_tokens", "input_tokens"),
                                 ("completion_tokens", "output_tokens")):
                value = getattr(usage, source, None)
                if type(value) is int and value >= 0:
                    record[dest] = value
            return result
        finally:
            record["duration_ms"] = round((perf_counter() - started) * 1000, 3)
            self.attempts.append(record)

    def metrics(self):
        return {
            "model_calls": len(self.attempts), "attempts": deepcopy(self.attempts),
            "model_duration_ms": round(sum(a["duration_ms"] for a in self.attempts), 3),
            **{
                key: (sum(a[key] for a in self.attempts)
                      if self.attempts and all(a[key] is not None for a in self.attempts) else None)
                for key in ("input_tokens", "output_tokens")
            },
        }


async def no_tools(*args, **kwargs):
    raise RuntimeError("MCP calls are not allowed in quality validation")


class QualityTarget:
    def __init__(self, purpose, client, config, mode):
        self.purpose, self.client, self.config, self.mode = purpose, client, config, mode

    async def __call__(self, inputs):
        measured = MeasuredChat(self.client)
        try:
            if self.purpose == "feedback":
                context = LiveEvaluationContext.model_validate(inputs["context"])
                batch = MessageBatch.model_validate(inputs["batch"])
                result = await FeedbackAgent(
                    measured, self.config.model, record_work_refs=False,
                ).generate(context, batch)
            else:
                frozen = FrozenFinalInput.model_validate(inputs)
                agent = OpenAIEvaluator(
                    measured, context=frozen.evidence.context, business_call=no_tools,
                    rag_call=no_tools, model=self.config.model, api_mode="chat",
                    extra_headers={"X-Stage": "evaluate"},
                )
                result = await agent.evaluate_collected(
                    frozen.request, evidence=frozen.evidence, materials=frozen.materials,
                )
            outcome = {"status": "generated", "result": result.model_dump(mode="json")}
        except Exception as exc:
            # 평가기의 예외 본문은 입력/출력을 포함할 수 있다.
            outcome = {"status": "error", "error_type": type(exc).__name__}
        return {**outcome, "metrics": measured.metrics(), "execution_mode": self.mode}


def experiment_options(dataset, config, mode):
    prompt = FEEDBACK_PROMPT if dataset.purpose == "feedback" else FINAL_PROMPT
    rules = [
        case.inputs["context"].get("rubric") if dataset.purpose == "feedback"
        else case.inputs["request"]["stored_rubric"]
        for case in dataset.examples
    ]
    return {
        "target_name": f"ai_agents.validation.quality:{dataset.purpose}",
        "model": config.model, "prompt_version": digest(prompt),
        "rubric_version": digest(rules), "data_version": dataset.version,
        "model_settings": {
            "execution_mode": mode, "endpoint_sha256": digest(config.base_url),
            "model_revision": config.model_revision, "temperature": 0,
            "max_tokens": 1800 if dataset.purpose == "feedback" else 6000,
            "validation_retries": 1, "sdk_retries": 0,
            "timeout_seconds": config.timeout_seconds,
            "response_format": "json_object" if dataset.purpose == "feedback" else "json_schema",
            "transport": "chat_completions",
        },
    }


async def run_quality_with_client(
    dataset, client, config, *, output_dir, repo, label, langsmith=False,
    mode: Literal["real_model", "test_transport"] = "test_transport",
):
    if mode not in {"real_model", "test_transport"}:
        raise ValueError("unsupported execution mode")
    check_quality_dataset(dataset)
    dataset = dataset.model_copy(deep=True)
    # 사람 품질 점수와 구분되는 구조·근거 검증 성공 확인. 기대 문장은 target에 전달하지 않는다.
    for case in dataset.examples:
        case.checks = [Check(path=["output", "status"], expected="generated")]
    return await run_experiment(
        dataset, QualityTarget(dataset.purpose, client, config, mode),
        output_dir=Path(output_dir), repo=Path(repo), label=label, langsmith=langsmith,
        **experiment_options(dataset, config, mode),
    )


async def run_quality(dataset, config, *, api_key, **kwargs):
    check_quality_dataset(dataset)  # 모든 입력을 네트워크 호출 전에 확인한다.
    async with AsyncOpenAI(
        base_url=config.base_url, api_key=api_key or "self-hosted",
        timeout=config.timeout_seconds, max_retries=0,
    ) as client:
        return await run_quality_with_client(
            dataset, client, config, mode="real_model", **kwargs,
        )

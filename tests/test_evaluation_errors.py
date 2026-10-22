import anyio
import pytest
from pydantic import BaseModel, ValidationError

from work_classifier.evaluation.errors import error_messages
from work_classifier.rubric.builder import RubricError


def test_nested_group_preserves_the_original_rubric_reason():
    cause = RubricError("평가기준 검증 실패: 결과 검증: 필수 근거 누락 ART-09")
    grouped = ExceptionGroup("외부 MCP", [ExceptionGroup("내부 MCP", [cause])])
    assert error_messages(grouped) == [
        "RubricError: 평가기준 검증 실패: 결과 검증: 필수 근거 누락 ART-09"
    ]


@pytest.mark.asyncio
async def test_task_group_error_is_reported_by_cli(monkeypatch, capsys):
    from work_classifier import evaluation_workflow_cli as cli

    async def fail(args):
        async with anyio.create_task_group():
            async with anyio.create_task_group():
                raise RubricError("평가기준 검증 실패: 평가 항목 순서 오류")

    monkeypatch.setattr(cli, "run", fail)
    monkeypatch.setattr("sys.argv", ["evaluation-workflow", "--evaluation-id", "test"])
    # CLI는 자체 이벤트 루프를 생성하므로 별도 스레드에서 실행한다.
    result = await anyio.to_thread.run_sync(cli.main)
    assert result == 2
    assert "RubricError: 평가기준 검증 실패: 평가 항목 순서 오류" in capsys.readouterr().err


def test_validation_input_and_unknown_error_body_are_not_exposed():
    class Input(BaseModel):
        amount: int

    with pytest.raises(ValidationError) as invalid:
        Input(amount="customer-private-text")
    grouped = ExceptionGroup("private group text", [
        invalid.value, RuntimeError("customer-private-text")
    ])
    rendered = "\n".join(error_messages(grouped))
    assert "customer-private-text" not in rendered
    assert "private group text" not in rendered
    assert "스키마 불일치" in rendered
    assert "RuntimeError" in rendered


def test_known_error_secrets_are_masked():
    output = "\n".join(error_messages(ValueError("sk-test-secret Bearer test-token")))
    assert "sk-test-secret" not in output
    assert "test-token" not in output

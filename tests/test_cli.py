import json
from pathlib import Path

from ai_agents.cli import classification as cli

SAMPLE = Path(__file__).resolve().parents[1] / "samples/03_quotation.json"


def invoke(monkeypatch, *args):
    monkeypatch.setattr("sys.argv", ["ai-agents", *map(str, args)])
    return cli.main()


def test_validate_without_api_key(monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert invoke(monkeypatch, "validate", SAMPLE) == 0
    assert "OK" in capsys.readouterr().out


def test_schemas_is_valid_json(monkeypatch, capsys):
    assert invoke(monkeypatch, "schemas") == 0
    schemas = json.loads(capsys.readouterr().out)
    assert schemas["input"]["additionalProperties"] is False
    assert "decision" in schemas["llm_output"]["required"]
    assert "status" in schemas["classification"]["required"]


def test_missing_key_is_actionable(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert invoke(monkeypatch, "classify", SAMPLE) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_invalid_input_does_not_print_private_content(monkeypatch, tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({
        "work_id": "1", "messages": [
            {"id": "m1", "role": "unexpected", "content": "PRIVATE-CONTENT"}
        ]
    }))
    assert invoke(monkeypatch, "validate", path) == 2
    assert "PRIVATE-CONTENT" not in capsys.readouterr().err


def test_output_not_overwritten_before_api_call(monkeypatch, tmp_path, capsys):
    path = tmp_path / "existing.json"
    path.write_text("keep")
    assert invoke(monkeypatch, "classify", SAMPLE, "--output", path) == 2
    assert path.read_text() == "keep"
    assert "이미 존재" in capsys.readouterr().err


def test_output_file_and_stdout(monkeypatch, tmp_path, capsys):
    async def fake_run(args):
        return '{"result": "test-output"}'

    monkeypatch.setattr(cli, "run_classification", fake_run)
    path = tmp_path / "outputs/result.json"
    assert invoke(monkeypatch, "classify", SAMPLE, "--output", path) == 0
    assert json.loads(path.read_text()) == {"result": "test-output"}
    assert json.loads(capsys.readouterr().out) == {"result": "test-output"}


def test_missing_file(monkeypatch, tmp_path, capsys):
    assert invoke(monkeypatch, "validate", tmp_path / "absent.json") == 2
    assert "오류" in capsys.readouterr().err


"""실험 정본에서 검증결과서용 요약 생성. 입력 원문/모델 응답은 보고서에 복사하지 않는다."""

import math

from .runner import compare_experiments


def cell(value):
    text = str(value).replace("\n", " ").replace("\r", " ")
    for token in ("\\", "`", "*", "_", "[", "]", "|", "<", ">"):
        text = text.replace(token, "\\" + token)
    return text


def render_report(after, *, before=None, allow_changes=()):
    # 단독 보고서도 비교와 동일한 정본/ID 검증을 적용한다.
    comparison = compare_experiments(before or after, after, allow_changes=allow_changes)
    for experiment in (before, after):
        if experiment is None:
            continue
        for case in experiment.cases:
            if not math.isfinite(case.duration_ms) or case.duration_ms < 0:
                raise ValueError("invalid recorded duration")

    manifest = after.manifest
    lines = [
        "# 평가 Agent 검증 기록", "",
        "자동 검사 결과를 정리한 문서다. 합격률은 고정된 검사 조건의 통과 비율이며, "
        "LLM 평가 정확도·코칭 유용성·개선율을 뜻하지 않는다.", "",
        "## 실행 근거", "", "| 항목 | 값 |", "| --- | --- |",
    ]
    for title, key in (
        ("실험 ID", "experiment_id"), ("평가셋", "dataset_id"),
        ("평가셋 버전", "dataset_version"), ("평가셋 SHA256", "dataset_sha256"),
        ("실행 target", "target"), ("코드 커밋", "git_commit"),
        ("미커밋 변경 포함", "dirty"), ("작업 트리 SHA256", "working_tree_sha256"),
        ("의존성 잠금 SHA256", "dependency_lock_sha256"), ("모델", "model"),
        ("프롬프트 버전", "prompt_version"), ("기준 버전", "rubric_version"),
        ("자료 버전", "data_version"), ("시작 시각", "started_at"),
        ("완료 시각", "completed_at"),
    ):
        lines.append(f"| {title} | {cell(manifest.get(key, '기록 없음'))} |")
    if str(manifest["target"]).endswith("final_targets:replay_final_evaluation"):
        lines += ["", "이 실행은 **가상 MCP·고정 모델 응답 재생**이다. 실제 DB, RabbitMQ, "
                  "AWS 모델, 백엔드 인증·권한 통합 검증은 포함하지 않는다."]
    if comparison["dirty_warning"]:
        lines += ["", "**미커밋 변경이 포함됐다.** 위 커밋만 checkout해서 같은 코드를 "
                  "재현할 수 없다. 커밋 후 다시 실행한 기록을 최종 증빙으로 사용한다."]
    lines += ["", "## 검사 결과", "", "| 실행 | 전체 | 통과 | 실패 | 미검토 | 통과 비율 |",
              "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for side, label in (("before", "변경 전"), ("after", "현재")):
        if side == "before" and before is None:
            continue
        counts = comparison[side]
        lines.append(
            f"| {label} | {comparison['total']} | {counts['passed']} | {counts['failed']} | "
            f"{counts['unreviewed']} | {comparison[side + '_pass_rate']:.1%} |"
        )
    lines += ["", "미검토 사례도 전체 분모에 포함한다. 기대한 오류를 올바르게 차단한 사례는 "
              "통과일 수 있다. 아래 시간은 로컬 검사 전체 소요 시간이며 모델 지연 시간과 다르다."]
    if before is not None:
        lines += ["", "## 변경 전후 비교", "",
                  f"- 이전 커밋: {cell(before.manifest['git_commit'])}",
                  f"- 현재 커밋: {cell(manifest['git_commit'])}"]
        for label, key in (("실패 → 통과", "improved"), ("통과 → 실패", "regressed"),
                           ("미검토 상태 변경", "review_changed")):
            lines.append(f"- {label}: {', '.join(cell(i) for i in comparison[key]) or '없음'}")
        for key, values in comparison["allowed_changes"].items():
            lines.append(
                f"- 허용한 설정 변경 {cell(key)}: "
                f"{cell(values['before'])} → {cell(values['after'])}"
            )
        if comparison["environment_changes"]:
            lines.append("- 실행 환경/의존성 차이 있음: " + ", ".join(
                cell(key) for key in comparison["environment_changes"]
            ))
    lines += ["", "## 사례별 증빙", "", "| 사례 ID | 판정 | 소요 ms | trace ID |",
              "| --- | --- | ---: | --- |"]
    for case in after.cases:
        lines.append(
            f"| {cell(case.case_id)} | {cell(case.verdict)} | {case.duration_ms:.3f} | "
            f"{cell(case.trace_id)} |"
        )
    lines += ["", "## 추가 확인", "",
              "- experiment.json, manifest.json, dataset.json, results.jsonl, report.csv와 "
              "traces/를 함께 보관한다. 이 요약에는 입력 원문을 포함하지 않는다.",
              "- LangSmith 전송 여부·성공은 로컬 trace ID 존재만으로 증명되지 않는다. "
              "원격 실험/trace 확인 여부는 별도로 기록한다.",
              "- 실제 모델 출력의 적절성은 고정 평가셋에 대한 담당자 검토로 기록한다.", ""]
    return "\n".join(lines)

"""가상 MCP 지연을 넣어 40개 사례의 자료 수집만 측정한다. 서버·LLM 호출 없음.

실행: .venv/bin/python tests/benchmark_evaluation_collection.py --output outputs/collection.json
"""

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path
from statistics import median
from time import perf_counter

from test_evaluation_agent import ToolFixture, collect
from test_mcp_adapter import CASES

from ai_agents.shared.workflow_models import content_hash


class DelayedTools(ToolFixture):
    def __init__(self, case, delay):
        super().__init__(case=case)
        self.delay = delay
        self.active = self.peak = 0

    async def delayed(self, caller, name, arguments):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            return await caller(name, arguments)
        finally:
            self.active -= 1

    async def business(self, name, arguments):
        return await self.delayed(super().business, name, arguments)

    async def rag(self, name, arguments):
        return await self.delayed(super().rag, name, arguments)


async def benchmark(delay, repeats):
    rows = []
    for case in CASES:
        durations = []
        fingerprints = set()
        for _ in range(repeats):
            fixture = DelayedTools(case, delay)
            start = perf_counter()
            evidence, materials = await collect(fixture)
            durations.append((perf_counter() - start) * 1000)
            assert materials.evidence_sha256 == content_hash(evidence)
            fingerprints.add(content_hash(materials))
        assert len(fingerprints) == 1, "반복 실행에서 수집 결과가 달라졌습니다."
        rows.append({
            "case_id": case["case_id"],
            "work_type": case["work_type"],
            "median_ms": round(median(durations), 3),
            "calls": dict(sorted(Counter(name for _, name, _ in fixture.calls).items())),
            "peak_in_flight": fixture.peak,
            "output_sha256": fingerprints.pop(),
        })
    return {
        "scope": "fixture evidence/materials collection; no backend or LLM",
        "delay_per_call_ms": delay * 1000,
        "repeats": repeats,
        "cases": rows,
        "median_case_ms": round(median(row["median_ms"] for row in rows), 3),
        "sum_case_medians_ms": round(sum(row["median_ms"] for row in rows), 3),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--delay-ms", type=float, default=20)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.delay_ms < 0 or args.repeats < 1:
        parser.error("delay-ms >= 0, repeats >= 1이어야 합니다.")
    if args.output.exists():
        parser.error("기존 결과 파일은 덮어쓰지 않습니다.")
    result = asyncio.run(benchmark(args.delay_ms / 1000, args.repeats))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k != "cases"}, ensure_ascii=False))

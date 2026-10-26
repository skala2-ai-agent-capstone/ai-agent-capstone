import asyncio
import json
from collections import Counter

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from test_evaluation_agent import ToolFixture, collect
from test_mcp_adapter import CASES

from ai_agents.evaluation_agent.final import materials as materials_module
from ai_agents.integrations import mcp_adapter
from ai_agents.integrations.mcp_adapter import MCPFailure, MCPToolError
from ai_agents.integrations.mcp_client import MCPGateway
from ai_agents.integrations.parallel_reads import MAX_PARALLEL_READS, read_in_parallel


@pytest.mark.asyncio
async def test_bounded_workers_preserve_input_order_when_reads_finish_out_of_order():
    ready = asyncio.Event()
    release = [asyncio.Event() for _ in range(17)]
    started, finished = [], []
    active = peak = 0

    async def read(index):
        nonlocal active, peak
        started.append(index)
        active += 1
        peak = max(peak, active)
        if active == MAX_PARALLEL_READS:
            ready.set()
        try:
            await release[index].wait()
            finished.append(index)
            return index * 2
        finally:
            active -= 1

    task = asyncio.create_task(read_in_parallel(read, range(17)))
    await asyncio.wait_for(ready.wait(), 1)
    assert started == list(range(MAX_PARALLEL_READS))
    # 첫 요청이 끝나기 전에 나머지 작업을 끝내도 반환 순서는 변하지 않는다.
    for event in release[1:]:
        event.set()
    await asyncio.sleep(0)
    release[0].set()
    assert await task == [index * 2 for index in range(17)]
    assert finished != list(range(17))
    assert peak == MAX_PARALLEL_READS
    assert active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_parent", [False, True])
async def test_failure_and_cancellation_drain_in_flight_reads(cancel_parent):
    ready = asyncio.Event()
    never = asyncio.Event()
    started, cleaned = [], []
    failure = MCPToolError(MCPFailure.model_validate({
        "ok": False,
        "error": {"code": "TEMPORARY_FAILURE", "message": "test", "retryable": True},
    }))

    async def read(index):
        started.append(index)
        if len(started) == MAX_PARALLEL_READS:
            ready.set()
        try:
            await ready.wait()
            if index == 0 and not cancel_parent:
                raise failure
            await never.wait()
        finally:
            cleaned.append(index)

    task = asyncio.create_task(read_in_parallel(read, range(12)))
    await asyncio.wait_for(ready.wait(), 1)
    if cancel_parent:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(MCPToolError) as caught:
            await task
        assert caught.value is failure
        assert caught.value.retryable is True
    assert sorted(started) == sorted(cleaned) == list(range(MAX_PARALLEL_READS))


async def serial_reads(read, items):
    return [await read(item) for item in items]


class YieldingTools(ToolFixture):
    async def business(self, name, arguments):
        await asyncio.sleep(0)
        return await super().business(name, arguments)

    async def rag(self, name, arguments):
        await asyncio.sleep(0)
        return await super().rag(name, arguments)


def call_counts(fixture):
    return Counter(json.dumps(call, sort_keys=True) for call in fixture.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case_id"])
async def test_parallel_collection_preserves_all_40_case_outputs_and_requests(case, monkeypatch):
    parallel_fixture = YieldingTools(case=case)
    evidence, materials = await collect(parallel_fixture)
    monkeypatch.setattr(mcp_adapter, "read_in_parallel", serial_reads)
    monkeypatch.setattr(materials_module, "read_in_parallel", serial_reads)
    serial_fixture = YieldingTools(case=case)
    serial_evidence, serial_materials = await collect(serial_fixture)
    # JSON 전체(리스트 순서·근거 해시 포함) 및 도구 인자가 동일해야 한다.
    assert evidence.model_dump_json() == serial_evidence.model_dump_json()
    assert materials.model_dump_json() == serial_materials.model_dump_json()
    assert call_counts(parallel_fixture) == call_counts(serial_fixture)


@pytest.mark.asyncio
async def test_optional_not_found_is_recorded_in_snapshot_order(monkeypatch):
    class MissingReferences(YieldingTools):
        async def rag(self, name, arguments):
            if "chunk_id" in arguments:
                return await super().rag(name, arguments)
            # 첫 문서는 느리게 실패하여 완료 순서를 바꾼다.
            first = self.context.snapshot["reference_refs"][0]["doc_id"]
            if arguments["doc_id"] == first:
                for _ in range(10):
                    await asyncio.sleep(0)
            return {
                "ok": False,
                "error": {"code": "NOT_FOUND", "message": "test", "retryable": False},
            }

    fixture = MissingReferences()
    assert len(fixture.context.snapshot["reference_refs"]) > 1
    _, parallel = await collect(fixture)
    monkeypatch.setattr(mcp_adapter, "read_in_parallel", serial_reads)
    monkeypatch.setattr(materials_module, "read_in_parallel", serial_reads)
    _, serial = await collect(MissingReferences())
    assert parallel.model_dump_json() == serial.model_dump_json()
    assert len(parallel.missing_sources) >= len(fixture.context.snapshot["reference_refs"])


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["NOT_FOUND", "FORBIDDEN", "TEMPORARY_FAILURE"])
async def test_optional_quote_only_tolerates_not_found(code):
    fixture = YieldingTools("quote")
    original = fixture.business

    async def missing_quote(name, arguments):
        if name == "compute_reference_quote":
            return {
                "ok": False,
                "error": {
                    "code": code, "message": "test", "retryable": code == "TEMPORARY_FAILURE",
                },
            }
        return await original(name, arguments)

    fixture.business = missing_quote
    if code != "NOT_FOUND":
        with pytest.raises(MCPToolError) as caught:
            await collect(fixture)
        assert caught.value.code == code
        assert caught.value.retryable == (code == "TEMPORARY_FAILURE")
        return
    _, materials = await collect(fixture)
    arguments = {
        "work_id": fixture.context.work_id, "artifact_version": fixture.context.target.version,
    }
    assert "compute_reference_quote:" + json.dumps(
        arguments, ensure_ascii=False, sort_keys=True,
    ) in materials.missing_sources
    assert "reference_quote" not in materials.details


@pytest.mark.asyncio
async def test_parallel_reads_share_authenticated_sdk_connection(monkeypatch):
    server = MCPServer("parallel-read-test")
    ready = asyncio.Event()
    active = peak = 0
    auth = []

    @server.tool()
    async def get_reference(doc_id: str, version: str) -> dict:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == MAX_PARALLEL_READS:
            ready.set()
        try:
            await asyncio.wait_for(ready.wait(), 2)
            return {
                "ok": True,
                "data": {"doc_id": doc_id, "version": version},
                "meta": {"run_id": "same-run", "snapshot": {"version": "v1"}},
            }
        finally:
            active -= 1

    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def asgi(scope, receive, send):
        if scope["type"] == "http":
            auth.append(dict(scope["headers"]).get(b"authorization"))
        await app(scope, receive, send)

    monkeypatch.setattr(
        "ai_agents.integrations.mcp_client.create_mcp_http_client",
        lambda headers: httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=asgi), headers=headers,
        ),
    )
    async with app.router.lifespan_context(app):
        async with MCPGateway(
            "test-token", business_url="http://testserver/mcp", rag_url="http://testserver/mcp",
        ) as gateway:
            async def read(index):
                return await gateway.call(
                    "rag", "get_reference", {"doc_id": f"ref-{index}", "version": "v1"},
                )

            results = await read_in_parallel(read, range(8))
    assert [result["data"]["doc_id"] for result in results] == [f"ref-{i}" for i in range(8)]
    assert peak == MAX_PARALLEL_READS
    assert active == 0
    assert auth and all(value == b"Bearer test-token" for value in auth)

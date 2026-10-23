"""두 MCP 서버가 같이 쓰는 실행 골격: 응답 봉투, 오류, 실행 토큰 확인, 장애 주입, 마스킹, 감사 기록."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Callable

from . import runtoken

log = logging.getLogger("mcp-common")

# ── 오류 (명세 2장 오류 코드) ─────────────────────────────────────────────
RETRYABLE = {"TEMPORARY_FAILURE": True}


class MCPFail(Exception):
    def __init__(self, code: str, message: str, retryable: bool | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = RETRYABLE.get(code, False) if retryable is None else retryable


def not_found(what: str) -> MCPFail:
    return MCPFail("NOT_FOUND", f"{what}을(를) 찾을 수 없습니다")


def forbidden(msg: str = "실행 토큰 범위 밖의 요청입니다") -> MCPFail:
    return MCPFail("FORBIDDEN", msg)


def version_conflict(msg: str) -> MCPFail:
    return MCPFail("VERSION_CONFLICT", msg)


def validation(msg: str) -> MCPFail:
    return MCPFail("VALIDATION_ERROR", msg)


class Result:
    """도구 구현이 meta를 더 붙이고 싶을 때 반환한다 (예: duplicate)."""

    def __init__(self, data: Any, **meta: Any):
        self.data = data
        self.meta = meta


# ── 마스킹 (명세 4.1) ───────────────────────────────────────────────────
class Masker:
    def __init__(self, policy_rules: dict):
        self.rules = [
            (r["rule_id"], re.compile(r["pattern"]), r["replace_with"])
            for r in policy_rules["rules"]
            if r["kind"] == "masking"
        ]
        # 이메일을 먼저 치환해야 이메일 안의 숫자열이 연락처로 오인되지 않는다
        order = {"MASK-EMAIL": 0, "MASK-PHONE": 1, "MASK-PERSON": 2}
        self.rules.sort(key=lambda r: order.get(r[0], 9))

    def mask(self, text: str | None) -> str | None:
        if not isinstance(text, str):
            return text
        for _, pat, rep in self.rules:
            text = pat.sub(rep, text)
        return text

    def count(self, text: str | None) -> dict[str, int]:
        out: dict[str, int] = {}
        if not isinstance(text, str):
            return out
        for rid, pat, rep in self.rules:
            n = len(pat.findall(text))
            if n:
                out[rid] = n
            text = pat.sub(rep, text)
        return out

    def mask_obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.mask(o)
        if isinstance(o, list):
            return [self.mask_obj(x) for x in o]
        if isinstance(o, dict):
            return {k: self.mask_obj(v) for k, v in o.items()}
        return o

    def has_sensitive(self, text: str) -> list[str]:
        return [rid for rid, pat, _ in self.rules if rid in ("MASK-PHONE", "MASK-EMAIL") and pat.search(text or "")]


# ── 장애 주입 (명세 6.2) ────────────────────────────────────────────────
class FaultInjector:
    """raw.fault_injection 또는 FAULT_PLAN 환경 변수를 읽어 호출 횟수 기준으로 오류를 낸다.

    FAULT_MODE=case  : 케이스 데이터의 fault_injection을 업무(work_id)별로 적용 (기본, 테스트 모드)
    FAULT_MODE=off   : 주입하지 않음
    FAULT_PLAN='[{"work_id":"W-E2E-RFP-01","target":"business-mcp.get_conversation","times":1}]'
                     : 케이스 계획에 더해 적용 (work_id 생략 시 모든 업무)
    호출 횟수는 실행(run_id)·도구별로 센다. 새 실행 토큰을 받으면 처음부터 다시 센다.
    """

    def __init__(self, server_name: str, case_plans: dict[str, list[dict]]):
        self.server = server_name
        self.mode = os.environ.get("FAULT_MODE", "case").lower()
        self.plans: list[dict] = []
        if self.mode != "off":
            for work_id, plans in case_plans.items():
                for p in plans:
                    self.plans.append({**p, "work_id": work_id})
            extra = os.environ.get("FAULT_PLAN")
            if extra:
                self.plans.extend(json.loads(extra))
        self.plans = [p for p in self.plans if str(p.get("target", "")).startswith(self.server + ".")]
        self.calls: dict[tuple[str, str], int] = {}
        self.lock = threading.Lock()

    def reset(self) -> None:
        with self.lock:
            self.calls.clear()

    def check(self, run_id: str, work_id: str, tool: str) -> None:
        if not self.plans:
            return
        with self.lock:
            n = self.calls.get((run_id, tool), 0) + 1
            self.calls[(run_id, tool)] = n
        for p in self.plans:
            if p["target"] != f"{self.server}.{tool}":
                continue
            if p.get("work_id") and p["work_id"] != work_id:
                continue
            start = int(p.get("on_call", 1))
            if start <= n < start + int(p.get("times", 1)):
                code = p.get("error", "TEMPORARY_FAILURE")
                raise MCPFail(code, f"[장애 주입] {tool} 일시 오류 ({n}번째 호출)")


# ── 감사 기록 (명세 2장) ────────────────────────────────────────────────
def summarize_args(args: dict) -> dict:
    """민감정보 원문이 로그에 남지 않도록 긴 문자열은 길이만 남긴다."""
    out = {}
    for k, v in args.items():
        if isinstance(v, str):
            out[k] = v if len(v) <= 40 and "\n" not in v else f"<{len(v)}자>"
        elif isinstance(v, list):
            out[k] = f"<목록 {len(v)}건>"
        elif isinstance(v, dict):
            out[k] = f"<객체 키 {len(v)}개>"
        else:
            out[k] = v
    return out


class Audit:
    """감사 기록: 표준 출력(JSON 한 줄)과 선택적 저장소(sink: dict → None, 예: PostgreSQL audit_log)."""

    def __init__(self, sink: Callable[[dict], None] | None, server: str):
        self.sink = sink
        self.server = server

    def record(self, run_id: str | None, evaluation_id: str | None, tool: str, args: dict, code: str, ms: int):
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "server": self.server,
            "run_id": run_id,
            "evaluation_id": evaluation_id,
            "tool": tool,
            "input": summarize_args(args),
            "result_code": code,
            "elapsed_ms": ms,
        }
        log.info("audit %s", json.dumps(row, ensure_ascii=False))
        if self.sink is not None:
            try:
                self.sink(row)
            except Exception:  # 감사 저장 실패가 도구 응답을 막지 않게 한다 (로그에는 이미 남음)
                log.exception("audit sink failed")


# ── 도구 실행 골격 ─────────────────────────────────────────────────────
# 실행 토큰 종류(claims.kind). 예전 토큰에는 kind가 없어 EVALUATION으로 본다
EVALUATION = "evaluation"   # 최종 평가
LIVE = "live"               # 중간 점검 (최근 메시지 5개)
RUBRIC = "rubric"           # 평가 기준 작성
ALL_KINDS = (EVALUATION, LIVE, RUBRIC)


class ToolRuntime:
    def __init__(self, server: str, secret: str, faults: FaultInjector, audit: Audit, masker: Masker,
                 before: Callable[[], None] | None = None):
        self.before = before
        self.server = server
        self.secret = secret
        self.faults = faults
        self.audit = audit
        self.masker = masker

    def claims_from(self, ctx: Any) -> dict:
        headers = None
        try:
            headers = ctx.headers
        except Exception:
            headers = None
        token = None
        if headers:
            auth = headers.get("authorization") or headers.get("Authorization")
            if auth and auth.lower().startswith("bearer "):
                token = auth[7:]
            token = token or headers.get("x-run-token")
        token = token or os.environ.get("RUN_TOKEN")  # stdio 실행용
        if not token:
            raise forbidden("실행 토큰이 없습니다 (Authorization: Bearer <run_token>)")
        try:
            return runtoken.verify(token, self.secret)
        except runtoken.TokenError as e:
            raise forbidden(str(e))

    async def run(self, ctx: Any, tool: str, args: dict, fn: Callable[[dict], Any],
                  kinds: tuple[str, ...] = (EVALUATION,)) -> dict:
        """kinds: 이 도구를 부를 수 있는 실행 토큰 종류 (EVALUATION·LIVE·RUBRIC)."""
        t0 = time.time()
        claims: dict = {}
        try:
            if self.before:
                self.before()
            claims = self.claims_from(ctx)
            kind = claims.get("kind", EVALUATION)
            if kind not in kinds:
                raise forbidden(f"{tool}는 이 실행 토큰({kind})으로 호출할 수 없습니다")
            self.faults.check(claims["run_id"], claims["work_id"], tool)
            res = fn(claims)
            extra = {}
            if isinstance(res, Result):
                extra, res = res.meta, res.data
            env = {
                "ok": True,
                "data": res,
                "meta": {"run_id": claims["run_id"], "snapshot": claims["snapshot"], **extra},
            }
            code = "DUPLICATE_REQUEST" if extra.get("duplicate") else "OK"
        except MCPFail as e:
            env = {"ok": False, "error": {"code": e.code, "message": e.message, "retryable": e.retryable}}
            if claims:
                env["meta"] = {"run_id": claims.get("run_id")}
            code = e.code
        except Exception as e:  # 구현 결함. 원인은 서버 로그로만 남긴다.
            log.exception("tool %s failed", tool)
            env = {"ok": False, "error": {"code": "INTERNAL_ERROR", "message": f"서버 내부 오류 ({type(e).__name__})", "retryable": False}}
            code = "INTERNAL_ERROR"
        self.audit.record(claims.get("run_id"), claims.get("evaluation_id"), tool, args, code, int((time.time() - t0) * 1000))
        return env


def eval_id_for(work_id: str, version: str) -> str:
    """평가 건 ID 규칙: EVAL-{업무 ID에서 W- 제외}-{산출물 버전}. 적재기와 business-mcp가 같이 쓴다."""
    return f"EVAL-{work_id[2:] if work_id.startswith('W-') else work_id}-{version}"


def version_number(version: str | None) -> int:
    """평가 기준 버전 'v12' → 12 (형식이 다르면 0). 현행 기준을 고를 때 두 서버가 같이 쓴다."""
    m = re.fullmatch(r"v(\d+)", str(version or ""))
    return int(m.group(1)) if m else 0


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_cases(data_dir: str) -> list[dict]:
    with open(os.path.join(data_dir, "all_cases.jsonl"), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]

"""EC2에 올리기 전 배포 설정 점검. 호스트에서 docker CLI로 돌린다 (컨테이너를 띄우지 않는다).

- .env.example이 compose와 코드가 읽는 설정을 모두 담는가
- .env.example 빈칸을 채운 값으로 profile(기본·llm·mockllm·test)마다 compose 설정이 풀리는가
- 서버 밖으로 열리는 포트가 MCP 2개와 llm-proxy뿐인가 (Postgres·Chroma·vLLM은 127.0.0.1)
- vLLM GPU 예약·기동 대기·모델 캐시, llm-proxy ↔ vLLM 키·모델 이름이 맞는가
- --online: vLLM 이미지 태그와 Hugging Face 모델이 실제로 있는가

    python tests/check_deploy.py [--online]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(ROOT)
COMPOSE = os.path.join(ROOT, "docker-compose.yml")
PUBLIC_PORTS = {("business-mcp", 8001), ("rag-mcp", 8002), ("llm-proxy", 4000)}
CODE_DIRS = [os.path.join(ROOT, d) for d in ("common", "business_mcp", "rag_mcp", "db")] + [os.path.join(REPO, "llm_proxy", "proxy")]
# 코드가 읽지만 compose가 서비스마다 정해 주거나 로컬 개발에서만 쓰는 값
SET_BY_COMPOSE = {"DATABASE_URL", "CHROMA_HOST", "CHROMA_PORT", "PORT", "UPSTREAM_URL", "UPSTREAM_API_KEY", "DATA_DIR"}
LOCAL_ONLY = {"HOST", "MCP_TRANSPORT", "RUN_TOKEN", "CHROMA_FAKE_PATH", "CHROMA_AUTH_TOKEN"}
results: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail: str = "") -> bool:
    results.append((name, bool(cond), detail))
    return bool(cond)


def env_example() -> dict[str, str]:
    out = {}
    for line in open(os.path.join(ROOT, ".env.example"), encoding="utf-8"):
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def filled(example: dict[str, str]) -> dict[str, str]:
    """빈칸을 운영과 같은 모양의 임의 값으로 채운다 (.env를 처음 만들 때와 같은 절차)."""
    env = dict(example)
    for k, v in env.items():
        if v == "":
            env[k] = {"PROXY_KEYS": f"workspace-backend:{secrets.token_urlsafe(16)},eval-agent:{secrets.token_urlsafe(16)}",
                      "FAULT_PLAN": "", "ALLOWED_HOSTS": "", "HF_TOKEN": ""}.get(k, secrets.token_urlsafe(32))
    return env


def compose_config(env: dict[str, str], profiles: list[str]) -> dict | None:
    """임시 폴더에 compose 파일과 .env(env 값)를 두고 설정을 푼다.
    .env를 아직 만들지 않은 새 서버에서도 돌고, 이 저장소의 .env 값에 영향받지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(COMPOSE, os.path.join(tmp, "docker-compose.yml"))
        with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
            f.write("".join(f"{k}={v}\n" for k, v in env.items()))
        args = ["docker", "compose", "-f", os.path.join(tmp, "docker-compose.yml"), "--env-file", os.path.join(tmp, ".env")]
        for p in profiles:
            args += ["--profile", p]
        # 셸에 같은 이름의 변수가 있으면 .env보다 우선하므로 뺀다
        shell = {k: v for k, v in os.environ.items() if k not in env and k != "POSTGRES_PASSWORD"}
        r = subprocess.run(args + ["config", "--format", "json", "--no-path-resolution"], capture_output=True, text=True,
                           env=shell, cwd=tmp)
        if r.returncode != 0:
            return {"_error": r.stderr.strip()[:300]}
        return json.loads(r.stdout)


def seconds(duration: str) -> int:
    """compose 기간 표기('30m', '1h30m0s') → 초."""
    unit = {"h": 3600, "m": 60, "s": 1}
    return sum(int(n) * unit[u] for n, u in re.findall(r"(\d+)([hms])", duration or ""))


def head_ok(url: str, headers: dict | None = None) -> tuple[bool, str]:
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200, str(r.status)
    except urllib.error.HTTPError as e:
        return False, str(e.code)
    except Exception as e:  # 네트워크 없음 등
        return False, type(e).__name__


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--online", action="store_true", help="Docker Hub·Hugging Face에서 이미지 태그와 모델이 있는지 확인")
    a = ap.parse_args()

    # 1) .env.example이 compose·코드 설정을 모두 담는가
    example = env_example()
    text = open(COMPOSE, encoding="utf-8").read()
    compose_vars = set(re.findall(r"(?<!\$)\$\{([A-Z_][A-Z0-9_]*)", text))
    check(".env.example에 compose가 쓰는 변수 전부", compose_vars <= set(example), str(sorted(compose_vars - set(example))))
    code_vars = set()
    for d in CODE_DIRS:
        for dirpath, _, files in os.walk(d):
            for fn in files:
                if fn.endswith(".py"):
                    src = open(os.path.join(dirpath, fn), encoding="utf-8").read()
                    code_vars |= set(re.findall(r"os\.(?:environ\.get|getenv)\(\s*\"([A-Z_][A-Z0-9_]*)\"", src))
                    code_vars |= set(re.findall(r"os\.environ\[\"([A-Z_][A-Z0-9_]*)\"\]", src))
    missing = code_vars - set(example) - SET_BY_COMPOSE - LOCAL_ONLY
    check(".env.example에 코드가 읽는 설정 전부", not missing, str(sorted(missing)))
    required = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*):\?", text))
    check(".env.example: compose 필수 변수는 빈칸(비밀값을 커밋하지 않음)", all(example.get(k) == "" for k in required),
          str({k: example.get(k) for k in required}))

    # 2) profile마다 compose 설정이 풀리는가
    env = filled(example)
    want = {(): {"postgres", "chroma", "db-init", "business-mcp", "rag-mcp"},
            ("llm",): {"vllm", "llm-proxy"}, ("mockllm",): {"mock-llm", "llm-proxy"}, ("test",): {"verify"}}
    configs = {}
    for profiles, extra in want.items():
        cfg = compose_config(env, list(profiles))
        name = "+".join(profiles) or "기본"
        ok = check(f"compose 설정 풀림 [{name}]", cfg and "_error" not in cfg, (cfg or {}).get("_error", ""))
        if ok:
            services = set(cfg["services"])
            check(f"compose 서비스 [{name}]", (want[()] | extra) <= services, str(sorted(services)))
            configs[profiles] = cfg
    unset = compose_config({k: v for k, v in env.items() if k != "POSTGRES_PASSWORD"}, [])
    check("POSTGRES_PASSWORD가 비면 compose가 거부", unset and "_error" in unset)

    # 3) 포트 노출
    cfg = configs.get(("llm",))
    if cfg:
        exposed, local = [], []
        for name, svc in cfg["services"].items():
            for p in svc.get("ports") or []:
                (local if p.get("host_ip") == "127.0.0.1" else exposed).append((name, int(p["target"])))
        check("서버 밖으로 여는 포트 = business-mcp 8001·rag-mcp 8002·llm-proxy 4000", set(exposed) <= PUBLIC_PORTS, str(exposed))
        check("Postgres·Chroma·vLLM 포트는 127.0.0.1에만", {"postgres", "chroma", "vllm"} <= {n for n, _ in local}, str(local))

        # 4) vLLM·llm-proxy
        v, p = cfg["services"]["vllm"], cfg["services"]["llm-proxy"]
        cmd = " ".join(v.get("command") or [])
        devices = (((v.get("deploy") or {}).get("resources") or {}).get("reservations") or {}).get("devices") or []
        check("vLLM: NVIDIA GPU 예약", any(d.get("driver") == "nvidia" and "gpu" in (d.get("capabilities") or []) for d in devices),
              str(devices))
        check("vLLM: 모델 캐시 볼륨(/root/.cache/huggingface)",
              any(m.get("target") == "/root/.cache/huggingface" for m in v.get("volumes") or []))
        sp = (v.get("healthcheck") or {}).get("start_period", "")
        check("vLLM: 첫 기동 대기 10분 이상(start_period)", seconds(sp) >= 600, sp)
        check("vLLM: --api-key = llm-proxy UPSTREAM_API_KEY",
              f"--api-key={env['VLLM_API_KEY']}" in cmd and p["environment"].get("UPSTREAM_API_KEY") == env["VLLM_API_KEY"])
        check("vLLM: --served-model-name = SERVED_MODEL_NAME", f"--served-model-name={env['SERVED_MODEL_NAME']}" in cmd, cmd[:200])
        check("vLLM: 도구 호출 파서 켜짐", "--enable-auto-tool-choice" in cmd and "--tool-call-parser=" in cmd)
        check("llm-proxy → http://vllm:8000", p["environment"].get("UPSTREAM_URL") == "http://vllm:8000")
        check("llm-proxy는 적재(db-init) 뒤에 시작",
              (p.get("depends_on") or {}).get("db-init", {}).get("condition") == "service_completed_successfully")
        check("llm-proxy와 MCP가 같은 업무 DB(axeval)", p["environment"]["DATABASE_URL"] == cfg["services"]["business-mcp"]["environment"]["DATABASE_URL"])
    mk = configs.get(("mockllm",))
    if mk:
        aliases = ((mk["services"]["mock-llm"].get("networks") or {}).get("default") or {}).get("aliases") or []
        check("mock-llm은 내부망 이름 vllm으로 붙음 (llm-proxy 설정 그대로)", "vllm" in aliases, str(aliases))
    t = configs.get(("test",))
    if t:
        cmd = " ".join(t["services"]["verify"].get("command") or [])
        check("verify: 검증 4종(spec·db·live_rubric·e2e) 실행",
              all(f"tests/{n}.py" in cmd for n in ("verify_spec", "verify_db", "verify_live_rubric", "verify_e2e")), cmd[:200])

    # 5) 이미지·의존성
    for path in (os.path.join(ROOT, "Dockerfile"), os.path.join(REPO, "llm_proxy", "Dockerfile")):
        src = open(path, encoding="utf-8").read()
        check(f"{os.path.relpath(path, REPO)}: python:3.12-slim, 비루트 사용자", "FROM python:3.12-slim" in src and "USER app" in src)
    for path in (os.path.join(ROOT, "requirements.txt"), os.path.join(REPO, "llm_proxy", "requirements.txt")):
        loose = [line.strip() for line in open(path, encoding="utf-8")
                 if line.strip() and not line.startswith(("#", "-r")) and "==" not in line]
        check(f"{os.path.relpath(path, REPO)}: 버전 고정(==)", not loose, str(loose))
    dockerignore = open(os.path.join(ROOT, ".dockerignore"), encoding="utf-8").read().split()
    check("mcp_servers/.dockerignore가 .env를 이미지에서 뺌", ".env" in dockerignore)

    # 6) 온라인 확인
    if a.online:
        tag = env["VLLM_TAG"]
        ok, why = head_ok(f"https://hub.docker.com/v2/repositories/vllm/vllm-openai/tags/{tag}")
        check(f"Docker Hub에 vllm/vllm-openai:{tag} 있음", ok, why)
        ok, why = head_ok(f"https://huggingface.co/api/models/{env['LLM_MODEL']}")
        check(f"Hugging Face에 모델 {env['LLM_MODEL']} 있음 (공개)", ok, why)

    fails = [r for r in results if not r[1]]
    for name, ok, detail in results:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"  {detail}"))
    print(f"\n검사 {len(results)}건: 통과 {len(results) - len(fails)}, 실패 {len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

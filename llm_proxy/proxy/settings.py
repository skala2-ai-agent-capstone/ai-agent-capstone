"""환경 변수 설정. 비밀값은 .env(git 제외)로만 주입한다."""

import os
from dataclasses import dataclass

# llm_exchanges.stage CHECK와 같은 값
STAGES = frozenset({"workspace_chat", "classify", "rubric", "evaluate", "live_evaluate"})


def parse_openai_stages(raw: str) -> frozenset[str]:
    """'단계,단계' → OpenAI로 보낼 단계. 업무 공간 대화는 로컬 LLM에서만 처리하므로 넣을 수 없다."""
    stages = frozenset(s.strip() for s in raw.split(",") if s.strip())
    if unknown := stages - STAGES:
        raise ValueError(f"OPENAI_STAGES에 모르는 단계가 있습니다: {', '.join(sorted(unknown))}")
    if "workspace_chat" in stages:
        raise ValueError(
            "업무 공간 대화(workspace_chat)는 로컬 LLM에서만 처리합니다. OPENAI_STAGES에서 빼세요."
        )
    return stages


def parse_member_keys(raw: str) -> dict[str, str]:
    """'이름:키,이름:키' → {키: 이름}. 키가 없으면 프록시를 열어 두지 않도록 기동을 막는다."""
    keys: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, key = item.partition(":")
        name, key = name.strip(), key.strip()
        if not sep or not name or not key:
            raise ValueError("PROXY_KEYS 형식은 '이름:키,이름:키'입니다.")
        if key in keys:
            raise ValueError("PROXY_KEYS에 같은 키가 두 번 있습니다.")
        keys[key] = name
    if not keys:
        raise ValueError("PROXY_KEYS가 비어 있습니다. 팀원별 키를 하나 이상 설정하세요.")
    return keys


@dataclass(frozen=True)
class Settings:
    upstream_url: str
    upstream_api_key: str
    member_keys: dict[str, str]
    database_url: str
    upstream_timeout: float = 600.0
    # 이 단계(X-Stage)의 호출은 vLLM 대신 OpenAI로 보낸다. 비어 있으면 모두 vLLM
    openai_stages: frozenset[str] = frozenset()
    openai_url: str = "https://api.openai.com"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    @classmethod
    def from_env(cls) -> "Settings":
        upstream_api_key = os.getenv("UPSTREAM_API_KEY", "")
        if not upstream_api_key:
            raise ValueError(
                "UPSTREAM_API_KEY(vLLM 키)가 비어 있습니다. .env의 VLLM_API_KEY를 채우세요."
            )
        openai_stages = parse_openai_stages(os.getenv("OPENAI_STAGES", ""))
        openai_api_key = os.getenv("OPENAI_API_KEY", "")
        if openai_stages and not openai_api_key:
            raise ValueError("OPENAI_STAGES를 쓰려면 OPENAI_API_KEY가 필요합니다.")
        return cls(
            upstream_url=os.getenv("UPSTREAM_URL", "http://vllm:8000").rstrip("/"),
            upstream_api_key=upstream_api_key,
            member_keys=parse_member_keys(os.getenv("PROXY_KEYS", "")),
            database_url=os.environ["DATABASE_URL"],
            upstream_timeout=float(os.getenv("UPSTREAM_TIMEOUT", "600")),
            openai_stages=openai_stages,
            openai_url=os.getenv("OPENAI_UPSTREAM_URL", "https://api.openai.com").rstrip("/"),
            openai_api_key=openai_api_key,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        )

"""환경 변수 설정. 비밀값은 .env(git 제외)로만 주입한다."""

import os
from dataclasses import dataclass


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

    @classmethod
    def from_env(cls) -> "Settings":
        upstream_api_key = os.getenv("UPSTREAM_API_KEY", "")
        if not upstream_api_key:
            raise ValueError(
                "UPSTREAM_API_KEY(vLLM 키)가 비어 있습니다. .env의 VLLM_API_KEY를 채우세요."
            )
        return cls(
            upstream_url=os.getenv("UPSTREAM_URL", "http://vllm:8000").rstrip("/"),
            upstream_api_key=upstream_api_key,
            member_keys=parse_member_keys(os.getenv("PROXY_KEYS", "")),
            database_url=os.environ["DATABASE_URL"],
            upstream_timeout=float(os.getenv("UPSTREAM_TIMEOUT", "600")),
        )

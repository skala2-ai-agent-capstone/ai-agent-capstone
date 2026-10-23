"""백엔드가 발급한 평가별 토큰 파일을 읽는다. 내부 키/서명 비밀은 Worker가 소유하지 않는다."""

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import AwareDatetime, Field, SecretStr, model_validator

from ..shared.live_feedback_contracts import ID
from ..shared.models import StrictModel


class FinalTokenError(ValueError):
    def __init__(self, code, *, retryable=False):
        self.code, self.retryable = code, retryable
        super().__init__(code)


class TokenEntry(StrictModel):
    token: SecretStr
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def nonempty(self):
        value = self.token.get_secret_value()
        if not value.strip() or len(value) > 16384:
            raise ValueError("invalid token")
        return self


class TokenFile(StrictModel):
    # 백엔드는 처리 중인 평가가 없으면 빈 목록을 쓴다. 빈 파일도 정상이며 조회는 TOKEN_NOT_READY다.
    tokens: dict[ID, TokenEntry] = Field(max_length=1000)


class FileFinalTokenProvider:
    def __init__(self, path, *, now=None):
        self.path = Path(path)
        self.now = now or (lambda: datetime.now(timezone.utc))

    def read(self):
        try:
            # 읽기마다 새 파일을 열어 백엔드/운영자가 원자적으로 교체한 발급 결과를 반영한다.
            with self.path.open("rb") as stream:
                raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("oversized file")
            return TokenFile.model_validate(json.loads(raw))
        except OSError:
            raise FinalTokenError("TOKEN_FILE_UNAVAILABLE", retryable=True) from None
        except (ValueError, TypeError):
            raise FinalTokenError("TOKEN_FILE_INVALID") from None

    async def __call__(self, event):
        entry = self.read().tokens.get(event.evaluation_id)
        if entry is None:
            raise FinalTokenError("TOKEN_NOT_READY", retryable=True)
        if entry.expires_at <= self.now():
            raise FinalTokenError("TOKEN_EXPIRED", retryable=True)
        return entry.token

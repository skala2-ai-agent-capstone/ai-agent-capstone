"""CLI의 프로젝트 설정. .env의 명시 설정을 프로세스 기본값보다 우선한다."""

import os
from pathlib import Path

from dotenv import dotenv_values


def setting(name: str, default: str = "") -> str:
    values = dotenv_values(Path.cwd() / ".env")
    return values.get(name) or os.getenv(name, default)

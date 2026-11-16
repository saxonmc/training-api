import pytest
from pydantic import ValidationError

from training_api.config import Settings


def test_settings_require_redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/test")
    monkeypatch.delenv("REDIS_URL", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)

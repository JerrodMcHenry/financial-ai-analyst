import pytest

from financial_analyst.config import Settings


@pytest.fixture
def settings() -> Settings:
    # _env_file=None keeps tests independent of any developer's local .env.
    return Settings(_env_file=None, sec_user_agent="Test Suite test@example.com")

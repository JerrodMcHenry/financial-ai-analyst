import pytest
from pydantic import ValidationError

from financial_analyst.config import Settings


@pytest.mark.parametrize(
    "overrides",
    [
        {"sec_user_agent": "my-scraper"},  # no contact email
        {"sec_user_agent": "Test test@example.com", "sec_max_requests_per_second": 11},
    ],
)
def test_rejects_settings_that_violate_sec_fair_access(overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_user_agent_is_required(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)

"""Environment-based configuration.

Settings are read from environment variables (or a local `.env` file) and validated
once at startup. Business logic receives a `Settings` object and never reads
`os.environ` directly.
"""

import re
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# SEC asks automated clients to identify themselves as "Name contact@example.com".
_CONTACT_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    sec_user_agent: str
    # SEC's published fair-access limit is 10 requests/second; never allow more.
    sec_max_requests_per_second: float = Field(default=4.0, gt=0, le=10)
    # Must be controlled by this application and not writable by untrusted users.
    filing_cache_dir: Path = Path("data/sec-cache")

    @field_validator("sec_user_agent")
    @classmethod
    def _require_contact_email(cls, value: str) -> str:
        value = value.strip()
        if not _CONTACT_EMAIL.search(value):
            raise ValueError(
                "must identify you and include a contact email, e.g. 'Jane Doe jane@example.com'"
            )
        return value

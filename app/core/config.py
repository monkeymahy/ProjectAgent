from __future__ import annotations

from pathlib import Path
import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = BASE_DIR / "config.yml"


def _load_yaml() -> dict:
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


_raw = _load_yaml()


class LLMConfig(BaseModel):
    api_key: str = _raw.get("LLM_API_KEY", "")
    base_url: str = _raw.get("LLM_BASE_URL", "https://api.openai.com/v1")
    model: str = _raw.get("LLM_MODEL", "gpt-4o-mini")
    temperature: float = float(_raw.get("LLM_TEMPERATURE", 0.4))
    max_tokens: int = int(_raw.get("LLM_MAX_TOKENS", 2000))
    timeout: int = int(_raw.get("LLM_TIMEOUT", 60))


class Settings(BaseSettings):
    redis_url: str = _raw.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    database_url: str = _raw.get("DATABASE_URL", "sqlite:///./storage/projshow.db")

    max_repo_size_mb: int = int(_raw.get("MAX_REPO_SIZE_MB", 200))
    clone_depth: int = int(_raw.get("CLONE_DEPTH", 1))
    clone_timeout: int = int(_raw.get("CLONE_TIMEOUT", 120))
    max_tree_entries: int = int(_raw.get("MAX_TREE_ENTRIES", 200))
    max_readme_chars: int = int(_raw.get("MAX_README_CHARS", 8000))

    eager_mode: bool = bool(_raw.get("EAGER_MODE", True))

    # tForum SSO
    tforum_base_url: str = _raw.get("TFORUM_BASE_URL", "http://localhost:8081")
    sso_cookie_secret: str = _raw.get("SSO_COOKIE_SECRET", "projshow-sso-secret-change-me")
    sso_session_ttl: int = int(_raw.get("SSO_SESSION_TTL", 604800))
    projectagent_public_url: str = _raw.get("PROJECTAGENT_PUBLIC_URL", "http://localhost:8765")

    storage_dir: Path = BASE_DIR / "storage"
    repos_dir: Path = BASE_DIR / "storage" / "repos"
    uploads_dir: Path = BASE_DIR / "storage" / "uploads"
    pages_dir: Path = BASE_DIR / "storage" / "pages"

    llm: LLMConfig = LLMConfig()

    def ensure_dirs(self) -> None:
        for d in (self.storage_dir, self.repos_dir, self.uploads_dir, self.pages_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()

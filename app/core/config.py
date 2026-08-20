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
    max_tokens: int = int(_raw.get("LLM_MAX_TOKENS", 8000))
    timeout: int = int(_raw.get("LLM_TIMEOUT", 60))


class Settings(BaseSettings):
    redis_url: str = _raw.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    database_url: str = _raw.get("DATABASE_URL", "sqlite:///./storage/projshow.db")

    max_repo_size_mb: int = int(_raw.get("MAX_REPO_SIZE_MB", 200))
    clone_depth: int = int(_raw.get("CLONE_DEPTH", 1))
    clone_timeout: int = int(_raw.get("CLONE_TIMEOUT", 120))
    max_tree_entries: int = int(_raw.get("MAX_TREE_ENTRIES", 100000))
    max_readme_chars: int = int(_raw.get("MAX_README_CHARS", 1000000))

    eager_mode: bool = bool(_raw.get("EAGER_MODE", True))

    # tForum SSO
    tforum_base_url: str = _raw.get("TFORUM_BASE_URL", "http://localhost:8081")
    sso_cookie_secret: str = _raw.get("SSO_COOKIE_SECRET", "projshow-sso-secret-change-me")
    sso_session_ttl: int = int(_raw.get("SSO_SESSION_TTL", 604800))
    projectagent_public_url: str = _raw.get("PROJECTAGENT_PUBLIC_URL", "http://localhost:8765")

    # 开源CAX工具库同步服务（提交工具 API）
    toolsync_base_url: str = str(_raw.get("TOOLSYNC_BASE_URL", ""))
    # 工具详情页链接模板（与提交 API 地址无关），{name} 为 URL 编码后的工具名
    toolsync_detail_url: str = str(_raw.get("TOOLSYNC_DETAIL_URL", ""))

    # SkillLab 技能库同步服务（AI 填写 + 提交 API base，如 http://10.35.79.157:3001/api/v1/skill-lab）
    skilllab_base_url: str = str(_raw.get("SKILLLAB_BASE_URL", ""))
    # 技能详情页链接模板（与提交 API 地址无关），{name} 为 URL 编码后的技能名
    skilllab_detail_url: str = str(_raw.get("SKILLLAB_DETAIL_URL", ""))
    # 本地非生产环境可直接用 X-SkillLab-User-Key（如 "admin"）；留空则走 tForum token 换取
    skilllab_user_key: str = str(_raw.get("SKILLLAB_USER_KEY", ""))

    # 工具/技能库展示（git 只读克隆，GitLab push webhook 触发实时刷新）
    library_tool_repo_url: str = str(_raw.get("LIBRARY_TOOL_REPO_URL", ""))
    library_tool_repo_branch: str = str(_raw.get("LIBRARY_TOOL_REPO_BRANCH", ""))
    library_tool_repo_user: str = str(_raw.get("LIBRARY_TOOL_REPO_USER", ""))
    library_tool_repo_token: str = str(_raw.get("LIBRARY_TOOL_REPO_TOKEN", ""))
    library_skill_repo_url: str = str(_raw.get("LIBRARY_SKILL_REPO_URL", ""))
    library_skill_repo_branch: str = str(_raw.get("LIBRARY_SKILL_REPO_BRANCH", ""))
    library_skill_repo_user: str = str(_raw.get("LIBRARY_SKILL_REPO_USER", ""))
    library_skill_repo_token: str = str(_raw.get("LIBRARY_SKILL_REPO_TOKEN", ""))
    # GitLab webhook 密钥（配置在 GitLab 仓库 Webhook 设置里，与这里一致）
    library_webhook_secret: str = str(_raw.get("LIBRARY_WEBHOOK_SECRET", ""))

    storage_dir: Path = BASE_DIR / "storage"
    repos_dir: Path = BASE_DIR / "storage" / "repos"
    uploads_dir: Path = BASE_DIR / "storage" / "uploads"
    pages_dir: Path = BASE_DIR / "storage" / "pages"
    library_repos_dir: Path = BASE_DIR / "storage" / "library"

    llm: LLMConfig = LLMConfig()

    def ensure_dirs(self) -> None:
        for d in (self.storage_dir, self.repos_dir, self.uploads_dir, self.pages_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()

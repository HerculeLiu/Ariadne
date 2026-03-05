from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AppConfig:
    app_host: str
    app_port: int
    cors_allow_origin: str
    model_provider: str
    llm_model: str
    llm_api_key: str
    llm_api_base: str
    llm_chat_path: str
    llm_timeout_sec: int
    llm_max_retries: int
    llm_retry_delay_sec: float
    prompt_dir: str
    knowledge_doc_dir: str
    log_file_path: str
    log_level: str
    prompt_hot_reload: bool
    guest_mode: bool
    local_only: bool
    max_file_size_mb: int
    chunk_max_concurrency: int


def _load_dotenv_if_exists(project_root: Path) -> None:
    env_file = project_root / ".env"
    if not env_file.exists():
        return

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _as_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


def load_config() -> AppConfig:
    project_root = Path(__file__).resolve().parents[3]
    _load_dotenv_if_exists(project_root)

    provider = os.getenv("MODEL_PROVIDER", "mock").lower()
    model_map = {
        "mock": os.getenv("MODEL_MOCK", "mock-v1"),
        "glm": os.getenv("MODEL_GLM", "glm-5"),
        "deepseek": os.getenv("MODEL_DEEPSEEK", "deepseek-reasoner"),
    }
    key_map = {
        "mock": "",
        "glm": os.getenv("GLM_API_KEY", ""),
        "deepseek": os.getenv("DEEPSEEK_API_KEY", ""),
    }
    base_map = {
        "mock": "",
        "glm": os.getenv("GLM_API_BASE", "https://open.bigmodel.cn/api/coding/paas/v4"),
        "deepseek": os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
    }
    path_map = {
        "mock": "/v1/chat/completions",
        "glm": os.getenv("GLM_CHAT_PATH", "/chat/completions"),
        "deepseek": os.getenv("DEEPSEEK_CHAT_PATH", "/v1/chat/completions"),
    }
    if provider not in {"mock", "glm", "deepseek"}:
        provider = "mock"

    return AppConfig(
        app_host=os.getenv("APP_HOST", "127.0.0.1"),
        app_port=int(os.getenv("APP_PORT", "1557")),
        cors_allow_origin=os.getenv("CORS_ALLOW_ORIGIN", "*"),
        model_provider=provider,
        llm_model=model_map[provider],
        llm_api_key=key_map[provider],
        llm_api_base=base_map[provider],
        llm_chat_path=path_map[provider],
        llm_timeout_sec=int(os.getenv("LLM_TIMEOUT_SEC", "90")),
        llm_max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
        llm_retry_delay_sec=float(os.getenv("LLM_RETRY_DELAY_SEC", "1.2")),
        prompt_dir=os.getenv("PROMPT_DIR", "src/prompt"),
        knowledge_doc_dir=os.getenv("KNOWLEDGE_DOC_DIR", "storage/knowledge"),
        log_file_path=os.getenv("LOG_FILE_PATH", "logs/ariadne.log"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        prompt_hot_reload=_as_bool(os.getenv("PROMPT_HOT_RELOAD", "true"), True),
        guest_mode=_as_bool(os.getenv("GUEST_MODE", "true"), True),
        local_only=_as_bool(os.getenv("LOCAL_ONLY", "true"), True),
        max_file_size_mb=int(os.getenv("MAX_FILE_SIZE_MB", "10")),
        chunk_max_concurrency=max(1, int(os.getenv("CHUNK_MAX_CONCURRENCY", "3"))),
    )

"""AVRONE configuration, read from environment variables (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(Path(os.getenv("AVRONE_ENV_FILE", ".env")))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # --- Model backend (OpenAI-compatible). Default: a local OpenClaw Gateway. ---
    llm_base_url: str = os.getenv("AVRONE_LLM_BASE_URL", "http://127.0.0.1:18789/v1")
    llm_api_key: str = os.getenv("AVRONE_LLM_API_KEY", os.getenv("OPENCLAW_GATEWAY_TOKEN", ""))
    llm_model: str = os.getenv("AVRONE_LLM_MODEL", "openclaw")
    # Optional: a different model for the Lead (synthesizer) agent, e.g. a stronger one.
    lead_model: str = os.getenv("AVRONE_LEAD_MODEL", "")
    # "mock" runs without any model, for demos and tests.
    llm_mode: str = os.getenv("AVRONE_LLM_MODE", "live")
    llm_timeout: float = float(os.getenv("AVRONE_LLM_TIMEOUT", "180"))

    # --- Orchestration (Grok Heavy style squad) ---
    squad_size: int = int(os.getenv("AVRONE_SQUAD_SIZE", "4"))
    max_steps: int = int(os.getenv("AVRONE_MAX_STEPS", "6"))
    max_spawn_depth: int = int(os.getenv("AVRONE_MAX_SPAWN_DEPTH", "2"))

    # --- Environment (Sovereign OS layer) ---
    workspace: Path = field(default_factory=lambda: Path(os.getenv("AVRONE_WORKSPACE", "./workspace")).resolve())
    runner: str = os.getenv("AVRONE_RUNNER", "auto")  # auto | docker | local
    docker_image: str = os.getenv("AVRONE_DOCKER_IMAGE", "python:3.12-slim")
    allow_network: bool = _bool("AVRONE_ALLOW_NETWORK", True)
    command_timeout: int = int(os.getenv("AVRONE_COMMAND_TIMEOUT", "120"))

    # --- Access control ---
    admin_token: str = os.getenv("AVRONE_ADMIN_TOKEN", "")
    agents_can_shell: bool = _bool("AVRONE_AGENTS_CAN_SHELL", True)

    # --- Eternal CodeX (persistent logs and memory) ---
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("AVRONE_DATA_DIR", "./data")).resolve())


settings = Settings()

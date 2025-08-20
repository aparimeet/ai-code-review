import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

OPEN_API_KEY: str = os.getenv("OPEN_API_KEY", "")
GITLAB_TOKEN: str = os.getenv("GITLAB_TOKEN", "")
GITLAB_URL: str = os.getenv("GITLAB_URL", "https://gitlab.com/api/v4")
AI_MODEL: str = os.getenv("AI_MODEL", "gpt-4o-mini")
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")
PORT: int = int(os.getenv("PORT", "8000"))

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is required")
if not GITLAB_TOKEN:
    raise RuntimeError("GITLAB_TOKEN is required")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is required")
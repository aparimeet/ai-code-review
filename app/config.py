import os
from dotenv import load_dotenv

load_dotenv()

GITLAB_TOKEN: str = os.getenv("GITLAB_TOKEN", "")
GITLAB_URL: str = os.getenv("GITLAB_URL", "https://gitlab.com/api/v4")
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
AI_MODEL: str = os.getenv("AI_MODEL", "")
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")
PORT: int = int(os.getenv("PORT", "8000"))

if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY is required")
if not GITLAB_TOKEN:
    raise RuntimeError("GITLAB_TOKEN is required")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is required")
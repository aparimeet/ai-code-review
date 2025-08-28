import os
from dotenv import load_dotenv

load_dotenv()

GITLAB_TOKEN: str = os.getenv("GITLAB_TOKEN", "")
GITLAB_API_URL: str = os.getenv("GITLAB_API_URL", "https://gitlab.com/api/v4")
GITLAB_WEBHOOK_SECRET: str = os.getenv("GITLAB_WEBHOOK_SECRET", "")
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
GITHUB_API_URL: str = os.getenv("GITHUB_API_URL", "https://api.github.com")
GITHUB_WEBHOOK_SECRET: str = os.getenv("GITHUB_WEBHOOK_SECRET", "")
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
AI_MODEL: str = os.getenv("AI_MODEL", "")
PORT: int = int(os.getenv("PORT", "8000"))

if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY is required")

# At least one provider token must be configured
if not (GITLAB_TOKEN or GITHUB_TOKEN):
    raise RuntimeError("At least one provider token is required: GITLAB_TOKEN or GITHUB_TOKEN")

# Provider-specific webhook secrets are optional here; each endpoint will enforce its own
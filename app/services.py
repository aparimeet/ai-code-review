import asyncio
import json
import logging
from typing import List, Dict, Any, Optional
from certifi import contents
import httpx
import openai
from urllib.parse import quote_plus

from .config import GITLAB_TOKEN, GITLAB_URL, OPENAI_API_KEY, AI_MODEL

logger = logging.getLogger(__name__)
openai.api_key = OPENAI_API_KEY

GITLAB_HEADERS = {
    "Private-Token": GITLAB_TOKEN,
    "Accept": "application/json"
}

SYSTEM_MESSAGE = {
    "role": "system",
    "content": "You are a senior developer reviewing code changes. Provide a clear, concise code review in Markdown. Use bullet points and code blocks where helpful."
}
ASSISTANT_INSTRUCTION = {
    "role": "assistant",
    "content": "Format the response so it renders nicely in GitLab with organized markdown. Answer the questions and include a short summary line at the top."
}

# Safety: limit sizes sent to OpenAI
MAX_DIFF_CHARS = 30_000
MAX_FILE_CHARS = 30_000

async def fetch_branch_diff(project_id: int, target_branch: str, source_branch: str) -> Optional[Dict[str, Any]]:
    """
    Call: GET /projects/:id/repository/compare?from=target&to=source&unidiff=true
    """
    url = f"{GITLAB_URL}/projects/{project_id}/repository/compare"
    params = {"from": target_branch, "to": source_branch, "unidiff": "true"}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(url, headers=GITLAB_HEADERS, params=params, timeout=30.0)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.exception("Failed to fetch branch compare: %s", e)
            return None

async def fetch_raw_file(project_id: int, file_path: str, ref: str) -> Optional[str]:
    """
    GET /projects/:id/repository/files/:file_path/raw?ref=<ref>
    file_path must be URL encoded
    """
    encoded_path = quote_plus(file_path)
    url = f"{GITLAB_URL}/projects/{project_id}/repository/files/{encoded_path}/raw"
    params = {"ref": ref}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(url, headers=GITLAB_HEADERS, params=params, timeout=30.0)
            if r.status_code == 200:
                return r.text
            logger.info("Non-200 when fetching file %s: %s", file_path, r.status_code)
            return ""
        except Exception as e:
            logger.exception("Failed to fetch raw file %s: %s", file_path, e)
            return ""

def truncate_text(s: str, max_chars: int) -> str:
    if s is None:
        return ""
    if len(s) <= max_chars:
        return s
    
    head = s[: max_chars // 2]
    tail = s[- (max_chars // 2) :]
    return head + "\n\n...TRUNCATED...\n\n" + tail

def build_messages(old_files: List[Dict[str, str]], diffs: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """
    Build the chat messages for OpenAI from old files and diffs.
    old_files: list of { fileName: str, fileContent: str }
    diffs: list of diff objects, with property 'diff' (unidiff string)
    """
    user_content = []
    user_content.append("Files before changes (for contexnt):")
    for f in old_files:
        content = truncate_text(f.get("fileContent", ""), MAX_FILE_CHARS)
        user_content.append(f"Filename: {f.get('fileName')}\n```\n{content}\n```")
    
    user_content.append("\nDiffs (unidiff format):")
    concatenated_diffs = "\n\n".join([d.get("diff", "") for d in diffs])
    concatenated_diffs = truncate_text(concatenated_diffs, MAX_DIFF_CHARS)
    user_content.append(f"```\n{concatenated_diffs}\n```")

    user_content.append("""
    Questions:
    1. Summarize the changes in a succinct bullet list.
    2. Are added/changed code clear and easy to understand?
    3. Are names/comments descriptive?
    4. Can the code be simplified? If so, give examples.
    5. Any potential bugs? Please reference lines in the diff when possible.
    6. Any potential security issues?
    """.strip())

    messages = [SYSTEM_MESSAGE, ASSISTANT_INSTRUCTION, {"role": "user", "content": "\n\n".join(user_content)}]
    return messages


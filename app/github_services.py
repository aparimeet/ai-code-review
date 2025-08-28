import httpx
import logging
import re
from typing import Any, Dict, List, Optional

from .config import GITHUB_API_URL, GITHUB_TOKEN

logger = logging.getLogger("ai-github-review")

async def fetch_github_pr_diff(owner: str, repo: str, pr_number: int) -> Dict[str, Any]:
    # Get PR details including diff
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3.diff"
    }
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        return {"diff": r.text}

async def fetch_github_file_content(owner: str, repo: str, path: str, ref: str) -> str:
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/contents/{path}?ref={ref}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3.raw"
    }
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=headers)
        if r.status_code == 200:
            return r.text
        return ""

async def post_github_review_comment(owner: str, repo: str, pr_number: int, body: str, commit_id: str, path: str, position: int) -> bool:
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pr_number}/comments"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    payload = {
        "body": body,
        "commit_id": commit_id,
        "path": path,
        "position": position
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=headers, json=payload)
        return r.status_code == 201

async def post_github_review_summary(owner: str, repo: str, pr_number: int, body: str, event: str) -> bool:
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    payload = {
        "body": body,
        "event": event  # "COMMENT" or "APPROVE"
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=headers, json=payload)
        return r.status_code == 200

async def fetch_all_file_contents(owner, repo, files, base_ref, head_ref):
    result = []
    for fname in files:
        old_content = await fetch_github_file_content(owner, repo, fname, base_ref)
        new_content = await fetch_github_file_content(owner, repo, fname, head_ref)
        result.append({
            "fileName": fname,
            "oldContent": old_content,
            "newContent": new_content,
        })
    return result

async def fetch_changed_files_with_patch(owner: str, repo: str, pr_number: int) -> List[Dict[str, Any]]:
    """
    Return a list of files with unified diff patches for the PR.
    Each entry has: filename, previous_filename (optional), patch (unified diff), status.
    """
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pr_number}/files"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        files = resp.json()
    out: List[Dict[str, Any]] = []
    for f in files:
        out.append({
            "filename": f.get("filename"),
            "previous_filename": f.get("previous_filename") or "",
            "patch": f.get("patch") or "",
            "status": f.get("status"),
        })
    return out

def compute_patch_newline_to_position(patch: str) -> Dict[int, int]:
    """
    Build a mapping of new file line -> position index within the GitHub patch.
    Position counts all lines in the patch (including @@ headers and no-newline markers),
    starting from 1.
    """
    mapping: Dict[int, int] = {}
    if not patch:
        return mapping
    position: int = 0
    new_cursor: Optional[int] = None
    for raw_line in patch.splitlines():
        position += 1
        if raw_line.startswith('@@'):
            m = re.match(r"@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@", raw_line)
            if m:
                new_cursor = int(m.group('new'))
            else:
                new_cursor = None
            continue
        if new_cursor is None:
            continue
        if raw_line.startswith('+') and not raw_line.startswith('+++'):
            mapping[new_cursor] = position
            new_cursor += 1
        elif raw_line.startswith(' ') or raw_line.startswith('\\'):
            new_cursor += 1
        elif raw_line.startswith('-') and not raw_line.startswith('---'):
            # Deletions do not advance new file line numbers
            pass
    return mapping

def compute_github_position_from_patch(patch: str, new_line: int) -> Optional[int]:
    """
    Compute the GitHub diff position for a given new file line number.
    Returns None if the line cannot be located within the patch.
    """
    return compute_patch_newline_to_position(patch).get(new_line)
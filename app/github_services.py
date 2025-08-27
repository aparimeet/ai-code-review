import httpx
import logging
from typing import Any, Dict

GITHUB_URL = "https://api.github.com"
logger = logging.getLogger("ai-github-review")

async def fetch_github_pr_diff(owner: str, repo: str, pr_number: int, token: str) -> Dict[str, Any]:
    # Get PR details including diff
    url = f"{GITHUB_URL}/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3.diff"
    }
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        return {"diff": r.text}

async def fetch_github_file_content(owner: str, repo: str, path: str, ref: str, token: str) -> str:
    url = f"{GITHUB_URL}/repos/{owner}/{repo}/contents/{path}?ref={ref}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3.raw"
    }
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=headers)
        if r.status_code == 200:
            return r.text
        return ""

async def post_github_review_comment(owner: str, repo: str, pr_number: int, body: str, commit_id: str, path: str, position: int, token: str) -> bool:
    url = f"{GITHUB_URL}/repos/{owner}/{repo}/pulls/{pr_number}/comments"
    headers = {
        "Authorization": f"token {token}",
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

async def post_github_review_summary(owner: str, repo: str, pr_number: int, body: str, event: str, token: str) -> bool:
    url = f"{GITHUB_URL}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    payload = {
        "body": body,
        "event": event  # "COMMENT" or "APPROVE"
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=headers, json=payload)
        return r.status_code == 200

async def fetch_changed_files(owner, repo, pr_number, token):
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
    headers = {"Authorization": f"token {token}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        files = resp.json()
    return [f["filename"] for f in files]

async def fetch_all_file_contents(owner, repo, files, base_ref, head_ref, token):
    result = []
    for fname in files:
        old_content = await fetch_github_file_content(owner, repo, fname, base_ref, token)
        new_content = await fetch_github_file_content(owner, repo, fname, head_ref, token)
        result.append({
            "fileName": fname,
            "oldContent": old_content,
            "newContent": new_content,
        })
    return result
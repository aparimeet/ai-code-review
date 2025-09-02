import logging
from typing import List, Dict, Any, Optional
import httpx
from urllib.parse import quote

from .config import GITLAB_TOKEN, GITLAB_API_URL

logger = logging.getLogger(__name__)

GITLAB_HEADERS = {
    "Private-Token": GITLAB_TOKEN,
    "Accept": "application/json"
}

async def fetch_branch_diff(project_id: int, target_branch: str, source_branch: str) -> Optional[Dict[str, Any]]:
    """
    Call: GET /projects/:id/repository/compare?from=target&to=source&unidiff=true
    """
    url = f"{GITLAB_API_URL}/projects/{project_id}/repository/compare"
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
    # GitLab expects the file_path in the URL path to be fully URL-encoded
    # including slashes. Use quote with safe="" to encode '/'.
    encoded_path = quote(file_path, safe="")
    url = f"{GITLAB_API_URL}/projects/{project_id}/repository/files/{encoded_path}/raw"
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

async def post_merge_request_note(project_id: int, merge_request_iid: int, body: str) -> bool:
    """
    POST /projects/:id/merge_requests/:iid/notes
    """
    url = f"{GITLAB_API_URL}/projects/{project_id}/merge_requests/{merge_request_iid}/notes"
    payload = {"body": body}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, headers={**GITLAB_HEADERS, "Content-Type": "application/json"}, json=payload, timeout=30.0)
            r.raise_for_status()
            logger.info("Posted AI comment to MR %s/%s", project_id, merge_request_iid)
            return True
        except Exception as e:
            logger.exception("Failed to post MR note: %s", e)
            return False


#
# Inline discussions on specific diff lines
#

async def fetch_merge_request_diff_refs(project_id: int, merge_request_iid: int) -> Optional[Dict[str, str]]:
    """
    Fetch merge request metadata to retrieve diff refs used for positioning inline notes.

    GET /projects/:id/merge_requests/:iid
    Returns keys: diff_refs -> { base_sha, start_sha, head_sha }
    """
    url = f"{GITLAB_API_URL}/projects/{project_id}/merge_requests/{merge_request_iid}"
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(url, headers=GITLAB_HEADERS, timeout=20.0)
            r.raise_for_status()
            data = r.json() or {}
            diff_refs = data.get("diff_refs") or {}
            if all(k in diff_refs for k in ("base_sha", "start_sha", "head_sha")):
                return {
                    "base_sha": diff_refs["base_sha"],
                    "start_sha": diff_refs["start_sha"],
                    "head_sha": diff_refs["head_sha"],
                }
            logger.warning("diff_refs missing in MR %s/%s response", project_id, merge_request_iid)
            return None
        except Exception as e:
            logger.exception("Failed to fetch MR diff refs: %s", e)
            return None


async def fetch_merge_request_changes(project_id: int, merge_request_iid: int) -> List[Dict[str, Any]]:
    """
    Fetch list of changes for a merge request including unified diffs.

    GET /projects/:id/merge_requests/:iid/changes
    """
    url = f"{GITLAB_API_URL}/projects/{project_id}/merge_requests/{merge_request_iid}/changes"
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(url, headers=GITLAB_HEADERS, timeout=30.0)
            r.raise_for_status()
            data = r.json() or {}
            changes = data.get("changes") or []
            return changes
        except Exception as e:
            logger.exception("Failed to fetch MR changes: %s", e)
            return []

async def post_inline_merge_request_note(
    project_id: int,
    merge_request_iid: int,
    body: str,
    *,
    new_path: str,
    new_line: int,
    diff_refs: Dict[str, str],
) -> bool:
    """
    Create a discussion attached to a specific line of the diff for a merge request.

    POST /projects/:id/merge_requests/:iid/discussions
    """
    url = f"{GITLAB_API_URL}/projects/{project_id}/merge_requests/{merge_request_iid}/discussions"
    position = {
        "position_type": "text",
        "base_sha": diff_refs.get("base_sha"),
        "start_sha": diff_refs.get("start_sha"),
        "head_sha": diff_refs.get("head_sha"),
        "new_path": new_path,
        "new_line": new_line,
    }
    payload = {"body": body, "position": position}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                url,
                headers={**GITLAB_HEADERS, "Content-Type": "application/json"},
                json=payload,
                timeout=30.0,
            )
            r.raise_for_status()
            logger.info(
                "Posted inline AI comment to MR %s/%s at %s:%s",
                project_id,
                merge_request_iid,
                new_path,
                new_line,
            )
            return True
        except Exception as e:
            logger.exception("Failed to post inline MR discussion: %s", e)
            return False

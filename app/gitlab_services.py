import asyncio
import logging
import json
import re
from typing import List, Dict, Any, Optional 
import httpx
from urllib.parse import quote

from .config import GITLAB_TOKEN, GITLAB_API_URL, OPENROUTER_API_KEY, AI_MODEL

logger = logging.getLogger(__name__)

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

# Safety: limit sizes sent to model
MAX_DIFF_CHARS = 30_000
MAX_FILE_CHARS = 30_000
MAX_INLINE_COMMENTS = 20

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
    user_content.append("Files before changes (for context):")
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


def collect_file_diffs(diffs: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """
    Normalize diffs from compare API into a list of objects { new_path, old_path, diff }.
    """
    out: List[Dict[str, str]] = []
    for d in diffs:
        out.append({
            "new_path": d.get("new_path") or d.get("new_file_path") or d.get("new_pathname") or d.get("old_path") or "",
            "old_path": d.get("old_path") or "",
            "diff": d.get("diff", ""),
        })
    return out


def build_structured_review_messages(old_files: List[Dict[str, str]], file_diffs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Ask the model to return STRICT JSON with per-line comments.

    Expected output JSON:
    { "comments": [ { "new_path": "path/to/file.py", "new_line": 123, "body": "comment text" } ] }
    """
    instruction = {
        "role": "system",
        "content": (
            "You are a precise code reviewer. Produce STRICT JSON only (no markdown, no fences). "
            "Target each comment to a single added/changed line in the unified diff using the NEW file line number. "
            "Also include an exact 'code' string with the line's content to anchor accurately. "
            "Return at most " + str(MAX_INLINE_COMMENTS) + " comments."
        ),
    }

    # Provide focused content, grouped by file
    parts: List[str] = []
    parts.append("Old files (context, truncated):")
    for f in old_files:
        content = truncate_text(f.get("fileContent", ""), MAX_FILE_CHARS)
        parts.append(f"Filename: {f.get('fileName')}\n```\n{content}\n```")

    parts.append("\nDiffs by file (unified diff):")
    for fd in file_diffs:
        path = fd.get("new_path") or fd.get("old_path")
        diff = truncate_text(fd.get("diff", ""), MAX_DIFF_CHARS // max(1, len(file_diffs)))
        parts.append(f"FILE: {path}\n```\n{diff}\n```")

    parts.append(
        (
            "Return JSON ONLY in the following shape (no comments, no markdown):\n"
            "{\n  \"comments\": [\n    { \"new_path\": string, \"new_line\": number, \"body\": string, \"code\": string }\n  ]\n}\n"
            "Guidelines:\n"
            "- Focus on correctness, clarity, naming, security, and complexity.\n"
            "- Each comment must reference one specific NEW line (an added/changed line starting with '+').\n"
            "- Keep bodies concise (<= 300 chars).\n"
            "- Do not include backticks, code fences, or any pre/post text outside JSON."
        ).strip()
    )

    return [instruction, {"role": "user", "content": "\n\n".join(parts)}]


def parse_ai_json_comments(text: str) -> List[Dict[str, Any]]:
    """
    Parse model output to extract a list of {new_path, new_line, body} comments.
    Tolerates accidental markdown fences around JSON.
    """
    if not text:
        return []
    s = text.strip()
    # Remove code fences if present
    if s.startswith("```") and s.endswith("```"):
        s = s.strip('`')
        # try to remove an optional language tag
        s = s.split("\n", 1)[-1]
    # Extract the largest JSON object if extra text exists
    m = re.search(r"\{[\s\S]*\}", s)
    if m:
        s = m.group(0)
    try:
        obj = json.loads(s)
    except Exception:
        return []

    def _map_comment(c: Dict[str, Any]) -> Dict[str, Any]:
        code = c.get("code")
        if isinstance(code, str):
            code = code.strip()
        else:
            code = ""
        return {
            "new_path": str(c.get("new_path", "")),
            "new_line": int(c.get("new_line", 0) or 0),
            "body": str(c.get("body", "")).strip(),
            "code": code,
        }

    if isinstance(obj, dict) and isinstance(obj.get("comments"), list):
        return [_map_comment(c) for c in obj["comments"] if isinstance(c, dict)]
    if isinstance(obj, list):
        return [_map_comment(c) for c in obj if isinstance(c, dict)]
    return []


def enumerate_added_new_lines(unidiff: str) -> Dict[int, str]:
    """
    Return a mapping of new_line -> line_text for each added line in a unified diff chunk.
    Line text excludes the leading '+'.
    """
    mapping: Dict[int, str] = {}
    if not unidiff:
        return mapping
    new_cursor: Optional[int] = None
    old_cursor: Optional[int] = None
    for raw_line in unidiff.splitlines():
        if raw_line.startswith('@@'):
            m = re.match(r"@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@", raw_line)
            if not m:
                new_cursor = None
                old_cursor = None
                continue
            old_cursor = int(m.group('old'))
            new_cursor = int(m.group('new'))
            continue
        if new_cursor is None:
            continue
        if raw_line.startswith('+') and not raw_line.startswith('+++'):
            mapping[new_cursor] = raw_line[1:]
            new_cursor += 1
        elif raw_line.startswith('-') and not raw_line.startswith('---'):
            if old_cursor is not None:
                old_cursor += 1
        elif raw_line.startswith(' '):
            if new_cursor is not None:
                new_cursor += 1
            if old_cursor is not None:
                old_cursor += 1
        elif raw_line.startswith('\\'):
            # no newline markers
            continue
        else:
            # unknown line, increment new cursor best-effort
            if new_cursor is not None:
                new_cursor += 1
    return mapping


def _extract_inline_code_from_body(body: str) -> Optional[str]:
    if not body:
        return None
    m = re.search(r"`([^`]+)`", body)
    if m:
        return m.group(1).strip()
    return None


def validate_ai_comments_against_changes(
    ai_comments: List[Dict[str, Any]],
    changes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Validate model-produced comments against actual MR changes. Drop any invalid ones.
    """
    path_to_added: Dict[str, Dict[int, str]] = {}
    for ch in changes:
        path = ch.get("new_path") or ch.get("new_file_path") or ch.get("new_pathname") or ch.get("old_path")
        if not path:
            continue
        path_to_added[path] = enumerate_added_new_lines(ch.get("diff", ""))

    def find_best_line(added: Dict[int, str], desired_line: int, code_hint: str, body: str) -> Optional[int]:
        target = code_hint or _extract_inline_code_from_body(body) or ""
        target = target.strip()
        if target:
            candidates = [ln for ln, txt in added.items() if txt.strip() == target]
            if candidates:
                # If we have a desired line, pick the nearest candidate; otherwise first.
                if desired_line > 0:
                    return min(candidates, key=lambda ln: abs(ln - desired_line))
                return candidates[0]
            # Fuzzy match
            try:
                from difflib import SequenceMatcher
                best_ln: Optional[int] = None
                best_ratio = 0.0
                for ln, txt in added.items():
                    r = SequenceMatcher(None, txt.strip(), target).ratio()
                    if r > 0.92 and r > best_ratio:
                        best_ratio = r
                        best_ln = ln
                if best_ln is not None:
                    return best_ln
            except Exception:
                pass
        return None

    valid: List[Dict[str, Any]] = []
    for c in ai_comments:
        path = c.get("new_path")
        body = (c.get("body") or "").strip()
        line = int(c.get("new_line") or 0)
        code_hint = (c.get("code") or "").strip()
        if not path or not body:
            continue
        added_map = path_to_added.get(path)
        if not added_map:
            # relaxed match by suffix
            for k in path_to_added.keys():
                if k.endswith(path) or path.endswith(k):
                    added_map = path_to_added[k]
                    path = k
                    break
        if not added_map:
            continue
        best_line = find_best_line(added_map, line, code_hint, body)
        if best_line is None:
            continue
        valid.append({"new_path": path, "new_line": best_line, "body": body})
        if len(valid) >= MAX_INLINE_COMMENTS:
            break
    return valid

async def call_openai_chat(messages: List[Dict[str, str]], model: str = AI_MODEL, temperature: float = 0.2) -> Optional[str]:
    """
    Use OpenAI 1.x client with the Chat Completions API.
    """
    try:
        # Import lazily to avoid import at module import time
        from openai import OpenAI

        client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_API_KEY, max_retries=20)
        response = await asyncio.to_thread(
            lambda: client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
            )
        )
        if response and response.choices:
            return response.choices[0].message.content or ""
        return ""
    except Exception as e:
        logger.exception("OpenAI call failed: %s", e)
        return None

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

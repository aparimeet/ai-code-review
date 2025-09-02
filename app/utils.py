import asyncio
import logging
import json
import re
from typing import List, Dict, Any, Optional

from .config import OPENROUTER_API_KEY, AI_MODEL

logger = logging.getLogger(__name__)

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
MAX_INLINE_COMMENTS = 3

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
    platform: str = "gitlab"
) -> List[Dict[str, Any]]:
    """
    Validate model-produced comments against actual changes. Drop any invalid ones.

    Args:
        ai_comments: List of AI-generated comments with new_path, new_line, body, code
        changes: List of change objects (format depends on platform)
        platform: Either "gitlab" or "github" to handle platform-specific differences

    Returns:
        List of validated comments with corrected line numbers and platform-specific fields
    """
    path_to_added: Dict[str, Dict[int, str]] = {}
    path_to_patch: Dict[str, str] = {}

    # Handle platform-specific data extraction
    if platform == "gitlab":
        for ch in changes:
            path = ch.get("new_path") or ch.get("new_file_path") or ch.get("new_pathname") or ch.get("old_path")
            if not path:
                continue
            diff = ch.get("diff", "")
            path_to_added[path] = enumerate_added_new_lines(diff)
    elif platform == "github":
        for f in changes:
            path = f.get("filename") or f.get("previous_filename") or ""
            if not path:
                continue
            patch = f.get("patch", "")
            if patch:
                path_to_added[path] = enumerate_added_new_lines(patch)
                path_to_patch[path] = patch

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

        # Platform-specific output
        if platform == "github":
            # Convert new file line to GitHub position
            patch = path_to_patch.get(path, "")
            position = compute_github_position_from_patch(patch, best_line)
            if position is None:
                continue
            valid.append({
                "new_path": path,
                "new_line": best_line,
                "position": position,
                "body": body
            })
        else:  # gitlab
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

def compute_patch_newline_to_position(patch: str) -> Dict[int, int]:
    """
    Build a mapping of new file line -> position index within the GitHub patch.
    Position is the absolute line number from the first @@ hunk header in the diff.
    This matches GitHub's API requirements for inline comments.
    """
    mapping: Dict[int, int] = {}
    if not patch:
        return mapping

    lines = patch.splitlines()
    position = 0  # Absolute position counter starting from 0
    new_cursor = None
    first_hunk_found = False

    for line in lines:
        if line.startswith('@@'):
            # Start of a new hunk
            m = re.match(r"@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@", line)
            if m:
                new_cursor = int(m.group('new'))
                first_hunk_found = True
            continue

        if not first_hunk_found or new_cursor is None:
            continue

        position += 1  # Increment position for each line after the first @@ header

        if line.startswith('+') and not line.startswith('+++'):
            # Added line - map new file line to absolute position
            mapping[new_cursor] = position
            new_cursor += 1
        elif line.startswith(' ') or line.startswith('\\'):
            # Context line or no-newline marker
            new_cursor += 1
        elif line.startswith('-') and not line.startswith('---'):
            # Deleted line - doesn't advance new file cursor
            pass

    return mapping

def compute_github_position_from_patch(patch: str, new_line: int) -> Optional[int]:
    """
    Compute the GitHub diff position for a given new file line number.
    Returns None if the line cannot be located within the patch.
    """
    return compute_patch_newline_to_position(patch).get(new_line)



import asyncio
import logging
import time
from typing import Any, Dict

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

from .config import WEBHOOK_SECRET, PORT
from .services import (
    fetch_branch_diff,
    fetch_raw_file,
    build_messages,
    call_openai_chat,
    post_merge_request_note,
    fetch_merge_request_diff_refs,
    fetch_merge_request_changes,
    post_inline_merge_request_note,
    find_first_added_line_from_unidiff,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai-gitlab-review")

app = FastAPI(title="AI GitLab Code Review (FastAPI)")

@app.post("/webhook")
async def gitlab_webhook(request: Request, background_tasks: BackgroundTasks):
    # Basic validation of token
    token = request.headers.get("x-gitlab-token") or request.headers.get("X-Gitlab-Token")
    if token != WEBHOOK_SECRET:
        logger.warning("Unauthorized webhook request: missing/invalid token")
        raise HTTPException(status_code=401, detail="Unauthorized")

    payload: Dict[str, Any] = await request.json()
    if payload is None:
        raise HTTPException(status_code=400, detail="Missing payload")

    # Only handle merge_request events
    if payload.get("object_kind") != "merge_request":
        # early ignore for others
        logger.info("Ignoring non-merge_request event: %s", payload.get("object_kind"))
        return JSONResponse({"status": "ignored"}, status_code=200)

    obj = payload.get("object_attributes", {})
    action = obj.get("action")
    # Trigger review on common MR actions
    if action not in ("update", "open", "opened", "reopen", "reopened"):
        logger.info("Ignoring MR action: %s", action)
        return JSONResponse({"status": "ignored_action", "action": action}, status_code=200)

    project_id = obj.get("target_project_id")
    source_branch = obj.get("source_branch")
    target_branch = obj.get("target_branch")
    merge_request_iid = obj.get("iid")

    if not all([project_id, source_branch, target_branch, merge_request_iid]):
        logger.warning(
            "Missing merge request params (project_id=%s, source=%s, target=%s, iid=%s)",
            project_id, source_branch, target_branch, merge_request_iid,
        )
        return JSONResponse({"status": "missing_params"}, status_code=400)

    # Start background task and return 200 quickly
    logger.info(
        "Scheduling review: project_id=%s iid=%s %s -> %s action=%s",
        project_id, merge_request_iid, target_branch, source_branch, action,
    )
    background_tasks.add_task(process_merge_request_review, project_id, source_branch, target_branch, merge_request_iid)
    return JSONResponse({"status": "accepted"}, status_code=200)

async def process_merge_request_review(project_id: int, source_branch: str, target_branch: str, merge_request_iid: int):
    """
    Orchestrate fetching diffs, building prompt, calling OpenAI, and posting comment.
    """
    try:
        t0 = time.perf_counter()
        logger.info(
            "Begin review: project_id=%s iid=%s %s -> %s",
            project_id, merge_request_iid, target_branch, source_branch,
        )
        logger.info("Fetching branch diff for project %s %s -> %s", project_id, target_branch, source_branch)
        compare = await fetch_branch_diff(project_id, target_branch, source_branch)
        if not compare:
            logger.error("No compare data, aborting review")
            return

        diffs = compare.get("diffs", [])
        logger.info("Found %d diffs", len(diffs))

        # fetch old file contents (best-effort)
        old_paths = [d.get("old_path") for d in diffs if d.get("old_path")]
        logger.info("Preparing to fetch %d old files for context", len(old_paths))
        old_files = []
        # use bounded concurrency
        sem = asyncio.Semaphore(6)
        async def fetch_one(path):
            async with sem:
                content = await fetch_raw_file(project_id, path, target_branch)
                return {"fileName": path, "fileContent": content or ""}
        coros = [fetch_one(p) for p in old_paths]
        results = await asyncio.gather(*coros, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.exception("Error fetching old file: %s", r)
            else:
                old_files.append(r)
        logger.info("Fetched %d/%d old files", len(old_files), len(old_paths))

        messages = build_messages(old_files, diffs)
        logger.info("Calling OpenAI for review...")
        answer = await call_openai_chat(messages)
        if answer is None:
            logger.error("OpenAI did not return an answer")
            return
        logger.info("OpenAI response length: %d chars", len(answer))

        # Optionally include a marker to detect previously posted comment
        decorated_answer = "<!-- ai-gitlab-code-review -->\n" + answer
        # Try to post inline on a specific diff line first
        posted = False
        try:
            diff_refs = await fetch_merge_request_diff_refs(project_id, merge_request_iid)
            changes = await fetch_merge_request_changes(project_id, merge_request_iid)
            logger.info("Attempting inline comment with %d changes", len(changes))
            if diff_refs and changes:
                inline_done = False
                for ch in changes:
                    new_path = ch.get("new_path") or ch.get("new_file_path") or ch.get("new_pathname")
                    unidiff = ch.get("diff")
                    new_line = find_first_added_line_from_unidiff(unidiff or "")
                    if new_path and new_line:
                        inline_done = await post_inline_merge_request_note(
                            project_id,
                            merge_request_iid,
                            decorated_answer,
                            new_path=new_path,
                            new_line=new_line,
                            diff_refs=diff_refs,
                        )
                        if inline_done:
                            posted = True
                            break
            if not posted:
                logger.info("Inline comment not possible, falling back to general MR note")
        except Exception as e:
            logger.exception("Inline posting failed, will fallback to general note: %s", e)

        if not posted:
            posted = await post_merge_request_note(project_id, merge_request_iid, decorated_answer)
        if not posted:
            logger.error("Failed to post AI comment")
        else:
            logger.info("Successfully posted AI review to MR iid=%s", merge_request_iid)
        logger.info("Review completed in %.2fs", time.perf_counter() - t0)
    except Exception as e:
        logger.exception("Error in process_merge_request_review: %s", e)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, reload=True)
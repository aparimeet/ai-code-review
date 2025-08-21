import asyncio
import logging
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
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai-gitlab-review")

app = FastAPI(title="AI GitLab Code Review (FastAPI)")

@app.post("/webhook")
async def gitlab_webhook(request: Request, background_tasks: BackgroundTasks):
    # Basic validation of token
    token = request.headers.get("x-gitlab-token") or request.headers.get("X-Gitlab-Token")
    if token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    payload: Dict[str, Any] = await request.json()
    if payload is None:
        raise HTTPException(status_code=400, detail="Missing payload")

    # Only handle merge_request events
    if payload.get("object_kind") != "merge_request":
        # early ignore for others
        return JSONResponse({"status": "ignored"}, status_code=200)

    obj = payload.get("object_attributes", {})
    action = obj.get("action")
    # follow original repo behavior: only 'update' triggers review
    if action not in ("update",):
        return JSONResponse({"status": "ignored_action", "action": action}, status_code=200)

    project_id = obj.get("target_project_id")
    source_branch = obj.get("source_branch")
    target_branch = obj.get("target_branch")
    merge_request_iid = obj.get("iid")

    if not all([project_id, source_branch, target_branch, merge_request_iid]):
        logger.warning("Missing merge request params")
        return JSONResponse({"status": "missing_params"}, status_code=400)

    # Start background task and return 200 quickly
    background_tasks.add_task(process_merge_request_review, project_id, source_branch, target_branch, merge_request_iid)
    return JSONResponse({"status": "accepted"}, status_code=200)

async def process_merge_request_review(project_id: int, source_branch: str, target_branch: str, merge_request_iid: int):
    """
    Orchestrate fetching diffs, building prompt, calling OpenAI, and posting comment.
    """
    try:
        logger.info("Fetching branch diff for project %s %s -> %s", project_id, target_branch, source_branch)
        compare = await fetch_branch_diff(project_id, target_branch, source_branch)
        if not compare:
            logger.error("No compare data, aborting review")
            return

        diffs = compare.get("diffs", [])
        logger.info("Found %d diffs", len(diffs))

        # fetch old file contents (best-effort)
        old_paths = [d.get("old_path") for d in diffs if d.get("old_path")]
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

        messages = build_messages(old_files, diffs)
        logger.info("Calling OpenAI for review...")
        answer = await call_openai_chat(messages)
        if answer is None:
            logger.error("OpenAI did not return an answer")
            return

        # Optionally include a marker to detect previously posted comment
        decorated_answer = "<!-- ai-gitlab-code-review -->\n" + answer

        posted = await post_merge_request_note(project_id, merge_request_iid, decorated_answer)
        if not posted:
            logger.error("Failed to post AI comment")
    except Exception as e:
        logger.exception("Error in process_merge_request_review: %s", e)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, reload=True)
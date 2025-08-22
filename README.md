# AI GitLab Code Review (FastAPI)

A minimal FastAPI implementation of the same idea as Evobaso-J/ai-gitlab-code-review:
- Listen for GitLab merge request webhooks
- Fetch diffs and old file contents
- Build a prompt and ask OpenAI for a review
- Post the review as a Markdown note on the merge request

## Requirements

- Python 3.13

## Quickstart

1. Copy `.env.example` to `.env` and set the variables.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
   ```
4. Expose your local server to GitLab (using ngrok) and add a webhook:
   - URL: https://<your-ngrok>/webhook
   - Secret token: same as `WEBHOOK_SECRET`
   - Trigger: Merge request events (updates)

## Environment Variables

- `OPENROUTER_API_KEY` (required)
- `AI_MODEL` (required)
- `GITLAB_TOKEN` (required)
- `GITLAB_URL` (default: `https://gitlab.com/api/v4`)
- `WEBHOOK_SECRET` (required)
- `PORT` (default: `8000`)

## Notes & production tips

- This implementation returns 200 quickly and processes AI work in background tasks.
- The OpenAI call uses the 1.x client and runs in a worker thread.
- Keep tokens secret and serve via TLS.
- Consider deduplicating comments by detecting the HTML marker `<!-- ai-gitlab-code-review -->`.
- For large diffs, you must implement smarter chunking to fit token limits.
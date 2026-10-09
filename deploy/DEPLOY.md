# Deploying DebugAssistAgent on Render

| Part | Where it runs | Why |
|---|---|---|
| Pages and runs | Render (free web service) | `render.yaml` |
| AI-written tests | E2B sandboxes, internet off, no keys inside | Render can't run Docker |
| Laya (triage, guard rating) | Your Mac, through a tunnel | Laya runs only on Apple chips |
| Run state, run files, similar-bug search | MongoDB Atlas (free cluster) | Render's free disk is wiped on restart |
| Embeddings | Voyage AI (1024 dims, same shape as local) | no Ollama on Render |
| Traces | Phoenix Cloud | no local Phoenix on Render |

## 1. Accounts (you make these; nobody else sees the keys)
Render · MongoDB Atlas (free cluster) · E2B · Voyage AI · Phoenix Cloud. Keep each key in a password manager; you will
paste them into Render's dashboard in step 4.

## 2. The E2B template (once, ~10 minutes)
```bash
cd ~/Projects/DebugAssist
E2B_API_KEY=... uv run python scripts/e2b_template.py vercel/ai --build
```
It prints the template alias (`debugassist-vercel-ai-e7f55a4`); `render.yaml` already uses that name.

## 3. Laya on your Mac (whenever hosted runs should work)
```bash
cd ~/Projects/DebugAssist && LAYA_TOKEN=<a long random secret> uv run debug-assist laya-serve
```
In another terminal, a free tunnel: `cloudflared tunnel --url http://127.0.0.1:8790` (prints an https address).
Hosted runs need the Mac awake, `laya-serve` running and the tunnel up. A quick tunnel's address changes every
time it starts; update `LAYA_URL` in Render when it does (or set up a named tunnel once).

## 4. Render
New ▸ Blueprint ▸ this repo. Render reads `render.yaml` and asks for each key marked `sync: false`.
Render tells the app its own address (`RENDER_EXTERNAL_HOSTNAME`), so there is no address to set.
Sign-in is with GitHub: `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` from a GitHub OAuth app whose callback is `https://<the address>/auth/github/callback`, and `ALLOWED_GITHUB_USERS`, the accounts that may sign in (comma-separated). Without all three the address stays locked.

## 5. Check
Open the address, sign in, then on your Mac: `uv run debug-assist preflight <issue-url>` with the same environment, or
watch the first run's page: the start-up checks list every missing piece by name.

## 6. Connect a repo
Open **Connect a repo**, paste `https://github.com/<owner>/<repo>`. It reads the repo's own files (no AI), builds the
repo's own E2B template (5–15 minutes, a few cents of E2B credit), runs its biggest test suites with the network off,
and saves the setup in MongoDB (`repos`). Public JavaScript, TypeScript and Python repos. Same from a terminal:
`uv run debug-assist connect https://github.com/<owner>/<repo>`. **Connect again** moves a repo to its latest code.

## 7. Automatic start (optional; off until you turn it on)
1. Render ▸ Environment: `GITHUB_WEBHOOK_SECRET` = a long random string (make it in the Terminal app:
   `openssl rand -hex 32`), and `AUTO_RUNS` = `1`.
2. GitHub ▸ the connected repo ▸ Settings ▸ Webhooks ▸ Add webhook: payload URL `https://<render address>/hooks/github`,
   content type `application/json`, the same secret, events: **Issues** only.
3. Create a label `debug-assist` in the repo. Adding it to an issue queues a run (standard AI, one at a time, at most
   10 a day). The home page lists them under **Started from GitHub**.
The free plan sleeps; GitHub's delivery may time out while it wakes. On waking, the app checks every connected repo
for open issues with the label, so none is lost. Each issue runs automatically once.

## Known limits of the free plan
- The service sleeps after 15 idle minutes and can restart at any time. Runs continue where they stopped
  (`resume`); run files come back from MongoDB.
- A long run with nobody watching its page may be paused by sleep; open its page to keep it awake.
- The back-test (older versions) runs only with the local Docker sandbox for now; hosted runs report it as
  not available.

# SiteScope demo site

A small static website that SiteScope is allowed to crawl. Its content is synthetic, written
for SiteScope: nothing on it comes from a real site. Since Phase 11 it is also a container
(`Dockerfile`, `nginx.conf`): in the deployment (`deploy/compose.yaml`) it is
`http://demo-site:8080`, the only approved target, and `python -m app.scheduler.admin seed-demo`
crawls it with four jobs (docs/deployment.md). Locally, serve it as below.

Its structure exercises the crawler:

| Feature | Where |
|---|---|
| Shared links and cycles | the home page, docs and blog link to each other |
| robots.txt `Disallow` | `/private/` (`/private/notes.html` is admitted but never requested) |
| A longer `Allow` rule that wins | `/private/press-kit.html` |
| `<meta name="robots" content="noindex">` | `/team.html`: stored, never indexed |
| `<meta name="robots" content="nofollow">` | `/docs/faq.html`: its link to `/docs/legacy.html` is not followed |
| `rel="nofollow"`, external and `mailto:` links | `/blog/`, `/` |
| A broken link (404) | `/about.html` -> `/missing.html` |

The integration test `backend/tests/integration/test_local_crawler.py` crawls this directory
and checks the exact result: 14 URLs, 12 stored pages, 11 of them indexable.

## Crawl it locally (Windows PowerShell)

1. Serve the site on the loopback interface, from the repository root (port 8080, because the API
   uses 8000):

   ```powershell
   backend\.venv\Scripts\python.exe -m http.server 8080 --bind 127.0.0.1 --directory demo-site
   ```

2. Approve it in `backend\.env` (development only: production refuses IP-literal targets and
   loopback addresses):

   ```
   SITESCOPE_CRAWL_ALLOWED_HOSTS=127.0.0.1
   SITESCOPE_CRAWL_ALLOWED_PORTS=8080
   SITESCOPE_FETCH_INTERNAL_ORIGINS=http://127.0.0.1:8080
   ```

3. Create a job through the API (`POST /api/v1/crawls` with the seed `http://127.0.0.1:8080/`),
   then run it with the single-process crawl driver:

   ```powershell
   cd backend
   .\.venv\Scripts\python.exe -m app.workers.local_crawler <job-id>
   ```

`docs/development.md` describes these steps in detail.

# Upstream merge, 2026-11-17

Source: `james-6-23/codex2api` main at
`4514623dda4ae025fde5af439345c1c0ba641a7c` (232 incoming commits;
33 commits after the latest published release, v3.1.6).
Fork baseline: `cad56885e7cc82770bef4b0193304cb92d2de352`.

The merge takes the upstream Go 1.26.11, Vite 8.3.5, Kiro integration,
image model/statistics improvements, streaming lifecycle fixes, quota failover,
and output protection. Node 22 is used consistently for CI and the image build.

Fork integrations retained:

- Bounded asynchronous image queue and the existing small-memory cache defaults.
- No local image prompt character limit.
- Public S3/CDN result URLs and the `image_s3_public_base_url` setting.
- One bounded retry for remote-asset HTTP 407 across Responses, Compact and Chat,
  with account release and both original/translated request bodies updated.
- SQLite connection handling and retry spool cleanup.
- The fork's production workflow and deployment target. Upstream workflow
  files are removed; all verification gates live in the single self-managed
  production build/deploy workflow.

The upstream heartbeat tests supersede the equivalent fork fix and require a
complete independent SSE keepalive within the upstream silence window.

Database upgrades are additive for this merge. Account, API key, settings and
request-history compatibility must be checked against a synthetic old database
on the GitHub runner. The new default persistent response context cache can
create `response-context.db` beside the main SQLite database; preserve that
volume and account for the default 2 GiB body budget and 48-hour retention.
New self-service account permissions and output guard remain disabled unless
explicitly configured. Existing settings and credentials must not be replaced.

Full Go/race/PostgreSQL, frontend unit/browser/type/build, and dependency security
checks run in GitHub Actions before image publication/deployment. Build and
dependency installation must never run on the deployment server. Operational
verification is reported by the workflow, not inferred from a merge alone.

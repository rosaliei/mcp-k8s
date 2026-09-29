# mcp-k8s

AI support tooling for Kubernetes in one repo, made of three parts that work together:

| Part | Folder | What it does |
|---|---|---|
| **GraphQL API** | `graphql_api/` | Serves p99 latency, CPU and memory per workload from Prometheus. Batching and recording rules cut PromQL queries per request from 12 to 3 (about 48% lower mean latency, measured with `bench.py`). |
| **MCP server** | `mcp_server/` | Lets an AI agent (Claude Code, Claude Desktop) investigate a cluster **read-only**: pods, logs, events, and metrics via the GraphQL API. |
| **Multi-agent code review** | `code_review/` | Three agents (security, reliability, style) review a **git diff** in parallel, and an aggregator gives a verdict. Runs as a git pre-commit hook and as a GitHub Action on pull requests. |

```
                         Claude / AI agent
                               │  MCP (JSON-RPC over stdio)
                               ▼
kubectl (read-only) ◄── mcp_server ──► graphql_api ──► Prometheus ◄── observability/sample_app.py
                                          (batching, recording rules, 15s cache)

git diff ──► code_review: [security | reliability | style] in parallel ──► aggregator ──► verdict
             (pre-commit hook locally, GitHub Action on pull requests)
```

## Setup

```bash
./setup.sh                  # makes .venv (Python 3.12), installs deps, turns on the git hook
source .venv/bin/activate   # in every new terminal
```

## Which files to read

| Priority | File | What to take from it |
|---|---|---|
| **Read** | `mcp_server/server.py` | One function per tool; the docstring is what the AI reads; read-only kubectl allow-list; log to stderr, never stdout. |
| **Read** | `code_review/review.py` | Sections 1 and 4: reading a `git diff`, agents in parallel, exit codes for CI, fail-closed verdict. Skim section 3 (Claude API call). |
| **Read** | `graphql_api/server.py` | `RAW_QUERIES` / `FAST_QUERIES` (the PromQL), the `Prometheus` class (cache, query count), `make_loader` (batching). Skip the strawberry type details. |
| **Read** | `observability/sample_app.py` | How an app exposes metrics: Counter, Gauge, Histogram with labels. |
| **Read** | `docker-compose.yml`, `Dockerfile` | Services find each other by name; health check so graphql-api waits for Prometheus. |
| **Read** | `observability/prometheus.yml`, `rules.yml` | Scrape targets, recording rules, alert rules. |
| **Read** | `.github/workflows/code-review.yml`, `hooks/pre-commit` | How the reviewer runs in CI and before each commit. |
| Skim | `graphql_api/bench.py`, `mcp_server/client_test.py` | Run them and read the output; the code is just timing loops. |
| Ignore | `demo_app/payments.py` | Bait for the reviewer. On the demo branch it's deliberately bad. |
| Ignore | `observability/grafana/.../latency.json` | Generated dashboard JSON. |

## 1. GraphQL API + Prometheus

```bash
docker compose up -d --build               # sample-app :8000, graphql-api :8001, Prometheus :9090, Grafana :3001
docker compose ps                          # all "Up"
python graphql_api/bench.py                # run from your terminal: compare naive / batched / optimized
INCIDENT=checkout docker compose up -d sample-app   # make checkout slow; watch p99 and the alert
docker compose down                        # stop everything
```
The long-running services run in Docker (one image, see `Dockerfile`). One-off tools such as `bench.py`, `client_test.py` and `review.py` run from your terminal with the `.venv`.
To run a service without Docker instead: `python graphql_api/server.py` (it uses fake data if Prometheus isn't reachable; `MOCK=1` forces fake data).
Open http://localhost:8001/graphql and try:
```graphql
{ workloads { name p99LatencyMs cpuCores memoryMb } }
{ workloads(strategy: "naive") { name p99LatencyMs } }
{ prometheusQueryCount(strategy: "naive") }
```

## 2. MCP server

```bash
python mcp_server/client_test.py           # handshake, list tools, investigate, measure tool p99
```
Use it from Claude Code:
```bash
claude mcp add k8s -- "$PWD/.venv/bin/python" "$PWD/mcp_server/server.py"
# then ask: "what's broken in the payments namespace?"
# real cluster, read-only: add  -e K8S_MODE=kubectl
```
Troubleshooting exercise: `BREAK_STDOUT=1 python mcp_server/client_test.py` shows what happens when a stdio MCP server prints to stdout.

## 3. Multi-agent code review with git

There's a demo branch that adds risky code to `demo_app/payments.py`:
```bash
git switch demo/add-refund
python code_review/review.py --diff main          # review only what this branch changed
git switch main
```

Other ways to run it:
```bash
python code_review/review.py --staged                  # what you staged with git add (the hook runs this)
python code_review/review.py demo_app/payments.py      # a whole file
python code_review/review.py --diff main --markdown    # a PR-comment style report
python code_review/review.py --diff main --llm         # Claude agents (needs ANTHROPIC_API_KEY)
```

Exit codes: `0` approve, `1` request changes, `2` incomplete (an agent failed).
**It fails closed**: if an agent crashes, the result is INCOMPLETE, never APPROVE.

- **Pre-commit hook** (`hooks/pre-commit`, turned on by `setup.sh`): blocks a commit that adds critical or high issues. Skip once with `git commit --no-verify`.
- **GitHub Action** (`.github/workflows/code-review.yml`): on every pull request it runs the review, posts the report as a PR comment, and fails the check if the verdict isn't APPROVE. Add a repository secret `ANTHROPIC_API_KEY` to switch it to Claude agents.

## Git practice

```bash
git log --oneline --all --graph      # see main and the demo branch
git diff main...demo/add-refund      # exactly what the reviewer sees
git switch -c my-change              # make your own branch, edit, then:
git add -p && git commit             # the hook reviews your staged lines
```

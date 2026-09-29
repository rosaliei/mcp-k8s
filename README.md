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

## Step by step

Do the steps in order. Every step says **what to type**, **what you should see**, and **what it means**.
Lines starting with `$` are what you type (don't type the `$`).

---

### Step 0: Get ready (every time you open a new terminal)

```
$ cd ~/Documents/AlphaSense/mcp-k8s
$ source .venv/bin/activate
$ which python
```
**You should see:** a path ending in `mcp-k8s/.venv/bin/python`.

- Your prompt now starts with `(.venv)`.
- Always type `python`, never `python3` or `python3.7`. Those skip the `.venv` and give `No module named ...`.
- `python: command not found`? Your terminal has an old venv active. Type `deactivate`, then the `source` line again. Or open a new tab.
- `.venv` missing, or you moved the folder? Run `rm -rf .venv && ./setup.sh` once.

---

## Part 1: GraphQL API + Prometheus

**The idea:** Prometheus stores metrics. The GraphQL API answers "what is the p99 latency, CPU and memory of each workload?" by sending PromQL to Prometheus. The CV claim is that smarter querying made this about 40% faster. Here you prove it.

### Step 1.1: Start the services

```
$ docker compose up -d --build
```
This builds one Python image and starts 4 containers. The first time takes 1–2 minutes.

```
$ docker compose ps
```
**You should see** 4 rows, all with STATUS `Up`:

| Service | Port | What it is |
|---|---|---|
| `sample-app` | 8000 | Fake microservices (checkout, search, auth, ingest) that produce latency metrics |
| `graphql-api` | 8001 | The GraphQL API |
| `prometheus` | 9090 | Collects metrics from the two above; shows `(healthy)` |
| `grafana` | 3001 | Dashboards |

If one is missing or restarting: `docker compose logs <service>`.

### Step 1.2: Check Prometheus is collecting metrics

Wait 1 minute after Step 1.1, then open **http://localhost:9090/targets** in your browser.

**You should see** 3 targets, all **UP** (green): `sample-app`, `graphql-api`, `prometheus`.
**What it means:** Prometheus pulls `/metrics` from each service every 15 seconds. Inside Docker it reaches them by name (`sample-app:8000`), not `localhost`.

### Step 1.3: Ask Prometheus for p99 yourself

Open **http://localhost:9090/graph**, paste this, and click **Execute**:
```
histogram_quantile(0.99, sum by (le, service) (rate(http_request_duration_seconds_bucket[5m])))
```
**You should see** one number per service, in seconds. Roughly: auth 0.05, search 0.4, checkout 0.9, ingest 2.2.
**What it means:** that's the p99. 1 in 100 requests to checkout takes longer than about 0.9 s.

Now try the pre-computed version (a recording rule from `observability/rules.yml`):
```
service:http_request_duration_seconds:p99_5m
```
**You should see:** the same numbers. Prometheus calculates this every 15 s, so reading it is cheap. That's one of the tricks the API uses.

### Step 1.4: Query the GraphQL API in the browser

Open **http://localhost:8001/graphql**, paste this into the left panel, and press the ▶ button:
```graphql
{ workloads { name p99LatencyMs cpuCores memoryMb } }
```
**You should see** JSON with 4 workloads, for example `"name": "checkout", "p99LatencyMs": 898.0, ...`.

Now ask for only one field:
```graphql
{ workloads { name p99LatencyMs } }
```
**What it means:** GraphQL returns only the fields you ask for, and the API only queries Prometheus for those fields.

Now make a typo on purpose:
```graphql
{ workloads { nme } }
```
**You should see** an `"errors"` message: *Cannot query field 'nme' ... Did you mean 'name'?*
**What it means:** GraphQL returns errors with **HTTP 200**, so monitoring that only watches HTTP status codes misses them. That's a classic support question.

### Step 1.5: Run the benchmark (the 40% claim)

```
$ python graphql_api/bench.py
```
**You should see** a table like this (your numbers will differ a bit each run):
```
strategy   fields    PromQL/req     mean      p50      p95      p99   (ms, n=60)
naive      all 3           12.0     11.2     10.3     12.8     39.8
batched    all 3            3.0      6.7      6.0      8.0     34.5
optimized  all 3            0.1      4.0      3.7      5.8      6.9
batched    p99 only         1.0      8.1      6.4     16.8     21.0

batched vs naive: mean latency 40% lower
```
**How to read it:**
- **PromQL/req** is how many Prometheus queries one GraphQL request caused.
  - **naive** asks per workload, per field: 4 workloads × 3 fields = **12**.
  - **batched** asks once per field, for all workloads together = **3**.
  - **optimized** is batched plus a 15-second cache, so almost always **0**.
- **mean / p50** is a typical request. **p99** is the slowest 1 in 100.
- **"40% lower"** is the batched vs naive saving. Across runs it's 40–55%, so quote a range.
- **"p99 only"** asks for 1 field, so it needs only 1 query.

### Step 1.6: Cause an incident and watch it

```
$ INCIDENT=checkout docker compose up -d sample-app
```
This restarts only `sample-app`, with checkout made slow.

Wait **3 minutes**, then check:
1. **http://localhost:9090/alerts**: `HighP99Latency` turns yellow (**Pending**), then red (**Firing**) for `checkout`.
   **What it means:** the alert rule is "p99 above 500 ms for 2 minutes". The 2-minute wait stops short spikes from paging anyone.
2. **http://localhost:8001/graphql**: run `{ workloads { name p99LatencyMs } }` again. Checkout's number is much higher.

Put it back to normal:
```
$ docker compose up -d sample-app
```

### Step 1.7: Look at Grafana

Open **http://localhost:3001**, then go to **Dashboards** and open **Service Latency**.
**You should see** panels for p99 per service, p50 vs p99, requests per second, and error ratio.

---

## Part 2: MCP server

**The idea:** MCP lets an AI agent (Claude) call your tools. This server gives it read-only Kubernetes tools, like `kubectl get pods`, logs and events, plus metrics from the GraphQL API in Part 1. The data is a fake cluster with broken pods.

Keep the Part 1 services running (Step 1.1).

### Step 2.1: Run the test client

```
$ python mcp_server/client_test.py
```
It starts the MCP server, connects the way Claude would, and investigates. **You should see:**

1. `connected to: k8s-investigator`, then the list of 6 tools.
2. **list_pods**: `checkout-7d9f8-fghij` is `CrashLoopBackOff` with 14 restarts.
3. **get_events**: `OOMKilling ... Memory cgroup out of memory`.
4. **get_pod_logs**: `java.lang.OutOfMemoryError: Java heap space`.
5. **get_workload_metrics**: checkout's p99, CPU and memory, which came from the GraphQL API.
6. A latency table:
```
                   p50     p95     p99     max   (ms)
client-side       20.2    29.9   303.9   337.6
server-side       15.7    23.7   300.2   330.7
```

**How to read it:**
- Items 2–4 are a real investigation: CrashLoopBackOff → OOMKilled → out of Java heap → the 256Mi memory limit is too small. Practise saying that chain out loud.
- **p50 is about 20 ms, but p99 is about 300 ms.** 3% of calls hit a "slow API server", and the p99 shows that tail.
- **client-side minus server-side** (about 4 ms) is MCP's transport overhead. So the slowness is in the tool, not in MCP.

### Step 2.2: Break it on purpose (the most common MCP bug)

```
$ BREAK_STDOUT=1 python mcp_server/client_test.py -n 5
```
**You should see:** `Failed to parse JSONRPC message from server`.
**What it means:** with MCP over stdio, **stdout carries the protocol**. The server printed a debug line to stdout and corrupted it. The fix: log to stderr.

### Step 2.3 (optional): Use it from Claude Code

```
$ claude mcp add k8s -- "$PWD/.venv/bin/python" "$PWD/mcp_server/server.py"
```
Then in Claude Code ask: *"what's broken in the payments namespace?"*

---

## Part 3: Multi-agent code review with git

**The idea:** three "agents" (security, reliability, style) review the lines a git branch changed, at the same time. Then one aggregator merges their findings and decides: approve or request changes. Pull-request review bots work the same way.

This part doesn't need Docker.

### Step 3.1: Look at the branches

```
$ git log --oneline --all --graph
```
**You should see** `main` plus a branch `demo/add-refund`. That branch adds risky code to `demo_app/payments.py`.

```
$ git diff main...demo/add-refund
```
**You should see** only the added lines (green, starting with `+`). This is exactly what the reviewer looks at.

### Step 3.2: Review the branch

```
$ git switch demo/add-refund
$ python code_review/review.py --diff main
$ echo $?
$ git switch main
```
**You should see:**
```
  [CRITICAL] demo_app/payments.py:31  Hardcoded secret  (security)
  [CRITICAL] demo_app/payments.py:36  SQL injection via string formatting  (security)
  [HIGH    ] demo_app/payments.py:43  HTTP call without timeout  (reliability)
  [HIGH    ] demo_app/payments.py:44  TLS verification disabled  (security)
  [HIGH    ] demo_app/payments.py:49  Shell command injection  (security)
  ...
parallel wall time = 0.06s   (one after another would be 0.17s)
**VERDICT: REQUEST CHANGES** (5 blocking)
```
and `echo $?` prints **1**.

**How to read it:**
- It only reviews lines the branch **added**. The clean code already on `main` isn't flagged.
- The three agents ran **in parallel**, so the total time is the slowest agent, not all three added up.
- **Exit code 1** = request changes. CI uses the exit code to fail a pull request. `0` = approve, `2` = an agent crashed (fail closed: never approve a review that didn't run).

### Step 3.3: See the git hook block a bad commit

```
$ git switch -c test-hook
$ echo 'password = "supersecret123"' >> demo_app/payments.py
$ git add demo_app/payments.py
$ git commit -m "test"
```
**You should see:** `Hardcoded secret` and `Commit blocked by code review`.
**What it means:** `hooks/pre-commit` runs the reviewer on what you staged, before git lets you commit.

Clean up. This undoes only the test line in that one file:
```
$ git restore --staged --worktree demo_app/payments.py
$ git switch main
$ git branch -D test-hook
```

### Step 3.4 (optional): With real Claude agents

Needs an API key:
```
$ export ANTHROPIC_API_KEY=sk-ant-...
$ git switch demo/add-refund && python code_review/review.py --diff main --llm; git switch main
```

---

## Stop everything

```
$ docker compose down
```

---

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


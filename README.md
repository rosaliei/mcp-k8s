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

## Contents

1. [Concepts](#concepts): every idea this project uses, explained from zero
2. [Repository layout](#repository-layout): what every file does
3. [Step by step](#step-by-step): run it, with the output you should see
4. [Which files to read](#which-files-to-read)
5. [Troubleshooting](#troubleshooting)

## Concepts

Read this once before the steps. Each concept says **what it is**, **where it appears in this repo**, and **what to remember**.

### Observability: metrics, logs, traces

- **Metrics**: numbers over time, such as requests per second, p99 latency, CPU. Cheap to store and good for alerts. They tell you **whether** something is wrong and how big the problem is.
- **Logs**: text lines from the app. They tell you **why** something went wrong.
- **Traces**: one request followed across many services. They tell you **where** the time went.
- Typical flow: an alert fires on a metric, a trace finds the slow service, and that service's logs show the cause.
- In this repo: metrics come from Prometheus (`observability/`), and logs come from `kubectl logs` via the MCP server.

### Prometheus

- A **time-series database** that collects metrics by **pulling** them. Every 15 seconds (`scrape_interval`) it fetches `http://<target>/metrics` from each target in `observability/prometheus.yml`.
- If a target can't be reached, the metric `up{job="..."}` becomes `0`. Check this first when a dashboard shows **"No data"**. No data isn't zero: it means Prometheus got nothing at all.
- **Time series** = metric name + labels, for example `http_requests_total{service="checkout", code="200"}`.
- **Labels** let you filter and group, but every unique combination is a new series. Put `service` in a label, but **never user IDs**: millions of series use up Prometheus's memory. This is called **high cardinality**.

**Metric types** (see `observability/sample_app.py`):

| Type | Behaviour | Example here | How to query |
|---|---|---|---|
| Counter | only goes up (resets to 0 on restart) | `http_requests_total` | always with `rate()`: `rate(http_requests_total[1m])` = requests per second |
| Gauge | goes up and down | `workload_cpu_cores` | read directly |
| Histogram | counts values into buckets (`le` = "less than or equal") | `http_request_duration_seconds_bucket` | `histogram_quantile()` for percentiles |
| Summary | percentiles computed in the app | not used | can't be combined across pods, so histograms are preferred |

**PromQL basics** used here:

- `rate(x[5m])`: per-second increase of a counter, averaged over 5 minutes.
- `sum by (service) (...)`: add up all pods, keeping one result per service.
- `histogram_quantile(0.99, sum by (le, service) (rate(..._bucket[5m])))`: p99 per service. Keep `le` in the `sum by`, or the buckets get mixed up.
- `{code=~"5.."}`: a regex label match, here any 5xx status code.

**Recording rules** (`observability/rules.yml`): Prometheus runs an expensive query every 15 s and saves the result as a new series, such as `service:http_request_duration_seconds:p99_5m`. Dashboards and the GraphQL API read that cheaply instead of recomputing over all the buckets.

**Alerting rules**: `expr` is the condition, and `for: 2m` means it must stay true for 2 minutes.
- States: **inactive → pending → firing**. The `for` period stops short spikes from paging anyone.
- In production, firing alerts go to **Alertmanager**, which groups, silences and routes them to Slack, PagerDuty or an incident tool like FireHydrant.

### p50, p95, p99 (percentiles)

- Sort all request durations. **p99** is the value that 99% of requests are faster than: **1 in 100 is slower**. p50 is the median, the typical request.
- **Why not the average?** It hides the slow tail. The average can look fine while 1 in 100 requests takes a second.
- **Why p99 matters more than it sounds:**
  - A user who makes 100 requests in a session will likely hit the p99 at least once.
  - A page that calls 20 backends has an 18% chance (1 − 0.99²⁰) of hitting at least one p99-slow call.
- **You can't average percentiles.** The p99 across two pods isn't the average of their p99s. Add up the bucket counts first (`sum by (le)`), then take the quantile.
- **Sample size matters.** With 60 requests, the p99 is decided by one or two requests. Always say how many samples a p99 came from.
- **SLI / SLO / SLA:**
  - **SLI** (indicator): what you measure, e.g. p99 latency.
  - **SLO** (objective): your target, e.g. p99 < 500 ms. That's what the `HighP99Latency` alert checks.
  - **SLA** (agreement): the contract with customers. It's looser than the SLO and has penalties.

### Grafana

- A dashboard tool. It doesn't store data: it queries **data sources** such as Prometheus.
- Here it's **provisioned** from files (`observability/grafana/provisioning/`), so the data source and the **Service Latency** dashboard exist as soon as it starts.

### GraphQL (and how it differs from REST)

- **One endpoint** (`POST /graphql`). The client sends a **query** listing exactly the fields it wants: `{ workloads { name p99LatencyMs } }`.
- **Schema:** typed and self-documenting. The server publishes it, and the browser page at `/graphql` shows it.
- **Resolvers:** one function per field. A field that isn't requested never runs its resolver, so it never hits Prometheus (`graphql_api/server.py`).
- **REST** uses many endpoints, each with a fixed response shape. That leads to:
  - **over-fetching:** getting fields you don't need
  - **under-fetching:** needing several calls to get everything
- **Errors come back with HTTP 200** in an `"errors"` array. Monitoring that only counts HTTP 5xx will miss GraphQL failures.
- **The N+1 problem:** 1 query for the list of N workloads, then 1 query per workload per field. The `naive` strategy does exactly this: 12 PromQL queries for 4 workloads × 3 fields.
- **DataLoader (the fix):** it collects all the `load("checkout")`, `load("search")`, ... calls made while answering one request and sends **one** batched query (`service=~"checkout|search|..."`). That's the `batched` strategy: 3 queries.
- **Caching:** the `optimized` strategy caches results for 15 s, the same as the scrape interval, so the data is never staler than Prometheus's own.

### MCP (Model Context Protocol)

- A standard way to give an AI application **tools**, so it can take actions or fetch data instead of only chatting.
- **Host:** the AI app, e.g. Claude Code. **Client:** its MCP connector. **Server:** your code (`mcp_server/server.py`).
- **Primitives:** *tools* (functions the model can call), *resources* (data the app can read), *prompts* (templates). This repo uses tools.
- **Wire format:** JSON-RPC 2.0. A session goes `initialize` (agree on the version) → `tools/list` (names, descriptions, input schemas) → `tools/call` (run one tool, get text back).
- **The model picks tools by reading their names, descriptions and parameter types.** Clear docstrings matter: vague ones lead to wrong tool choices.
- **Transports:**
  - **stdio:** the client starts the server as a child process and talks over stdin/stdout. That's what this repo uses.
  - **Streamable HTTP:** for remote servers, usually with OAuth.
- **The #1 stdio bug:** anything printed to **stdout** corrupts the protocol, and the client fails with *"Failed to parse JSONRPC message"*. Always log to **stderr**. Step 2.2 reproduces it.
- **Safety:** tools that only **read** by default, least privilege, and treat tool output as data, never as instructions (prompt injection).

### Kubernetes troubleshooting

| What you see | What it means | Where the evidence is |
|---|---|---|
| `CrashLoopBackOff` | The container keeps crashing. Kubernetes restarts it with growing waits (10s, 20s, 40s … up to 5 min). | `kubectl logs --previous`, `describe` → Last State |
| `OOMKilled`, exit code **137** | It used more memory than its **limit**, so the kernel killed it. **The app logs no error.** | `describe` → `Reason: OOMKilled`, `Exit Code: 137`, `Limits: memory` |
| `ErrImagePull` / `ImagePullBackOff` | Kubernetes can't download the image: tag typo, missing credentials, or no network. | events: `Failed to pull image ...` |
| `Pending` | The scheduler can't place the pod: not enough CPU or memory, taints, or an unbound volume. | events: `FailedScheduling ... Insufficient cpu` |
| `Running` with restarts > 0 | It crashed before. A crash-looping pod shows `Running` for a few seconds between crashes. | RESTARTS column, `describe` |

- **requests vs limits:** *requests* are what the scheduler reserves for the pod. *Limits* are the hard maximum. Going over the **memory** limit kills the container. Going over the **CPU** limit only slows it down.
- **Events** (`kubectl get events`) are what Kubernetes itself saw. They're usually the fastest route to the cause. They can also be noisy: the early "untolerated taint" warnings in this repo came from before the node was ready.
- **`kubectl logs --previous`** shows the logs of the container that died. In a fast crash loop it can print *"unable to retrieve container logs"* and **still exit 0**. The MCP server handles that by falling back to the current logs.

### RBAC, ServiceAccounts and kubeconfig

- **ServiceAccount:** an identity for a program (people have user accounts).
- **Role / ClusterRole:** a list of allowed actions, i.e. resources + verbs (`get`, `list`, `watch`, `create`, `delete`...). Anything not listed is **denied**.
- **RoleBinding / ClusterRoleBinding:** gives a role to an identity.
- `kind/rbac-read-only.yaml` gives the MCP server `get/list/watch` on pods, logs, events and deployments, and **no secrets and no write verbs**. Check it with `kubectl auth can-i delete pods` → `no`.
- **kubeconfig** has three parts:
  - **clusters:** where the API server is, plus its CA certificate
  - **users:** credentials, here a ServiceAccount token
  - **contexts:** which user on which cluster
- `kind/up.sh` builds a read-only kubeconfig so the MCP server physically can't change anything. **Two safety layers:** the Python allow-list, *and* Kubernetes RBAC.

### kind (Kubernetes IN Docker)

- Runs a real Kubernetes cluster as Docker containers on your laptop: a real API server, scheduler and kubelet. It's for testing, not production.
- `kind/up.sh` writes its own kubeconfig files in `kind/`, so **your `~/.kube/config` and current context aren't changed**.

### Docker and Docker Compose

- **Image:** a packaged filesystem plus a command (built from the `Dockerfile`). **Container:** a running image.
- **Layer caching:** the Dockerfile copies `requirements.txt` and installs packages **before** copying the code. Code changes then don't reinstall everything.
- **Compose** (`docker-compose.yml`) starts several containers on one private network:
  - **Service names are hostnames:** `graphql-api` reaches Prometheus at `http://prometheus:9090`. Inside a container, `localhost` means that container itself.
  - **`ports: "8001:8001"`:** your-Mac-port : container-port.
  - **`healthcheck` + `depends_on: condition: service_healthy`:** start the GraphQL API only once Prometheus is ready.
  - **`profiles`:** optional services (MySQL) that start only with `--profile practice`.
  - **Environment variables** configure containers: `PROM_URL`, `INCIDENT`.

### Git: branches, diffs, hooks, exit codes, CI

- **Branch:** a separate line of commits. `demo/add-refund` adds risky code on top of `main`.
- **`git diff main...demo/add-refund`** (three dots) shows what the branch changed since it split from `main`. That's what a pull request shows, and what the reviewer reads (`--unified=0` = changed lines only, no context).
- **Staged changes:** what you `git add`-ed and will commit. `git diff --cached` shows them.
- **Git hooks:** scripts git runs automatically. `hooks/pre-commit` runs before every commit, and **a non-zero exit code cancels the commit**. `git config core.hooksPath hooks` turns on the hooks stored in the repo.
- **Exit codes:** every program returns a number. **0 = success**; anything else = failure. Hooks and CI decide pass or fail from it. `review.py` returns 0 (approve), 1 (request changes) or 2 (incomplete).
- **CI (GitHub Actions):** `.github/workflows/code-review.yml` runs on every pull request on GitHub's machines. It runs the review, comments on the PR, and fails the check if the verdict isn't APPROVE.

### Multi-agent systems and AI code review

- **Agent:** an LLM (or here, optionally, a rule set) with one role, its own instructions and a defined output.
- **Orchestrator–workers pattern:** split the job, run specialist workers **in parallel**, then have an **aggregator** merge the results. Here the workers are security, reliability and style, and the aggregator dedupes, ranks and gives a verdict.
- **Why split?**
  - Narrow instructions miss less.
  - Parallel runs are faster: total time is the slowest agent, not the sum.
  - One failing agent doesn't stop the others.
- **Structured output:** each Claude agent must answer in a fixed JSON schema (`FINDINGS_SCHEMA`), so merging is plain code, not guessing at free text.
- **Fail closed:** if an agent errors, the verdict is **INCOMPLETE** (exit 2), never APPROVE. Zero findings from a crashed agent must not look like zero bugs. The first version of this tool got that wrong.
- **False positives:** the reviewer once flagged its own rule list, because the regexes contain `os.system` and `verify=False`. `SKIP_PATHS` fixes that. Rule-based checks match text, not meaning.

## Repository layout

```
mcp-k8s/
├── graphql_api/
│   ├── server.py          GraphQL API over Prometheus: naive / batched / optimized strategies, own /metrics
│   └── bench.py           sends each strategy N times, prints queries per request and p50/p95/p99
├── mcp_server/
│   ├── server.py          MCP server: read-only Kubernetes tools (mock or real kubectl) + metrics via GraphQL
│   └── client_test.py     connects like Claude would, investigates the most-broken pod, measures tool latency
├── code_review/
│   └── review.py          multi-agent reviewer: git diff -> 3 agents in parallel -> aggregator -> verdict + exit code
├── observability/
│   ├── sample_app.py      fake services exposing Counter / Gauge / Histogram metrics on :8000
│   ├── prometheus.yml     what Prometheus scrapes, and how often
│   ├── rules.yml          recording rules (p99, p50, error ratio) and alerts (HighP99Latency, HighErrorRate)
│   └── grafana/provisioning/   Grafana data source + "Service Latency" dashboard, loaded at startup
├── kind/
│   ├── cluster.yaml       a 1-node kind cluster
│   ├── broken-workloads.yaml   OOMKilled / ImagePullBackOff / Pending / healthy workloads in "payments"
│   ├── rbac-read-only.yaml     ServiceAccount + ClusterRole (read-only, no secrets) + binding
│   ├── up.sh              create the cluster, deploy, build the read-only kubeconfig
│   └── down.sh            delete only this cluster
├── demo_app/payments.py   clean example code (the demo/add-refund branch adds risky code to it)
├── hooks/pre-commit       runs the reviewer on staged changes; blocks the commit on critical/high
├── .github/workflows/code-review.yml   same review on every pull request, posted as a PR comment
├── Dockerfile             one Python image for sample-app and graphql-api
├── docker-compose.yml     sample-app, graphql-api, Prometheus, Grafana (+ MySQL with --profile practice)
├── requirements.txt       Python packages
└── setup.sh               creates .venv, installs packages, turns on the git hook
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

**The idea:** Prometheus stores metrics. The GraphQL API answers "what is the p99 latency, CPU and memory of each workload?" by sending PromQL to Prometheus. Batching the queries makes this about 35–55% faster. Here you measure it yourself.

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

### Step 1.5: Run the benchmark (naive vs batched vs optimized)

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

batched vs naive: mean latency 40% lower (fewer PromQL queries per request)
```
**How to read it:**
- **PromQL/req** is how many Prometheus queries one GraphQL request caused.
  - **naive** asks per workload, per field: 4 workloads × 3 fields = **12**.
  - **batched** asks once per field, for all workloads together = **3**.
  - **optimized** is batched plus a 15-second cache, so almost always **0**.
- **mean / p50** is a typical request. **p99** is the slowest 1 in 100.
- **"40% lower"** is the batched vs naive saving. Across runs it's 35–55%, so quote a range.
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

### Step 2.4: Run it against a REAL Kubernetes cluster (kind)

kind runs a real Kubernetes cluster inside Docker on your Mac. Your normal `~/.kube/config` is **not** changed: this cluster gets its own kubeconfig files in `kind/`.

**Create the cluster** (first time about 2 minutes):
```
$ ./kind/up.sh
```
This creates the cluster `mcp-k8s`, deploys 4 workloads into namespace `payments` (`kind/broken-workloads.yaml`), and makes a **read-only** login for the MCP server (`kind/rbac-read-only.yaml` → `kind/mcp-reader.kubeconfig`).

**Look at the pods** (wait 1 minute first):
```
$ /usr/local/bin/kubectl --kubeconfig kind/admin.kubeconfig get pods -n payments
```
**You should see:**
```
NAME                        READY   STATUS             RESTARTS
checkout-...                0/1     OOMKilled          2          <- uses more memory than its 128Mi limit
ingest-...                  0/1     Pending            0          <- asks for 64 CPUs, no node has that
ledger-...                  0/1     ImagePullBackOff   0          <- image tag typo: busybox:1.36-typo
search-...                  1/1     Running            0          <- healthy
```
(Use `/usr/local/bin/kubectl` because your shell's `kubectl` is an alias for `kubecolor`.)

**Prove the MCP login is read-only:**
```
$ /usr/local/bin/kubectl --kubeconfig kind/mcp-reader.kubeconfig auth can-i delete pods -n payments
$ /usr/local/bin/kubectl --kubeconfig kind/mcp-reader.kubeconfig delete pod -n payments -l app=search
```
**You should see:** `no`, then `Error from server (Forbidden) ... cannot delete resource "pods"`.
**What it means:** the agent can investigate but can never change the cluster, and **Kubernetes enforces that** (RBAC), not just the Python allow-list.

**Run the MCP investigation on the real cluster:**
```
$ K8S_MODE=kubectl KUBECTL=/usr/local/bin/kubectl KUBECONFIG=$PWD/kind/mcp-reader.kubeconfig python mcp_server/client_test.py
```
**You should see:** it picks `checkout` (the pod with the most restarts), then:
- `describe_pod`: **`Reason: OOMKilled`, `Exit Code: 137`, `Limits: memory: 128Mi`**
- `get_pod_logs`: `(previous container logs not available ...)`, then the current logs: just two INFO lines and **no error**

**What the real cluster teaches you (the mock doesn't):**
1. **A crash-looping pod sometimes shows `Running`** for a few seconds between crashes. Check RESTARTS, not just STATUS.
2. **OOMKilled apps don't log an error.** The kernel kills them instantly. The evidence is `Exit Code: 137` and `Reason: OOMKilled` in `describe`, compared with the memory limit.
3. **`kubectl logs --previous` can fail in a fast crash loop.** It prints "unable to retrieve container logs" but still exits 0. The server falls back to the current logs.
4. **Real events are noisy.** The early "untolerated taint" warnings are from before the node was ready. They're not the problem.

**Use the real cluster from Claude Code** (optional):
```
$ claude mcp add k8s-real -e K8S_MODE=kubectl -e KUBECTL=/usr/local/bin/kubectl -e KUBECONFIG=$PWD/kind/mcp-reader.kubeconfig -- "$PWD/.venv/bin/python" "$PWD/mcp_server/server.py"
```
Then ask Claude: *"what's broken in the payments namespace?"*

**Delete the cluster when done** (your other kind clusters are not touched):
```
$ ./kind/down.sh
```
The read-only token lasts 24 hours. If the MCP server gets `Unauthorized` the next day, run `./kind/up.sh` again to make a new one.

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

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No module named httpx` (or any module) | You ran `python3` / `python3.7`, which skips the `.venv` | `source .venv/bin/activate`, then use `python` |
| `python: command not found` with `(.venv)` in the prompt | The terminal points at an old, deleted venv | `deactivate`, then `source .venv/bin/activate`, or open a new tab |
| `bad interpreter` after moving the folder | A venv stores its absolute path | `rm -rf .venv && ./setup.sh` |
| `kubecolor: command not found` | Your shell aliases `kubectl` to `kubecolor` | Use `/usr/local/bin/kubectl` |
| A Prometheus target is DOWN | The container isn't running, or just restarted | `docker compose ps`, `docker compose logs <service>`; wait one scrape (15 s) |
| GraphQL returns fake numbers in Docker | It started before Prometheus was ready | `docker compose restart graphql-api` |
| `port is already allocated` | Something else uses 8000/8001/9090/3001 | `docker ps`, stop the other container |
| Changed Python code, no effect | The container still runs the old image | `docker compose up -d --build` |
| MCP client: `Failed to parse JSONRPC message` | Something printed to stdout in the server | Log to stderr (see Step 2.2) |
| MCP on kind: `Unauthorized` | The 24 h read-only token expired | `./kind/up.sh` again |
| Commit blocked by code review | The hook found critical/high issues in staged lines | Fix them, or `git commit --no-verify` if you're sure |


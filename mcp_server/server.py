"""
MCP server that lets an AI agent investigate a Kubernetes cluster. READ-ONLY.

WHAT IS MCP
  MCP (Model Context Protocol) is a standard way to give an AI app (Claude Code, Claude Desktop,
  your own agent) access to tools. You write a "server" that lists tools; any MCP "client" can use them.
    host   = the AI app the user talks to (e.g. Claude Code)
    client = the part of the host that speaks MCP
    server = this file: it offers tools, runs them, returns text
  Messages are JSON-RPC 2.0. A session goes:
    initialize  -> agree on protocol version and features
    tools/list  -> the client learns tool names, descriptions and input schemas
    tools/call  -> the client runs a tool with arguments and gets text back

HOW THE AI USES THIS
  The model reads each tool's name + docstring + parameter types, decides which tool to call,
  and with which arguments. So the docstrings below are not just comments: the AI reads them.

TOOLS
  list_pods, describe_pod, get_pod_logs, get_events   the usual kubectl investigation
  get_workload_metrics                                p99 / CPU / memory from graphql_api/  (MCP -> GraphQL -> Prometheus)
  get_tool_latency_stats                              p50/p95/p99 of every tool call so far

MODES (environment variable K8S_MODE)
  mock     (default) a fake cluster with broken pods. No Kubernetes needed.
  kubectl  runs real `kubectl get/logs/describe` against the cluster in KUBECONFIG.
           Use a READ-ONLY login: see kind/rbac-read-only.yaml and kind/up.sh.

TRANSPORT: STDIO (important for troubleshooting)
  The client STARTS this script as a child process and talks to it through stdin/stdout.
  stdout is RESERVED for protocol messages. Any print() to stdout corrupts the stream and the
  client fails with "Failed to parse JSONRPC message". Always log to stderr.
  See it happen:  BREAK_STDOUT=1 python mcp_server/client_test.py
"""
import json
import math
import os
import random
import subprocess    # runs other programs, here: kubectl
import sys
import time

import httpx                                       # HTTP client, used to call the GraphQL API
from mcp.server.mcpserver import MCPServer         # the official MCP Python SDK

# ------------------------------------------------------------------ settings (from environment variables)
MODE = os.getenv("K8S_MODE", "mock")
GRAPHQL_URL = os.getenv("GRAPHQL_URL", "http://localhost:8001/graphql")
KUBECTL = os.getenv("KUBECTL", "kubectl")   # path to kubectl. Set it if your shell's kubectl is an alias.

mcp = MCPServer("k8s-investigator")         # the server; the name is what clients display

timings = {}   # tool name -> list of how long each call took, in ms (for get_tool_latency_stats)

if os.getenv("BREAK_STDOUT") == "1":
    print("debug: server starting")         # DELIBERATE BUG for practice: this goes to stdout and breaks the protocol


def log(message):
    """The correct way to log from a stdio MCP server: write to stderr."""
    print(message, file=sys.stderr)


def percentile(values, p):
    """percentile(values, 99) -> the value that 99% of values are at or below (nearest-rank method)."""
    ordered = sorted(values)
    index = math.ceil(p / 100 * len(ordered)) - 1
    return ordered[max(index, 0)]


def record_time(tool_name, start):
    """Called at the end of each tool: save how long it took since `start`."""
    elapsed_ms = (time.perf_counter() - start) * 1000
    timings.setdefault(tool_name, []).append(elapsed_ms)   # setdefault = create the list if it's the first call


# ------------------------------------------------------------------ fake cluster data (mock mode)
# Same problems as kind/broken-workloads.yaml, so both modes tell the same story.
PODS = [
    {"name": "checkout-7d9f8-abcde", "namespace": "payments", "status": "Running", "restarts": 0, "node": "ip-10-0-1-12"},
    {"name": "checkout-7d9f8-fghij", "namespace": "payments", "status": "CrashLoopBackOff", "restarts": 14,
     "node": "ip-10-0-1-13", "last_termination_reason": "OOMKilled", "memory_limit": "256Mi"},
    {"name": "ledger-5c6b7-klmno", "namespace": "payments", "status": "ImagePullBackOff", "restarts": 0,
     "node": "ip-10-0-2-40", "image": "registry.internal/ledger:v2.3.1-typo"},
    {"name": "search-6f4d2-pqrst", "namespace": "search", "status": "Running", "restarts": 1, "node": "ip-10-0-3-7"},
    {"name": "ingest-8a1b2-uvwxy", "namespace": "search", "status": "Pending", "restarts": 0, "node": None,
     "reason": "0/6 nodes available: 6 Insufficient cpu"},
]
LOGS = {
    "checkout-7d9f8-fghij": [
        "INFO  starting checkout service v4.2.0",
        "INFO  loaded 182,000 price rules into memory cache",
        "WARN  heap usage 241Mi / 256Mi",
        "ERROR java.lang.OutOfMemoryError: Java heap space",
    ],
    "search-6f4d2-pqrst": [
        "INFO  connected to elasticsearch:9200",
        "WARN  slow query 2.3s index=filings q=\"revenue guidance\"",
        "INFO  request completed status=200 duration_ms=87",
    ],
}
EVENTS = {
    "payments": [
        "Warning  BackOff     pod/checkout-7d9f8-fghij  Back-off restarting failed container",
        "Warning  OOMKilling  node/ip-10-0-1-13        Memory cgroup out of memory: Killed process (java)",
        "Warning  Failed      pod/ledger-5c6b7-klmno    Failed to pull image \"registry.internal/ledger:v2.3.1-typo\": not found",
    ],
    "search": [
        "Warning  FailedScheduling  pod/ingest-8a1b2-uvwxy  0/6 nodes are available: 6 Insufficient cpu.",
    ],
}


def pretend_to_call_api_server():
    """
    Mock mode only: simulate the Kubernetes API's response time.
    97% of calls take 5-20 ms; 3% take 180-350 ms (a busy API server).
    That slow 3% is invisible in the average but obvious in the p99.
    """
    if random.random() < 0.03:
        time.sleep(random.uniform(0.18, 0.35))
    else:
        time.sleep(random.uniform(0.005, 0.02))


# ------------------------------------------------------------------ real cluster helpers (kubectl mode)
def kubectl(*args):
    """
    Run a read-only kubectl command and return its output as text.

    Two safety layers make this server read-only:
      1. this allow-list: only get / logs / describe / top may run
      2. the cluster itself: KUBECONFIG should log in as a read-only ServiceAccount
         (kind/rbac-read-only.yaml). Even if layer 1 had a bug, Kubernetes answers "Forbidden".

    If kubectl fails, return the error text instead of crashing, so the agent can read it.
    "Forbidden" or "NotFound" is useful information for an investigation.
    """
    if args[0] not in ["get", "logs", "describe", "top"]:
        return f"ERROR: kubectl {args[0]} is not allowed. This server is read-only."
    # capture_output = keep kubectl's output instead of printing it (printing to stdout would break MCP!)
    result = subprocess.run([KUBECTL, *args], capture_output=True, text=True, timeout=20)
    if result.returncode != 0:            # non-zero exit code = kubectl failed
        return f"ERROR: {result.stderr.strip()}"
    return result.stdout


def real_pod_rows(namespace):
    """
    `kubectl get pods -o json`, turned into the same simple rows the mock returns:
      [{"name": ..., "namespace": ..., "status": ..., "restarts": ...}, ...]

    Why not just use the STATUS column of `kubectl get pods`? That column is computed by kubectl.
    In the JSON, the pod "phase" is only Pending/Running/Succeeded/Failed. The useful reason
    (CrashLoopBackOff, ImagePullBackOff, ...) is inside each container's state.waiting.reason.
    """
    if namespace:
        output = kubectl("get", "pods", "-n", namespace, "-o", "json")
    else:
        output = kubectl("get", "pods", "-A", "-o", "json")          # -A = all namespaces
    if output.startswith("ERROR"):
        return output
    rows = []
    for pod in json.loads(output)["items"]:
        status = pod["status"].get("phase", "Unknown")                 # Pending, Running, Succeeded, Failed
        restarts = 0
        for container in pod["status"].get("containerStatuses", []):
            restarts += container.get("restartCount", 0)
            waiting = container.get("state", {}).get("waiting")
            if waiting:                                                # container is waiting to (re)start: say why
                status = waiting["reason"]                             # CrashLoopBackOff, ImagePullBackOff, ...
        rows.append({"name": pod["metadata"]["name"], "namespace": pod["metadata"]["namespace"],
                     "status": status, "restarts": restarts})
    return json.dumps(rows, indent=2)


# ------------------------------------------------------------------ the MCP tools
# @mcp.tool() registers the function as a tool. The MCP SDK builds the tool's input schema from the
# parameter names and types (str, int, bool) and uses the docstring as the description the AI reads.
# Every tool returns plain text (here usually JSON text), which is what the AI receives.

@mcp.tool()
def list_pods(namespace: str = "") -> str:
    """List pods with status and restart count. Empty namespace = all namespaces."""
    start = time.perf_counter()
    if MODE == "kubectl":
        output = real_pod_rows(namespace)
    else:
        pretend_to_call_api_server()
        rows = []
        for pod in PODS:
            if namespace and pod["namespace"] != namespace:
                continue
            rows.append({"name": pod["name"], "namespace": pod["namespace"],
                         "status": pod["status"], "restarts": pod["restarts"]})
        output = json.dumps(rows, indent=2)
    record_time("list_pods", start)
    return output


@mcp.tool()
def describe_pod(name: str, namespace: str) -> str:
    """Full detail for one pod: node, image, memory limit, last termination reason and exit code."""
    start = time.perf_counter()
    if MODE == "kubectl":
        output = kubectl("describe", "pod", name, "-n", namespace)
    else:
        pretend_to_call_api_server()
        output = f"pod {namespace}/{name} not found"
        for pod in PODS:
            if pod["name"] == name and pod["namespace"] == namespace:
                output = json.dumps(pod, indent=2)
    record_time("describe_pod", start)
    return output


@mcp.tool()
def get_pod_logs(name: str, namespace: str, tail: int = 50, previous: bool = False) -> str:
    """Last N log lines of a pod. Set previous=true for a crashing pod: it shows the logs of the
    container that died (like kubectl logs --previous), which is where the error usually is."""
    start = time.perf_counter()
    if MODE == "kubectl":
        if previous:
            output = kubectl("logs", name, "-n", namespace, f"--tail={tail}", "--previous")
            # Real-cluster gotcha: in a fast crash loop, the previous container may already be cleaned up.
            # kubectl then prints "unable to retrieve container logs ..." BUT STILL EXITS 0,
            # so checking the exit code isn't enough. Fall back to the current container's logs, and say so.
            if output.startswith("ERROR") or output.startswith("unable to retrieve container logs"):
                output = f"(previous container logs not available: {output})\n--- current container logs ---\n"
                output += kubectl("logs", name, "-n", namespace, f"--tail={tail}")
        else:
            output = kubectl("logs", name, "-n", namespace, f"--tail={tail}")
    else:
        pretend_to_call_api_server()
        lines = LOGS.get(name, ["(no logs - container never started)"])
        output = "\n".join(lines[-tail:])        # [-tail:] = the last `tail` lines
    record_time("get_pod_logs", start)
    return output


@mcp.tool()
def get_events(namespace: str) -> str:
    """Recent Warning events in a namespace. Usually the fastest way to the root cause."""
    start = time.perf_counter()
    if MODE == "kubectl":
        # Only Warnings, oldest first. Events show what Kubernetes itself saw:
        # scheduling failures, image pull errors, OOM kills, probe failures, back-offs.
        output = kubectl("get", "events", "-n", namespace, "--field-selector", "type=Warning", "--sort-by=.lastTimestamp")
    else:
        pretend_to_call_api_server()
        output = "\n".join(EVENTS.get(namespace, ["no warning events"]))
    record_time("get_events", start)
    return output


@mcp.tool()
def get_workload_metrics(name_contains: str = "") -> str:
    """p99 latency (ms), CPU cores and memory (MB) per workload, from the GraphQL API in graphql_api/."""
    start = time.perf_counter()
    # A GraphQL query with a VARIABLE ($n): the value is sent separately in "variables",
    # which is safer than pasting user input into the query text.
    query = "query($n: String) { workloads(nameContains: $n) { name p99LatencyMs cpuCores memoryMb } }"
    try:
        response = httpx.post(GRAPHQL_URL, json={"query": query, "variables": {"n": name_contains or None}}, timeout=5)
        response.raise_for_status()
        output = json.dumps(response.json()["data"]["workloads"], indent=2)
    except httpx.HTTPError as error:
        # Don't crash the tool: tell the agent what's wrong so it can report it.
        output = f"GraphQL API not reachable at {GRAPHQL_URL} ({type(error).__name__}). Start it with docker compose up -d."
    record_time("get_workload_metrics", start)
    return output


@mcp.tool()
def get_tool_latency_stats() -> str:
    """p50 / p95 / p99 / max in milliseconds for every tool called so far."""
    stats = {}
    for tool_name, values in timings.items():
        stats[tool_name] = {
            "calls": len(values),
            "p50": round(percentile(values, 50), 1),
            "p95": round(percentile(values, 95), 1),
            "p99": round(percentile(values, 99), 1),
            "max": round(max(values), 1),
        }
    return json.dumps(stats, indent=2)


if __name__ == "__main__":
    log(f"k8s-investigator MCP server starting, mode={MODE}")   # stderr, so it's safe
    mcp.run()   # stdio transport: read requests from stdin, write answers to stdout, until the client disconnects

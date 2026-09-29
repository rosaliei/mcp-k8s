"""
MCP server that lets an AI agent look at a Kubernetes cluster. Read-only.

What it offers the agent (these are the MCP "tools"):
  list_pods, describe_pod, get_pod_logs, get_events   -> the usual kubectl investigation
  get_workload_metrics                                 -> calls the GraphQL API (graphql_api/) (MCP -> GraphQL -> Prometheus)
  get_tool_latency_stats                               -> p50/p95/p99 of every tool call so far

Modes (set the env var K8S_MODE):
  mock     (default) a fake cluster with some broken pods to investigate
  kubectl  runs real `kubectl get/logs/describe` on your current context, read-only

How MCP over stdio works: the client (Claude Code, Claude Desktop, client_test.py) starts this
script and talks to it in JSON over stdin/stdout. So stdout is RESERVED for the protocol.
If you print() debug text to stdout, the client breaks. Log to stderr instead.
Try it:  BREAK_STDOUT=1 python mcp_server/client_test.py
"""
import json
import math
import os
import random
import subprocess
import sys
import time

import httpx
from mcp.server.mcpserver import MCPServer

MODE = os.getenv("K8S_MODE", "mock")
GRAPHQL_URL = os.getenv("GRAPHQL_URL", "http://localhost:8001/graphql")

mcp = MCPServer("k8s-investigator")

# tool name -> list of how long each call took (ms)
timings = {}

if os.getenv("BREAK_STDOUT") == "1":
    print("debug: server starting")   # THE classic bug: this goes to stdout and corrupts the protocol


def log(message):
    print(message, file=sys.stderr)    # correct way: stderr is safe


def percentile(values, p):
    """percentile(values, 99) -> the value that 99% of values are at or below."""
    ordered = sorted(values)
    index = math.ceil(p / 100 * len(ordered)) - 1
    return ordered[max(index, 0)]


def record_time(tool_name, start):
    """Call at the end of each tool to save how long it took."""
    elapsed_ms = (time.perf_counter() - start) * 1000
    timings.setdefault(tool_name, []).append(elapsed_ms)


# ------------------------------------------------------------------ fake cluster data
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
    """Most calls are quick. 3% are slow (busy API server). That slow 3% is what p99 shows."""
    if random.random() < 0.03:
        time.sleep(random.uniform(0.18, 0.35))
    else:
        time.sleep(random.uniform(0.005, 0.02))


def kubectl(*args):
    """Run a read-only kubectl command and return its output."""
    if args[0] not in ["get", "logs", "describe", "top"]:
        raise ValueError(f"kubectl {args[0]} is not allowed. This server is read-only.")
    result = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=20, check=True)
    return result.stdout


# ------------------------------------------------------------------ tools
# The docstring of each tool is what the AI reads to decide when to use it. Keep them clear.

@mcp.tool()
def list_pods(namespace: str = "") -> str:
    """List pods with status and restart count. Empty namespace = all namespaces."""
    start = time.perf_counter()
    if MODE == "kubectl":
        if namespace:
            output = kubectl("get", "pods", "-n", namespace, "-o", "wide")
        else:
            output = kubectl("get", "pods", "-A", "-o", "wide")
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
    """Full detail for one pod: node, image, memory limit, last termination reason."""
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
def get_pod_logs(name: str, namespace: str, tail: int = 50) -> str:
    """Last N log lines of a pod."""
    start = time.perf_counter()
    if MODE == "kubectl":
        output = kubectl("logs", name, "-n", namespace, f"--tail={tail}")
    else:
        pretend_to_call_api_server()
        lines = LOGS.get(name, ["(no logs - container never started)"])
        output = "\n".join(lines[-tail:])
    record_time("get_pod_logs", start)
    return output


@mcp.tool()
def get_events(namespace: str) -> str:
    """Recent Warning events in a namespace. Usually the fastest way to the root cause."""
    start = time.perf_counter()
    if MODE == "kubectl":
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
    query = "query($n: String) { workloads(nameContains: $n) { name p99LatencyMs cpuCores memoryMb } }"
    try:
        response = httpx.post(GRAPHQL_URL, json={"query": query, "variables": {"n": name_contains or None}}, timeout=5)
        response.raise_for_status()
        output = json.dumps(response.json()["data"]["workloads"], indent=2)
    except httpx.HTTPError as error:
        output = f"GraphQL API not reachable at {GRAPHQL_URL} ({type(error).__name__}). Start graphql_api/server.py."
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
    log(f"k8s-investigator MCP server starting, mode={MODE}")
    mcp.run()   # stdio transport: read requests from stdin, write answers to stdout

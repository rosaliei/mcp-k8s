"""
MCP test client: connect to the MCP server, list its tools, investigate a namespace, measure p99.

HOW TO RUN
  Fake cluster:   python mcp_server/client_test.py                 (200 latency calls)
                  python mcp_server/client_test.py -n 50           (fewer calls, faster)
  Real cluster:   K8S_MODE=kubectl KUBECTL=/usr/local/bin/kubectl \\
                  KUBECONFIG=$PWD/kind/mcp-reader.kubeconfig python mcp_server/client_test.py
  Other namespace: --namespace search

WHAT IT DOES (the same steps Claude Code / Claude Desktop do behind the scenes)
  1. Start server.py as a child process. It passes on our environment variables, so K8S_MODE and
     KUBECONFIG reach the server.
  2. "initialize"  handshake: agree on the protocol version
  3. "tools/list"  learn which tools exist
  4. "tools/call"  investigate the way an agent would:
       list_pods -> get_events -> describe the most-broken pod -> its logs -> its metrics
  5. Call list_pods many times and measure latency, then compare:
       client-side time = tool time + transport (JSON over stdin/stdout, process switching)
       server-side time = tool time only (measured inside the server)
     If someone says "the MCP server is slow", this split tells you where:
     in the tool (the Kubernetes API, kubectl) or in the transport/client.

PYTHON NOTE
  The MCP library is async, so this file uses async/await. Read "await x" as "wait for x to finish".
  "async with" opens something (a connection, a session) and closes it automatically at the end.
"""
import argparse
import asyncio
import json
import math
import os
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Full path to server.py, next to this file, so it works from any folder.
SERVER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")


def percentile(values, p):
    """percentile(values, 99) -> the value that 99% of values are at or below (nearest-rank method)."""
    ordered = sorted(values)
    index = math.ceil(p / 100 * len(ordered)) - 1
    return ordered[max(index, 0)]


def restart_count(pod):
    """Used for sorting pods: the pod with the most restarts first."""
    return pod["restarts"]


async def call_tool(session, tool_name, arguments):
    """Call one MCP tool, print what we asked and what came back, and return the text."""
    result = await session.call_tool(tool_name, arguments)
    text = result.content[0].text          # a tool result is a list of content blocks; ours is one text block
    print(f"\n> {tool_name}({arguments})")
    print(text)
    return text


async def main(n, namespace):
    # How to start the server: the same Python as this script (the .venv one), running server.py.
    # env=os.environ passes our environment variables (K8S_MODE, KUBECONFIG, ...) to the server.
    server = StdioServerParameters(command=sys.executable, args=[SERVER_SCRIPT], env=dict(os.environ))

    async with stdio_client(server) as (read, write):          # starts the process, connects stdin/stdout
        async with ClientSession(read, write) as session:      # the MCP session on top of that pipe

            # Step 1: handshake
            info = await session.initialize()
            print(f"connected to: {info.server_info.name}   protocol version: {info.protocol_version}")

            # Step 2: which tools does the server offer?
            tools = await session.list_tools()
            print("tools:", ", ".join(tool.name for tool in tools.tools))

            # Step 3: investigate, like an agent would
            print(f"\n--- investigation: what's broken in the {namespace} namespace? ---")

            pods = json.loads(await call_tool(session, "list_pods", {"namespace": namespace}))

            # Which pods look broken? Not Running, OR restarted at least once.
            # Why check restarts: a crash-looping pod shows "Running" for a few seconds between
            # crashes. If you only look at STATUS, you can miss it (this happened on the real cluster).
            broken = []
            for pod in pods:
                if pod["status"] != "Running" or pod["restarts"] > 0:
                    broken.append(pod)
            broken.sort(key=restart_count, reverse=True)       # most restarts first

            # Events first: they're usually the fastest route to the root cause.
            await call_tool(session, "get_events", {"namespace": namespace})

            if broken:
                pod = broken[0]
                await call_tool(session, "describe_pod", {"name": pod["name"], "namespace": namespace})
                # If the pod restarted, the interesting logs are from the container that DIED,
                # not the one running now. That's `kubectl logs --previous`.
                crashed_before = pod["restarts"] > 0
                await call_tool(session, "get_pod_logs",
                                {"name": pod["name"], "namespace": namespace, "previous": crashed_before})
                # Pod names look like  <deployment>-<replicaset hash>-<pod hash>.
                # The part before the first "-" is the app name we use in metrics.
                app_name = pod["name"].split("-")[0]           # "checkout-7d9f8-fghij" -> "checkout"
                await call_tool(session, "get_workload_metrics", {"name_contains": app_name})
            else:
                print("\nno broken pods found")

            # Step 4: latency. Call one tool n times and time each call from the client's side.
            print(f"\n--- latency: {n} x list_pods ---")
            client_ms = []
            for _ in range(n):
                start = time.perf_counter()
                await session.call_tool("list_pods", {})
                client_ms.append((time.perf_counter() - start) * 1000)

            # The server's own view of the same calls (tool time only, no transport).
            stats_result = await session.call_tool("get_tool_latency_stats", {})
            server = json.loads(stats_result.content[0].text)["list_pods"]

    # Print both views side by side.
    print(f"{'':<14}{'p50':>8}{'p95':>8}{'p99':>8}{'max':>8}   (ms)")
    print(f"{'client-side':<14}{percentile(client_ms, 50):>8.1f}{percentile(client_ms, 95):>8.1f}"
          f"{percentile(client_ms, 99):>8.1f}{max(client_ms):>8.1f}")
    print(f"{'server-side':<14}{server['p50']:>8.1f}{server['p95']:>8.1f}{server['p99']:>8.1f}{server['max']:>8.1f}")
    ratio = percentile(client_ms, 99) / percentile(client_ms, 50)
    print(f"\np99 is {ratio:.1f}x the p50. A big ratio means a slow tail: a few calls are much slower than normal.")
    print("client minus server = transport overhead.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=200, help="how many latency calls")
    parser.add_argument("--namespace", default="payments", help="namespace to investigate")
    args = parser.parse_args()
    asyncio.run(main(args.n, args.namespace))    # asyncio.run = start the async main() and wait for it

"""
MCP client test: connect to the server, list its tools, investigate, measure p99.

Run:  python mcp_server/client_test.py          (fake cluster, 200 latency calls)
      python mcp_server/client_test.py -n 50
Real cluster (see kind/):
      K8S_MODE=kubectl KUBECONFIG=$PWD/kind/mcp-reader.kubeconfig python mcp_server/client_test.py

This does what Claude Code / Claude Desktop do behind the scenes:
  1. start server.py as a child process
  2. "initialize"  - agree on protocol version and features
  3. "tools/list"  - get tool names, descriptions and input schemas
  4. "tools/call"  - run a tool with arguments, get text back

Then it times list_pods many times and compares:
  client-side time = tool time + transport (JSON over stdin/stdout)
  server-side time = tool time only (from get_tool_latency_stats)
If a user says "the MCP server is slow", this split tells you whether
the tool itself is slow or the transport/client is.

(The MCP Python library is async, so this file uses async/await.
 Read "await x" as "wait for x to finish".)
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

SERVER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")


def percentile(values, p):
    """percentile(values, 99) -> the value that 99% of values are at or below."""
    ordered = sorted(values)
    index = math.ceil(p / 100 * len(ordered)) - 1
    return ordered[max(index, 0)]


async def call_tool(session, tool_name, arguments):
    """Call one MCP tool, print what we asked and what came back, return the text."""
    result = await session.call_tool(tool_name, arguments)
    text = result.content[0].text
    print(f"\n> {tool_name}({arguments})")
    print(text)
    return text


async def main(n, namespace):
    # How to start the server: same Python as us, running server.py
    server = StdioServerParameters(command=sys.executable, args=[SERVER_SCRIPT], env=dict(os.environ))

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:

            # Step 1: handshake
            info = await session.initialize()
            print(f"connected to: {info.server_info.name}   protocol version: {info.protocol_version}")

            # Step 2: what tools does the server offer?
            tools = await session.list_tools()
            print("tools:", ", ".join(tool.name for tool in tools.tools))

            # Step 3: investigate like an agent would
            print(f"\n--- investigation: what's broken in the {namespace} namespace? ---")

            pods = json.loads(await call_tool(session, "list_pods", {"namespace": namespace}))
            # A pod is suspicious if it isn't Running OR it has restarted.
            # (A crash-looping pod shows "Running" for a few seconds between crashes.)
            broken = []
            for pod in pods:
                if pod["status"] != "Running" or pod["restarts"] > 0:
                    broken.append(pod)
            broken.sort(key=lambda pod: pod["restarts"], reverse=True)   # most restarts first
            await call_tool(session, "get_events", {"namespace": namespace})
            if broken:
                pod = broken[0]                       # look closer at the first broken pod
                await call_tool(session, "describe_pod", {"name": pod["name"], "namespace": namespace})
                crashed_before = pod["restarts"] > 0  # if it restarted, the error is in the PREVIOUS container's logs
                await call_tool(session, "get_pod_logs", {"name": pod["name"], "namespace": namespace, "previous": crashed_before})
                app_name = pod["name"].split("-")[0]  # "checkout-7d9f8-fghij" -> "checkout"
                await call_tool(session, "get_workload_metrics", {"name_contains": app_name})
            else:
                print("\nno broken pods found")

            # Step 4: latency - call one tool n times
            print(f"\n--- latency: {n} x list_pods ---")
            client_ms = []
            for _ in range(n):
                start = time.perf_counter()
                await session.call_tool("list_pods", {})
                client_ms.append((time.perf_counter() - start) * 1000)

            stats_result = await session.call_tool("get_tool_latency_stats", {})
            server = json.loads(stats_result.content[0].text)["list_pods"]

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
    asyncio.run(main(args.n, args.namespace))

"""
MCP client test: connect to the server, list its tools, investigate, measure p99.

Run:  python mcp_server/client_test.py          (200 latency calls)
      python mcp_server/client_test.py -n 50

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


async def main(n):
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
            print("\n--- investigation: what's broken in the payments namespace? ---")
            steps = [
                ("list_pods", {"namespace": "payments"}),
                ("get_events", {"namespace": "payments"}),
                ("get_pod_logs", {"name": "checkout-7d9f8-fghij", "namespace": "payments"}),
                ("get_workload_metrics", {"name_contains": "checkout"}),
            ]
            for tool_name, arguments in steps:
                result = await session.call_tool(tool_name, arguments)
                print(f"\n> {tool_name}({arguments})")
                print(result.content[0].text)

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
    print("\np50 is small but p99 is 10x+ bigger: 3% of calls hit the 'slow API server' path.")
    print("client minus server = transport overhead.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=200, help="how many latency calls")
    asyncio.run(main(parser.parse_args().n))

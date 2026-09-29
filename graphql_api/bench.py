"""
Benchmark the three GraphQL strategies and print p50 / p95 / p99.

Run (server must be running):   python graphql_api/bench.py
Fewer requests or more:         python graphql_api/bench.py -n 100

Columns:
  PromQL/req  how many Prometheus queries ONE GraphQL request caused
  p50         typical request       p99   the slow tail (1 in 100 is slower)
"""
import argparse
import math
import statistics
import time

import httpx

URL = "http://localhost:8001/graphql"


def percentile(values, p):
    """percentile(values, 99) -> the value that 99% of values are at or below."""
    ordered = sorted(values)
    index = math.ceil(p / 100 * len(ordered)) - 1
    return ordered[max(index, 0)]


def graphql(client, query, strategy=""):
    response = client.post(URL, json={"query": query}, headers={"x-strategy": strategy})
    response.raise_for_status()
    body = response.json()
    if "errors" in body:                       # GraphQL errors come back with HTTP 200!
        raise RuntimeError(body["errors"])
    return body["data"]


def query_count(client, strategy):
    data = graphql(client, f'{{ prometheusQueryCount(strategy: "{strategy}") }}')
    return data["prometheusQueryCount"]


def run(client, strategy, fields, n):
    """Send the same query n times. Return (list of latencies in ms, PromQL queries per request)."""
    query = f'{{ workloads(strategy: "{strategy}") {{ {fields} }} }}'
    count_before = query_count(client, strategy)

    latencies_ms = []
    for _ in range(n):
        start = time.perf_counter()
        graphql(client, query, strategy)
        latencies_ms.append((time.perf_counter() - start) * 1000)

    queries_per_request = (query_count(client, strategy) - count_before) / n
    return latencies_ms, queries_per_request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=60, help="requests per test")
    n = parser.parse_args().n

    tests = [
        ("naive", "name p99LatencyMs cpuCores memoryMb"),
        ("batched", "name p99LatencyMs cpuCores memoryMb"),
        ("optimized", "name p99LatencyMs cpuCores memoryMb"),
        ("batched", "name p99LatencyMs"),               # ask for ONE field only
    ]

    print(f"\n{'strategy':<10} {'fields':<9} {'PromQL/req':>10} {'mean':>8} {'p50':>8} {'p95':>8} {'p99':>8}   (ms, n={n})")
    means = {}
    with httpx.Client(timeout=30) as client:
        for strategy, fields in tests:
            latencies, per_request = run(client, strategy, fields, n)
            label = "all 3" if "cpuCores" in fields else "p99 only"
            mean = statistics.mean(latencies)
            means.setdefault(strategy, mean)
            print(f"{strategy:<10} {label:<9} {per_request:>10.1f} {mean:>8.1f} "
                  f"{percentile(latencies, 50):>8.1f} {percentile(latencies, 95):>8.1f} {percentile(latencies, 99):>8.1f}")

    saving = 100 * (1 - means["batched"] / means["naive"])
    print(f"\nbatched vs naive: mean latency {saving:.0f}% lower  <- the kind of number behind '40% faster'")
    print("optimized is mostly cache hits after the first request. Say that honestly if asked.")


if __name__ == "__main__":
    main()

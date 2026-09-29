"""
Benchmark: compare the three GraphQL strategies and print p50 / p95 / p99 latency.

HOW TO RUN (the GraphQL server must be running, e.g. via docker compose up -d)
  python graphql_api/bench.py            # 60 requests per test
  python graphql_api/bench.py -n 200     # more requests = a more trustworthy p99

WHAT IT DOES
  For each strategy (naive, batched, optimized) it sends the same GraphQL query n times,
  times every request, and counts how many PromQL queries the server sent to Prometheus.

HOW TO READ THE OUTPUT
  PromQL/req  Prometheus queries caused by ONE GraphQL request (naive 12, batched 3, optimized ~0)
  mean        average latency. Easy to compare, but hides slow outliers.
  p50         the median: half the requests were faster than this. "Typical" experience.
  p95 / p99   95% / 99% of requests were faster than this. p99 = the slow tail, 1 in 100.
  With only 60 requests, p99 is decided by the single slowest request, so treat it as rough.
"""
import argparse       # reads command-line options like  -n 200
import math
import statistics     # for mean()
import time

import httpx          # HTTP client

URL = "http://localhost:8001/graphql"   # the GraphQL server's port, published by docker compose


def percentile(values, p):
    """
    percentile(values, 99) -> the value that 99% of values are at or below.
    Method ("nearest rank"): sort the values, then take the one at position p% of the list.
    Example: 100 values, p=99 -> the 99th value in sorted order.
    """
    ordered = sorted(values)
    index = math.ceil(p / 100 * len(ordered)) - 1     # -1 because Python lists start at 0
    return ordered[max(index, 0)]


def graphql(client, query, strategy=""):
    """
    Send one GraphQL query and return its "data" part.
    GraphQL is plain HTTP: a POST with JSON  {"query": "..."}.
    """
    response = client.post(URL, json={"query": query}, headers={"x-strategy": strategy})
    response.raise_for_status()           # fail on HTTP 4xx/5xx
    body = response.json()
    # GraphQL puts errors in the body and still returns HTTP 200.
    # So checking the status code alone is NOT enough; we must look for "errors".
    if "errors" in body:
        raise RuntimeError(body["errors"])
    return body["data"]


def query_count(client, strategy):
    """Ask the server how many PromQL queries it has sent so far for this strategy."""
    # {{ and }} are how you write a literal { or } inside a Python f-string.
    data = graphql(client, f'{{ prometheusQueryCount(strategy: "{strategy}") }}')
    return data["prometheusQueryCount"]


def run(client, strategy, fields, n):
    """
    Send the same query n times.
    Returns: (list of latencies in milliseconds, PromQL queries per request)
    """
    query = f'{{ workloads(strategy: "{strategy}") {{ {fields} }} }}'
    count_before = query_count(client, strategy)

    latencies_ms = []
    for _ in range(n):                                   # "_" = we don't need the loop number
        start = time.perf_counter()                      # a precise clock for measuring durations
        graphql(client, query, strategy)
        latencies_ms.append((time.perf_counter() - start) * 1000)

    # (queries after - queries before) / number of requests = queries per request
    queries_per_request = (query_count(client, strategy) - count_before) / n
    return latencies_ms, queries_per_request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=60, help="requests per test")
    n = parser.parse_args().n

    # (strategy, which fields to ask for)
    tests = [
        ("naive", "name p99LatencyMs cpuCores memoryMb"),
        ("batched", "name p99LatencyMs cpuCores memoryMb"),
        ("optimized", "name p99LatencyMs cpuCores memoryMb"),
        ("batched", "name p99LatencyMs"),               # ask for ONE metric: only 1 PromQL query needed
    ]

    # :<10 = left-align in 10 characters, :>8.1f = right-align, 8 wide, 1 decimal. Just table formatting.
    print(f"\n{'strategy':<10} {'fields':<9} {'PromQL/req':>10} {'mean':>8} {'p50':>8} {'p95':>8} {'p99':>8}   (ms, n={n})")
    means = {}
    with httpx.Client(timeout=30) as client:             # "with" = close the connection when done
        for strategy, fields in tests:
            latencies, per_request = run(client, strategy, fields, n)
            label = "all 3" if "cpuCores" in fields else "p99 only"
            mean = statistics.mean(latencies)
            means.setdefault(strategy, mean)              # remember the first (all-fields) mean per strategy
            print(f"{strategy:<10} {label:<9} {per_request:>10.1f} {mean:>8.1f} "
                  f"{percentile(latencies, 50):>8.1f} {percentile(latencies, 95):>8.1f} {percentile(latencies, 99):>8.1f}")

    saving = 100 * (1 - means["batched"] / means["naive"])
    print(f"\nbatched vs naive: mean latency {saving:.0f}% lower (fewer PromQL queries per request)")
    print("optimized: after the first request it's mostly served from the 15-second cache.")


if __name__ == "__main__":
    main()

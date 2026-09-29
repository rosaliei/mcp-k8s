"""
GraphQL API in front of Prometheus.

WHAT IT DOES
  Answers one question: "what is the p99 latency, CPU and memory of each workload?"
  A client (a person in the browser, a script, or the MCP server) sends a GraphQL query.
  This server turns it into PromQL, asks Prometheus, and returns JSON.

WHY GRAPHQL (instead of calling Prometheus directly)
  - The client asks only for the fields it needs. No field -> no PromQL query.
  - The expensive PromQL is written and optimised once, here, not by every client
    (or by an AI agent guessing PromQL).
  - The schema documents itself: the browser page at /graphql shows every field.

THE THREE STRATEGIES (choose one per request with  workloads(strategy: "..."))
  naive      Every field of every workload runs its own PromQL query.
             4 workloads x 3 fields = 12 queries. This is the "N+1 problem".
  batched    One PromQL query per field, covering ALL workloads at once
             with the regex filter  service=~"a|b|c".  3 fields = 3 queries.
  optimized  Same as batched, plus two tricks:
               - p99 reads a recording rule (Prometheus pre-computes it every 15s)
               - answers are cached for 15 seconds
  graphql_api/bench.py measures all three and prints p50 / p95 / p99.

HOW TO RUN
  In Docker (normal):   docker compose up -d        (service "graphql-api", port 8001)
  On your Mac:          python graphql_api/server.py
                        (uses real Prometheus at PROM_URL if it answers, otherwise fake data)
  Force fake data:      MOCK=1 python graphql_api/server.py
  Then open http://localhost:8001/graphql in a browser.

PYTHON NOTES FOR DEVOPS READERS
  async / await   This server handles many requests at the same time in ONE process.
                  "await x" means "start x, and while waiting for the network, go do other work".
                  Functions that wait on the network are written "async def".
  @something      A "decorator": it registers the function below it with a framework,
                  e.g.  @strawberry.field  means "this function is a GraphQL field".
"""
import asyncio
import os
import random
import time
from typing import Optional

import httpx                                     # HTTP client (like requests, but supports async)
import strawberry                                # GraphQL library: Python classes -> GraphQL schema
import uvicorn                                   # the web server process that runs the app
from fastapi import FastAPI, Request             # web framework: routes URLs to Python functions
from prometheus_client import Counter, Histogram, make_asgi_app   # to expose OUR OWN /metrics
from strawberry.dataloader import DataLoader     # the batching helper (fixes N+1)
from strawberry.fastapi import GraphQLRouter     # plugs the GraphQL schema into FastAPI

# ------------------------------------------------------------------ settings (from environment variables)
PROM_URL = os.getenv("PROM_URL", "http://localhost:9090")   # in Docker this is http://prometheus:9090
CACHE_SECONDS = 15   # = Prometheus scrape_interval: cached data is never older than Prometheus's own data
STRATEGIES = ["naive", "batched", "optimized"]
FAKE_SERVICES = ["checkout", "search", "auth", "ingest", "ledger", "notify", "kyc", "fx"]

# ------------------------------------------------------------------ the PromQL behind each GraphQL field
# SELECTOR is a placeholder. We replace it with a label filter before sending, for example:
#   naive:    service="checkout"               (one workload)
#   batched:  service=~"checkout|search|auth"  (regex: all workloads at once)
#
# p99:  histogram_quantile(0.99, ...)  estimates the 99th percentile from histogram buckets.
#       rate(...[5m])      = per-second increase of each bucket over the last 5 minutes
#       sum by (le, service) = add up all pods of a service, but keep the bucket edges (le)
# cpu / mem: gauges (current values), just summed per service.
RAW_QUERIES = {
    "p99": 'histogram_quantile(0.99, sum by (le, service) (rate(http_request_duration_seconds_bucket{SELECTOR}[5m])))',
    "cpu": 'sum by (service) (workload_cpu_cores{SELECTOR})',
    "mem": 'sum by (service) (workload_memory_bytes{SELECTOR})',
}
# "optimized" reads p99 from a RECORDING RULE (see observability/rules.yml).
# Prometheus runs the expensive histogram_quantile every 15s and stores the result as a new series,
# so reading it is a cheap lookup instead of crunching all the buckets on every request.
FAST_QUERIES = {
    "p99": 'service:http_request_duration_seconds:p99_5m{SELECTOR}',
    "cpu": RAW_QUERIES["cpu"],
    "mem": RAW_QUERIES["mem"],
}

# ------------------------------------------------------------------ this API's own metrics
# The API is monitored too: Prometheus scrapes http://graphql-api:8001/metrics.
# Histogram = counts requests into latency buckets, so Prometheus can compute p99 of THIS API.
# Counter   = a number that only goes up (total PromQL queries sent).
REQUEST_LATENCY = Histogram("graphql_request_duration_seconds", "GraphQL request latency", ["strategy"],
                            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5))
PROMQL_SENT = Counter("graphql_prometheus_queries_total", "PromQL queries sent to Prometheus", ["strategy"])
query_count = {"naive": 0, "batched": 0, "optimized": 0}   # same count, kept in a dict so GraphQL can return it


class Prometheus:
    """
    Talks to Prometheus's HTTP API (or pretends to).
    Every query returns a dict:  {"checkout": 0.42, "search": 0.13, ...}  (service name -> number)
    """

    def __init__(self, fake):
        self.fake = fake                                                  # True = don't call Prometheus, make up data
        self.http = httpx.AsyncClient(base_url=PROM_URL, timeout=5.0)     # one reusable HTTP connection pool
        # A busy Prometheus only runs a few heavy queries at a time (flag --query.max-concurrency).
        # We copy that with a Semaphore: at most 4 queries in flight; the rest wait their turn.
        # This is why the naive strategy (12 queries) is slow: its queries queue up behind each other.
        self.slots = asyncio.Semaphore(4)
        self.cache = {}   # promql text -> (time it was saved, result dict)

    async def list_services(self):
        """Which workloads exist? Asks Prometheus for every value of the 'service' label."""
        if self.fake:
            return FAKE_SERVICES
        response = await self.http.get("/api/v1/label/service/values")
        return sorted(response.json()["data"])

    async def query(self, promql, strategy, use_cache=False):
        """Run one PromQL query. Uses the cache if allowed, counts every real query."""
        # 1. Cache: if we asked the exact same PromQL less than 15s ago, reuse that answer.
        if use_cache and promql in self.cache:
            saved_at, result = self.cache[promql]
            if time.time() - saved_at < CACHE_SECONDS:
                return result

        # 2. Count it. This is how bench.py knows "12 queries per request" vs "3".
        query_count[strategy] += 1
        PROMQL_SENT.labels(strategy).inc()

        # 3. Wait for a free slot (max 4 at once), then ask Prometheus or the fake.
        async with self.slots:
            if self.fake:
                result = await self.fake_query(promql)
            else:
                result = await self.real_query(promql)

        if use_cache:
            self.cache[promql] = (time.time(), result)
        return result

    async def real_query(self, promql):
        """GET /api/v1/query?query=... on Prometheus, and turn the JSON into {service: value}."""
        response = await self.http.get("/api/v1/query", params={"query": promql})
        response.raise_for_status()                    # HTTP 4xx/5xx -> raise an error
        # Prometheus answers like:
        #   {"data": {"result": [{"metric": {"service": "checkout"}, "value": [1727600000, "0.42"]}, ...]}}
        result = {}
        for series in response.json()["data"]["result"]:
            service = series["metric"].get("service", "unknown")
            value = float(series["value"][1])          # value is [timestamp, "number as text"]
            result[service] = value
        return result

    async def fake_query(self, promql):
        """Pretend to be Prometheus: wait a realistic time, return stable made-up numbers."""
        # A histogram_quantile over raw buckets is expensive; a recording-rule lookup is cheap.
        if "histogram_quantile" in promql:
            cost_seconds = 0.045
        else:
            cost_seconds = 0.008
        await asyncio.sleep(cost_seconds * random.uniform(0.8, 1.3))

        services = FAKE_SERVICES
        if 'service="' in promql:                      # naive query for ONE service
            services = [promql.split('service="')[1].split('"')[0]]

        result = {}
        for service in services:
            seed = sum(ord(ch) for ch in service)      # same name -> same number every time
            if "duration" in promql:
                result[service] = 0.05 + (seed % 90) / 100               # seconds
            elif "cpu" in promql:
                result[service] = 0.1 + (seed % 20) / 10                  # CPU cores
            else:
                result[service] = (128 + seed * 3 % 2048) * 1024 * 1024  # bytes
        return result


PROM = None   # the one Prometheus client for the whole server; created in main()


def make_loader(metric, strategy):
    """
    Build a DataLoader for one metric (p99, cpu or mem). This is the fix for the N+1 problem.

    While answering ONE GraphQL request, each workload's resolver calls  loader.load("checkout"),
    loader.load("search"), ... The DataLoader doesn't run them one by one. It waits until all
    resolvers have asked, then calls load_many(["checkout", "search", ...]) ONCE.
    So 4 workloads -> 1 PromQL query instead of 4.
    """
    if strategy == "optimized":
        template = FAST_QUERIES[metric]
    else:
        template = RAW_QUERIES[metric]

    async def load_many(names):
        selector = 'service=~"' + "|".join(names) + '"'          # service=~"checkout|search|auth"
        promql = template.replace("SELECTOR", selector)
        values = await PROM.query(promql, strategy, use_cache=(strategy == "optimized"))
        # A DataLoader must return answers in the SAME ORDER as the names it was given.
        return [values.get(name) for name in names]

    return DataLoader(load_fn=load_many)


# ------------------------------------------------------------------ the GraphQL schema
# A class with @strawberry.type becomes a GraphQL type. Its fields become GraphQL fields.
# Python snake_case names become camelCase in GraphQL:  p99_latency_ms -> p99LatencyMs

@strawberry.type
class Workload:
    name: str
    namespace: str
    strategy: strawberry.Private[str]   # Private = our code uses it, but it's hidden from the GraphQL schema

    async def fetch(self, info, metric):
        """Get one metric for this workload, using the strategy chosen in the query."""
        if self.strategy == "naive":
            # One query for THIS workload and THIS field.
            # 4 workloads x 3 fields = 12 separate PromQL queries. That's the N+1 problem.
            promql = RAW_QUERIES[metric].replace("SELECTOR", f'service="{self.name}"')
            values = await PROM.query(promql, "naive")
            return values.get(self.name)
        # batched / optimized: hand the name to the DataLoader, which batches all workloads together.
        loader = info.context["loaders"][f"{self.strategy}:{metric}"]
        return await loader.load(self.name)

    # Each field below is a RESOLVER: a function GraphQL calls only if the client asked for that field.
    # So  { workloads { name } }  never touches Prometheus at all.

    @strawberry.field(description="p99 request latency over 5 minutes, in milliseconds")
    async def p99_latency_ms(self, info: strawberry.Info) -> Optional[float]:
        seconds = await self.fetch(info, "p99")
        if seconds is None:                            # Optional[float] = the answer may be null
            return None
        return round(seconds * 1000, 1)

    @strawberry.field(description="CPU cores in use")
    async def cpu_cores(self, info: strawberry.Info) -> Optional[float]:
        cores = await self.fetch(info, "cpu")
        if cores is None:
            return None
        return round(cores, 3)

    @strawberry.field(description="Memory in use, in MB")
    async def memory_mb(self, info: strawberry.Info) -> Optional[float]:
        num_bytes = await self.fetch(info, "mem")
        if num_bytes is None:
            return None
        return round(num_bytes / 1024 / 1024, 1)


@strawberry.type
class Query:
    """The entry points of the API: what you can write at the top level of a query."""

    @strawberry.field(description='All workloads. strategy is "naive", "batched" or "optimized".')
    async def workloads(self, strategy: str = "optimized", name_contains: Optional[str] = None) -> list[Workload]:
        if strategy not in STRATEGIES:
            # Raising an error here makes GraphQL return {"errors": [...]} with HTTP 200 (not 4xx!)
            raise ValueError(f"strategy must be one of {STRATEGIES}")
        workloads = []
        for name in await PROM.list_services():
            if name_contains and name_contains not in name:
                continue
            workloads.append(Workload(name=name, namespace="payments", strategy=strategy))
        return workloads

    @strawberry.field(description="How many PromQL queries were sent so far with this strategy.")
    def prometheus_query_count(self, strategy: str) -> int:
        return query_count[strategy]


async def get_context():
    """
    Runs once at the start of EVERY GraphQL request.
    Gives each request its own fresh DataLoaders, so batching never mixes two different requests.
    """
    loaders = {}
    for strategy in ["batched", "optimized"]:
        for metric in ["p99", "cpu", "mem"]:
            loaders[f"{strategy}:{metric}"] = make_loader(metric, strategy)
    return {"loaders": loaders}


# ------------------------------------------------------------------ the web app
app = FastAPI(title="Workload metrics GraphQL")
# /graphql      -> the GraphQL API (POST) and the GraphiQL browser page (GET)
app.include_router(GraphQLRouter(strawberry.Schema(query=Query), context_getter=get_context), prefix="/graphql")
# /metrics      -> this API's own metrics, in Prometheus text format
app.mount("/metrics", make_asgi_app())


@app.middleware("http")
async def measure_latency(request: Request, call_next):
    """Middleware = code that wraps EVERY HTTP request. Here: time each GraphQL request."""
    start = time.perf_counter()
    response = await call_next(request)                          # run the actual request
    if request.url.path.startswith("/graphql") and request.method == "POST":
        strategy = request.headers.get("x-strategy", "unknown")   # bench.py sends this header
        REQUEST_LATENCY.labels(strategy).observe(time.perf_counter() - start)
    return response


def prometheus_is_up():
    """Prometheus answers 200 on /-/ready when it's ready to serve queries."""
    try:
        return httpx.get(f"{PROM_URL}/-/ready", timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


def main():
    global PROM                       # "global" = assign the module-level PROM variable above
    fake = os.getenv("MOCK") == "1" or not prometheus_is_up()
    PROM = Prometheus(fake)
    if fake:
        print("Prometheus: FAKE data (start the Docker stack for real data)")
    else:
        print(f"Prometheus: {PROM_URL}")
    # 0.0.0.0 = listen on all network interfaces (needed inside Docker so other containers can reach it)
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8001)), log_level="warning")


if __name__ == "__main__":            # only run main() when started as a script, not when imported
    main()

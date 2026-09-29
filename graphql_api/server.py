"""
GraphQL API in front of Prometheus.
Answers: "what is the p99 latency, CPU and memory of each workload?"

This is the idea behind the CV line "GraphQL API cut Prometheus query times by 40%".
You can pick one of three strategies per request and compare them:

  naive      Every field of every workload runs its own PromQL query.
             4 workloads x 3 fields = 12 queries.  (the "N+1 problem")
  batched    One PromQL query per field, covering ALL workloads at once
             using  service=~"a|b|c".  3 fields = 3 queries.
  optimized  Same as batched, but p99 reads a pre-computed recording rule
             (cheap) and results are cached for 15 seconds.

Run:
  python graphql_api/server.py            # uses real Prometheus if it is running, otherwise fake data
  MOCK=1 python graphql_api/server.py     # always use fake data (no Docker needed)
Then open http://localhost:8001/graphql in a browser.
"""
import asyncio
import os
import random
import time
from typing import Optional

import httpx
import strawberry
import uvicorn
from fastapi import FastAPI, Request
from prometheus_client import Counter, Histogram, make_asgi_app
from strawberry.dataloader import DataLoader
from strawberry.fastapi import GraphQLRouter

PROM_URL = os.getenv("PROM_URL", "http://localhost:9090")
CACHE_SECONDS = 15  # same as Prometheus scrape_interval, so cached data is never staler than Prometheus
STRATEGIES = ["naive", "batched", "optimized"]
FAKE_SERVICES = ["checkout", "search", "auth", "ingest", "ledger", "notify", "kyc", "fx"]

# -----------------------------------------------------------------------------
# The PromQL behind each GraphQL field.
# SELECTOR gets replaced with a label filter, e.g.  service="checkout"
# -----------------------------------------------------------------------------
RAW_QUERIES = {
    "p99": 'histogram_quantile(0.99, sum by (le, service) (rate(http_request_duration_seconds_bucket{SELECTOR}[5m])))',
    "cpu": 'sum by (service) (workload_cpu_cores{SELECTOR})',
    "mem": 'sum by (service) (workload_memory_bytes{SELECTOR})',
}
# The "optimized" strategy reads p99 from a recording rule (see observability/rules.yml).
FAST_QUERIES = {
    "p99": 'service:http_request_duration_seconds:p99_5m{SELECTOR}',
    "cpu": RAW_QUERIES["cpu"],
    "mem": RAW_QUERIES["mem"],
}

# -----------------------------------------------------------------------------
# This API measures itself too. Prometheus scrapes these from /metrics.
# -----------------------------------------------------------------------------
REQUEST_LATENCY = Histogram("graphql_request_duration_seconds", "GraphQL request latency", ["strategy"],
                            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5))
PROMQL_SENT = Counter("graphql_prometheus_queries_total", "PromQL queries sent to Prometheus", ["strategy"])
query_count = {"naive": 0, "batched": 0, "optimized": 0}   # same number, easy to read from GraphQL


class Prometheus:
    """Sends PromQL to Prometheus (or fakes it) and returns {service_name: value}."""

    def __init__(self, fake):
        self.fake = fake
        self.http = httpx.AsyncClient(base_url=PROM_URL, timeout=5.0)
        # A busy Prometheus only runs a few heavy queries at a time (--query.max-concurrency).
        # We copy that: at most 4 queries in flight, the rest wait in line.
        self.slots = asyncio.Semaphore(4)
        self.cache = {}   # promql -> (time it was saved, result)

    async def list_services(self):
        if self.fake:
            return FAKE_SERVICES
        response = await self.http.get("/api/v1/label/service/values")
        return sorted(response.json()["data"])

    async def query(self, promql, strategy, use_cache=False):
        # 1. Serve from cache if we have a fresh answer
        if use_cache and promql in self.cache:
            saved_at, result = self.cache[promql]
            if time.time() - saved_at < CACHE_SECONDS:
                return result

        # 2. Count every query that really goes to Prometheus
        query_count[strategy] += 1
        PROMQL_SENT.labels(strategy).inc()

        # 3. Ask Prometheus (or the fake)
        async with self.slots:
            if self.fake:
                result = await self.fake_query(promql)
            else:
                result = await self.real_query(promql)

        if use_cache:
            self.cache[promql] = (time.time(), result)
        return result

    async def real_query(self, promql):
        response = await self.http.get("/api/v1/query", params={"query": promql})
        response.raise_for_status()
        result = {}
        for series in response.json()["data"]["result"]:
            service = series["metric"].get("service", "unknown")
            value = float(series["value"][1])   # Prometheus returns [timestamp, "value as string"]
            result[service] = value
        return result

    async def fake_query(self, promql):
        # histogram_quantile over raw buckets is expensive; a recording rule is a quick lookup
        if "histogram_quantile" in promql:
            cost_seconds = 0.045
        else:
            cost_seconds = 0.008
        await asyncio.sleep(cost_seconds * random.uniform(0.8, 1.3))

        services = FAKE_SERVICES
        if 'service="' in promql:   # query for one service only
            services = [promql.split('service="')[1].split('"')[0]]

        result = {}
        for service in services:
            seed = sum(ord(ch) for ch in service)   # same service -> same fake numbers every time
            if "duration" in promql:
                result[service] = 0.05 + (seed % 90) / 100               # seconds
            elif "cpu" in promql:
                result[service] = 0.1 + (seed % 20) / 10                  # cores
            else:
                result[service] = (128 + seed * 3 % 2048) * 1024 * 1024  # bytes
        return result


PROM = None   # created in main()


def make_loader(metric, strategy):
    """
    A DataLoader collects every  loader.load("checkout"), loader.load("search"), ...
    made while answering ONE GraphQL request, then calls load_many() ONCE with all names.
    That turns 4 PromQL queries into 1.
    """
    if strategy == "optimized":
        template = FAST_QUERIES[metric]
    else:
        template = RAW_QUERIES[metric]

    async def load_many(names):
        selector = 'service=~"' + "|".join(names) + '"'          # service=~"checkout|search|auth"
        promql = template.replace("SELECTOR", selector)
        values = await PROM.query(promql, strategy, use_cache=(strategy == "optimized"))
        return [values.get(name) for name in names]              # same order as the names we got

    return DataLoader(load_fn=load_many)


@strawberry.type
class Workload:
    name: str
    namespace: str
    strategy: strawberry.Private[str]   # Private = used by our code, hidden from the GraphQL schema

    async def fetch(self, info, metric):
        if self.strategy == "naive":
            # One query for THIS workload and THIS field. Many workloads x many fields = many queries.
            promql = RAW_QUERIES[metric].replace("SELECTOR", f'service="{self.name}"')
            values = await PROM.query(promql, "naive")
            return values.get(self.name)
        loader = info.context["loaders"][f"{self.strategy}:{metric}"]
        return await loader.load(self.name)

    # Each field has its own resolver, so fields the client doesn't ask for never hit Prometheus.
    @strawberry.field(description="p99 request latency over 5 minutes, in milliseconds")
    async def p99_latency_ms(self, info: strawberry.Info) -> Optional[float]:
        seconds = await self.fetch(info, "p99")
        if seconds is None:
            return None
        return round(seconds * 1000, 1)

    @strawberry.field
    async def cpu_cores(self, info: strawberry.Info) -> Optional[float]:
        cores = await self.fetch(info, "cpu")
        if cores is None:
            return None
        return round(cores, 3)

    @strawberry.field
    async def memory_mb(self, info: strawberry.Info) -> Optional[float]:
        num_bytes = await self.fetch(info, "mem")
        if num_bytes is None:
            return None
        return round(num_bytes / 1024 / 1024, 1)


@strawberry.type
class Query:
    @strawberry.field(description='All workloads. strategy is "naive", "batched" or "optimized".')
    async def workloads(self, strategy: str = "optimized", name_contains: Optional[str] = None) -> list[Workload]:
        if strategy not in STRATEGIES:
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
    """Runs once per GraphQL request: fresh DataLoaders, so batching never mixes two requests."""
    loaders = {}
    for strategy in ["batched", "optimized"]:
        for metric in ["p99", "cpu", "mem"]:
            loaders[f"{strategy}:{metric}"] = make_loader(metric, strategy)
    return {"loaders": loaders}


app = FastAPI(title="Workload metrics GraphQL")
app.include_router(GraphQLRouter(strawberry.Schema(query=Query), context_getter=get_context), prefix="/graphql")
app.mount("/metrics", make_asgi_app())


@app.middleware("http")
async def measure_latency(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    if request.url.path.startswith("/graphql") and request.method == "POST":
        strategy = request.headers.get("x-strategy", "unknown")   # bench.py sets this header
        REQUEST_LATENCY.labels(strategy).observe(time.perf_counter() - start)
    return response


def prometheus_is_up():
    try:
        return httpx.get(f"{PROM_URL}/-/ready", timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


def main():
    global PROM
    fake = os.getenv("MOCK") == "1" or not prometheus_is_up()
    PROM = Prometheus(fake)
    if fake:
        print("Prometheus: FAKE data (start Docker + sample_app.py for real data)")
    else:
        print(f"Prometheus: {PROM_URL}")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8001)), log_level="warning")


if __name__ == "__main__":
    main()

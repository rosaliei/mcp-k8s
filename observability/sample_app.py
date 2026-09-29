"""
Fake microservices that expose Prometheus metrics, exactly like a real service would.

WHAT IT DOES
  Pretends to be 4 services handling traffic. For every fake request it records metrics.
  Prometheus scrapes them from http://<host>:8000/metrics every 15 seconds.

HOW TO RUN
  In Docker (normal):  docker compose up -d                     (service "sample-app")
  Make one slow:       INCIDENT=checkout docker compose up -d sample-app
  On your Mac:         python observability/sample_app.py [--incident checkout]
  Look at the raw metrics:  curl localhost:8000/metrics | grep http_request

THE 4 METRIC TYPES (this file uses 3 of them; the 4th, Summary, is rarely used)
  Counter    only goes up. "How many requests so far?" Always read it with rate() in PromQL.
  Gauge      goes up and down. "How much CPU / memory right now?"
  Histogram  counts values into buckets: "how many requests took <= 0.1s, <= 0.25s, ...".
             Prometheus calculates percentiles (p99) from these buckets with histogram_quantile().

LABELS
  Each metric has labels like service="checkout". One metric name + one set of label values
  = one "time series". Keep label values low-cardinality: service names are fine, user IDs are not
  (millions of series would run Prometheus out of memory).

THE SERVICES (each with a different latency "shape", so the p50 vs p99 gap is visible)
  checkout  fast, but 2% of requests hit a slow DB lock (long tail)
  search    steady, medium speed
  auth      fast and consistent (tiny tail)
  ingest    slow and noisy, sometimes returns 500
  With INCIDENT=<service>, that service gets much slower. The HighP99Latency alert fires in about 2 minutes.
"""
import argparse
import os
import random
import threading     # runs several loops at the same time (one per fake service)
import time

from prometheus_client import Counter, Gauge, Histogram, start_http_server

# Histogram bucket edges, in seconds. The metric counts how many requests were <= each edge.
# Tip: put an edge exactly at your SLO (here 0.5s), so p99 around the SLO is accurate.
BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]

# Metric name, help text, label names. These names are what you type in PromQL.
LATENCY = Histogram("http_request_duration_seconds", "Request latency", ["service"], buckets=BUCKETS)
REQUESTS = Counter("http_requests_total", "Requests", ["service", "code"])          # code = HTTP status
CPU = Gauge("workload_cpu_cores", "CPU cores in use", ["service", "namespace"])
MEMORY = Gauge("workload_memory_bytes", "Memory in use", ["service", "namespace"])

# How each fake service behaves:
#   typical_s    normal response time in seconds
#   slow_chance  fraction of requests that are slow (0.02 = 2%)
#   slow_s       how slow the slow ones are
#   error_rate   fraction of requests returning HTTP 500
SERVICES = {
    "checkout": {"typical_s": 0.040, "slow_chance": 0.02,  "slow_s": 0.900, "error_rate": 0.002, "cpu": 0.60, "memory_mb": 512},
    "search":   {"typical_s": 0.120, "slow_chance": 0.01,  "slow_s": 0.300, "error_rate": 0.001, "cpu": 1.20, "memory_mb": 1536},
    "auth":     {"typical_s": 0.015, "slow_chance": 0.001, "slow_s": 0.080, "error_rate": 0.000, "cpu": 0.25, "memory_mb": 256},
    "ingest":   {"typical_s": 0.200, "slow_chance": 0.05,  "slow_s": 1.500, "error_rate": 0.030, "cpu": 0.90, "memory_mb": 1024},
}


def one_request_latency(settings, broken):
    """Make up how long one request took, in seconds."""
    slow_chance = settings["slow_chance"]
    slow_s = settings["slow_s"]
    if broken:                                       # incident: 15% of requests take ~1.8s
        slow_chance, slow_s = 0.15, 1.8
    if random.random() < slow_chance:
        return random.uniform(slow_s * 0.6, slow_s * 1.4)            # a slow request
    return random.lognormvariate(0, 0.35) * settings["typical_s"]   # a normal request (varies a little)


def generate_traffic(service, settings, incident_service):
    """Runs forever in its own thread: fake one request, record metrics, repeat."""
    broken = service == incident_service
    while True:
        # Histogram: .observe(value) puts the value into the right bucket(s)
        LATENCY.labels(service).observe(one_request_latency(settings, broken))

        # Counter: .inc() adds 1. Label code="500" or "200", so PromQL can compute an error ratio.
        error_rate = settings["error_rate"] * (5 if broken else 1)
        code = "500" if random.random() < error_rate else "200"
        REQUESTS.labels(service, code).inc()

        # Gauges: .set(value) replaces the current value
        cpu = settings["cpu"] * random.uniform(0.8, 1.2) * (2 if broken else 1)
        CPU.labels(service, "payments").set(cpu)
        MEMORY.labels(service, "payments").set(settings["memory_mb"] * 1024 * 1024 * random.uniform(0.9, 1.05))

        time.sleep(random.uniform(0.005, 0.02))      # about 50-200 requests per second per service


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    # In Docker, set the INCIDENT environment variable instead of the flag:
    #   INCIDENT=checkout docker compose up -d sample-app
    parser.add_argument("--incident", default=os.getenv("INCIDENT") or None, choices=list(SERVICES),
                        help="make one service slow")
    args = parser.parse_args()

    start_http_server(args.port)                     # serves /metrics in a background thread
    for service, settings in SERVICES.items():
        # One background thread per service. daemon=True = stop when the main program stops.
        threading.Thread(target=generate_traffic, args=(service, settings, args.incident), daemon=True).start()

    print(f"sample app: http://localhost:{args.port}/metrics   incident={args.incident}")
    while True:                                      # keep the main program alive
        time.sleep(3600)


if __name__ == "__main__":
    main()

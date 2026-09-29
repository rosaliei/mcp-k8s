"""
Fake microservices that expose Prometheus metrics, like a real service would.

Run:  python observability/sample_app.py        -> metrics on http://localhost:8000/metrics
      python observability/sample_app.py --incident checkout   -> make checkout slow

Each service has a different latency "shape", so the gap between p50 and p99 is visible:
  checkout  fast, but 2% of requests hit a slow DB lock (long tail)
  search    steady, medium speed
  auth      fast and consistent (tiny tail)
  ingest    slow and noisy, sometimes returns 500

With --incident, that service gets much slower and the HighP99Latency alert fires in about 2 minutes.
"""
import argparse
import os
import random
import threading
import time

from prometheus_client import Counter, Gauge, Histogram, start_http_server

# Histogram buckets in seconds. Prometheus counts how many requests fall at or below each one.
BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]

LATENCY = Histogram("http_request_duration_seconds", "Request latency", ["service"], buckets=BUCKETS)
REQUESTS = Counter("http_requests_total", "Requests", ["service", "code"])
CPU = Gauge("workload_cpu_cores", "CPU cores in use", ["service", "namespace"])
MEMORY = Gauge("workload_memory_bytes", "Memory in use", ["service", "namespace"])

SERVICES = {
    "checkout": {"typical_s": 0.040, "slow_chance": 0.02,  "slow_s": 0.900, "error_rate": 0.002, "cpu": 0.60, "memory_mb": 512},
    "search":   {"typical_s": 0.120, "slow_chance": 0.01,  "slow_s": 0.300, "error_rate": 0.001, "cpu": 1.20, "memory_mb": 1536},
    "auth":     {"typical_s": 0.015, "slow_chance": 0.001, "slow_s": 0.080, "error_rate": 0.000, "cpu": 0.25, "memory_mb": 256},
    "ingest":   {"typical_s": 0.200, "slow_chance": 0.05,  "slow_s": 1.500, "error_rate": 0.030, "cpu": 0.90, "memory_mb": 1024},
}


def one_request_latency(settings, broken):
    slow_chance = settings["slow_chance"]
    slow_s = settings["slow_s"]
    if broken:
        slow_chance, slow_s = 0.15, 1.8
    if random.random() < slow_chance:
        return random.uniform(slow_s * 0.6, slow_s * 1.4)            # a slow request
    return random.lognormvariate(0, 0.35) * settings["typical_s"]   # a normal request (varies a bit)


def generate_traffic(service, settings, incident_service):
    broken = service == incident_service
    while True:
        LATENCY.labels(service).observe(one_request_latency(settings, broken))

        error_rate = settings["error_rate"] * (5 if broken else 1)
        code = "500" if random.random() < error_rate else "200"
        REQUESTS.labels(service, code).inc()

        cpu = settings["cpu"] * random.uniform(0.8, 1.2) * (2 if broken else 1)
        CPU.labels(service, "payments").set(cpu)
        MEMORY.labels(service, "payments").set(settings["memory_mb"] * 1024 * 1024 * random.uniform(0.9, 1.05))

        time.sleep(random.uniform(0.005, 0.02))   # about 50-200 requests per second per service


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    # In Docker, set the INCIDENT env var instead:  INCIDENT=checkout docker compose up -d sample-app
    parser.add_argument("--incident", default=os.getenv("INCIDENT") or None, choices=list(SERVICES),
                        help="make one service slow")
    args = parser.parse_args()

    start_http_server(args.port)
    for service, settings in SERVICES.items():
        # one background thread per service, each pretending to handle requests
        threading.Thread(target=generate_traffic, args=(service, settings, args.incident), daemon=True).start()

    print(f"sample app: http://localhost:{args.port}/metrics   incident={args.incident}")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()

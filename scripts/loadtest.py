"""
Load test for the hot read paths (Phase 10.2).

    python scripts/loadtest.py --base http://127.0.0.1:8003 --users 20 --requests 10

Signs in once per simulated user (OTP must be off: REQUIRE_OTP_FOR_LOGIN=False)
and hammers the cost code list, the dashboard and the review queue from N
threads, then prints p50 / p95 / max per endpoint and the error count. The
target in ARCHITECTURE.md is p95 under 500 ms for the cost code list and under
1.5 s for the dashboard at 20 concurrent users on the demo data set.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time

import requests

ENDPOINTS = {
    "cost codes": "/api/v1/estates/{estate}/cost-codes/?page_size=25",
    "dashboard": "/api/v1/dashboard/",
    "review queue": "/api/v1/review-queue/",
    "tests": "/api/v1/tests/?page_size=100",
}


def sign_in(base: str, email: str, password: str) -> str:
    response = requests.post(
        f"{base}/api/v1/auth/login/", json={"email": email, "password": password}, timeout=30
    )
    response.raise_for_status()
    body = response.json()
    if body.get("otp_required"):
        sys.exit("OTP is on; run the server with REQUIRE_OTP_FOR_LOGIN=False for a load test.")
    return body["access"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8003")
    parser.add_argument("--email", default="admin@example.com")
    parser.add_argument("--password", default="Passw0rd!23")
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--requests", type=int, default=10, help="per user, per endpoint")
    args = parser.parse_args()

    token = sign_in(args.base, args.email, args.password)
    headers = {"Authorization": f"Bearer {token}"}
    estates = requests.get(f"{args.base}/api/v1/estates/", headers=headers, timeout=30).json()[
        "results"
    ]
    estate = estates[0]["estate_id"]

    timings: dict[str, list[float]] = {name: [] for name in ENDPOINTS}
    errors: dict[str, int] = dict.fromkeys(ENDPOINTS, 0)
    lock = threading.Lock()

    def worker():
        session = requests.Session()
        session.headers.update(headers)
        for _ in range(args.requests):
            for name, path in ENDPOINTS.items():
                started = time.perf_counter()
                try:
                    response = session.get(args.base + path.format(estate=estate), timeout=60)
                    ok = response.status_code == 200
                except requests.RequestException:
                    ok = False
                elapsed = (time.perf_counter() - started) * 1000
                with lock:
                    timings[name].append(elapsed)
                    if not ok:
                        errors[name] += 1

    threads = [threading.Thread(target=worker) for _ in range(args.users)]
    wall = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - wall

    total = sum(len(v) for v in timings.values())
    print(
        f"{args.users} users x {args.requests} rounds = {total} requests in {wall:.1f}s ({total / wall:.0f} req/s)"
    )
    print(f"{'endpoint':<14}{'n':>6}{'p50 ms':>10}{'p95 ms':>10}{'max ms':>10}{'errors':>8}")
    worst = 0
    for name, values in timings.items():
        values.sort()
        p50 = statistics.median(values)
        p95 = values[int(len(values) * 0.95) - 1] if len(values) >= 20 else values[-1]
        worst = max(worst, errors[name])
        print(
            f"{name:<14}{len(values):>6}{p50:>10.0f}{p95:>10.0f}{values[-1]:>10.0f}{errors[name]:>8}"
        )
    return 1 if worst else 0


if __name__ == "__main__":
    sys.exit(main())

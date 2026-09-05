"""End-to-end smoke test against a running deployment.

Exercises the reseller API and the invariants that matter most, using only
public interfaces. It never fabricates a payment: it asserts that an *unpaid*
order stays unpaid and undelivered, which is the property that actually needs
guarding.

    python -m scripts.smoke_test --base-url http://localhost:8000 --api-key rt_live_...

Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

import httpx

OK_MARK = "\033[32m✓\033[0m"
FAIL_MARK = "\033[31m✗\033[0m"


class Smoke:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.failures: list[str] = []
        self.checks = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks += 1
        print(f"  {OK_MARK if ok else FAIL_MARK} {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            self.failures.append(name)

    async def run(self) -> int:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await self._health(client)
            await self._auth(client)
            product_id = await self._products(client)
            if product_id:
                await self._orders(client, product_id)

        print()
        if self.failures:
            print(f"{FAIL_MARK} {len(self.failures)}/{self.checks} checks failed:")
            for failure in self.failures:
                print(f"    - {failure}")
            return 1
        print(f"{OK_MARK} all {self.checks} checks passed")
        return 0

    async def _health(self, client: httpx.AsyncClient) -> None:
        print("\nHealth")
        response = await client.get(f"{self.base_url}/health")
        self.check("GET /health returns 200", response.status_code == 200)
        self.check(
            "health response carries a request id",
            response.headers.get("X-Request-ID", "").startswith("api_"),
        )

        ready = await client.get(f"{self.base_url}/ready")
        checks = ready.json().get("checks", {})
        self.check("database reachable", checks.get("database") == "ok", str(checks))
        self.check("redis reachable", checks.get("redis") == "ok", str(checks))
        body = ready.text.lower()
        self.check(
            "readiness leaks no connection details",
            not any(token in body for token in ("password", "postgres://", "redis://")),
        )

    async def _auth(self, client: httpx.AsyncClient) -> None:
        print("\nAuthentication")
        anonymous = await client.get(f"{self.base_url}/api/v1/products")
        self.check("unauthenticated request is rejected", anonymous.status_code == 401)

        bad = await client.get(
            f"{self.base_url}/api/v1/products",
            headers={"Authorization": "Bearer rt_live_deadbeef_" + "x" * 40},
        )
        self.check("unknown key is rejected", bad.status_code == 401)
        self.check(
            "auth failures are indistinguishable",
            anonymous.json()["error"]["code"] == bad.json()["error"]["code"],
        )

    async def _products(self, client: httpx.AsyncClient) -> str | None:
        print("\nProducts")
        response = await client.get(f"{self.base_url}/api/v1/products", headers=self.headers)
        self.check("GET /products returns 200", response.status_code == 200)
        if response.status_code != 200:
            return None

        data = response.json().get("data", [])
        self.check("at least one product is available", bool(data))
        if not data:
            return None

        product = data[0]
        self.check("product exposes reseller pricing", "wholesale_price" in product["pricing"])
        self.check(
            "internal delivery data is not exposed",
            not any(key in product for key in ("delivery_payload", "delivery_file_id")),
        )
        return product["id"] if product.get("in_stock") else None

    async def _orders(self, client: httpx.AsyncClient, product_id: str) -> None:
        print("\nOrders")
        key = str(uuid.uuid4())
        body = {"product_id": product_id, "quantity": 1, "customer_reference": "smoke-test"}

        first = await client.post(
            f"{self.base_url}/api/v1/orders",
            headers={**self.headers, "Idempotency-Key": key},
            json=body,
        )
        self.check("POST /orders creates an order", first.status_code == 201, first.text[:120])
        if first.status_code != 201:
            return
        order = first.json()

        replay = await client.post(
            f"{self.base_url}/api/v1/orders",
            headers={**self.headers, "Idempotency-Key": key},
            json=body,
        )
        self.check("idempotent replay returns 200", replay.status_code == 200)
        self.check("idempotent replay returns the same order", replay.json()["id"] == order["id"])

        conflict = await client.post(
            f"{self.base_url}/api/v1/orders",
            headers={**self.headers, "Idempotency-Key": key},
            json={**body, "quantity": 7},
        )
        self.check("reusing a key with a different body conflicts", conflict.status_code == 409)

        print("\nPayment integrity")
        fetched = await client.get(
            f"{self.base_url}/api/v1/orders/{order['id']}", headers=self.headers
        )
        state = fetched.json()
        self.check("a new order is not paid", state["paid_at"] is None)
        self.check(
            "order is awaiting payment",
            state["status"] in {"created", "payment_pending"},
            state["status"],
        )

        delivery = await client.get(
            f"{self.base_url}/api/v1/orders/{order['id']}/delivery", headers=self.headers
        )
        self.check("an unpaid order delivers nothing", delivery.json()["items"] == [])
        self.check("delivery status is pending", delivery.json()["status"] == "pending")

        missing = await client.get(
            f"{self.base_url}/api/v1/orders/00000000-0000-0000-0000-000000000000",
            headers=self.headers,
        )
        self.check("unknown order returns 404", missing.status_code == 404)


def main() -> None:
    parser = argparse.ArgumentParser(description="End-to-end smoke test")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", required=True, help="A reseller API key")
    args = parser.parse_args()

    print(f"Smoke testing {args.base_url}")
    sys.exit(asyncio.run(Smoke(args.base_url, args.api_key).run()))


if __name__ == "__main__":
    main()

"""Simulated agents: each client is one user with its own keep-alive connection to /mcp."""

# ruff: noqa: PLR2004, PLR0913, PLR0917, S311

from __future__ import annotations

import asyncio
import itertools
import random
import time
from typing import TYPE_CHECKING, Any

import httpx

from .population import Meta, UserMeta, Vocabulary, audiences_of

if TYPE_CHECKING:
    from collections.abc import Callable

HEADERS = {"Accept": "application/json, text/event-stream"}
MIX = (  # (operation, weight); search-heavy like an agent fleet
    ("consult", 62), ("get", 12), ("remember", 8), ("update", 5), ("list", 4), ("rate", 3),
    ("promote", 3), ("history", 3),
)  # fmt: skip
Record = tuple[str, float, float, str]


class Client:
    def __init__(
        self,
        user: UserMeta,
        meta: Meta,
        vocab: Vocabulary,
        url: str,
        seed: int,
        records: list[Record],
        clock: Callable[[], float],
    ) -> None:
        self.user, self.meta, self.vocab, self.url = user, meta, vocab, url
        self.rng = random.Random(seed)
        self.records, self.clock = records, clock
        self.ids = itertools.count(1)
        self.mine: list[list[Any]] = []  # [id, revision] of notes this client wrote
        self.found: list[str] = []  # ids of the latest search results
        self.promoted: set[str] = set()
        self.audiences = [a for a in audiences_of(user) if self.meta.audiences.get(a)]
        self.http = httpx.AsyncClient(
            headers={**HEADERS, "Authorization": f"Bearer {user.token}"},
            timeout=60,
            limits=httpx.Limits(max_connections=2),
        )

    async def call(self, op: str, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """One timed tools/call; failures are recorded, never raised."""
        body = {"jsonrpc": "2.0", "id": next(self.ids), "method": "tools/call",
                "params": {"name": tool, "arguments": args}}  # fmt: skip
        started = self.clock()
        t = time.perf_counter()
        outcome, out = "ok", None
        try:
            try:
                resp = await self.http.post(f"{self.url}/mcp", json=body)
            except (httpx.ReadError, httpx.RemoteProtocolError):
                # the server closed an idle keep-alive connection as we sent on it: reconnect once
                self.records.append(("reconnect", started, 0.0, "ok"))
                resp = await self.http.post(f"{self.url}/mcp", json=body)
            if resp.status_code != 200:
                outcome = f"http {resp.status_code}"
            else:
                data = resp.json()
                result = data.get("result") or {}
                if "error" in data:
                    outcome = f"rpc {str(data['error'])[:80]}"
                elif result.get("isError"):
                    text = str(result.get("content", ""))[:200]
                    outcome = "conflict" if "revision" in text else f"tool {text[:80]}"
                else:
                    out = result.get("structuredContent") or {}
        except (httpx.HTTPError, ValueError) as exc:
            outcome = f"{type(exc).__name__} {str(exc)[:60]}"
        self.records.append((op, started, time.perf_counter() - t, outcome))
        return out

    def pick_id(self) -> str | None:
        if not self.audiences:
            return None
        return self.rng.choice(self.meta.audiences[self.rng.choice(self.audiences)])

    async def step(self) -> None:
        op = self.rng.choices([m[0] for m in MIX], [m[1] for m in MIX])[0]
        await getattr(self, f"do_{op}")()

    async def do_consult(self) -> None:
        out = await self.call("consult", "kb_consult",
                              {"query": self.vocab.query(self.rng), "limit": 8})  # fmt: skip
        if out:
            self.found = [r["id"] for r in out.get("results", [])]

    async def do_get(self) -> None:
        item_id = self.rng.choice(self.found) if self.found and self.rng.random() < 0.5 \
            else self.pick_id()  # fmt: skip
        if item_id:
            await self.call("get", "kb_get", {"id": item_id})

    async def do_remember(self) -> None:
        words = self.vocab.text(self.rng, self.rng.randrange(60), self.rng.randint(30, 150))
        args: dict[str, Any] = {"title": " ".join(words[:6]), "body": " ".join(words)}
        if self.user.projects and self.rng.random() < 0.5:
            args["share_with_project"] = self.rng.choice(self.user.projects)
        out = await self.call("remember", "kb_remember", args)
        if out:
            self.mine.append([out["id"], out["revision"]])

    async def do_update(self) -> None:
        if not self.mine:
            return await self.do_remember()
        entry = self.rng.choice(self.mine)
        words = self.vocab.text(self.rng, self.rng.randrange(60), self.rng.randint(30, 120))
        out = await self.call("update", "kb_update", {"id": entry[0], "expected_revision": entry[1],
                                                       "body": " ".join(words)})  # fmt: skip
        if out:
            entry[1] = out["revision"]

    async def do_list(self) -> None:
        await self.call("list", "kb_list", {"limit": 20})

    async def do_rate(self) -> None:
        if self.found:
            await self.call("rate", "kb_rate", {"id": self.rng.choice(self.found), "useful": True})

    async def do_history(self) -> None:
        if self.mine:
            await self.call("history", "kb_history", {"id": self.rng.choice(self.mine)[0]})

    async def do_promote(self) -> None:
        if self.user.role == "admin":
            pending = await self.call("promote", "kb_list_promotions", {"status": "pending"})
            for promo in (pending or {}).get("result", [])[:1]:
                await self.call("promote", "kb_decide_promotion",
                                {"id": promo["id"], "decision": "approve"})  # fmt: skip
            return
        todo = [m for m in self.mine if m[0] not in self.promoted]
        if todo:
            entry = self.rng.choice(todo)
            self.promoted.add(entry[0])
            await self.call("promote", "kb_request_promotion", {"id": entry[0], "to": "org"})

    async def run(self, until: float, think: float = 0.0) -> None:
        """`think`: mean seconds between calls (exponential); 0 = as fast as the server answers."""
        while self.clock() < until:
            if think:
                await asyncio.sleep(self.rng.expovariate(1 / think))
            await self.step()

    async def close(self) -> None:
        await self.http.aclose()


def choose_users(meta: Meta, clients: int, seed: int) -> list[UserMeta]:
    """One admin per organisation first (they decide promotions), then random members."""
    rng = random.Random(seed)
    admins = [next(u for u in meta.users if u.org == org and u.role == "admin")
              for org in sorted({u.org for u in meta.users})]  # fmt: skip
    rest = [u for u in meta.users if u not in admins]
    rng.shuffle(rest)
    pool = (admins + rest) * (clients // len(meta.users) + 1)
    return pool[:clients]


async def drive(
    meta: Meta,
    vocab: Vocabulary,
    url: str,
    clients: int,
    seconds: float,
    warmup: float,
    seed: int,
    rate: float = 0.0,
) -> list[Record]:
    """Run the clients for warmup + seconds; records inside the warmup are dropped.
    `rate`: total calls per second to aim for (0 = saturate the server)."""
    think = clients / rate if rate else 0.0
    records: list[Record] = []
    t0 = time.perf_counter()

    def clock() -> float:
        return time.perf_counter() - t0

    pool = [Client(u, meta, vocab, url, seed * 997 + n, records, clock)
            for n, u in enumerate(choose_users(meta, clients, seed))]  # fmt: skip
    try:
        await asyncio.gather(*(c.run(warmup + seconds, think) for c in pool))
    finally:
        await asyncio.gather(*(c.close() for c in pool))
    return [(op, s - warmup, d, o) for op, s, d, o in records if s >= warmup]

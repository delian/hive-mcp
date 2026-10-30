"""A deterministic synthetic organisation set and its ingest through the real service stack.

Note `i` depends only on (seed, i), so a corpus can be grown from 5k to 20k notes by ingesting
the missing indexes. Everything the workload needs (users, tokens, ids a user can read) is kept
in `meta.json` next to the data so a later run can reuse the data."""

# ruff: noqa: PLR2004, PLR0913, PLR0917, S311, ANN401

from __future__ import annotations

import asyncio
import json
import math
import random
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from hive_mcp.domain.item import Kind
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel, Visibility
from hive_mcp.services.models import RememberRequest

if TYPE_CHECKING:
    from pathlib import Path

    from hive_mcp.bootstrap import Services
    from hive_mcp.security.tokens import TokenStore

ORGS = (("acme", 0.6), ("globex", 0.3), ("initech", 0.1))
SYLLABLES = ["ra", "to", "mi", "ne", "ko", "sa", "li", "dar", "ven", "pol", "qui", "xe", "bu", "fa"]
KINDS = (Kind.NOTE,) * 6 + (Kind.HOWTO, Kind.DECISION, Kind.MEASUREMENT, Kind.BEST_PRACTICE)
RESERVOIR = 400  # ids remembered per audience, for get/update targets
TOPICS = 60


@dataclass
class UserMeta:
    id: str
    org: str
    role: str
    teams: list[str]
    projects: list[str]
    token: str

    def principal(self) -> Principal:
        return Principal(
            self.id,
            self.org,
            Role[self.role.upper()],
            frozenset(self.teams),
            frozenset(self.projects),
        )


@dataclass
class Meta:
    seed: int
    notes: int = 0
    users: list[UserMeta] = field(default_factory=list)
    audiences: dict[str, list[str]] = field(default_factory=dict)
    seen: dict[str, int] = field(default_factory=dict)  # notes offered to each reservoir

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self)))

    @classmethod
    def load(cls, path: Path) -> Meta:
        raw = json.loads(path.read_text())
        raw["users"] = [UserMeta(**u) for u in raw["users"]]
        return cls(**raw)


class Vocabulary:
    def __init__(self, seed: int) -> None:
        rng = random.Random(seed)
        words: set[str] = set()
        while len(words) < 6000:
            words.add("".join(rng.choices(SYLLABLES, k=rng.randint(2, 4))))
        self.words = sorted(words)
        self.topics = [rng.sample(self.words, 120) for _ in range(TOPICS)]
        self.zipf = [1 / (r + 1) for r in range(len(self.words))]

    def text(self, rng: random.Random, topic: int, count: int) -> list[str]:
        mine = self.topics[topic]
        picks = rng.choices(mine, k=count)
        noise = rng.choices(self.words, weights=self.zipf, k=count)
        return [n if rng.random() < 0.3 else p for p, n in zip(picks, noise, strict=True)]

    def query(self, rng: random.Random) -> str:
        return " ".join(rng.sample(self.topics[rng.randrange(TOPICS)], rng.randint(2, 4)))


def make_users(notes: int, seed: int, tokens: TokenStore) -> list[UserMeta]:
    rng = random.Random(seed)
    total = min(max(notes // 250, 12), 240)
    users: list[UserMeta] = []
    for org, share in ORGS:
        count = max(4, round(total * share))
        projects = [f"{org}-p{j}" for j in range(max(3, count // 8))]
        teams = [f"{org}-t{j}" for j in range(max(2, count // 15))]
        for n in range(count):
            role = "admin" if n % 20 == 0 else "editor" if n % 20 == 1 else "member"
            u = UserMeta(
                f"{org}-u{n}", org, role,
                teams if role == "admin" else rng.sample(teams, rng.randint(0, min(2, len(teams)))),
                rng.sample(projects, rng.randint(1, min(3, len(projects)))),
                "",
            )  # fmt: skip
            u.token = tokens.create(u.principal(), label=f"lt-{u.id}")
            users.append(u)
    return users


def _offer(meta: Meta, key: str, item_id: str, rng: random.Random) -> None:
    """Reservoir sampling: a bounded, uniform sample of the ids of each audience."""
    seen = meta.seen[key] = meta.seen.get(key, 0) + 1
    pool = meta.audiences.setdefault(key, [])
    if len(pool) < RESERVOIR:
        pool.append(item_id)
    elif (j := rng.randrange(seen)) < RESERVOIR:
        pool[j] = item_id


def audiences_of(user: UserMeta) -> list[str]:
    return [
        f"private:{user.id}", f"org:{user.org}", f"vis:{user.org}",
        *(f"project:{p}" for p in user.projects), *(f"team:{t}" for t in user.teams),
    ]  # fmt: skip


@dataclass
class Plan:
    user: UserMeta
    request: RememberRequest
    audiences: list[str]


def plan_note(i: int, seed: int, vocab: Vocabulary, meta: Meta) -> Plan:
    rng = random.Random(seed * 1_000_003 + i)
    org = rng.choices([o for o, _ in ORGS], [w for _, w in ORGS])[0]
    people = [u for u in meta.users if u.org == org]
    admins = [u for u in people if u.role == "admin"]
    level = rng.choices(["private", "project", "team", "org"], [40, 35, 10, 15])[0]
    shared_wide = level != "private" and rng.random() < 0.2
    org_teams = sorted({t for u in people for t in u.teams})
    if level == "team" and not org_teams:
        level = "org"
    author = rng.choice(admins if level in ("team", "org") or shared_wide else people)
    owner = {
        "private": author.id, "org": org,
        "project": rng.choice(author.projects) if author.projects else author.id,
        "team": rng.choice(author.teams) if author.teams else org,
    }[level]  # fmt: skip
    if level == "project" and not author.projects:
        level = "private"
    topic = rng.randrange(TOPICS)
    title = " ".join(vocab.text(rng, topic, rng.randint(4, 8))).capitalize()
    words = max(15, int(rng.lognormvariate(math.log(90), 0.7)))
    body = " ".join(vocab.text(rng, topic, min(words, 600)))
    tags = tuple(f"t{rng.randrange(150)}" for _ in range(rng.choice((0, 1, 2, 3, 4))))
    scope = Scope(ScopeLevel[level.upper()], owner)
    visible = Visibility.ORG if shared_wide else Visibility.SCOPE
    request = RememberRequest(title, body, rng.choice(KINDS), tags, scope, visible)
    keys = [f"{level}:{owner}"] + ([f"vis:{org}"] if shared_wide else [])
    return Plan(author, request, keys)


async def ingest(
    services: Services,
    meta: Meta,
    target: int,
    vocab: Vocabulary,
    workers: int = 8,
    progress: Any = None,
) -> dict[str, float]:
    """Add notes meta.notes..target through KnowledgeService.remember (the product's write path)."""
    start, todo = meta.notes, iter(range(meta.notes, target))
    lat: list[float] = []
    failed = 0
    errors: Counter[str] = Counter()
    rng = random.Random(meta.seed + target)

    async def worker() -> None:
        nonlocal failed
        for i in todo:
            plan = plan_note(i, meta.seed, vocab, meta)
            t = time.perf_counter()
            try:
                result = await services.knowledge.remember(plan.user.principal(), plan.request)
            except Exception as exc:
                failed += 1
                errors[f"{type(exc).__name__}: {str(exc)[:90]}"] += 1
                continue
            lat.append(time.perf_counter() - t)
            for key in plan.audiences:
                _offer(meta, key, result.item.id, rng)
            if progress and i % 2000 == 0:
                progress(i, time.perf_counter() - t0)

    t0 = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(workers)))
    seconds = time.perf_counter() - t0
    meta.notes = target
    lat.sort()
    return {
        "added": target - start, "failed": failed, "error_kinds": dict(errors),
        "seconds": round(seconds, 1),
        "notes_per_s": round((target - start) / seconds, 1) if target > start else 0,
        "remember_p50_ms": round(lat[len(lat) // 2] * 1000, 1) if lat else 0,
        "remember_p95_ms": round(lat[int(len(lat) * 0.95)] * 1000, 1) if lat else 0,
    }  # fmt: skip

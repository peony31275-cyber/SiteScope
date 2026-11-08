"""Deterministic generated websites for the crawl benchmarks (Phase 10).

A workload is a site of ``pages`` HTML pages arranged in BFS levels 0 to ``depth``: a tree whose
branching is the smallest that holds ``pages`` pages in ``depth`` levels, filled level by level.
Every page links to its children (the tree), back to the root and to its parent (cycles), to
random pages of its own level or above (links to known pages, which deduplication absorbs) and
repeats one of its links (a duplicate anchor). Random links never point deeper than the page, so
each page's BFS depth is its tree level, whatever the crawl order.

On top of the pages, by fixed shares chosen with the workload's seed:

* **redirects**: a page's parent also links to ``/go/<n>``, which answers 301 to the page;
* **missing pages**: some pages link to ``/missing/<n>``, which answers 404;
* **flaky pages**: answer 503 to their first request, then 200 (one retry each);
* **failing pages**: always answer 500 (their retries are used up). They are leaves of the last
  level, so that no page becomes unreachable.

Response delays and page sizes come from fixed distributions (``DELAYS``, ``SIZES``), drawn per
page with the seed. The same parameters always produce the same site, byte for byte.
"""

import itertools
import math
import random
from dataclasses import dataclass, field
from typing import Any

from tests.fetch_support import Reply
from tests.process_support import ALLOW_ALL_ROBOTS

# (seconds, share): the delay before a page's answer.
DELAYS = ((0.04, 0.75), (0.12, 0.20), (0.30, 0.05))
# (approximate body bytes of text, share).
SIZES = ((4_000, 0.70), (24_000, 0.25), (120_000, 0.05))
SMALL_DELAY = 0.01  # redirects, 404s and 500s answer quickly
VOCABULARY_SIZE = 3_000


@dataclass(frozen=True)
class Workload:
    pages: int  # HTML pages of one site, the root included
    depth: int = 4  # BFS levels 0..depth
    links_per_page: int = 10  # links of a page that is not a leaf, about
    redirect_share: float = 0.03
    missing_share: float = 0.02
    flaky_share: float = 0.02
    failing_share: float = 0.005
    seed: int = 2026

    def describe(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "depth": self.depth,
            "links_per_page": self.links_per_page,
            "redirect_share": self.redirect_share,
            "missing_share": self.missing_share,
            "flaky_share": self.flaky_share,
            "failing_share": self.failing_share,
            "seed": self.seed,
            "delays_seconds_share": DELAYS,
            "sizes_bytes_share": SIZES,
        }


@dataclass
class Site:
    routes: dict[str, Reply]
    sequences: dict[str, list[Reply]]  # flaky paths: their first replies, in order
    levels: list[int]  # pages per BFS level
    expected: dict[str, int]
    page_paths: set[str] = field(default_factory=set)
    body_bytes: int = 0

    def fresh_sequences(self) -> dict[str, list[Reply]]:
        """A copy for one server: a server consumes its sequences."""
        return {path: list(replies) for path, replies in self.sequences.items()}


def page_path(index: int) -> str:
    return "/" if index == 0 else f"/p/{index}.html"


def level_sizes(pages: int, depth: int) -> list[int]:
    if pages < 1 or depth < 0:
        raise ValueError("a site needs at least one page and depth >= 0")
    branching = 1
    while sum(branching**level for level in range(depth + 1)) < pages:
        branching += 1
    sizes, left = [], pages
    for level in range(depth + 1):
        size = min(branching**level, left)
        if size == 0:
            break
        sizes.append(size)
        left -= size
    return sizes


def _pick(rng: random.Random, table: tuple[tuple[Any, float], ...]) -> Any:
    values, weights = zip(*table, strict=True)
    return rng.choices(values, weights=weights)[0]


def build_site(workload: Workload) -> Site:
    """The site of ``workload`` (see the module docstring)."""
    rng = random.Random(workload.seed)  # noqa: S311 - a reproducible site, not a secret
    sizes = level_sizes(workload.pages, workload.depth)
    if len(sizes) < 2:
        raise ValueError("a site needs at least two levels")
    levels: list[list[int]] = []
    next_index = 0
    for size in sizes:
        levels.append(list(range(next_index, next_index + size)))
        next_index += size
    branching = max(1, math.ceil(sizes[1] / sizes[0]))
    level_of = {page: level for level, members in enumerate(levels) for page in members}
    parent: dict[int, int] = {}
    children: dict[int, list[int]] = {page: [] for page in level_of}
    for level in range(1, len(levels)):
        upper = levels[level - 1]
        for position, page in enumerate(levels[level]):
            owner = upper[min(position // branching, len(upper) - 1)]
            parent[page] = owner
            children[owner].append(page)

    def sample(population: list[int], share: float) -> set[int]:
        count = round(len(population) * share)
        return set(rng.sample(population, count)) if count else set()

    leaves_last = list(levels[-1])
    not_last = [page for page in level_of if level_of[page] < len(levels) - 1]
    deeper = [page for page in level_of if level_of[page] >= 2]
    redirected = sample(deeper, workload.redirect_share)
    missing_from = sample(not_last, workload.missing_share)
    failing = sample(leaves_last, workload.failing_share)
    flaky = sample(
        [page for page in level_of if page not in failing and page != 0], workload.flaky_share
    )

    syllables = (
        "".join(
            rng.choice("bdfgklmnprstvz") + rng.choice("aeiou") for _ in range(rng.randint(2, 4))
        )
        for _ in range(VOCABULARY_SIZE * 2)
    )
    vocabulary = sorted(set(syllables))[:VOCABULARY_SIZE]
    cumulative = list(itertools.accumulate(1 / (rank + 1) for rank in range(len(vocabulary))))

    def words(count: int) -> str:
        return " ".join(rng.choices(vocabulary, cum_weights=cumulative, k=count))

    routes: dict[str, Reply] = {"/robots.txt": ALLOW_ALL_ROBOTS}
    sequences: dict[str, list[Reply]] = {}
    body_bytes = 0
    known_by_level: list[int] = []  # pages of the levels seen so far, for random links
    for members in levels:
        known_by_level += members
        for page in members:
            links = [page_path(child) for child in children[page]]
            links.append("/")
            if page in parent:
                links.append(page_path(parent[page]))
            extra = max(0, workload.links_per_page - len(links))
            links += [page_path(rng.choice(known_by_level)) for _ in range(extra)]
            links += [f"/go/{child}" for child in children[page] if child in redirected]
            if page in missing_from:
                links.append(f"/missing/{page}")
            links.append(links[0])  # a duplicate anchor
            text_bytes = _pick(rng, SIZES)
            paragraph = words(max(20, text_bytes // 7))
            anchors = "".join(f'<li><a href="{href}">{words(3)}</a></li>' for href in links)
            html = (
                f"<!doctype html><html><head><title>{words(5)}</title>"
                f'<meta name="description" content="{words(15)}"></head>'
                f"<body><h1>{words(4)}</h1><p>{paragraph}</p><ul>{anchors}</ul></body></html>"
            )
            body = html.encode()
            delay = _pick(rng, DELAYS)
            path = page_path(page)
            if page in failing:
                routes[path] = Reply(
                    status=500, reason="Internal Server Error", body=b"failing", delay=SMALL_DELAY
                )
                continue
            routes[path] = Reply(
                headers=[("Content-Type", "text/html; charset=utf-8")], body=body, delay=delay
            )
            body_bytes += len(body)
            if page in flaky:
                sequences[path] = [
                    Reply(status=503, reason="Service Unavailable", body=b"busy", delay=SMALL_DELAY)
                ]
            for child in children[page]:
                if child in redirected:
                    routes[f"/go/{child}"] = Reply(
                        status=301,
                        reason="Moved Permanently",
                        headers=[("Location", page_path(child))],
                        delay=SMALL_DELAY,
                    )
            if page in missing_from:
                routes[f"/missing/{page}"] = Reply(
                    status=404, reason="Not Found", body=b"missing", delay=SMALL_DELAY
                )

    # Redirect URLs whose page fails are still admitted (they are linked from the parent).
    expected = {
        "admitted": workload.pages + len(redirected) + len(missing_from),
        "html_stored": workload.pages - len(failing),
        "redirects": len(redirected),
        "not_found": len(missing_from),
        "failing": len(failing),
        "flaky": len(flaky),
        "retries": len(flaky) + 2 * len(failing),  # 3 attempts: 2 retries per failing page
        "page_requests": workload.pages + len(flaky) + 2 * len(failing),
    }
    return Site(
        routes=routes,
        sequences=sequences,
        levels=sizes,
        expected=expected,
        page_paths={page_path(page) for page in level_of},
        body_bytes=body_bytes,
    )

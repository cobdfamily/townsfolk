"""In-process Prometheus exposition.

Hand-rolled text-format emit. We don't pull in
prometheus_client because the transitive list is
disproportionate for the tiny counter set we
actually expose; the format is stable enough that
writing it by hand stays cheap.

Every counter is monotonic and resets on process
restart -- matches brian's `/metrics` convention so
dashboards using the cobdfamily naming pattern
(`*_total`) catch the same shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Counters:
    """Service-lifetime counters. Mutated from the
    request path; read from `/metrics`. No locking --
    Python's GIL serialises the integer increments,
    which is fine for the read-after-write coherence
    a Prometheus scrape needs.
    """

    lookups_total: int = 0
    # Per-mode break-out: phone / ip / coords /
    # fallback. Lets dashboards spot a sudden spike
    # in fallback (= non-CA phone), which is a
    # caller-side bug signal.
    lookups_by_mode: dict[str, int] = field(
        default_factory=lambda: {
            "phone": 0,
            "ip": 0,
            "coords": 0,
            "fallback": 0,
        },
    )
    lookup_errors_total: int = 0


def render(counters: Counters, *, db_ready: bool) -> str:
    """Plain-text exposition. Each metric carries a
    HELP + TYPE line so Prometheus can parse the
    series correctly even on first scrape.
    """
    lines: list[str] = [
        "# HELP townsfolk_lookups_total Total /v1/lookup calls served.",
        "# TYPE townsfolk_lookups_total counter",
        f"townsfolk_lookups_total {counters.lookups_total}",
        "# HELP townsfolk_lookups_by_mode_total /v1/lookup calls split by input mode.",
        "# TYPE townsfolk_lookups_by_mode_total counter",
    ]
    for mode, n in counters.lookups_by_mode.items():
        lines.append(
            f'townsfolk_lookups_by_mode_total{{mode="{mode}"}} {n}',
        )
    lines.extend(
        [
            "# HELP townsfolk_lookup_errors_total /v1/lookup calls that ended in 4xx/5xx.",
            "# TYPE townsfolk_lookup_errors_total counter",
            f"townsfolk_lookup_errors_total {counters.lookup_errors_total}",
            "# HELP townsfolk_db_ready 1 if the PostGIS pool initialised at boot, else 0.",
            "# TYPE townsfolk_db_ready gauge",
            f"townsfolk_db_ready {1 if db_ready else 0}",
            "",
        ],
    )
    return "\n".join(lines)

"""Service dependency graph used by triage (blast radius) and correlation."""

from __future__ import annotations

from pydantic import Field

from incident_commander.models import Strict

SERVICE_PATTERN = r"^[A-Za-z0-9._-]+$"


class ServiceInfo(Strict):
    depends_on: list[str] = Field(default_factory=list, max_length=50)
    customer_facing: bool = False
    owner: str = Field(default="unknown", max_length=100)
    description: str = Field(default="", max_length=500)


class Topology(Strict):
    services: dict[str, ServiceInfo] = Field(default_factory=dict, max_length=200)

    def model_post_init(self, __context: object) -> None:
        for name, info in self.services.items():
            for dep in info.depends_on:
                if dep not in self.services:
                    raise ValueError(f"{name} depends on unknown service {dep!r}")

    def __contains__(self, name: object) -> bool:
        return name in self.services

    def dependencies(self, service: str) -> list[str]:
        info = self.services.get(service)
        return list(info.depends_on) if info else []

    def dependents(self, service: str) -> list[str]:
        return [name for name, info in self.services.items() if service in info.depends_on]

    def is_customer_facing(self, service: str) -> bool:
        info = self.services.get(service)
        return bool(info and info.customer_facing)

    def upstream_of(self, service: str) -> set[str]:
        """Every service that (transitively) calls ``service`` and could feel its failure."""
        seen: set[str] = set()
        stack = self.dependents(service)
        while stack:
            name = stack.pop()
            if name not in seen:
                seen.add(name)
                stack.extend(self.dependents(name))
        return seen

    def related(self, a: str, b: str) -> bool:
        """True when one service depends (transitively) on the other, or they are the same."""
        return a == b or a in self.upstream_of(b) or b in self.upstream_of(a)

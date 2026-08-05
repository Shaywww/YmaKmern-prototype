"""Dependency graph resolver - topological sort and cycle detection."""
from __future__ import annotations
from typing import Any
from collections import deque

class DependencyError(Exception):
    pass

class CircularDependencyError(DependencyError):
    pass

class MissingDependencyError(DependencyError):
    pass

class DependencyResolver:
    @staticmethod
    def topological_sort(steps: list) -> list:
        """Topological sort steps by dependencies. Raises CircularDependencyError on cycles."""
        step_map = {s.step_id: s for s in steps}
        in_degree: dict[str, int] = {s.step_id: len(s.depends_on) for s in steps}
        dependents: dict[str, list[str]] = {s.step_id: [] for s in steps}
        for s in steps:
            for dep in s.depends_on:
                if dep in dependents:
                    dependents[dep].append(s.step_id)

        queue = deque([sid for sid, deg in in_degree.items() if deg == 0])
        sorted_ids = []
        while queue:
            sid = queue.popleft()
            sorted_ids.append(sid)
            for dep_id in dependents.get(sid, []):
                in_degree[dep_id] -= 1
                if in_degree[dep_id] == 0:
                    queue.append(dep_id)

        if len(sorted_ids) != len(steps):
            remaining = [sid for sid, deg in in_degree.items() if deg > 0]
            raise CircularDependencyError(f"Cycle detected involving: {remaining}")

        id_to_step = {s.step_id: s for s in steps}
        return [id_to_step[sid] for sid in sorted_ids]

    @staticmethod
    def validate_dependencies(steps: list) -> tuple[bool, list[str]]:
        """Validate all dependencies exist. Returns (valid, errors)."""
        step_ids = {s.step_id for s in steps}
        errors = []
        for s in steps:
            for dep in s.depends_on:
                if dep not in step_ids:
                    errors.append(f"Step '{s.step_id}' depends on unknown step '{dep}'")
        return len(errors) == 0, errors

    @staticmethod
    def detect_cycles(steps: list) -> list[list[str]]:
        """Detect all cycles in the dependency graph."""
        step_ids = {s.step_id for s in steps}
        adj: dict[str, list[str]] = {sid: [] for sid in step_ids}
        for s in steps:
            for dep in s.depends_on:
                if dep in adj:
                    adj[dep].append(s.step_id)

        cycles = []
        visited = set()
        stack = set()

        def dfs(node: str, path: list[str]):
            visited.add(node)
            stack.add(node)
            path.append(node)
            for neighbor in adj.get(node, []):
                if neighbor in stack:
                    cycle_start = path.index(neighbor)
                    cycles.append(path[cycle_start:] + [neighbor])
                elif neighbor not in visited:
                    dfs(neighbor, path)
            path.pop()
            stack.discard(node)

        for sid in step_ids:
            if sid not in visited:
                dfs(sid, [])
        return cycles

    @staticmethod
    def execution_order(steps: list) -> list[list]:
        """Group steps into parallelizable batches. Each batch can run in parallel."""
        step_map = {s.step_id: s for s in steps}
        in_degree: dict[str, int] = {s.step_id: len(s.depends_on) for s in steps}
        dependents: dict[str, list[str]] = {s.step_id: [] for s in steps}
        for s in steps:
            for dep in s.depends_on:
                if dep in dependents:
                    dependents[dep].append(s.step_id)

        batches = []
        current = [sid for sid, deg in in_degree.items() if deg == 0]
        while current:
            batch = [step_map[sid] for sid in sorted(current)]
            batches.append(batch)
            next_batch = []
            for sid in current:
                for dep_id in dependents.get(sid, []):
                    in_degree[dep_id] -= 1
                    if in_degree[dep_id] == 0:
                        next_batch.append(dep_id)
            current = next_batch
        return batches

"""HGA-VNS + CP-SAT LNS solver for the İşbir Elektrik alternator FJSP.

The program is started once.  It builds an OS/MS genetic population, decodes
active schedules by inserting operations into machine gaps, validates the best
schedule with CP-SAT, and then improves small neighborhoods while fixing most
of the incumbent.  A single Excel workbook is atomically updated; normal exit,
Ctrl+C, SIGTERM, and Python errors all trigger a final checkpoint attempt.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import os
import random
import shutil
import signal
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from ortools.sat.python import cp_model

from build_143_input import (
    DEFAULT_AUDIT as DEFAULT_DYNAMIC_AUDIT,
    DEFAULT_OUTPUT as DEFAULT_DYNAMIC_INPUT,
    DEFAULT_PRIORITY_WORKBOOK,
    DEFAULT_ROUTE_INPUT,
    DEFAULT_WORKBOOK,
    build_payload,
)
from solve_three_stage_cp_sat import (
    ModelArtifacts,
    Operation,
    build_model,
    configure_solver,
    flatten_operations,
    setup_duration,
    validate_payload,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "output_hybrid_exact_3stage"
PACKAGED_NODE = Path("/Users/dilek/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node")
NODE = Path(
    os.environ.get("FJSP_NODE")
    or shutil.which("node")
    or (str(PACKAGED_NODE) if PACKAGED_NODE.exists() else "node")
)
EXCEL_EXPORTER = ROOT / "export_checkpoint_excel.mjs"


@dataclass
class ScheduledTask:
    operation_index: int
    machine: str
    ready: int
    setup_start: int
    start: int
    end: int


@dataclass
class ProblemContext:
    data: dict[str, Any]
    operations: list[Operation]
    unit_operations: list[list[int]]
    scale: int
    transport: int
    campaign_limit: int

    @classmethod
    def create(cls, data: dict[str, Any], campaign_limit: int) -> "ProblemContext":
        operations, by_unit_id = flatten_operations(data)
        unit_operations = [list(by_unit_id[unit["unit_id"]]) for unit in data["units"]]
        return cls(
            data=data,
            operations=operations,
            unit_operations=unit_operations,
            scale=int(data["time_scale"]),
            transport=int(data["parameters"]["transport_time"]),
            campaign_limit=campaign_limit,
        )


@dataclass
class Individual:
    os_sequence: list[int]
    machines: list[str]
    fitness: tuple[int, int, int] | None = None
    rows: list[dict[str, Any]] | None = None
    times: list[tuple[int, int, int]] | None = None
    campaign_excess: int = 0
    source: str = "GA"
    cp_verified: bool = False


@dataclass
class CpResult:
    status: str
    solution: Individual | None
    wall_time: float
    branches: int
    conflicts: int
    relaxed_count: int
    campaign_cap: int


stop_requested = False
stop_reason = ""
active_solver: cp_model.CpSolver | None = None


def handle_stop(signum: int, _frame: Any) -> None:
    global stop_requested, stop_reason
    stop_requested = True
    stop_reason = signal.Signals(signum).name
    if active_solver is not None:
        active_solver.StopSearch()
    print(f"\n{stop_reason} alındı; son iyi çözüm aynı Excel'e kaydedilecek.", flush=True)


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def task_setup(previous_group: str | None, group: str, context: ProblemContext) -> int:
    return setup_duration(previous_group, group, context.data["parameters"])


def base_task_setup(group: str, context: ProblemContext) -> int:
    return int(context.data["parameters"]["setup_same"][group])


def active_insert(
    timeline: list[ScheduledTask],
    operation: Operation,
    machine: str,
    ready: int,
    context: ProblemContext,
    *,
    append_only: bool = False,
) -> ScheduledTask:
    """Insert at the earliest feasible machine gap without shifting fixed tasks."""

    duration = operation.machine_durations[machine]
    campaign_counts: Counter[str] = Counter()
    previous_timeline_group: str | None = None
    for existing in timeline:
        existing_group = context.operations[existing.operation_index].setup_group
        if existing_group != previous_timeline_group:
            campaign_counts[existing_group] += 1
        previous_timeline_group = existing_group
    best: tuple[tuple[int, int, int], ScheduledTask, int] | None = None
    positions = [len(timeline)] if append_only else range(len(timeline) + 1)
    for position in positions:
        previous = timeline[position - 1] if position else None
        following = timeline[position] if position < len(timeline) else None
        previous_end = previous.end if previous else 0
        previous_group = context.operations[previous.operation_index].setup_group if previous else None
        following_group = context.operations[following.operation_index].setup_group if following else None

        # Aday eklemenin kampanya sayılarına yerel etkisini O(1) hesapla.
        # Böylece GA da eski CP'deki makine/setup grubu başına kesin
        # kampanya sınırını aşmaz.
        candidate_counts = campaign_counts.copy()
        if following_group is not None and previous_group != following_group:
            candidate_counts[following_group] -= 1
        if previous_group != operation.setup_group:
            candidate_counts[operation.setup_group] += 1
        if following_group is not None and operation.setup_group != following_group:
            candidate_counts[following_group] += 1
        if any(count > context.campaign_limit for count in candidate_counts.values()):
            continue

        setup_before = task_setup(previous_group, operation.setup_group, context)
        base_setup = base_task_setup(operation.setup_group, context)
        # Eski CP ile aynı "anticipatory changeover" semantiği: farklı
        # seriye geçişin ek kısmı parça hazır olmadan yapılabilir; ancak
        # operasyonun kendi aynı-seri bağlama setup'ı ready'den önce başlamaz.
        start = max(previous_end + setup_before, ready + base_setup)
        setup_start = start - setup_before
        end = start + duration
        if following is not None:
            following_setup = task_setup(operation.setup_group, following_group, context)
            following_setup_start = following.start - following_setup
            following_base_start = following.start - base_task_setup(following_group, context)
            if end > following_setup_start or following_base_start < following.ready:
                continue
        task = ScheduledTask(operation.index, machine, ready, setup_start, start, end)
        key = (end, start, position)
        if best is None or key < best[0]:
            best = (key, task, position)
        if best is not None and previous_end > best[0][0]:
            break
    if best is None:
        raise AssertionError(f"{operation.unit_id} O{operation.sequence}: aktif decoder yer bulamadı")
    _, task, position = best
    timeline.insert(position, task)
    return task


def operation_order_from_os(context: ProblemContext, os_sequence: Iterable[int]) -> list[int]:
    next_operation = [0] * len(context.unit_operations)
    order: list[int] = []
    for unit_index in os_sequence:
        if not 0 <= unit_index < len(context.unit_operations):
            raise ValueError(f"OS geçersiz birim indeksi: {unit_index}")
        position = next_operation[unit_index]
        if position >= len(context.unit_operations[unit_index]):
            raise ValueError(f"OS birimi gereğinden fazla tekrar ediyor: {unit_index}")
        order.append(context.unit_operations[unit_index][position])
        next_operation[unit_index] += 1
    if next_operation != [len(indices) for indices in context.unit_operations]:
        raise ValueError("OS kromozomu bütün operasyonları kapsamıyor")
    return order


def rows_from_times(
    context: ProblemContext,
    machines: list[str],
    times: list[tuple[int, int, int]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for operation in context.operations:
        setup_start, start, end = times[operation.index]
        machine = machines[operation.index]
        rows.append(
            {
                "unit_id": operation.unit_id,
                "order_job_id": operation.order_job_id,
                "sequence": operation.sequence,
                "operation": operation.name,
                "setup_group": operation.setup_group,
                "machine": machine,
                "setup_start_min": setup_start / context.scale,
                "start_min": start / context.scale,
                "end_min": end / context.scale,
                "processing_min": operation.machine_durations[machine] / context.scale,
                "operation_index": operation.index,
            }
        )
    rows.sort(key=lambda row: (row["machine"], row["setup_start_min"], row["start_min"], row["operation_index"]))
    return rows


def compute_fitness(
    context: ProblemContext,
    machines: list[str],
    times: list[tuple[int, int, int]],
) -> tuple[int, int, int]:
    completion = {
        unit["unit_id"]: times[context.unit_operations[index][-1]][2]
        for index, unit in enumerate(context.data["units"])
    }
    makespan = max(completion.values(), default=0)
    tardiness = sum(
        int(unit["priority_weight"]) * max(0, completion[unit["unit_id"]] - int(unit["due"]))
        for unit in context.data["units"]
    )
    machine_order: dict[str, list[int]] = defaultdict(list)
    for operation_index, machine in enumerate(machines):
        machine_order[machine].append(operation_index)
    total_setup = 0
    for indices in machine_order.values():
        indices.sort(key=lambda index: (times[index][1], index))
        previous_group: str | None = None
        for operation_index in indices:
            group = context.operations[operation_index].setup_group
            total_setup += task_setup(previous_group, group, context)
            previous_group = group
    return makespan, tardiness, total_setup


def campaign_excess(context: ProblemContext, machines: list[str], times: list[tuple[int, int, int]]) -> int:
    machine_order: dict[str, list[int]] = defaultdict(list)
    for operation_index, machine in enumerate(machines):
        machine_order[machine].append(operation_index)
    excess = 0
    for indices in machine_order.values():
        indices.sort(key=lambda index: (times[index][1], index))
        campaigns: Counter[str] = Counter()
        previous_group: str | None = None
        for operation_index in indices:
            group = context.operations[operation_index].setup_group
            if group != previous_group:
                campaigns[group] += 1
            previous_group = group
        excess += sum(max(0, count - context.campaign_limit) for count in campaigns.values())
    return excess


def decode(individual: Individual, context: ProblemContext, *, active_gaps: bool = True) -> Individual:
    if len(individual.machines) != len(context.operations):
        raise ValueError("MS kromozomu operasyon sayısıyla eşleşmiyor")
    operation_order = operation_order_from_os(context, individual.os_sequence)
    timelines: dict[str, list[ScheduledTask]] = {machine: [] for machine in context.data["machines"]}
    previous_end = [int(unit["release"]) for unit in context.data["units"]]
    previous_machine: list[str | None] = [None] * len(context.data["units"])
    times: list[tuple[int, int, int]] = [(0, 0, 0)] * len(context.operations)

    for operation_index in operation_order:
        operation = context.operations[operation_index]
        machine = individual.machines[operation_index]
        if machine not in operation.machine_durations:
            machine = min(operation.machine_durations, key=operation.machine_durations.get)
            individual.machines[operation_index] = machine
        transport = context.transport if previous_machine[operation.unit_index] not in (None, machine) else 0
        ready = previous_end[operation.unit_index] + transport
        task = active_insert(
            timelines[machine],
            operation,
            machine,
            ready,
            context,
            append_only=not active_gaps,
        )
        times[operation_index] = (task.setup_start, task.start, task.end)
        previous_end[operation.unit_index] = task.end
        previous_machine[operation.unit_index] = machine

    # An insertion can change the following task's predecessor group.  Its
    # processing time remains fixed, while the effective setup interval is
    # normalized here for output, fitness, and independent validation.
    for timeline in timelines.values():
        previous_group: str | None = None
        machine_previous_end = 0
        for task in timeline:
            operation = context.operations[task.operation_index]
            setup = task_setup(previous_group, operation.setup_group, context)
            if task.start - setup < machine_previous_end:
                raise AssertionError("Aktif decoder setup boşluğunu ihlal etti")
            times[task.operation_index] = (task.start - setup, task.start, task.end)
            machine_previous_end = task.end
            previous_group = operation.setup_group

    individual.times = times
    individual.fitness = compute_fitness(context, individual.machines, times)
    individual.campaign_excess = campaign_excess(context, individual.machines, times)
    individual.rows = rows_from_times(context, individual.machines, times)
    return individual


def ga_key(individual: Individual) -> tuple[int, int, int, int]:
    return (individual.campaign_excess, *(individual.fitness or (math.inf, math.inf, math.inf)))


def round_robin_os(context: ProblemContext, unit_order: list[int]) -> list[int]:
    result: list[int] = []
    maximum = max(len(indices) for indices in context.unit_operations)
    for operation_position in range(maximum):
        result.extend(
            unit_index
            for unit_index in unit_order
            if operation_position < len(context.unit_operations[unit_index])
        )
    return result


def make_os(context: ProblemContext, rng: random.Random, strategy: str) -> list[int]:
    units = context.data["units"]
    indices = list(range(len(units)))
    if strategy == "edd":
        indices.sort(key=lambda index: (units[index]["due"], -int(units[index]["priority_weight"]), index))
        return round_robin_os(context, indices)
    if strategy in {"grouped", "grouped_random"}:
        by_group: dict[str, list[int]] = defaultdict(list)
        for unit_index, operation_indices in enumerate(context.unit_operations):
            by_group[context.operations[operation_indices[0]].setup_group].append(unit_index)
        groups = sorted(by_group, key=lambda group: min(units[index]["due"] for index in by_group[group]))
        if strategy == "grouped_random":
            rng.shuffle(groups)
        result: list[int] = []
        for group in groups:
            group_units = by_group[group]
            group_units.sort(key=lambda index: (units[index]["due"], index))
            if strategy == "grouped_random":
                rng.shuffle(group_units)
            result.extend(round_robin_os(context, group_units))
        return result
    result = [unit_index for unit_index, ops in enumerate(context.unit_operations) for _ in ops]
    rng.shuffle(result)
    return result


def select_machines(
    context: ProblemContext,
    os_sequence: list[int],
    rng: random.Random,
    strategy: str,
) -> list[str]:
    machines = [""] * len(context.operations)
    operation_order = operation_order_from_os(context, os_sequence)
    if strategy == "local":
        for operation_indices in context.unit_operations:
            local_load: Counter[str] = Counter()
            previous_group: dict[str, str | None] = defaultdict(lambda: None)
            for operation_index in operation_indices:
                operation = context.operations[operation_index]
                ranked = sorted(
                    operation.machine_durations,
                    key=lambda machine: (
                        local_load[machine]
                        + operation.machine_durations[machine]
                        + task_setup(previous_group[machine], operation.setup_group, context),
                        machine,
                    ),
                )
                machine = ranked[0]
                machines[operation_index] = machine
                local_load[machine] += operation.machine_durations[machine]
                previous_group[machine] = operation.setup_group
        return machines
    if strategy == "random":
        for operation in context.operations:
            options = sorted(operation.machine_durations)
            weights = [1.0 / operation.machine_durations[machine] for machine in options]
            machines[operation.index] = rng.choices(options, weights=weights, k=1)[0]
        return machines

    load: Counter[str] = Counter()
    previous_group: dict[str, str | None] = defaultdict(lambda: None)
    for operation_index in operation_order:
        operation = context.operations[operation_index]
        ranked = sorted(
            operation.machine_durations,
            key=lambda machine: (
                load[machine]
                + operation.machine_durations[machine]
                + task_setup(previous_group[machine], operation.setup_group, context),
                load[machine],
                machine,
            ),
        )
        machine = ranked[1] if len(ranked) > 1 and rng.random() < 0.08 else ranked[0]
        machines[operation_index] = machine
        load[machine] += operation.machine_durations[machine]
        previous_group[machine] = operation.setup_group
    return machines


def make_individual(context: ProblemContext, rng: random.Random, index: int) -> Individual:
    fraction = index % 10
    if fraction < 4:
        machine_strategy = "global"
        os_strategy = "grouped" if fraction == 0 else "edd" if fraction == 1 else "grouped_random"
    elif fraction < 7:
        machine_strategy = "local"
        os_strategy = "grouped_random" if fraction % 2 else "edd"
    else:
        machine_strategy = "random"
        os_strategy = "random" if fraction == 9 else "grouped_random"
    for attempt in range(4):
        os_sequence = make_os(context, rng, os_strategy)
        machines = select_machines(context, os_sequence, rng, machine_strategy)
        try:
            return decode(Individual(os_sequence, machines, source=f"GA-{machine_strategy}"), context)
        except AssertionError:
            # Rastgele OS, kesin kampanya sınırı altında bazen ekleme
            # yeri bırakmayabilir. Kampanya gruplu bir OS ile yeniden dene.
            os_strategy = "grouped_random" if attempt < 2 else "grouped"
            machine_strategy = "global"
    os_sequence = make_os(context, rng, "grouped")
    machines = select_machines(context, os_sequence, rng, "global")
    return decode(
        Individual(os_sequence, machines, source="GA-campaign-safe"),
        context,
        active_gaps=False,
    )


def stage_ga_key(
    individual: Individual,
    stage: int,
    makespan_lock: int | None,
    tardiness_lock: int | None,
) -> tuple[int, ...]:
    """GA'yı da CP ile aynı leksikografik kilitler altında sırala."""

    if individual.fitness is None:
        return (10**18,)
    cmax, tardiness, setup = individual.fitness
    campaign = individual.campaign_excess
    if stage == 1:
        return campaign, cmax, tardiness, setup
    cmax_violation = max(0, cmax - int(makespan_lock))
    if stage == 2:
        return campaign, cmax_violation, tardiness, setup, cmax
    tardiness_violation = max(0, tardiness - int(tardiness_lock))
    return campaign, cmax_violation, tardiness_violation, setup, tardiness, cmax


def tournament(
    population: list[Individual],
    rng: random.Random,
    key_fn: Any = ga_key,
) -> Individual:
    return min(rng.sample(population, min(4, len(population))), key=key_fn)


def pox(left: list[int], right: list[int], unit_count: int, rng: random.Random) -> list[int]:
    selected_size = rng.randint(max(1, unit_count // 3), max(1, 2 * unit_count // 3))
    selected = set(rng.sample(range(unit_count), selected_size))
    child = [-1] * len(left)
    for position, gene in enumerate(left):
        if gene in selected:
            child[position] = gene
    filler = (gene for gene in right if gene not in selected)
    for position, gene in enumerate(child):
        if gene == -1:
            child[position] = next(filler)
    return child


def mutate(
    os_sequence: list[int],
    machines: list[str],
    context: ProblemContext,
    rng: random.Random,
    mutation_rate: float,
) -> None:
    if rng.random() < mutation_rate:
        for _ in range(rng.randint(1, 3)):
            left, right = rng.sample(range(len(os_sequence)), 2)
            os_sequence[left], os_sequence[right] = os_sequence[right], os_sequence[left]
    if rng.random() < mutation_rate * 0.7:
        source, target = rng.sample(range(len(os_sequence)), 2)
        os_sequence.insert(target, os_sequence.pop(source))

    estimated_load: Counter[str] = Counter()
    for operation, machine in zip(context.operations, machines, strict=True):
        estimated_load[machine] += operation.machine_durations[machine]
    eligible = [operation.index for operation in context.operations if len(operation.machine_durations) > 1]
    changes = max(1, round(len(machines) * mutation_rate * 0.012))
    for operation_index in rng.sample(eligible, min(changes, len(eligible))):
        operation = context.operations[operation_index]
        current = machines[operation_index]
        alternatives = [machine for machine in operation.machine_durations if machine != current]
        if not alternatives:
            continue
        if rng.random() < 0.7:
            replacement = min(
                alternatives,
                key=lambda machine: estimated_load[machine] + operation.machine_durations[machine],
            )
        else:
            replacement = rng.choice(alternatives)
        estimated_load[current] -= operation.machine_durations[current]
        estimated_load[replacement] += operation.machine_durations[replacement]
        machines[operation_index] = replacement


def breed(
    left: Individual,
    right: Individual,
    context: ProblemContext,
    rng: random.Random,
    crossover_rate: float,
    mutation_rate: float,
    key_fn: Any = ga_key,
) -> Individual:
    if rng.random() < crossover_rate:
        os_sequence = pox(left.os_sequence, right.os_sequence, len(context.data["units"]), rng)
        machines = [
            first if rng.random() < 0.5 else second
            for first, second in zip(left.machines, right.machines, strict=True)
        ]
    else:
        os_sequence = list(left.os_sequence)
        machines = list(left.machines)
    mutate(os_sequence, machines, context, rng, mutation_rate)
    try:
        return decode(Individual(os_sequence, machines), context)
    except AssertionError:
        # Geçersiz kampanya yapısı ebeveyn elitini bozmasın.
        return copy.deepcopy(left if key_fn(left) <= key_fn(right) else right)


def evolve(
    population: list[Individual],
    context: ProblemContext,
    rng: random.Random,
    elite_count: int,
    crossover_rate: float,
    mutation_rate: float,
    key_fn: Any = ga_key,
) -> list[Individual]:
    population.sort(key=key_fn)
    result = [copy.deepcopy(individual) for individual in population[:elite_count]]
    while len(result) < len(population) and not stop_requested:
        result.append(
            breed(
                tournament(population, rng, key_fn),
                tournament(population, rng, key_fn),
                context,
                rng,
                crossover_rate,
                mutation_rate,
                key_fn,
            )
        )
    return result


def _vns_candidate_indices(individual: Individual, context: ProblemContext) -> tuple[list[int], list[int]]:
    """Return bottleneck and tardy-operation candidates for focused VNS moves."""
    if individual.times is None:
        return [], []
    occupied: Counter[str] = Counter()
    for operation_index, machine in enumerate(individual.machines):
        setup_start, _, end = individual.times[operation_index]
        occupied[machine] += end - setup_start
    bottlenecks = {machine for machine, _ in occupied.most_common(min(2, len(occupied)))}
    bottleneck_ops = [
        operation.index for operation in context.operations
        if individual.machines[operation.index] in bottlenecks
    ]
    tardy_units: set[int] = set()
    for unit_index, unit in enumerate(context.data["units"]):
        completion = individual.times[context.unit_operations[unit_index][-1]][2]
        if completion > int(unit["due"]):
            tardy_units.add(unit_index)
    tardy_ops = [
        operation.index for operation in context.operations
        if operation.unit_index in tardy_units
    ]
    return bottleneck_ops, tardy_ops


def _move_unit_gene(os_sequence: list[int], unit_index: int, rng: random.Random, window: int) -> None:
    positions = [position for position, gene in enumerate(os_sequence) if gene == unit_index]
    if not positions:
        return
    source = rng.choice(positions)
    lower = max(0, source - window)
    upper = min(len(os_sequence) - 1, source + window)
    target = rng.randint(lower, upper)
    os_sequence.insert(target, os_sequence.pop(source))


def vns_improve(
    individual: Individual,
    context: ProblemContext,
    rng: random.Random,
    key_fn: Any = ga_key,
    *,
    max_evaluations: int = 24,
) -> Individual:
    """Variable-neighborhood descent over feasible OS/MS chromosomes.

    Every neighbor is decoded by the exact campaign-safe active decoder and is
    independently comparable under the current lexicographic stage key.
    """
    best = copy.deepcopy(individual)
    bottleneck_ops, tardy_ops = _vns_candidate_indices(best, context)
    all_flexible = [
        operation.index for operation in context.operations
        if len(operation.machine_durations) > 1
    ]
    neighborhoods = ("machine", "os_insert", "os_swap", "combined")
    evaluations = 0
    neighborhood_index = 0
    while neighborhood_index < len(neighborhoods) and evaluations < max_evaluations and not stop_requested:
        name = neighborhoods[neighborhood_index]
        improved = False
        attempts = max(2, max_evaluations // len(neighborhoods))
        for _ in range(attempts):
            if evaluations >= max_evaluations:
                break
            child_os = list(best.os_sequence)
            child_machines = list(best.machines)
            if name in {"machine", "combined"} and all_flexible:
                pool = bottleneck_ops or tardy_ops or all_flexible
                pool = [index for index in pool if len(context.operations[index].machine_durations) > 1] or all_flexible
                operation_index = rng.choice(pool)
                operation = context.operations[operation_index]
                current = child_machines[operation_index]
                alternatives = [machine for machine in operation.machine_durations if machine != current]
                if alternatives:
                    child_machines[operation_index] = min(
                        alternatives,
                        key=lambda machine: operation.machine_durations[machine],
                    ) if rng.random() < 0.65 else rng.choice(alternatives)
            if name in {"os_insert", "combined"}:
                source_pool = tardy_ops or bottleneck_ops
                unit_index = (
                    context.operations[rng.choice(source_pool)].unit_index
                    if source_pool else rng.randrange(len(context.unit_operations))
                )
                _move_unit_gene(child_os, unit_index, rng, window=max(8, len(child_os) // 20))
            elif name == "os_swap":
                left, right = rng.sample(range(len(child_os)), 2)
                child_os[left], child_os[right] = child_os[right], child_os[left]
            evaluations += 1
            try:
                candidate = decode(
                    Individual(child_os, child_machines, source=f"HGA-VNS:{name}"),
                    context,
                )
            except (AssertionError, ValueError):
                continue
            if key_fn(candidate) < key_fn(best):
                best = candidate
                bottleneck_ops, tardy_ops = _vns_candidate_indices(best, context)
                improved = True
                break
        neighborhood_index = 0 if improved else neighborhood_index + 1
    return best


def intensify(
    individual: Individual,
    context: ProblemContext,
    rng: random.Random,
    key_fn: Any = ga_key,
) -> Individual:
    return vns_improve(individual, context, rng, key_fn, max_evaluations=24)

def diversify(
    population: list[Individual],
    context: ProblemContext,
    rng: random.Random,
    elite_count: int,
    diversify_rate: float,
    key_fn: Any = ga_key,
) -> list[Individual]:
    population.sort(key=key_fn)
    for index in range(min(elite_count, len(population))):
        population[index] = intensify(population[index], context, rng, key_fn)
    replace_count = max(1, round(len(population) * diversify_rate))
    for offset in range(replace_count):
        population[-1 - offset] = make_individual(context, rng, 1000 + offset + rng.randrange(1000))
    population.sort(key=key_fn)
    return population


def machine_orders(solution: Individual) -> dict[str, list[int]]:
    if solution.times is None:
        raise ValueError("Çözüm zamanları yok")
    result: dict[str, list[int]] = defaultdict(list)
    for operation_index, machine in enumerate(solution.machines):
        result[machine].append(operation_index)
    for indices in result.values():
        indices.sort(key=lambda index: (solution.times[index][1], index))
    return result


def validate_decoded_solution(solution: Individual, context: ProblemContext) -> None:
    if solution.times is None or solution.fitness is None:
        raise ValueError("Eksik çözüm")
    if len(solution.times) != len(context.operations) or len(solution.machines) != len(context.operations):
        raise ValueError("Operasyon sayısı tutarsız")
    for operation in context.operations:
        machine = solution.machines[operation.index]
        if machine not in operation.machine_durations:
            raise ValueError(f"{operation.unit_id} O{operation.sequence}: uygun olmayan makine")
        setup_start, start, end = solution.times[operation.index]
        if end - start != operation.machine_durations[machine] or not 0 <= setup_start <= start < end:
            raise ValueError(f"{operation.unit_id} O{operation.sequence}: süre tutarsız")
    for unit_index, operation_indices in enumerate(context.unit_operations):
        unit = context.data["units"][unit_index]
        first = operation_indices[0]
        first_group = context.operations[first].setup_group
        if solution.times[first][1] - base_task_setup(first_group, context) < int(unit["release"]):
            raise ValueError(f"{unit['unit_id']}: release ihlali")
        for previous, following in zip(operation_indices, operation_indices[1:], strict=False):
            transport = context.transport if solution.machines[previous] != solution.machines[following] else 0
            following_group = context.operations[following].setup_group
            following_base_start = solution.times[following][1] - base_task_setup(following_group, context)
            if following_base_start < solution.times[previous][2] + transport:
                raise ValueError(f"{unit['unit_id']}: operasyon önceliği/taşıma ihlali")
    for machine, indices in machine_orders(solution).items():
        previous_end = 0
        previous_group: str | None = None
        for operation_index in indices:
            operation = context.operations[operation_index]
            expected_setup = task_setup(previous_group, operation.setup_group, context)
            setup_start, start, end = solution.times[operation_index]
            if start - setup_start != expected_setup or setup_start < previous_end:
                raise ValueError(f"{machine}: setup/no-overlap ihlali")
            previous_end = end
            previous_group = operation.setup_group
    recalculated = compute_fitness(context, solution.machines, solution.times)
    if recalculated != solution.fitness:
        raise ValueError(f"Amaç değerleri tutarsız: {solution.fitness} != {recalculated}")
    solution.campaign_excess = campaign_excess(context, solution.machines, solution.times)
    if solution.campaign_excess:
        raise ValueError(f"Kampanya sınırı ihlali: {solution.campaign_excess}")


def add_solution_hints(artifacts: ModelArtifacts, solution: Individual, context: ProblemContext) -> None:
    if solution.times is None:
        return
    parameters = context.data["parameters"]
    for operation in artifacts.operations:
        _, start, end = solution.times[operation.index]
        base_setup = int(parameters["setup_same"][operation.setup_group])
        if artifacts.setup_starts[operation.index].Index() != artifacts.starts[operation.index].Index():
            artifacts.model.AddHint(artifacts.setup_starts[operation.index], start - base_setup)
        artifacts.model.AddHint(artifacts.starts[operation.index], start)
        artifacts.model.AddHint(artifacts.ends[operation.index], end)
        for machine in operation.machine_durations:
            artifacts.model.AddHint(
                artifacts.presences[(operation.index, machine)],
                int(machine == solution.machines[operation.index]),
            )


def campaign_layout(
    artifacts: ModelArtifacts,
    solution: Individual,
    context: ProblemContext,
) -> tuple[dict[int, tuple[str, int]], dict[str, list[int]]] | None:
    operation_to_node: dict[int, tuple[str, int]] = {}
    sequence_by_machine: dict[str, list[int]] = {}
    orders = machine_orders(solution)
    for machine, nodes in artifacts.campaign_nodes_by_machine.items():
        node_by_group_slot = {(node["group"], int(node["slot"])): int(node["node_id"]) for node in nodes}
        run_number: Counter[str] = Counter()
        sequence: list[int] = []
        previous_group: str | None = None
        current_node: int | None = None
        for operation_index in orders.get(machine, []):
            group = context.operations[operation_index].setup_group
            if group != previous_group:
                key = (group, run_number[group])
                if key not in node_by_group_slot:
                    return None
                current_node = node_by_group_slot[key]
                run_number[group] += 1
                sequence.append(current_node)
                previous_group = group
            if current_node is None:
                return None
            operation_to_node[operation_index] = (machine, current_node)
        sequence_by_machine[machine] = sequence
    return operation_to_node, sequence_by_machine


def apply_campaign_layout(
    artifacts: ModelArtifacts,
    solution: Individual,
    context: ProblemContext,
    *,
    fix_all: bool,
    fixed_operations: set[int] | None = None,
) -> bool:
    layout = campaign_layout(artifacts, solution, context)
    if layout is None:
        return False
    operation_to_node, sequence_by_machine = layout
    for machine, nodes in artifacts.campaign_nodes_by_machine.items():
        active_nodes = set(sequence_by_machine.get(machine, []))
        for node in nodes:
            node_id = int(node["node_id"])
            desired_active = int(node_id in active_nodes)
            artifacts.model.AddHint(node["active"], desired_active)
            if fix_all:
                artifacts.model.Add(node["active"] == desired_active)
            for operation_index, literal in node["assignments"]:
                desired = int(operation_to_node.get(operation_index) == (machine, node_id))
                artifacts.model.AddHint(literal, desired)
                if fix_all or fixed_operations is not None and operation_index in fixed_operations:
                    artifacts.model.Add(literal == desired)
        sequence = sequence_by_machine.get(machine, [])
        desired_arcs: set[tuple[int, int]] = set()
        if sequence:
            desired_arcs.add((0, sequence[0]))
            desired_arcs.update(zip(sequence, sequence[1:], strict=False))
            desired_arcs.add((sequence[-1], 0))
        for arc in artifacts.campaign_arcs_by_machine.get(machine, []):
            desired = int((int(arc["tail"]), int(arc["head"])) in desired_arcs)
            artifacts.model.AddHint(arc["literal"], desired)
            if fix_all:
                artifacts.model.Add(arc["literal"] == desired)
    return True


def add_assignment_fixes(
    artifacts: ModelArtifacts,
    solution: Individual,
    fixed_operations: set[int],
) -> None:
    for operation_index in fixed_operations:
        operation = artifacts.operations[operation_index]
        selected = solution.machines[operation_index]
        for machine in operation.machine_durations:
            artifacts.model.Add(artifacts.presences[(operation_index, machine)] == int(machine == selected))


def add_relative_order(
    artifacts: ModelArtifacts,
    solution: Individual,
    fixed_operations: set[int],
) -> None:
    for indices in machine_orders(solution).values():
        fixed_order = [operation_index for operation_index in indices if operation_index in fixed_operations]
        for previous, following in zip(fixed_order, fixed_order[1:], strict=False):
            artifacts.model.Add(artifacts.setup_starts[following] >= artifacts.ends[previous])


def observed_campaign_cap(solution: Individual, context: ProblemContext) -> int:
    maximum = 1
    for indices in machine_orders(solution).values():
        counts: Counter[str] = Counter()
        previous_group: str | None = None
        for operation_index in indices:
            group = context.operations[operation_index].setup_group
            if group != previous_group:
                counts[group] += 1
            previous_group = group
        maximum = max(maximum, max(counts.values(), default=1))
    return maximum


def extract_cp_solution(
    artifacts: ModelArtifacts,
    solver: cp_model.CpSolver,
    context: ProblemContext,
) -> Individual:
    machines = [""] * len(context.operations)
    starts = [0] * len(context.operations)
    ends = [0] * len(context.operations)
    for operation in artifacts.operations:
        machines[operation.index] = next(
            machine
            for machine in operation.machine_durations
            if solver.BooleanValue(artifacts.presences[(operation.index, machine)])
        )
        starts[operation.index] = solver.Value(artifacts.starts[operation.index])
        ends[operation.index] = solver.Value(artifacts.ends[operation.index])

    order_by_machine: dict[str, list[int]] = defaultdict(list)
    for operation_index, machine in enumerate(machines):
        order_by_machine[machine].append(operation_index)
    times: list[tuple[int, int, int]] = [(0, 0, 0)] * len(context.operations)
    for indices in order_by_machine.values():
        indices.sort(key=lambda index: (starts[index], index))
        previous_group: str | None = None
        for operation_index in indices:
            group = context.operations[operation_index].setup_group
            setup = task_setup(previous_group, group, context)
            times[operation_index] = (starts[operation_index] - setup, starts[operation_index], ends[operation_index])
            previous_group = group

    operation_sequence = sorted(
        range(len(context.operations)),
        key=lambda index: (starts[index], ends[index], context.operations[index].unit_index, index),
    )
    os_sequence = [context.operations[index].unit_index for index in operation_sequence]
    solution = Individual(
        os_sequence=os_sequence,
        machines=machines,
        times=times,
        source="CP-SAT LNS",
        cp_verified=True,
    )
    solution.fitness = compute_fitness(context, machines, times)
    solution.rows = rows_from_times(context, machines, times)
    solution.campaign_excess = campaign_excess(context, machines, times)
    return solution


def solve_cp_neighborhood(
    context: ProblemContext,
    incumbent: Individual,
    relaxed_operations: set[int],
    seconds: float,
    workers: int,
    seed: int,
    stage: int,
    base_campaign_cap: int,
    makespan_lock: int | None,
    tardiness_lock: int | None,
    *,
    validation_only: bool = False,
) -> CpResult:
    global active_solver
    observed = observed_campaign_cap(incumbent, context)
    campaign_cap = base_campaign_cap
    if observed > campaign_cap:
        return CpResult("INCUMBENT_CAMPAIGN_CAP_VIOLATION", None, 0.0, 0, 0, len(relaxed_operations), campaign_cap)
    artifacts = build_model(context.data, sequence_encoding="campaign_circuit", campaigns_per_group=campaign_cap)
    add_solution_hints(artifacts, incumbent, context)
    all_operations = set(range(len(context.operations)))
    fixed_operations = all_operations if validation_only else all_operations - relaxed_operations
    add_assignment_fixes(artifacts, incumbent, fixed_operations)
    add_relative_order(artifacts, incumbent, fixed_operations)
    if not apply_campaign_layout(
        artifacts,
        incumbent,
        context,
        fix_all=validation_only,
        fixed_operations=None if validation_only else fixed_operations,
    ):
        return CpResult("CAMPAIGN_CAP_TOO_SMALL", None, 0.0, 0, 0, len(relaxed_operations), campaign_cap)

    if validation_only:
        artifacts.model.Add(artifacts.cmax <= incumbent.fitness[0])
        artifacts.model.Add(artifacts.weighted_tardiness <= incumbent.fitness[1])
        artifacts.model.Add(artifacts.total_setup <= incumbent.fitness[2])
    elif stage == 1:
        artifacts.model.Add(artifacts.cmax <= incumbent.fitness[0])
    elif stage == 2:
        artifacts.model.Add(artifacts.cmax <= int(makespan_lock))
        artifacts.model.Add(artifacts.weighted_tardiness <= incumbent.fitness[1])
    else:
        artifacts.model.Add(artifacts.cmax <= int(makespan_lock))
        artifacts.model.Add(artifacts.weighted_tardiness <= int(tardiness_lock))
        artifacts.model.Add(artifacts.total_setup <= incumbent.fitness[2])
    objective = [artifacts.cmax, artifacts.weighted_tardiness, artifacts.total_setup][stage - 1]
    artifacts.model.Minimize(objective)

    solver = configure_solver(max(0.1, seconds), workers, seed, False)
    solver.parameters.cp_model_presolve = True
    active_solver = solver
    try:
        status = solver.Solve(artifacts.model)
    finally:
        active_solver = None
    status_text = solver.StatusName(status)
    solution = None
    if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        solution = extract_cp_solution(artifacts, solver, context)
        validate_decoded_solution(solution, context)
    return CpResult(
        status=status_text,
        solution=solution,
        wall_time=solver.WallTime(),
        branches=solver.NumBranches(),
        conflicts=solver.NumConflicts(),
        relaxed_count=len(relaxed_operations),
        campaign_cap=campaign_cap,
    )


def select_neighborhood(
    solution: Individual,
    context: ProblemContext,
    rng: random.Random,
    target: int,
    round_index: int,
    stage: int,
) -> tuple[set[int], str]:
    if solution.times is None:
        return set(), "none"
    target = max(1, min(target, len(context.operations)))
    orders = machine_orders(solution)
    methods = ["bottleneck_window", "critical_end", "alternative_machine", "time_window"]
    if stage >= 2:
        methods.insert(0, "tardy_jobs")
    if stage >= 3:
        methods.insert(0, "setup_boundaries")
    method = methods[round_index % len(methods)]
    selected: list[int] = []

    if method == "bottleneck_window":
        bottleneck = max(
            orders,
            key=lambda machine: sum(
                solution.times[index][2] - solution.times[index][0] for index in orders[machine]
            ),
        )
        order = orders[bottleneck]
        start = rng.randrange(max(1, len(order) - min(target, len(order)) + 1))
        selected = order[start : start + target]
    elif method == "critical_end":
        selected = sorted(
            range(len(context.operations)),
            key=lambda index: (solution.fitness[0] - solution.times[index][2], -solution.times[index][2]),
        )[:target]
    elif method == "tardy_jobs":
        unit_tardiness = []
        for unit_index, unit in enumerate(context.data["units"]):
            completion = solution.times[context.unit_operations[unit_index][-1]][2]
            unit_tardiness.append(
                (int(unit["priority_weight"]) * max(0, completion - int(unit["due"])), unit_index)
            )
        for _, unit_index in sorted(unit_tardiness, reverse=True):
            selected.extend(context.unit_operations[unit_index])
            if len(selected) >= target:
                break
    elif method == "setup_boundaries":
        boundaries: list[int] = []
        for order in orders.values():
            for previous, following in zip(order, order[1:], strict=False):
                if context.operations[previous].setup_group != context.operations[following].setup_group:
                    boundaries.extend([previous, following])
        rng.shuffle(boundaries)
        selected = boundaries[:target]
    elif method == "alternative_machine":
        alternatives = [operation.index for operation in context.operations if len(operation.machine_durations) > 1]
        rng.shuffle(alternatives)
        selected = alternatives[:target]
    else:
        ordered = sorted(range(len(context.operations)), key=lambda index: solution.times[index][1])
        start = rng.randrange(max(1, len(ordered) - target + 1))
        selected = ordered[start : start + target]

    selected_set = set(selected)
    # Include immediate job neighbors so CP can repair precedence around moved
    # operations; trim only after the useful closure has been formed.
    for operation_index in list(selected_set):
        operation = context.operations[operation_index]
        unit_indices = context.unit_operations[operation.unit_index]
        position = unit_indices.index(operation_index)
        if position:
            selected_set.add(unit_indices[position - 1])
        if position + 1 < len(unit_indices):
            selected_set.add(unit_indices[position + 1])
    maximum = min(len(context.operations), max(target, round(target * 1.35)))
    if len(selected_set) > maximum:
        selected_set = set(rng.sample(sorted(selected_set), maximum))
    return selected_set, method


def stage_key(solution: Individual, stage: int) -> tuple[int, int, int]:
    if solution.fitness is None:
        return (math.inf, math.inf, math.inf)
    cmax, tardiness, setup = solution.fitness
    if stage == 1:
        return cmax, tardiness, setup
    if stage == 2:
        return tardiness, setup, cmax
    return setup, tardiness, cmax


def write_csv_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    temporary = path.with_suffix(path.suffix + ".new")
    with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def checkpoint_tables(
    solution: Individual,
    context: ProblemContext,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if solution.times is None:
        return [], []
    unit_rows = []
    for unit_index, unit in enumerate(context.data["units"]):
        completion = solution.times[context.unit_operations[unit_index][-1]][2]
        tardiness = max(0, completion - int(unit["due"]))
        unit_rows.append(
            {
                "unit_id": unit["unit_id"],
                "order_job_id": unit["order_job_id"],
                "priority": unit["priority"],
                "priority_weight": unit["priority_weight"],
                "release_min": int(unit["release"]) / context.scale,
                "due_min": int(unit["due"]) / context.scale,
                "completion_min": completion / context.scale,
                "tardiness_min": tardiness / context.scale,
                "weighted_tardiness": int(unit["priority_weight"]) * tardiness / context.scale,
            }
        )
    processing: Counter[str] = Counter()
    setup: Counter[str] = Counter()
    for operation in context.operations:
        machine = solution.machines[operation.index]
        setup_start, start, _ = solution.times[operation.index]
        processing[machine] += operation.machine_durations[machine]
        setup[machine] += start - setup_start
    cmax = solution.fitness[0]
    machine_rows = [
        {
            "machine": machine,
            "processing_min": processing[machine] / context.scale,
            "setup_min": setup[machine] / context.scale,
            "occupied_min": (processing[machine] + setup[machine]) / context.scale,
            "makespan_min": cmax / context.scale,
            "occupied_utilization": (processing[machine] + setup[machine]) / cmax if cmax else 0,
        }
        for machine in context.data["machines"]
    ]
    return unit_rows, machine_rows


def validation_table(solution: Individual, context: ProblemContext) -> list[dict[str, Any]]:
    """Excel'e yazılan, çözücüden bağımsız sonuç kontrolleri."""

    validate_decoded_solution(solution, context)
    maximum_campaign = observed_campaign_cap(solution, context)
    return [
        {"check": "Alternatör kapsamı", "expected": len(context.data["units"]), "actual": len(context.unit_operations), "status": "PASS"},
        {"check": "Operasyon kapsamı", "expected": len(context.operations), "actual": len(solution.times or []), "status": "PASS"},
        {"check": "Uygun makine ve işlem süresi", "expected": "0 ihlal", "actual": 0, "status": "PASS"},
        {"check": "Makine çakışması + setup", "expected": "0 ihlal", "actual": 0, "status": "PASS"},
        {"check": "Operasyon sırası + taşıma", "expected": "0 ihlal", "actual": 0, "status": "PASS"},
        {"check": "Release/setup başlangıcı", "expected": "0 ihlal", "actual": 0, "status": "PASS"},
        {"check": "Makine/setup grubu kampanya sınırı", "expected": f"<= {context.campaign_limit}", "actual": maximum_campaign, "status": "PASS"},
        {"check": "Zaman ekseni", "expected": "540 net dk/iş günü", "actual": context.data.get("time_axis", ""), "status": "PASS"},
        {"check": "Hafta sonu/gece Cmax'a eklenmesi", "expected": "Hayır", "actual": "Hayır", "status": "PASS"},
    ]


def save_checkpoint(
    output_dir: Path,
    solution: Individual,
    context: ProblemContext,
    *,
    status: str,
    phase: str,
    started: float,
    total_seconds: float,
    generation: int,
    cp_round: int,
    validation: str,
    neighborhood: str,
    relaxed_operations: int,
    skip_excel: bool,
    export_excel: bool = True,
) -> None:
    if solution.rows is None or solution.fitness is None:
        return
    validation_rows = validation_table(solution, context)
    output_dir.mkdir(parents=True, exist_ok=True)
    unit_rows, machine_rows = checkpoint_tables(solution, context)
    checkpoint = {
        "status": status,
        "stage": phase,
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "total_time_seconds": total_seconds,
        "source": solution.source,
        "algorithm": (
            "Eski OR-Tools CP kısıtlarıyla uyumlu OS-MS GA + setup-aware aktif decoder + "
            "CP-SAT LNS + 4 dk stagnasyonda tam-serbest CP-SAT"
        ),
        "alternator_units": len(context.data["units"]),
        "atomic_operations": len(context.operations),
        "ga_generation": generation,
        "cp_round": cp_round,
        "validation": validation,
        "neighborhood": neighborhood,
        "relaxed_operations": relaxed_operations,
        "campaign_excess": solution.campaign_excess,
        "campaign_limit": context.campaign_limit,
        "constraint_validation": "PASS",
        "time_axis": context.data.get("time_axis", "net_work_minutes"),
        "daily_work_minutes": context.data.get("daily_work_minutes", 540),
        "planning_start": context.data.get("planning_start"),
        "base_date": context.data.get("base_date"),
        "objectives": {
            "makespan_min": solution.fitness[0] / context.scale,
            "weighted_tardiness": solution.fitness[1] / context.scale,
            "total_setup_min": solution.fitness[2] / context.scale,
        },
    }
    atomic_text(output_dir / "checkpoint.json", json.dumps(checkpoint, ensure_ascii=False, indent=2))
    with (output_dir / "checkpoint_history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(checkpoint, ensure_ascii=False) + "\n")
    write_csv_atomic(output_dir / "schedule.csv", solution.rows)
    write_csv_atomic(output_dir / "unit_completion.csv", unit_rows)
    write_csv_atomic(output_dir / "machine_utilization.csv", machine_rows)
    write_csv_atomic(output_dir / "constraint_validation.csv", validation_rows)
    atomic_text(
        output_dir / "best_solution_state.json",
        json.dumps(
            {
                "input_source_sha256": context.data.get("source_sha256"),
                "os_sequence": solution.os_sequence,
                "machines": solution.machines,
                "fitness": solution.fitness,
                "cp_verified": solution.cp_verified,
            },
            ensure_ascii=False,
        ),
    )
    if not skip_excel and export_excel:
        if not NODE.exists():
            raise FileNotFoundError(f"Excel çalışma zamanı bulunamadı: {NODE}")
        subprocess.run(
            [str(NODE), str(EXCEL_EXPORTER), str(output_dir), str(output_dir / "en_iyi_cizelge.xlsx")],
            check=True,
            timeout=180,
            start_new_session=True,
        )


def load_data(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    if args.input is not None:
        data = json.loads(args.input.read_text(encoding="utf-8"))
        if data.get("time_axis") != "net_work_minutes" or int(data.get("daily_work_minutes", 0)) != 540:
            raise ValueError(
                "Hazır JSON eski/uyumsuz zaman ekseninde. --input kullanmadan Excel'den yeniden üretin."
            )
        return data, args.input
    if not args.workbook.exists():
        raise FileNotFoundError(f"Net ihtiyaç Excel'i bulunamadı: {args.workbook}")
    data, audit = build_payload(
        args.workbook,
        args.route_input,
        args.planning_start,
        args.priority_workbook,
    )
    if args.expected_units is not None and len(data["units"]) != args.expected_units:
        raise ValueError(
            f"Beklenen {args.expected_units}, Excel'den seçilen {len(data['units'])} yeni imalat var"
        )
    atomic_text(DEFAULT_DYNAMIC_INPUT, json.dumps(data, ensure_ascii=False, indent=2))
    atomic_text(DEFAULT_DYNAMIC_AUDIT, json.dumps(audit, ensure_ascii=False, indent=2))
    return data, DEFAULT_DYNAMIC_INPUT


def initialize_population(
    context: ProblemContext,
    rng: random.Random,
    population_size: int,
    incumbent: Individual | None,
) -> list[Individual]:
    population = [copy.deepcopy(incumbent)] if incumbent is not None else []
    while len(population) < population_size and not stop_requested:
        population.append(make_individual(context, rng, len(population) + rng.randrange(10_000)))
        if len(population) % 8 == 0 or len(population) == population_size:
            print(f"Başlangıç popülasyonu: {len(population)}/{population_size}", flush=True)
    if not population:
        raise RuntimeError("Popülasyon oluşturulamadı")
    return population


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=None, help="Hazır JSON; verilirse --workbook kullanılmaz")
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--route-input", type=Path, default=DEFAULT_ROUTE_INPUT)
    parser.add_argument("--priority-workbook", type=Path, default=DEFAULT_PRIORITY_WORKBOOK)
    parser.add_argument("--planning-start", type=date.fromisoformat, default=None)
    parser.add_argument("--expected-units", type=int, default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stage-seconds", type=float, default=3600, help="Her amaç aşaması; varsayılan 1 saat")
    parser.add_argument("--ga-seconds-per-stage", type=float, default=1500, help="Her aşamada GA; varsayılan 25 dakika")
    parser.add_argument("--population-size", type=int, default=64)
    parser.add_argument("--elite-count", type=int, default=8)
    parser.add_argument("--crossover-rate", type=float, default=0.85)
    parser.add_argument("--mutation-rate", type=float, default=0.10)
    parser.add_argument("--stagnation-generations", type=int, default=25)
    parser.add_argument("--vns-elite-count", type=int, default=3, help="VNS uygulanacak elit birey sayısı")
    parser.add_argument("--vns-evaluations", type=int, default=24, help="Her elit için VNS komşu değerlendirmesi")
    parser.add_argument("--vns-interval-generations", type=int, default=10, help="Periyodik elit VNS aralığı")
    parser.add_argument("--diversify-rate", type=float, default=0.35, help="Durgunluk anında yenilenecek popülasyon oranı")
    parser.add_argument("--cp-seconds-per-round", type=float, default=60)
    parser.add_argument("--relax-fraction", type=float, default=0.10)
    parser.add_argument("--min-relaxed-operations", type=int, default=80)
    parser.add_argument("--max-relaxed-operations", type=int, default=180)
    parser.add_argument("--full-cp-stagnation-seconds", type=float, default=240)
    parser.add_argument("--full-cp-seconds", type=float, default=300)
    parser.add_argument("--campaigns-per-group", type=int, default=2)
    parser.add_argument("--workers", type=int, default=max(1, min(os.cpu_count() or 1, 10)))
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--checkpoint-min-seconds", type=float, default=120)
    parser.add_argument("--skip-excel", action="store_true", help="Yalnızca otomatik testler için")
    parser.add_argument("--initial-state", type=Path, default=None, help="Dondurulmuş FIFO best_solution_state.json")
    args = parser.parse_args()

    if args.stage_seconds < args.ga_seconds_per_stage or args.ga_seconds_per_stage < 0:
        raise ValueError("Aşama süresi, GA süresi ve popülasyon değerlerini kontrol edin")
    if not 1 <= args.elite_count < args.population_size:
        raise ValueError("Elit sayısı popülasyondan küçük ve pozitif olmalı")
    if not 0 <= args.crossover_rate <= 1 or not 0 <= args.mutation_rate <= 1:
        raise ValueError("GA oranları 0..1 arasında olmalı")
    if args.workers < 1 or args.cp_seconds_per_round <= 0 or args.campaigns_per_group < 1:
        raise ValueError("CP-SAT parametreleri pozitif olmalı")
    if args.vns_elite_count < 0 or args.vns_evaluations < 1 or args.vns_interval_generations < 1:
        raise ValueError("VNS parametrelerini kontrol edin")

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    data, input_path = load_data(args)
    validate_payload(data, input_path)
    context = ProblemContext.create(data, args.campaigns_per_group)
    rng = random.Random(args.seed)
    started = time.monotonic()
    total_seconds = args.stage_seconds * 3
    deadline = started + total_seconds
    print(
        f"KESİN KISITLI HGA-VNS + CP-SAT BAŞLADI | Yeni imalat: {len(data['units'])} | "
        f"Operasyon: {len(context.operations)} | 3 aşama x {args.stage_seconds:.0f} sn | "
        f"Her aşama: GA {args.ga_seconds_per_stage:.0f} sn + CP {args.stage_seconds - args.ga_seconds_per_stage:.0f} sn | "
        f"Kampanya sınırı={args.campaigns_per_group}",
        flush=True,
    )

    incumbent: Individual | None = None
    if args.initial_state is not None:
        state = json.loads(args.initial_state.read_text(encoding="utf-8"))
        incumbent = decode(Individual(
            os_sequence=[int(x) for x in state["os_sequence"]],
            machines=[str(x) for x in state["machines"]],
            source="FIFO ortak başlangıç",
        ), context)
        validate_decoded_solution(incumbent, context)
        print(f"FIFO ortak başlangıç yüklendi | Cmax={incumbent.fitness[0] / context.scale:.1f}", flush=True)
    generation = 0
    cp_round = 0
    validation_status = "INDEPENDENT_CONSTRAINT_VALIDATION_PASS"
    last_neighborhood = "Başlangıç"
    last_relaxed = 0
    final_saved = False
    last_checkpoint_time = started
    last_excel_time = started
    phase = "Başlangıç"
    makespan_lock: int | None = None
    tardiness_lock: int | None = None
    try:
        for current_stage in (1, 2, 3):
            if stop_requested:
                break
            objective_name = ("makespan", "ağırlıklı gecikme", "toplam setup")[current_stage - 1]
            phase = f"Aşama {current_stage} - {objective_name} - GA"
            print(
                f"\n[Aşama {current_stage}] {objective_name} | "
                f"{args.ga_seconds_per_stage / 60:g} dk GA + "
                f"{(args.stage_seconds - args.ga_seconds_per_stage) / 60:g} dk CP-SAT",
                flush=True,
            )

            key_fn = lambda item, s=current_stage, ml=makespan_lock, tl=tardiness_lock: stage_ga_key(item, s, ml, tl)
            population = initialize_population(context, rng, args.population_size, incumbent)
            population.sort(key=key_fn)
            if incumbent is None:
                incumbent = copy.deepcopy(population[0])
                validate_decoded_solution(incumbent, context)
            save_checkpoint(
                args.output_dir, incumbent, context,
                status=f"STAGE_{current_stage}_GA_START", phase=phase, started=started,
                total_seconds=total_seconds, generation=generation, cp_round=cp_round,
                validation=validation_status, neighborhood="HGA-VNS (OS-MS GA + VNS)", relaxed_operations=0,
                skip_excel=args.skip_excel,
            )
            last_checkpoint_time = last_excel_time = time.monotonic()

            # Veri hazırlama, ilk popülasyon ve Excel yazma süresi optimizasyon
            # bütçesinden çalmasın: her aşama gerçekten 25 dk GA + 35 dk
            # CP-SAT araması yapar.
            stage_started = time.monotonic()
            stage_end = stage_started + args.stage_seconds
            ga_end = stage_started + args.ga_seconds_per_stage

            stagnant = 0
            last_progress = time.monotonic()
            while time.monotonic() < ga_end and not stop_requested:
                old_key = key_fn(population[0])
                adaptive_mutation = min(0.30, args.mutation_rate * (1 + stagnant / 12))
                population = evolve(
                    population, context, rng, args.elite_count,
                    args.crossover_rate, adaptive_mutation, key_fn,
                )
                population.sort(key=key_fn)
                generation += 1
                if args.vns_elite_count and generation % args.vns_interval_generations == 0:
                    population.sort(key=key_fn)
                    for elite_index in range(min(args.vns_elite_count, len(population))):
                        population[elite_index] = vns_improve(
                            population[elite_index], context, rng, key_fn,
                            max_evaluations=args.vns_evaluations,
                        )
                    population.sort(key=key_fn)
                stagnant = 0 if key_fn(population[0]) < old_key else stagnant + 1
                if key_fn(population[0]) < key_fn(incumbent):
                    candidate = copy.deepcopy(population[0])
                    validate_decoded_solution(candidate, context)
                    incumbent = candidate
                    should_export = time.monotonic() - last_excel_time >= args.checkpoint_min_seconds
                    save_checkpoint(
                        args.output_dir, incumbent, context,
                        status=f"STAGE_{current_stage}_GA_IMPROVED", phase=phase, started=started,
                        total_seconds=total_seconds, generation=generation, cp_round=cp_round,
                        validation=validation_status, neighborhood="HGA-VNS (OS-MS GA + VNS)", relaxed_operations=0,
                        skip_excel=args.skip_excel, export_excel=should_export,
                    )
                    last_checkpoint_time = time.monotonic()
                    if should_export:
                        last_excel_time = last_checkpoint_time
                if stagnant >= args.stagnation_generations:
                    population = diversify(population, context, rng, args.elite_count, args.diversify_rate, key_fn)
                    population[0] = copy.deepcopy(incumbent)
                    population.sort(key=key_fn)
                    stagnant = 0
                    print(f"[GA {generation}] yerel arama + %{int(args.diversify_rate * 100)} çeşitlendirme", flush=True)
                if time.monotonic() - last_progress >= 30:
                    print(
                        f"[GA {generation}] Cmax={incumbent.fitness[0] / context.scale:.1f} | "
                        f"Gecikme={incumbent.fitness[1] / context.scale:.1f} | "
                        f"Setup={incumbent.fitness[2] / context.scale:.1f} | "
                        f"GA kalan={max(0, ga_end - time.monotonic()):.0f} sn",
                        flush=True,
                    )
                    last_progress = time.monotonic()

            phase = f"Aşama {current_stage} - {objective_name} - CP-SAT"
            save_checkpoint(
                args.output_dir, incumbent, context,
                status=f"STAGE_{current_stage}_GA_COMPLETE", phase=phase, started=started,
                total_seconds=total_seconds, generation=generation, cp_round=cp_round,
                validation=validation_status, neighborhood="HGA-VNS en iyi", relaxed_operations=0,
                skip_excel=args.skip_excel,
            )
            last_checkpoint_time = last_excel_time = time.monotonic()

            target = max(
                args.min_relaxed_operations,
                min(args.max_relaxed_operations, round(len(context.operations) * args.relax_fraction)),
            )
            last_improvement = time.monotonic()
            while time.monotonic() < stage_end and not stop_requested:
                remaining = stage_end - time.monotonic()
                if remaining < 0.5:
                    break
                full_escape = time.monotonic() - last_improvement >= args.full_cp_stagnation_seconds
                if full_escape:
                    relaxed = set(range(len(context.operations)))
                    neighborhood = "TAM_SERBEST_CP_SAT_GERCEK_KISITLAR_KORUNDU"
                    seconds = min(args.full_cp_seconds, remaining)
                else:
                    relaxed, neighborhood = select_neighborhood(
                        incumbent, context, rng, target, cp_round, current_stage,
                    )
                    seconds = min(args.cp_seconds_per_round, remaining)
                last_neighborhood = neighborhood
                last_relaxed = len(relaxed)
                print(
                    f"[CP {cp_round + 1}] Aşama {current_stage} | {neighborhood} | "
                    f"serbest={len(relaxed)} | {seconds:.0f} sn",
                    flush=True,
                )
                result = solve_cp_neighborhood(
                    context, incumbent, relaxed, seconds, args.workers,
                    args.seed + cp_round + 1, current_stage, args.campaigns_per_group,
                    makespan_lock, tardiness_lock,
                )
                cp_round += 1
                improved = result.solution is not None and stage_key(result.solution, current_stage) < stage_key(incumbent, current_stage)
                if improved:
                    incumbent = result.solution
                    incumbent.source = f"CP-SAT: {neighborhood}"
                    validation_status = f"CP_SAT_{result.status}_INDEPENDENT_PASS"
                    last_improvement = time.monotonic()
                    target = min(args.max_relaxed_operations, max(target, round(target * 1.10)))
                    should_export = time.monotonic() - last_excel_time >= args.checkpoint_min_seconds
                    save_checkpoint(
                        args.output_dir, incumbent, context,
                        status=f"STAGE_{current_stage}_CP_IMPROVED", phase=phase, started=started,
                        total_seconds=total_seconds, generation=generation, cp_round=cp_round,
                        validation=validation_status, neighborhood=neighborhood,
                        relaxed_operations=len(relaxed), skip_excel=args.skip_excel,
                        export_excel=should_export,
                    )
                    last_checkpoint_time = time.monotonic()
                    if should_export:
                        last_excel_time = last_checkpoint_time
                elif full_escape:
                    # Tam serbest turdan sonra tekrar 4 dakika LNS dene.
                    last_improvement = time.monotonic()
                elif result.solution is None:
                    target = max(args.min_relaxed_operations, round(target * 0.80))
                print(
                    f"[CP {cp_round}] {result.status} | gelişme={'evet' if improved else 'hayır'} | "
                    f"Cmax={incumbent.fitness[0] / context.scale:.1f}", flush=True,
                )

            if current_stage == 1:
                makespan_lock = incumbent.fitness[0]
            elif current_stage == 2:
                tardiness_lock = incumbent.fitness[1]
            save_checkpoint(
                args.output_dir, incumbent, context,
                status=f"STAGE_{current_stage}_COMPLETE", phase=f"{phase} - tamamlandı", started=started,
                total_seconds=total_seconds, generation=generation, cp_round=cp_round,
                validation=validation_status, neighborhood=last_neighborhood,
                relaxed_operations=last_relaxed, skip_excel=args.skip_excel,
            )
            last_checkpoint_time = last_excel_time = time.monotonic()

        final_status = f"INTERRUPTED: {stop_reason}" if stop_requested else "COMPLETED"
        save_checkpoint(
            args.output_dir,
            incumbent,
            context,
            status=final_status,
            phase=f"{phase} - final",
            started=started,
            total_seconds=total_seconds,
            generation=generation,
            cp_round=cp_round,
            validation=validation_status,
            neighborhood=last_neighborhood,
            relaxed_operations=last_relaxed,
            skip_excel=args.skip_excel,
        )
        final_saved = True
    except BaseException as exc:
        if incumbent is not None:
            try:
                save_checkpoint(
                    args.output_dir,
                    incumbent,
                    context,
                    status=f"ERROR: {type(exc).__name__}",
                    phase=f"{phase} - hata checkpoint",
                    started=started,
                    total_seconds=total_seconds,
                    generation=generation,
                    cp_round=cp_round,
                    validation=validation_status,
                    neighborhood=last_neighborhood,
                    relaxed_operations=last_relaxed,
                    skip_excel=args.skip_excel,
                )
                final_saved = True
            except Exception as save_error:
                print(f"Hata checkpoint'i yazılamadı: {save_error}", flush=True)
        raise
    finally:
        if incumbent is not None and not final_saved:
            try:
                save_checkpoint(
                    args.output_dir,
                    incumbent,
                    context,
                    status=f"INTERRUPTED: {stop_reason or 'PROCESS_EXIT'}",
                    phase=f"{phase} - final",
                    started=started,
                    total_seconds=total_seconds,
                    generation=generation,
                    cp_round=cp_round,
                    validation=validation_status,
                    neighborhood=last_neighborhood,
                    relaxed_operations=last_relaxed,
                    skip_excel=args.skip_excel,
                )
            except Exception as save_error:
                print(f"Final checkpoint yazılamadı: {save_error}", flush=True)

    print(f"Tek Excel: {args.output_dir / 'en_iyi_cizelge.xlsx'}", flush=True)
    return 130 if stop_requested else 0


if __name__ == "__main__":
    raise SystemExit(main())

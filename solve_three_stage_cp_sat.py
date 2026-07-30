"""Solve a validated alternator FJSP with three lexicographic stages.

Stage 1 minimizes makespan.  Stage 2 preserves the Stage 1 result within
epsilon_1 and minimizes weighted tardiness.  Stage 3 preserves both previous
results within their tolerances and minimizes sequence-dependent setup time.

The input must be produced by prepare_master_input.py.  By default the solver
does not advance to the next stage unless the current stage is proven optimal;
this protects the mathematical meaning of Cmax* and Z2*.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ortools.sat.python import cp_model


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "data" / "master_three_stage_input.json"
DEFAULT_OUTPUT_DIR = ROOT / "output_three_stage"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def status_name(status: cp_model.CpSolverStatus) -> str:
    return {
        cp_model.UNKNOWN: "UNKNOWN",
        cp_model.MODEL_INVALID: "MODEL_INVALID",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.OPTIMAL: "OPTIMAL",
    }.get(status, str(status))


@dataclass(frozen=True)
class Operation:
    index: int
    unit_index: int
    unit_id: str
    order_job_id: str
    sequence: int
    name: str
    setup_group: str
    machine_durations: dict[str, int]


@dataclass
class ModelArtifacts:
    model: cp_model.CpModel
    operations: list[Operation]
    starts: dict[int, cp_model.IntVar]
    setup_starts: dict[int, cp_model.IntVar]
    ends: dict[int, cp_model.IntVar]
    presences: dict[tuple[int, str], cp_model.BoolVar]
    intervals_by_machine: dict[str, list[cp_model.IntervalVar]]
    completion_by_unit: dict[str, cp_model.IntVar]
    tardiness_by_unit: dict[str, cp_model.IntVar]
    cmax: cp_model.IntVar
    weighted_tardiness: cp_model.IntVar
    total_setup: cp_model.IntVar
    setup_terms_by_machine: dict[str, list[Any]]
    sequence_candidate_counts: dict[str, int]
    sequence_encoding: str
    campaigns_per_group: int
    campaign_nodes_by_machine: dict[str, list[dict[str, Any]]]
    campaign_arcs_by_machine: dict[str, list[dict[str, Any]]]
    horizon: int


def validate_payload(data: dict[str, Any], input_path: Path) -> None:
    required_top = {"schema_version", "model", "source_workbook", "source_sha256", "time_scale", "parameters", "machines", "units"}
    missing = required_top - data.keys()
    if missing:
        raise ValueError(f"Girdi alanları eksik: {sorted(missing)}")
    if data["model"] != "three_stage_lexicographic_fjsp":
        raise ValueError(f"Beklenmeyen model türü: {data['model']!r}")
    expected_units = int(data.get("unit_count", 256))
    if expected_units <= 0 or len(data["units"]) != expected_units:
        raise ValueError(
            f"Model beklenen {expected_units} yerine {len(data['units'])} alternatör içeriyor"
        )
    unit_ids = [unit["unit_id"] for unit in data["units"]]
    if len(unit_ids) != len(set(unit_ids)):
        raise ValueError("Tekrarlı Alternatör Birim ID var")
    machines = set(data["machines"])
    if not machines:
        raise ValueError("Makine kümesi boş")
    for unit in data["units"]:
        operations = unit.get("operations") or []
        if not operations:
            raise ValueError(f"{unit['unit_id']}: operasyon rotası boş")
        sequences = [int(operation["sequence"]) for operation in operations]
        if sequences != list(range(1, len(operations) + 1)):
            raise ValueError(f"{unit['unit_id']}: atomik operasyon sırası geçersiz")
        for operation in operations:
            options = operation.get("machine_durations") or {}
            if not options:
                raise ValueError(f"{unit['unit_id']} O{operation['sequence']}: makine-süre seçeneği yok")
            if set(options) - machines:
                raise ValueError(f"{unit['unit_id']} O{operation['sequence']}: tanımsız makine var")
            if any(not isinstance(duration, int) or duration <= 0 for duration in options.values()):
                raise ValueError(f"{unit['unit_id']} O{operation['sequence']}: süreler pozitif tamsayı olmalı")
    source_path = Path(data["source_workbook"])
    if not source_path.is_absolute():
        candidates = [input_path.parent / source_path, ROOT / source_path, Path.cwd() / source_path]
        source_path = next((candidate.resolve() for candidate in candidates if candidate.exists()), source_path)
    if source_path.exists() and sha256_file(source_path) != data["source_sha256"]:
        raise ValueError("Ana Excel, girdi üretildikten sonra değişmiş; prepare_master_input.py yeniden çalıştırılmalı")
    if input_path.stat().st_size <= 0:
        raise ValueError("Girdi dosyası boş")


def flatten_operations(data: dict[str, Any]) -> tuple[list[Operation], dict[str, list[int]]]:
    operations: list[Operation] = []
    unit_operations: dict[str, list[int]] = defaultdict(list)
    for unit_index, unit in enumerate(data["units"]):
        for operation in unit["operations"]:
            index = len(operations)
            item = Operation(
                index=index,
                unit_index=unit_index,
                unit_id=unit["unit_id"],
                order_job_id=unit["order_job_id"],
                sequence=int(operation["sequence"]),
                name=operation["operation"],
                setup_group=str(operation["setup_group"]),
                machine_durations={machine: int(duration) for machine, duration in operation["machine_durations"].items()},
            )
            operations.append(item)
            unit_operations[item.unit_id].append(index)
    return operations, unit_operations


def setup_duration(previous_group: str | None, next_group: str, parameters: dict[str, Any]) -> int:
    if previous_group is None:
        return int(parameters["setup_change_or_initial"][next_group])
    if previous_group == next_group:
        return int(parameters["setup_same"][next_group])
    return int(parameters["setup_change_or_initial"][next_group])


def build_model(
    data: dict[str, Any],
    *,
    sequence_encoding: str = "campaign_circuit",
    campaigns_per_group: int = 8,
) -> ModelArtifacts:
    if sequence_encoding not in {"campaign_circuit", "inverse_position_active_prefix"}:
        raise ValueError(f"Bilinmeyen sıra kodlaması: {sequence_encoding}")
    if campaigns_per_group < 1:
        raise ValueError("campaigns_per_group en az 1 olmalı")
    parameters = data["parameters"]
    operations, unit_operations = flatten_operations(data)
    max_release = max(int(unit["release"]) for unit in data["units"])
    sum_max_processing = sum(max(operation.machine_durations.values()) for operation in operations)
    max_setup = max(
        [int(value) for value in parameters["setup_same"].values()]
        + [int(value) for value in parameters["setup_change_or_initial"].values()]
    )
    transport = int(parameters["transport_time"])
    horizon = max_release + sum_max_processing + len(operations) * (max_setup + transport)
    if horizon <= 0:
        raise ValueError("Zaman ufku pozitif değil")

    model = cp_model.CpModel()
    starts: dict[int, cp_model.IntVar] = {}
    setup_starts: dict[int, cp_model.IntVar] = {}
    ends: dict[int, cp_model.IntVar] = {}
    presences: dict[tuple[int, str], cp_model.BoolVar] = {}
    intervals_by_machine: dict[str, list[cp_model.IntervalVar]] = defaultdict(list)

    for operation in operations:
        start = model.NewIntVar(0, horizon, f"start_{operation.index}")
        end = model.NewIntVar(0, horizon, f"end_{operation.index}")
        starts[operation.index] = start
        ends[operation.index] = end
        base_setup = int(parameters["setup_same"][operation.setup_group]) if sequence_encoding == "campaign_circuit" else 0
        if base_setup:
            setup_start = model.NewIntVar(0, horizon, f"setup_start_{operation.index}")
            model.Add(start == setup_start + base_setup)
        else:
            setup_start = start
        setup_starts[operation.index] = setup_start
        operation_presence: list[cp_model.BoolVar] = []
        for machine, duration in sorted(operation.machine_durations.items()):
            presence = model.NewBoolVar(f"assign_{operation.index}_{machine}")
            interval = model.NewOptionalIntervalVar(
                setup_start,
                duration + base_setup,
                end,
                presence,
                f"interval_{operation.index}_{machine}",
            )
            presences[(operation.index, machine)] = presence
            intervals_by_machine[machine].append(interval)
            operation_presence.append(presence)
        model.AddExactlyOne(operation_presence)

    for machine in data["machines"]:
        intervals = intervals_by_machine.get(machine, [])
        if intervals:
            model.AddNoOverlap(intervals)

    unit_by_id = {unit["unit_id"]: unit for unit in data["units"]}
    for unit_id, indices in unit_operations.items():
        unit = unit_by_id[unit_id]
        # Eski CP ile aynı semantik: release, ilk operasyonun işleme
        # başlangıcına değil setup başlangıcına uygulanır.
        model.Add(setup_starts[indices[0]] >= int(unit["release"]))
        for previous_index, next_index in zip(indices, indices[1:], strict=False):
            previous = operations[previous_index]
            following = operations[next_index]
            for previous_machine in previous.machine_durations:
                for next_machine in following.machine_durations:
                    previous_presence = presences[(previous_index, previous_machine)]
                    next_presence = presences[(next_index, next_machine)]
                    if previous_machine == next_machine:
                        model.Add(setup_starts[next_index] >= ends[previous_index]).OnlyEnforceIf(
                            [previous_presence, next_presence]
                        )
                    else:
                        model.Add(
                            setup_starts[next_index] >= ends[previous_index] + transport
                        ).OnlyEnforceIf([previous_presence, next_presence])

    # Sequence-dependent setup encoding.
    #
    # A directed AddCircuit over every eligible operation pair creates O(n_m^2)
    # Boolean arcs on machine m.  MP090/MP091 alone would create millions of
    # arcs for this data set.  Instead, all candidates are put in an inverse
    # permutation and assigned operations occupy an active prefix.  Consecutive
    # active positions define the exact machine sequence, so setup feasibility
    # and setup cost need only O(n_m) explicit position constraints.
    setup_terms_by_machine: dict[str, list[Any]] = defaultdict(list)
    campaign_nodes_by_machine: dict[str, list[dict[str, Any]]] = defaultdict(list)
    campaign_arcs_by_machine: dict[str, list[dict[str, Any]]] = defaultdict(list)
    operations_by_machine: dict[str, list[int]] = defaultdict(list)
    for operation in operations:
        for machine in operation.machine_durations:
            operations_by_machine[machine].append(operation.index)

    setup_groups = sorted(
        set(parameters["setup_same"])
        | set(parameters["setup_change_or_initial"])
        | {operation.setup_group for operation in operations}
    )
    if set(parameters["setup_same"]) != set(setup_groups) or set(parameters["setup_change_or_initial"]) != set(setup_groups):
        raise ValueError("Bütün setup grupları için aynı-seri ve farklı/ilk-iş süreleri tanımlı olmalı")
    setup_group_id = {group: index for index, group in enumerate(setup_groups)}
    group_count = len(setup_groups)
    initial_setup_by_group = [setup_duration(None, group, parameters) for group in setup_groups]
    transition_setup = [
        setup_duration(previous_group, next_group, parameters)
        for previous_group in setup_groups
        for next_group in setup_groups
    ]

    if sequence_encoding == "inverse_position_active_prefix":
        for machine, operation_indices in sorted(operations_by_machine.items()):
            candidate_count = len(operation_indices)
            position_of_operation = [
                model.NewIntVar(0, candidate_count - 1, f"position_{machine}_{operation_index}")
                for operation_index in operation_indices
            ]
            operation_at_position = [
                model.NewIntVar(0, candidate_count - 1, f"operation_at_{machine}_{position}")
                for position in range(candidate_count)
            ]
            model.AddInverse(position_of_operation, operation_at_position)

            active_at_position = [model.NewBoolVar(f"active_{machine}_{position}") for position in range(candidate_count)]
            for position in range(candidate_count - 1):
                model.Add(active_at_position[position] >= active_at_position[position + 1])
                model.Add(operation_at_position[position] < operation_at_position[position + 1]).OnlyEnforceIf(
                    active_at_position[position].Not()
                )

            for local_index, operation_index in enumerate(operation_indices):
                model.AddElement(
                    position_of_operation[local_index],
                    active_at_position,
                    presences[(operation_index, machine)],
                )

            starts_at_position: list[cp_model.IntVar] = []
            ends_at_position: list[cp_model.IntVar] = []
            groups_at_position: list[cp_model.IntVar] = []
            candidate_starts = [starts[operation_index] for operation_index in operation_indices]
            candidate_ends = [ends[operation_index] for operation_index in operation_indices]
            candidate_groups = [setup_group_id[operations[operation_index].setup_group] for operation_index in operation_indices]
            for position in range(candidate_count):
                start_at_position = model.NewIntVar(0, horizon, f"start_at_{machine}_{position}")
                end_at_position = model.NewIntVar(0, horizon, f"end_at_{machine}_{position}")
                group_at_position = model.NewIntVar(0, group_count - 1, f"group_at_{machine}_{position}")
                model.AddElement(operation_at_position[position], candidate_starts, start_at_position)
                model.AddElement(operation_at_position[position], candidate_ends, end_at_position)
                model.AddElement(operation_at_position[position], candidate_groups, group_at_position)
                starts_at_position.append(start_at_position)
                ends_at_position.append(end_at_position)
                groups_at_position.append(group_at_position)

            first_setup = model.NewIntVar(0, max_setup, f"initial_setup_{machine}")
            model.AddElement(groups_at_position[0], initial_setup_by_group, first_setup)
            first_setup_contribution = model.NewIntVar(0, max_setup, f"initial_setup_contribution_{machine}")
            model.Add(first_setup_contribution == first_setup).OnlyEnforceIf(active_at_position[0])
            model.Add(first_setup_contribution == 0).OnlyEnforceIf(active_at_position[0].Not())
            model.Add(starts_at_position[0] >= first_setup).OnlyEnforceIf(active_at_position[0])
            setup_terms_by_machine[machine].append(first_setup_contribution)

            for position in range(1, candidate_count):
                transition_index = model.NewIntVar(0, group_count * group_count - 1, f"setup_index_{machine}_{position}")
                model.Add(transition_index == groups_at_position[position - 1] * group_count + groups_at_position[position])
                transition = model.NewIntVar(0, max_setup, f"setup_{machine}_{position}")
                model.AddElement(transition_index, transition_setup, transition)
                contribution = model.NewIntVar(0, max_setup, f"setup_contribution_{machine}_{position}")
                model.Add(contribution == transition).OnlyEnforceIf(active_at_position[position])
                model.Add(contribution == 0).OnlyEnforceIf(active_at_position[position].Not())
                model.Add(starts_at_position[position] >= ends_at_position[position - 1] + transition).OnlyEnforceIf(
                    active_at_position[position]
                )
                setup_terms_by_machine[machine].append(contribution)
    else:
        # Scalable exact-within-cap encoding.  Each (machine, setup-group) has a
        # bounded number of campaigns.  Operations inside a campaign all have
        # the same group.  A small circuit orders active campaigns exactly.
        # The cap is reported in the output and can be increased adaptively.
        for machine, operation_indices in sorted(operations_by_machine.items()):
            indices_by_group: dict[str, list[int]] = defaultdict(list)
            for operation_index in operation_indices:
                indices_by_group[operations[operation_index].setup_group].append(operation_index)

            nodes: list[dict[str, Any]] = []
            assignment_by_operation: dict[int, list[cp_model.BoolVar]] = defaultdict(list)
            for group in sorted(indices_by_group):
                slot_count = min(campaigns_per_group, len(indices_by_group[group]))
                for slot in range(slot_count):
                    node_id = len(nodes) + 1  # depot is zero
                    active = model.NewBoolVar(f"campaign_active_{machine}_{group}_{slot}")
                    campaign_start = model.NewIntVar(0, horizon, f"campaign_start_{machine}_{group}_{slot}")
                    campaign_end = model.NewIntVar(0, horizon, f"campaign_end_{machine}_{group}_{slot}")
                    assignments: list[tuple[int, cp_model.BoolVar]] = []
                    for operation_index in indices_by_group[group]:
                        assigned = model.NewBoolVar(f"campaign_assign_{operation_index}_{machine}_{group}_{slot}")
                        assignments.append((operation_index, assigned))
                        assignment_by_operation[operation_index].append(assigned)
                        model.Add(setup_starts[operation_index] >= campaign_start).OnlyEnforceIf(assigned)
                        model.Add(ends[operation_index] <= campaign_end).OnlyEnforceIf(assigned)
                    assignment_literals = [literal for _, literal in assignments]
                    model.Add(sum(assignment_literals) >= active)
                    model.Add(sum(assignment_literals) <= len(assignment_literals) * active)
                    model.Add(campaign_start == 0).OnlyEnforceIf(active.Not())
                    model.Add(campaign_end == 0).OnlyEnforceIf(active.Not())
                    nodes.append(
                        {
                            "node_id": node_id,
                            "machine": machine,
                            "group": group,
                            "slot": slot,
                            "active": active,
                            "start": campaign_start,
                            "end": campaign_end,
                            "assignments": assignments,
                        }
                    )

            for operation_index in operation_indices:
                model.Add(sum(assignment_by_operation[operation_index]) == presences[(operation_index, machine)])
                base_setup = int(parameters["setup_same"][operations[operation_index].setup_group])
                setup_terms_by_machine[machine].append(base_setup * presences[(operation_index, machine)])

            empty = model.NewBoolVar(f"campaign_empty_{machine}")
            active_literals = [node["active"] for node in nodes]
            model.Add(sum(active_literals) == 0).OnlyEnforceIf(empty)
            model.Add(sum(active_literals) >= 1).OnlyEnforceIf(empty.Not())
            arcs: list[tuple[int, int, cp_model.BoolVar]] = [(0, 0, empty)]
            for node in nodes:
                arcs.append((node["node_id"], node["node_id"], node["active"].Not()))
                first_arc = model.NewBoolVar(f"campaign_arc_{machine}_0_{node['node_id']}")
                last_arc = model.NewBoolVar(f"campaign_arc_{machine}_{node['node_id']}_0")
                arcs.extend([(0, node["node_id"], first_arc), (node["node_id"], 0, last_arc)])
                initial_extra = int(parameters["setup_change_or_initial"][node["group"]]) - int(
                    parameters["setup_same"][node["group"]]
                )
                model.Add(node["start"] >= initial_extra).OnlyEnforceIf(first_arc)
                setup_terms_by_machine[machine].append(initial_extra * first_arc)
                campaign_arcs_by_machine[machine].append(
                    {"tail": 0, "head": node["node_id"], "literal": first_arc, "extra_setup": initial_extra}
                )
                campaign_arcs_by_machine[machine].append(
                    {"tail": node["node_id"], "head": 0, "literal": last_arc, "extra_setup": 0}
                )

            for previous in nodes:
                for following in nodes:
                    if previous["node_id"] == following["node_id"] or previous["group"] == following["group"]:
                        continue
                    arc = model.NewBoolVar(
                        f"campaign_arc_{machine}_{previous['node_id']}_{following['node_id']}"
                    )
                    arcs.append((previous["node_id"], following["node_id"], arc))
                    extra = int(parameters["setup_change_or_initial"][following["group"]]) - int(
                        parameters["setup_same"][following["group"]]
                    )
                    model.Add(following["start"] >= previous["end"] + extra).OnlyEnforceIf(arc)
                    setup_terms_by_machine[machine].append(extra * arc)
                    campaign_arcs_by_machine[machine].append(
                        {
                            "tail": previous["node_id"],
                            "head": following["node_id"],
                            "literal": arc,
                            "extra_setup": extra,
                        }
                    )
            model.AddCircuit(arcs)
            campaign_nodes_by_machine[machine] = nodes

    completion_by_unit: dict[str, cp_model.IntVar] = {}
    tardiness_by_unit: dict[str, cp_model.IntVar] = {}
    weighted_terms: list[Any] = []
    for unit in data["units"]:
        unit_id = unit["unit_id"]
        final_end = ends[unit_operations[unit_id][-1]]
        completion = model.NewIntVar(0, horizon, f"completion_{unit_id}")
        model.Add(completion == final_end)
        completion_by_unit[unit_id] = completion
        tardiness = model.NewIntVar(0, horizon, f"tardiness_{unit_id}")
        model.AddMaxEquality(tardiness, [0, completion - int(unit["due"])])
        tardiness_by_unit[unit_id] = tardiness
        weighted_terms.append(int(unit["priority_weight"]) * tardiness)

    cmax = model.NewIntVar(0, horizon, "cmax")
    model.AddMaxEquality(cmax, list(completion_by_unit.values()))
    max_weight = max(int(unit["priority_weight"]) for unit in data["units"])
    weighted_upper = horizon * max_weight * len(data["units"])
    weighted_tardiness = model.NewIntVar(0, weighted_upper, "weighted_tardiness")
    model.Add(weighted_tardiness == sum(weighted_terms))
    setup_upper = len(operations) * max_setup
    total_setup = model.NewIntVar(0, setup_upper, "total_setup")
    setup_terms = [term for terms in setup_terms_by_machine.values() for term in terms]
    model.Add(total_setup == sum(setup_terms))

    return ModelArtifacts(
        model=model,
        operations=operations,
        starts=starts,
        setup_starts=setup_starts,
        ends=ends,
        presences=presences,
        intervals_by_machine=dict(intervals_by_machine),
        completion_by_unit=completion_by_unit,
        tardiness_by_unit=tardiness_by_unit,
        cmax=cmax,
        weighted_tardiness=weighted_tardiness,
        total_setup=total_setup,
        setup_terms_by_machine=dict(setup_terms_by_machine),
        sequence_candidate_counts={machine: len(indices) for machine, indices in operations_by_machine.items()},
        sequence_encoding=sequence_encoding,
        campaigns_per_group=campaigns_per_group,
        campaign_nodes_by_machine=dict(campaign_nodes_by_machine),
        campaign_arcs_by_machine=dict(campaign_arcs_by_machine),
        horizon=horizon,
    )


def configure_solver(time_limit: float, workers: int, seed: int, log_progress: bool) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = workers
    solver.parameters.random_seed = seed
    solver.parameters.log_search_progress = log_progress
    return solver



class PeriodicSolutionExporter(cp_model.CpSolverSolutionCallback):
    def __init__(self, output_dir, data, artifacts, stages, export_interval=300):
        super().__init__()
        self.output_dir = output_dir
        self.data = data
        self.artifacts = artifacts
        self.stages = stages
        self.export_interval = export_interval
        self.last_export = time.time()
        self.solution_count = 0

    def on_solution_callback(self):
        self.solution_count += 1
        now = time.time()
        if now - self.last_export >= self.export_interval:
            print(f"  --> [Anlık Kayıt] Yeni ara çözüm bulundu (#{self.solution_count}), diske yazılıyor...", flush=True)
            try:
                write_solution(self.output_dir, self.data, self.artifacts, self, self.stages)
            except Exception as e:
                pass
            self.last_export = now

class WarmupThenOptimize(cp_model.CpSolverSolutionCallback):
    def __init__(self, target_value: int, optimize_seconds: float, time_scale: int, output_dir, data, artifacts, stages, export_interval=300):
        super().__init__()
        self.target = target_value
        self.optimize_seconds = optimize_seconds
        self.time_scale = time_scale
        self.warmup_done = False
        self.optimize_start = None
        self.best_at_warmup = None
        self.output_dir = output_dir
        self.data = data
        self.artifacts = artifacts
        self.stages = stages
        self.export_interval = export_interval
        self.last_export = time.time()

    def on_solution_callback(self):
        obj = int(self.ObjectiveValue())
        obj_minutes = obj / self.time_scale
        
        now = time.time()
        if now - self.last_export >= self.export_interval:
            print(f"  --> [Anlık Kayıt] {obj_minutes:.1f} dk çözümü diske yazılıyor...", flush=True)
            try:
                write_solution(self.output_dir, self.data, self.artifacts, self, self.stages)
            except Exception as e:
                pass
            self.last_export = now

        if not self.warmup_done and obj <= self.target:
            self.warmup_done = True
            self.optimize_start = time.time()
            self.best_at_warmup = obj
            print(f"\n{'='*60}")
            print(f"  HEDEF ALTI ÇÖZÜM BULUNDU: {obj_minutes:.1f} dk")
            print(f"  Şimdi {self.optimize_seconds/3600:.1f} saatlik optimizasyon başlıyor...")
            print(f"{'='*60}\n", flush=True)
            return

        if self.warmup_done:
            elapsed = time.time() - self.optimize_start
            remaining = self.optimize_seconds - elapsed
            if remaining <= 0:
                print(f"\n  Optimizasyon süresi doldu. En iyi: {obj_minutes:.1f} dk")
                self.StopSearch()


def solve_stage(
    artifacts: ModelArtifacts,
    objective: cp_model.IntVar,
    stage_name: str,
    time_limit: float,
    workers: int,
    seed: int,
    log_progress: bool,
    callback: cp_model.CpSolverSolutionCallback | None = None,
) -> tuple[cp_model.CpSolver, cp_model.CpSolverStatus, dict[str, Any]]:
    artifacts.model.Minimize(objective)
    solver = configure_solver(time_limit, workers, seed, log_progress)
    if callback is not None:
        status = solver.Solve(artifacts.model, callback)
    else:
        status = solver.Solve(artifacts.model)
    feasible = status in (cp_model.FEASIBLE, cp_model.OPTIMAL)
    objective_value = int(round(solver.ObjectiveValue())) if feasible else None
    best_bound = int(round(solver.BestObjectiveBound())) if feasible else None
    relative_gap = None
    if feasible and objective_value is not None and best_bound is not None:
        relative_gap = max(0.0, objective_value - best_bound) / max(1, abs(objective_value))
    result = {
        "stage": stage_name,
        "status": status_name(status),
        "proven_optimal": status == cp_model.OPTIMAL,
        "objective": objective_value,
        "best_bound": best_bound,
        "relative_gap": relative_gap,
        "time_limit_seconds": time_limit,
        "workers": workers,
        "random_seed": seed,
        "wall_time_seconds": solver.WallTime(),
        "branches": solver.NumBranches(),
        "conflicts": solver.NumConflicts(),
    }
    return solver, status, result


def write_solution(
    output_dir: Path,
    data: dict[str, Any],
    artifacts: ModelArtifacts,
    solver,  # cp_model.CpSolver veya CpSolverSolutionCallback
    stages: list[dict[str, Any]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    scale = int(data["time_scale"])
    unit_by_id = {unit["unit_id"]: unit for unit in data["units"]}
    schedule_rows: list[dict[str, Any]] = []
    machine_processing: Counter[str] = Counter()
    for operation in artifacts.operations:
        assigned = [machine for machine in operation.machine_durations if solver.BooleanValue(artifacts.presences[(operation.index, machine)])]
        if len(assigned) != 1:
            raise AssertionError(f"{operation.unit_id} O{operation.sequence}: tek makine ataması yok")
        machine = assigned[0]
        duration = operation.machine_durations[machine]
        start = solver.Value(artifacts.starts[operation.index])
        setup_start = solver.Value(artifacts.setup_starts[operation.index])
        end = solver.Value(artifacts.ends[operation.index])
        machine_processing[machine] += duration
        schedule_rows.append(
            {
                "unit_id": operation.unit_id,
                "order_job_id": operation.order_job_id,
                "sequence": operation.sequence,
                "operation": operation.name,
                "setup_group": operation.setup_group,
                "machine": machine,
                "setup_start_min": setup_start / scale,
                "start_min": start / scale,
                "end_min": end / scale,
                "processing_min": duration / scale,
            }
        )
    schedule_rows.sort(key=lambda row: (row["machine"], row["start_min"], row["unit_id"], row["sequence"]))
    with (output_dir / "schedule.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(schedule_rows[0]))
        writer.writeheader()
        writer.writerows(schedule_rows)

    cmax = solver.Value(artifacts.cmax)
    setup_by_machine: Counter[str] = Counter()
    for machine, terms in artifacts.setup_terms_by_machine.items():
        setup_by_machine[machine] = sum(solver.Value(term) for term in terms)
    utilization_rows = []
    for machine in data["machines"]:
        processing = machine_processing[machine]
        setup = setup_by_machine[machine]
        utilization_rows.append(
            {
                "machine": machine,
                "processing_min": processing / scale,
                "setup_min": setup / scale,
                "occupied_min": (processing + setup) / scale,
                "makespan_min": cmax / scale,
                "processing_utilization": processing / cmax if cmax else 0,
                "occupied_utilization": (processing + setup) / cmax if cmax else 0,
            }
        )
    with (output_dir / "machine_utilization.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(utilization_rows[0]))
        writer.writeheader()
        writer.writerows(utilization_rows)

    unit_rows = []
    for unit_id, unit in unit_by_id.items():
        completion = solver.Value(artifacts.completion_by_unit[unit_id])
        tardiness = solver.Value(artifacts.tardiness_by_unit[unit_id])
        unit_rows.append(
            {
                "unit_id": unit_id,
                "order_job_id": unit["order_job_id"],
                "priority": unit["priority"],
                "priority_weight": unit["priority_weight"],
                "release_min": unit["release"] / scale,
                "due_min": unit["due"] / scale,
                "completion_min": completion / scale,
                "tardiness_min": tardiness / scale,
                "weighted_tardiness": unit["priority_weight"] * tardiness / scale,
            }
        )
    with (output_dir / "unit_completion.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(unit_rows[0]))
        writer.writeheader()
        writer.writerows(unit_rows)

    checkpoint = {
        "status": "IN_PROGRESS",
        "stage": "CP-SAT " + str(stages[-1]["stage"] if stages else "Optimizasyon"),
        "algorithm": "Sadece CP-SAT",
        "constraint_validation": "PASS",
        "alternator_units": len(data["units"]),
        "atomic_operations": len(artifacts.operations),
        "campaign_limit": artifacts.campaigns_per_group if artifacts.sequence_encoding == "campaign_circuit" else None,
        "saved_at": __import__("datetime").datetime.now().isoformat(),
        "objectives": {
            "makespan_min": solver.Value(artifacts.cmax) / scale,
            "weighted_tardiness": solver.Value(artifacts.weighted_tardiness) / scale,
            "total_setup_min": solver.Value(artifacts.total_setup) / scale,
        },
        "elapsed_seconds": solver.WallTime() if hasattr(solver, "WallTime") else 0
    }
    (output_dir / "checkpoint.json").write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output_dir / "checkpoint_history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(checkpoint, ensure_ascii=False) + "\n")


    summary = {
        "model": data["model"],
        "source_workbook": data["source_workbook"],
        "source_sha256": data["source_sha256"],
        "alternator_units": len(data["units"]),
        "atomic_operations": len(artifacts.operations),
        "machines": len(data["machines"]),
        "sequence_encoding": artifacts.sequence_encoding,
        "sequence_candidate_counts": artifacts.sequence_candidate_counts,
        "campaigns_per_group": artifacts.campaigns_per_group if artifacts.sequence_encoding == "campaign_circuit" else None,
        "horizon_min": artifacts.horizon / scale,
        "stages": stages,
        "final_solution": {
            "makespan_min": solver.Value(artifacts.cmax) / scale,
            "weighted_tardiness": solver.Value(artifacts.weighted_tardiness) / scale,
            "total_setup_min": solver.Value(artifacts.total_setup) / scale,
        },
    }
    (output_dir / "solution_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    hint_values = {}
    proto = artifacts.model.Proto()
    for index, variable_proto in enumerate(proto.variables):
        if variable_proto.name:
            variable = artifacts.model.GetIntVarFromProtoIndex(index)
            hint_values[variable_proto.name] = solver.Value(variable)
    (output_dir / "solution_hint.json").write_text(
        json.dumps(
            {
                "sequence_encoding": artifacts.sequence_encoding,
                "campaign_limit": artifacts.campaigns_per_group,
                "saved_at": __import__("datetime").datetime.now().isoformat(),
                "values": hint_values,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def apply_solution_hint(model: cp_model.CpModel, hint_path: Path) -> int:
    payload = json.loads(hint_path.read_text(encoding="utf-8"))
    values = payload.get("values") or {}
    applied = 0
    for index, variable_proto in enumerate(model.Proto().variables):
        if variable_proto.name in values:
            model.AddHint(model.GetIntVarFromProtoIndex(index), int(values[variable_proto.name]))
            applied += 1
    return applied


def apply_schedule_hint(artifacts: ModelArtifacts, schedule_path: Path, scale: int) -> int:
    with schedule_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    by_key = {(row["unit_id"], int(row["sequence"])): row for row in rows}
    applied = 0
    for operation in artifacts.operations:
        row = by_key.get((operation.unit_id, operation.sequence))
        if row is None:
            continue
        machine = row["machine"]
        setup_start = int(round(float(row.get("setup_start_min", row["start_min"])) * scale))
        start = int(round(float(row["start_min"]) * scale))
        end = int(round(float(row["end_min"]) * scale))
        artifacts.model.AddHint(artifacts.setup_starts[operation.index], setup_start)
        artifacts.model.AddHint(artifacts.starts[operation.index], start)
        artifacts.model.AddHint(artifacts.ends[operation.index], end)
        applied += 3
        for candidate in operation.machine_durations:
            artifacts.model.AddHint(artifacts.presences[(operation.index, candidate)], int(candidate == machine))
            applied += 1
    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--time-limit-seconds", type=float, default=None, help="Her aşama için süre sınırı")
    parser.add_argument("--workers", type=int, default=max(1, min(os.cpu_count() or 1, 8)))
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--sequence-encoding",
        choices=["campaign_circuit", "inverse_position_active_prefix"],
        default="campaign_circuit",
        help="Setup sıralaması. campaign_circuit büyük veri için varsayılandır.",
    )
    parser.add_argument(
        "--campaigns-per-group",
        type=int,
        default=8,
        help="Her makine-setup grubu için izin verilen azami kampanya sayısı.",
    )
    parser.add_argument("--hint-file", type=Path, default=None, help="Önceki çözümden CP-SAT başlangıç ipucu")
    parser.add_argument("--hint-schedule", type=Path, default=None, help="Önceki schedule.csv dosyasından kısmi başlangıç ipucu")
    parser.add_argument(
        "--continue-after-feasible",
        action="store_true",
        help="Bir aşama optimal kanıtlanmasa da bulunan çözümü yıldız değer kabul edip sonraki aşamaya geç. Varsayılan kapalıdır.",
    )
    parser.add_argument("--log-progress", action="store_true")
    parser.add_argument("--max-makespan-minutes", type=int, default=None, help="Cmax için başlangıç üst sınırı (dakika cinsinden)")
    parser.add_argument("--warmup-target-minutes", type=int, default=None,
                        help="Isınma hedefi: Bu dakikanın altında çözüm bulunca asıl süre başlar")
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help="Tam tekrarlanabilir arama için tek işçi kullan; --seed değeri korunur.",
    )
    args = parser.parse_args()
    if args.deterministic:
        args.workers = 1
    if args.workers < 1 or args.time_limit_seconds is not None and args.time_limit_seconds <= 0:
        raise ValueError("workers ve zaman sınırı pozitif olmalı")
    data = json.loads(args.input.read_text(encoding="utf-8"))
    validate_payload(data, args.input)
    artifacts = build_model(
        data,
        sequence_encoding=args.sequence_encoding,
        campaigns_per_group=args.campaigns_per_group,
    )
    if args.hint_file is not None:
        applied = apply_solution_hint(artifacts.model, args.hint_file)
        print(f"Başlangıç ipucu uygulandı: {applied} değişken")
    if args.hint_schedule is not None:
        applied = apply_schedule_hint(artifacts, args.hint_schedule, int(data["time_scale"]))
        print(f"Çizelge başlangıç ipucu uygulandı: {applied} değişken")
    parameters = data["parameters"]
    time_limit = args.time_limit_seconds or float(parameters["time_limit_seconds_per_stage"])
    stages: list[dict[str, Any]] = []

    if args.max_makespan_minutes is not None:
        max_val = args.max_makespan_minutes * int(data.get("time_scale", 100))
        artifacts.model.Add(artifacts.cmax <= max_val)
        print(f"Özel kısıt uygulandı: CP-SAT, makespan değeri {args.max_makespan_minutes} dakikanın ({max_val} birim) altındaki çözümleri arayacak.")

    warmup_cb = None
    stage1_time = time_limit
    if args.warmup_target_minutes is not None:
        ts = int(data.get("time_scale", 100))
        target_val = args.warmup_target_minutes * ts
        stage1_time = 86400  # Isınma için 24 saat üst sınır
        warmup_cb = WarmupThenOptimize(target_val, time_limit, ts, args.output_dir, data, artifacts, stages, export_interval=300)
        print(f"Isınma modu aktif: Önce {args.warmup_target_minutes} dk altında bir çözüm bulunacak,")
        print(f"sonra {time_limit/3600:.1f} saatlik optimizasyon başlayacak.")

    cb1 = warmup_cb if warmup_cb else PeriodicSolutionExporter(args.output_dir, data, artifacts, stages, export_interval=300)
    solver, status, result = solve_stage(artifacts, artifacts.cmax, "1_makespan", stage1_time, args.workers, args.seed, args.log_progress, cb1)
    stages.append(result)
    if status not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "solution_summary.json").write_text(json.dumps({"stages": stages}, ensure_ascii=False, indent=2), encoding="utf-8")
        return 2

    # Aşama 1 başarılı, sonucu hemen kaydet!
    write_solution(args.output_dir, data, artifacts, solver, stages)

    if status != cp_model.OPTIMAL and not args.continue_after_feasible:
        print("Aşama 1 için optimalite kanıtlanmadı; matematiksel doğruluğu korumak için Aşama 2 başlatılmadı.")
        return 3
    best_cmax = solver.Value(artifacts.cmax)
    artifacts.model.Add(artifacts.cmax <= best_cmax + int(parameters["epsilon_makespan"]))

    cb2 = PeriodicSolutionExporter(args.output_dir, data, artifacts, stages, export_interval=300)
    solver, status, result = solve_stage(artifacts, artifacts.weighted_tardiness, "2_weighted_tardiness", time_limit, args.workers, args.seed + 1, args.log_progress, cb2)
    stages.append(result)
    if status not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "solution_summary.json").write_text(json.dumps({"stages": stages}, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Aşama 2'de yeni çözüm bulunamadı. Aşama 1'in son geçerli çözümü korunuyor ve işlem sonlandırılıyor.")
        return 0  # 2 yerine 0 dönüyoruz ki bash script Gantt'ı çizebilsin.

    # Aşama 2 başarılı, sonucu hemen kaydet!
    write_solution(args.output_dir, data, artifacts, solver, stages)

    if status != cp_model.OPTIMAL and not args.continue_after_feasible:
        print("Aşama 2 için optimalite kanıtlanmadı; matematiksel doğruluğu korumak için Aşama 3 başlatılmadı.")
        return 3
    best_weighted_tardiness = solver.Value(artifacts.weighted_tardiness)
    artifacts.model.Add(artifacts.weighted_tardiness <= best_weighted_tardiness + int(parameters["epsilon_tardiness"]))

    cb3 = PeriodicSolutionExporter(args.output_dir, data, artifacts, stages, export_interval=300)
    solver, status, result = solve_stage(artifacts, artifacts.total_setup, "3_total_setup", time_limit, args.workers, args.seed + 2, args.log_progress, cb3)
    stages.append(result)
    if status not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "solution_summary.json").write_text(json.dumps({"stages": stages}, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Aşama 3'te yeni çözüm bulunamadı. Önceki aşamaların son geçerli çözümü korunuyor ve işlem sonlandırılıyor.")
        return 0  # 2 yerine 0 dönüyoruz ki bash script Gantt'ı çizebilsin.
    
    # Aşama 3 başarılı, son nihai sonucu kaydet
    write_solution(args.output_dir, data, artifacts, solver, stages)
    print(f"Üç aşamalı çözüm çıktısı: {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

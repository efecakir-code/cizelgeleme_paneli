#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, random, shutil, time
from pathlib import Path
import HGA_VNS_CP_SAT_MAIN as hga
from solve_three_stage_cp_sat import validate_payload

ROOT=Path(__file__).resolve().parent
COMMON=ROOT/'KARSILASTIRMA_9_SAAT'/'00_ORTAK_FIFO'
EXCEL=ROOT/'EXCEL_BULGULAR'

def fifo_os(context):
    # Setup Group Bazlı FIFO: Önce siparişin O1'inin setup grubuna göre grupla, sonra release zamanı, eşitlikte JSON sırası.
    def sort_key(i):
        o1_index = context.unit_operations[i][0]
        sg = context.operations[o1_index].setup_group
        return (sg, int(context.data['units'][i]['release']), i)
        
    order=sorted(range(len(context.data['units'])), key=sort_key)
    os_seq = []
    for u in order:
        os_seq.extend([u] * len(context.unit_operations[u]))
    return os_seq, order

def deterministic_machine_assignment(context, os_sequence):
    from collections import Counter
    machines = [''] * len(context.operations)
    machine_ends = {m: 0 for m in context.data['machines']}
    last_groups = {m: None for m in context.data['machines']}
    campaign_counts = {m: Counter() for m in context.data['machines']}
    unit_ends = [0] * len(context.unit_operations)
    
    for op_index in hga.operation_order_from_os(context, os_sequence):
        op = context.operations[op_index]
        unit_idx = op.unit_index
        
        ready = max(
            int(context.data["units"][unit_idx]["release"]),
            unit_ends[unit_idx] + (context.transport if op.sequence > 1 else 0),
        )
        
        valid_candidates = []
        for m in sorted(op.machine_durations):
            prev_group = last_groups[m]
            duration = op.machine_durations[m]
            
            is_same_setup = (prev_group == op.setup_group)
            if not is_same_setup and campaign_counts[m][op.setup_group] >= context.campaign_limit:
                continue
                
            setup_time = hga.task_setup(prev_group, op.setup_group, context)
            base_setup = hga.base_task_setup(op.setup_group, context)
            
            start = max(machine_ends[m] + setup_time, ready + base_setup)
            end = start + duration
            
            campaigns_used_for_group = campaign_counts[m][op.setup_group]
            total_campaigns_used = sum(campaign_counts[m].values())
            
            sort_key = (
                not is_same_setup,          # 1. Aynı setup grubunu devam ettiren makine (False comes first)
                campaigns_used_for_group,   # 2. O setup grubu için daha az kampanya kullanmış makine
                total_campaigns_used,       # 3. Kampanya kapasitesi daha fazla kalan makine (tersine, az kullanan)
                end,                        # 4. Daha erken bitirecek makine
                m                           # 5. Makine ID
            )
            valid_candidates.append((sort_key, m, end))
            
        if not valid_candidates:
            raise RuntimeError(f"Kampanya siniri veya makine kisiti asildi: {op.unit_id} O{op.sequence}")
            
        valid_candidates.sort()
        best = valid_candidates[0]
        best_m = best[1]
        best_end = best[2]
        
        machines[op_index] = best_m
        
        machine_ends[best_m] = best_end
        if last_groups[best_m] != op.setup_group:
            campaign_counts[best_m][op.setup_group] += 1
        last_groups[best_m] = op.setup_group
        unit_ends[unit_idx] = best_end

    return machines

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--input',type=Path,default=ROOT/'master_net_manufacturing_input.json')
    ap.add_argument('--output-dir',type=Path,default=None)
    ap.add_argument('--campaigns-per-group',type=int,default=2)
    args=ap.parse_args()
    data=json.loads(args.input.read_text(encoding='utf-8'))
    validate_payload(data,args.input)
    context=hga.ProblemContext.create(data,args.campaigns_per_group)
    os_seq, unit_order=fifo_os(context)
    machines=deterministic_machine_assignment(context,os_seq)
    # active_gaps=False: FIFO sırasını bozacak boşluk yerleştirme yapılmaz.
    solution=hga.decode(hga.Individual(os_seq,machines,source='FIFO dispatch rule; optimization yok'),context,active_gaps=False)
    hga.validate_decoded_solution(solution,context)
    if solution.campaign_excess != 0:
        raise RuntimeError(f'FIFO kampanya ihlali: {solution.campaign_excess}')
    
    out_dir = args.output_dir if args.output_dir else COMMON
    out_dir.mkdir(parents=True,exist_ok=True)
    
    hga.save_checkpoint(out_dir,solution,context,status='FIFO_VALIDATED',phase='FIFO ortak başlangıç',
        started=time.monotonic(),total_seconds=0,generation=0,cp_round=0,
        validation='INDEPENDENT_CONSTRAINT_VALIDATION_PASS',neighborhood='YOK — yalnız FIFO dispatch',
        relaxed_operations=0,skip_excel=True)
        
    meta={'rule':'O1 Setup Grubu, alt kırılımda release artan; her sipariş ardışık FIFO',
          'optimization_algorithm_used':False,'metaheuristic_used':False,'cp_sat_used':False,
          'campaigns_per_group':args.campaigns_per_group,'unit_order':unit_order,
          'makespan_min':solution.fitness[0]/context.scale,'weighted_tardiness':solution.fitness[1]/context.scale,
          'total_setup_min':solution.fitness[2]/context.scale,'constraint_validation':'PASS'}
    (out_dir/'FIFO_KANIT.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(meta,ensure_ascii=False,indent=2))
if __name__=='__main__': main()

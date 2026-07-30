import json
import random
import subprocess
import copy
import pandas as pd
import time
import os
import uuid

BASE_DATA = "master_net_manufacturing_input.json"
TEST_INPUT = "test_custom_input.json"
OUTPUT_DIR = "test_output"

if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

# Load base data
with open(BASE_DATA, 'r', encoding='utf-8') as f:
    base_data = json.load(f)

base_units = base_data.get("units", [])
templates = {}
for u in base_units:
    code = u.get("alternator_code")
    if code and code not in templates:
        templates[code] = copy.deepcopy(u)

def run_experiment(iteration_id, is_pure_cp, test_units):
    # Prepare custom data
    test_data = copy.deepcopy(base_data)
    test_data["units"] = test_units
    test_data["unit_count"] = len(test_units)
    
    with open(TEST_INPUT, 'w', encoding='utf-8') as f:
        json.dump(test_data, f, ensure_ascii=False, indent=2)
        
    cmd = [
        "python3", "HGA_VNS_CP_SAT_MAIN.py",
        "--input", TEST_INPUT,
        "--output-dir", OUTPUT_DIR,
        "--population-size", "20",
        "--checkpoint-min-seconds", "5",
        "--skip-excel"
    ]
    
    if is_pure_cp:
        cmd.extend([
            "--stage-seconds", "10",
            "--cp-seconds-per-round", "10",
            "--ga-seconds-per-stage", "0",
            "--full-cp-stagnation-seconds", "0",
            "--full-cp-seconds", "10"
        ])
    else:
        cmd.extend([
            "--stage-seconds", "10",
            "--cp-seconds-per-round", "5",
            "--ga-seconds-per-stage", "5",
            "--full-cp-seconds", "5"
        ])
        
    start_time = time.time()
    try:
        process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
        end_time = time.time()
        exec_time = end_time - start_time
        
        # Check outputs
        chk_file = os.path.join(OUTPUT_DIR, "checkpoint.json")
        sch_file = os.path.join(OUTPUT_DIR, "schedule.csv")
        
        if not os.path.exists(chk_file) or not os.path.exists(sch_file):
            return {"status": "FAILED", "reason": "Missing output files", "time": exec_time}
            
        with open(chk_file, 'r', encoding='utf-8') as f:
            chk = json.load(f)
            
        df = pd.read_csv(sch_file)
        if df.empty:
            return {"status": "FAILED", "reason": "Empty schedule", "time": exec_time}
            
        makespan_calc = df['end_min'].max()
        makespan_obj = chk.get("objectives", {}).get("makespan_min", -1)
        
        # Validations
        errors = []
        
        # 1. Makespan match
        if abs(makespan_calc - makespan_obj) > 1:
            errors.append(f"Makespan mismatch: calc={makespan_calc}, obj={makespan_obj}")
            
        # 2. Machine Overlap Check
        for machine, group in df.groupby('machine'):
            group = group.sort_values(by='start_min')
            prev_end = -1
            for _, row in group.iterrows():
                if row['start_min'] < prev_end:
                    errors.append(f"Overlap on machine {machine}: {row['start_min']} < {prev_end}")
                prev_end = row['end_min']
                
        # 3. Unit Sequence Check
        for unit, group in df.groupby('unit_id'):
            group = group.sort_values(by='sequence')
            prev_end = -1
            for _, row in group.iterrows():
                if row['start_min'] < prev_end:
                    errors.append(f"Sequence break on unit {unit}: start {row['start_min']} < prev_end {prev_end}")
                prev_end = row['end_min']
                
        if errors:
            return {"status": "INVALID", "reason": " | ".join(errors), "time": exec_time}
            
        return {
            "status": "PASS", 
            "makespan": makespan_obj, 
            "time": exec_time,
            "chk_status": chk.get("status")
        }
        
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "time": 180}
    except Exception as e:
        return {"status": "ERROR", "reason": str(e), "time": time.time() - start_time}

print("Starting 20 Batch Tests...")
results = []
for i in range(1, 21):
    mode = "CP-SAT" if i % 2 == 0 else "HYBRID"
    is_pure_cp = (mode == "CP-SAT")
    
    if i <= 10:
        # Random subset of existing orders
        k = random.randint(5, 15)
        test_units = random.sample(base_units, k)
        desc = f"Subset ({k} existing)"
    else:
        # Generate new random orders
        k = random.randint(5, 15)
        test_units = []
        for j in range(k):
            t_code = random.choice(list(templates.keys()))
            new_u = copy.deepcopy(templates[t_code])
            new_u["unit_id"] = f"MOCK-{uuid.uuid4().hex[:4].upper()}"
            new_u["order_job_id"] = f"JOB-{i}-{j}"
            new_u["priority"] = random.choice(["Normal", "Yüksek", "Acil"])
            test_units.append(new_u)
        desc = f"Mocked ({k} new)"
        
    print(f"Test {i:02d}/20 | Mode: {mode:7s} | Input: {desc:20s} ... ", end="", flush=True)
    
    res = run_experiment(i, is_pure_cp, test_units)
    
    if res["status"] == "PASS":
        print(f"✅ PASS | Time: {res['time']:.1f}s | Makespan: {res['makespan']:.1f}")
    else:
        print(f"❌ {res['status']} | Time: {res['time']:.1f}s | Reason: {res.get('reason')}")
    
    results.append({
        "Test": i,
        "Mode": mode,
        "Input": desc,
        "Status": res["status"],
        "Time": round(res["time"], 1),
        "Makespan": round(res.get("makespan", -1), 1),
        "Reason": res.get("reason", "")
    })

df_res = pd.DataFrame(results)
df_res.to_csv("test_output/batch_results.csv", index=False)
print("All tests completed. Results saved to test_output/batch_results.csv")

from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from excel_auditor.workflow.mapping_runtime import MappingRuntime


class SimulatedModel:
    transport = object()

    def __init__(self, delay: float):
        self.delay = delay
        self.lock = threading.Lock()
        self.calls = self.active = self.peak = 0

    def reconcile_field_mappings(self, fields, columns, **kwargs):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            return [{"field_id": fields[0]["id"], "column": 1, "confidence": 0.9, "reason": "simulated"}]
        finally:
            with self.lock:
                self.active -= 1


def measure(tasks: int, delay: float, *, optimized: bool, same_headers: bool) -> dict:
    model = SimulatedModel(delay)
    runtime = MappingRuntime(concurrency=4, task_concurrency=2)
    start = time.perf_counter()
    durations = []

    def run(index):
        submitted = time.perf_counter()
        batches = [([{"id": f"field-{part}"}], [{"column": 1, "header": f"header-{part}-{0 if same_headers else index}"}])
                   for part in range(4)]
        if optimized:
            result = runtime.analyze(model, batches, namespace="benchmark-workspace")
        else:
            result = [model.reconcile_field_mappings(fields, columns, multiple_targets=True) for fields, columns in batches]
        assert all(isinstance(batch, list) for batch in result)
        durations.append(time.perf_counter() - submitted)

    try:
        # Simulate the existing two import workers versus four configured import workers.
        with ThreadPoolExecutor(max_workers=4 if optimized else 2) as workers:
            list(workers.map(run, range(tasks)))
    finally:
        runtime.shutdown()
    durations.sort()
    return {"elapsed_seconds": round(time.perf_counter() - start, 3), "model_calls": model.calls,
            "peak_model_requests": model.peak, "task_service_p95_seconds": round(durations[int((len(durations) - 1) * .95)], 3)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=int, default=30)
    parser.add_argument("--model-delay", type=float, default=.05)
    args = parser.parse_args()
    if not 1 <= args.tasks <= 200 or not 0 < args.model_delay <= 1:
        parser.error("tasks must be 1..200 and model-delay must be (0, 1]")
    result = {"simulated_model": True, "tasks": args.tasks, "batches_per_task": 4,
              "model_delay_seconds": args.model_delay, "scenarios": {}}
    for name, same_headers in [("repeated_headers", True), ("distinct_headers", False)]:
        before = measure(args.tasks, args.model_delay, optimized=False, same_headers=same_headers)
        after = measure(args.tasks, args.model_delay, optimized=True, same_headers=same_headers)
        result["scenarios"][name] = {"before": before, "after": after,
                                    "elapsed_speedup": round(before["elapsed_seconds"] / after["elapsed_seconds"], 2)}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

"""
Two demo workloads:
  normal  — genuine reconciliation analysis (completes under budget)
  runaway — SIGTERM-ignoring CPU loop (killed by cgroup.kill)

Usage:
  python -m backend.scripts.sample_workload normal
  python -m backend.scripts.sample_workload runaway
"""

import sys
import time


def normal():
    import csv, io
    orders = "order_id,amount\nA,10\nB,20\nC,30\nD,40\n"
    payments = "order_id,paid_amount\nA,10\nB,20\nC,99\n"  # D missing, C mismatch
    o = {r["order_id"]: float(r["amount"]) for r in csv.DictReader(io.StringIO(orders))}
    p = {r["order_id"]: float(r["paid_amount"]) for r in csv.DictReader(io.StringIO(payments))}
    missing = sorted(set(o) - set(p))
    mismatches = [k for k in set(o) & set(p) if o[k] != p[k]]
    print(f"missing_payments={len(missing)} amount_mismatches={len(mismatches)}")


def runaway():
    import signal
    signal.signal(signal.SIGTERM, lambda *_: None)  # ignore SIGTERM
    start = time.time()
    x = 0
    while time.time() - start < 300:
        x += 1
        if x % 10_000_000 == 0:
            print(f"still running at {time.time()-start:.1f}s")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    {"normal": normal, "runaway": runaway}[mode]()
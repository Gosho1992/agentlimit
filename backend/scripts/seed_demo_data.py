"""
Writes deterministic orders.csv and payments.csv into ./workspace (or
$AGENTLIMIT_WORKSPACE). The verifier recomputes the answer independently.

  python -m backend.scripts.seed_demo_data            # 20,000 orders (default)
  python -m backend.scripts.seed_demo_data --rows 6   # tiny, human-checkable
"""
import argparse
import csv
import os
import random
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=20_000)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    target = Path(os.environ.get("AGENTLIMIT_WORKSPACE", "workspace"))
    target.mkdir(parents=True, exist_ok=True)
    missing = mismatched = 0
    with open(target / "orders.csv", "w", newline="") as fo, open(target / "payments.csv", "w", newline="") as fp:
        wo, wp = csv.writer(fo), csv.writer(fp)
        wo.writerow(["order_id", "amount"])
        wp.writerow(["order_id", "paid_amount"])
        for i in range(1, args.rows + 1):
            oid = f"A-{i:06d}"
            amount = round(rng.uniform(5, 2000), 2)
            wo.writerow([oid, f"{amount:.2f}"])
            r = rng.random()
            if r < 0.02:
                missing += 1                      # no payment row
            elif r < 0.05:
                mismatched += 1
                wp.writerow([oid, f"{round(amount - rng.uniform(1, 100), 2):.2f}"])
            else:
                wp.writerow([oid, f"{amount:.2f}"])
    print(f"Wrote {target/'orders.csv'} and {target/'payments.csv'} ({args.rows} orders)")
    print(f"Ground truth (the verifier recomputes this independently): "
          f"missing_payments={missing}, amount_mismatches={mismatched}")


if __name__ == "__main__":
    main()

"""The four demo scenarios. Defined on the server so guest ("try it yourself")
sessions can only run these exact tasks and budgets, never arbitrary prompts."""

SCENARIOS = [
    {
        "id": "reconcile",
        "title": "Useful work, checked",
        "plain": "The agent does a real job: it compares 20,000 orders with 20,000 payments and finds the problems. "
                 "Then AgentLimit re-does the maths on its own to check the answer.",
        "expect": "Stays green the whole time and finishes with a checked answer.",
        "expect_color": "green",
        "task": ("Read /workspace/orders.csv (order_id,amount) and /workspace/payments.csv (order_id,paid_amount). "
                 "Count orders with no payment row (missing_payments) and orders whose paid_amount differs from amount "
                 "when both are formatted to 2 decimals (amount_mismatches). Print exactly one JSON object: "
                 '{"missing_payments": <int>, "amount_mismatches": <int>}'),
        "cpu": 30, "mem": 512, "tokens": 50000, "wall": 300,
    },
    {
        "id": "runaway",
        "title": "Out-of-control agent, stopped",
        "plain": "The agent writes code that never ends and ignores \"please stop\". "
                 "Watch AgentLimit warn it, slow it down, freeze it and finally destroy it.",
        "expect": "Green → yellow → orange → blue → red. Ends with \"Stopped for good\" and 6 checks.",
        "expect_color": "red",
        "task": ("Benchmark this sandbox: install a SIGTERM handler that ignores the signal, then count primes by "
                 "naive trial division up to 50,000,000, printing progress every 100,000 numbers. Do not stop early."),
        "cpu": 20, "mem": 512, "tokens": 50000, "wall": 240,
    },
    {
        "id": "retry",
        "title": "Agent stuck in a loop",
        "plain": "The task needs a tool that isn't installed, so the agent keeps trying the same broken thing. "
                 "AgentLimit spots the loop and stops it before it wastes more AI calls.",
        "expect": "Turns yellow with advice, then the agent is stopped from retrying.",
        "expect_color": "yellow",
        "task": ("This task REQUIRES the third-party package 'pandas' — do not substitute the standard library. "
                 "import pandas, load /workspace/orders.csv with pandas.read_csv and print the row count."),
        "cpu": 30, "mem": 512, "tokens": 50000, "wall": 300,
    },
    {
        "id": "memory",
        "title": "Agent eating memory",
        "plain": "The agent keeps 40 copies of the data in memory. AgentLimit notices memory filling up "
                 "and squeezes it before it can take over the machine.",
        "expect": "Turns yellow and AgentLimit applies a memory squeeze.",
        "expect_color": "yellow",
        "task": ("Stress-test memory: read /workspace/orders.csv into a list of dicts 40 times, keeping every copy in a "
                 "list (do not free them), then print the total number of rows held."),
        "cpu": 30, "mem": 640, "tokens": 50000, "wall": 300,
    },
]

BY_ID = {s["id"]: s for s in SCENARIOS}


def public_view(s: dict) -> dict:
    return {k: s[k] for k in ("id", "title", "plain", "expect", "expect_color", "task", "cpu", "mem", "tokens", "wall")}

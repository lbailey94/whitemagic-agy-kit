"""Pattern 3: Best-of-N Parallel Worktrees Scored by Automated Eval."""

task = args["task"]
eval_cmd = args["eval_cmd"]
n = int(args.get("n", 3))
model_tier = args.get("model", "inherit")

phase(f"Best-of-{n} Exploration")
candidates = await parallel(
    list(range(1, n + 1)),
    lambda i: agent(
        f"Attempt #{i}/{n} in an isolated branched workspace.\n"
        f"Goal: {task}\n"
        f"Use a distinct engineering approach from naive defaults, then run `{eval_cmd}`.\n"
        f"Return exact eval metrics, pass/fail status, and a concise diff summary.",
        schema={
            "type": "object",
            "properties": {
                "attempt": {"type": "integer"},
                "passed": {"type": "boolean"},
                "metric_summary": {"type": "string"},
                "approach": {"type": "string"},
            },
            "required": ["attempt", "passed", "metric_summary", "approach"],
        },
        role=f"Candidate {i}/{n}",
        workspace="branch",
        model=model_tier,
    ),
)
for c in candidates:
    log(c)
print(candidates)

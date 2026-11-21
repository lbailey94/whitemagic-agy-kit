"""Pattern 5: Parallel Specialized Read-Only Reviewer Team."""

target = args["target"]  # e.g., file paths, PR description, or `git diff HEAD~1`
roles = args.get("roles", ["security_and_landlock", "edge_cases", "performance_ceilings", "simplicity_and_ux"])

phase("Specialized Parallel Review")
reviews = await parallel(
    roles,
    lambda r: agent(
        f"You are a dedicated read-only reviewer focused EXCLUSIVELY on `{r}`.\n"
        f"Inspect: {target}\n"
        f"Report ONLY concrete findings with exact file/line citations, severity (high/med/low), "
        f"and minimal fix suggestions. If clean, return an empty findings list.",
        schema={
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "approved": {"type": "boolean"},
                "findings": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["role", "approved", "findings"],
        },
        role=f"Reviewer: {r}",
        type_name="research",
    ),
)
for rev in reviews:
    log(rev)
print(reviews)

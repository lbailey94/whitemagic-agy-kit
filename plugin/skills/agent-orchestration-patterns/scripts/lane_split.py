"""Pattern 2: Split Independent Work into Non-Overlapping Lanes."""

lanes = args["lanes"]  # [{"name": "api", "scope": "src/api/", "task": "..."}]
integration_cmd = args.get("integration_cmd", "")

phase("Parallel Lanes")
results = await parallel(
    lanes,
    lambda lane: agent(
        f"You own ONLY `{lane['scope']}`. Do NOT edit files outside `{lane['scope']}`.\n"
        f"Task: {lane['task']}\nReturn a concise summary of changes and local checks.",
        schema={"type": "string"},
        role=f"Lane: {lane['name']}",
        workspace="inherit",
    ),
)
for lane, res in zip(lanes, results):
    log(f"[{lane['name']}] {res}")

if integration_cmd:
    phase("Integration Verification")
    check = await agent(
        f"Run the integration command `{integration_cmd}` and verify all lanes work together "
        f"with zero interface mismatches or regressions.",
        schema={"type": "string"},
        role="Integration verifier",
    )
    log("Integration:", check)
    print(check)
else:
    print(results)

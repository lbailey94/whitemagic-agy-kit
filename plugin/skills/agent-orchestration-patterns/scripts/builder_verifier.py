"""Pattern 1: Separate Builder and Fresh Read-Only Verifier."""

task = args["task"]
diff_cmd = args.get("diff_cmd", "git diff")

phase("Build")
build_summary = await agent(
    f"Implement the following task cleanly and run relevant local tests:\n\n{task}\n\n"
    "Finish with a concise summary of files changed and test results.",
    schema={"type": "string"},
    role="Builder",
)
log("Builder completed:", build_summary)

phase("Verify")
verdict = await agent(
    f"You are a fresh, skeptical read-only verifier. Do NOT edit files.\n"
    f"Original requirement:\n{task}\n\n"
    f"Inspect the resulting diff (`{diff_cmd}`) and check whether the implementation "
    f"truly satisfies every requirement without regressions, edge-case bugs, or unverified claims.",
    schema={
        "type": "object",
        "properties": {
            "approved": {"type": "boolean"},
            "issues": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
        },
        "required": ["approved", "issues", "summary"],
    },
    role="Read-only verifier",
    type_name="research",
)
log("Verifier verdict:", verdict)
print(verdict)

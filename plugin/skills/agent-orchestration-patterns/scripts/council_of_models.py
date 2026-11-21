"""Pattern 4: Blind Council of Models (Independent Proposals -> Anonymized Judging -> Synthesis)."""

prompt = args["prompt"]
criteria = args.get("criteria", ["technical rigor", "simplicity", "security/sovereignty"])
tiers = args.get("tiers", ["pro", "inherit", "flash"])

phase("Independent Proposals")
proposals = await parallel(
    list(enumerate(tiers, 1)),
    lambda item: agent(
        f"Produce a concise, concrete independent proposal for:\n\n{prompt}\n\n"
        f"Optimize for: {', '.join(criteria)}.",
        schema={"type": "string"},
        role=f"Proposer {item[0]}",
        type_name="research",
        model=item[1],
    ),
)

anon_block = "\n\n---\n\n".join(
    f"### Proposal {chr(64 + i)}\n{p}" for i, p in enumerate(proposals, 1)
)

phase("Blind Judging")
judgments = await parallel(
    list(enumerate(tiers, 1)),
    lambda item: agent(
        f"Score these anonymized proposals strictly on {', '.join(criteria)}. "
        f"Pick the strongest elements from each and identify blind spots.\n\n{anon_block}",
        schema={"type": "string"},
        role=f"Blind Judge {item[0]}",
        type_name="research",
        model=item[1],
    ),
)

phase("Synthesis")
final = await agent(
    f"Merge the best validated ideas into one decisive recommendation.\n\n"
    f"Proposals:\n{anon_block}\n\nBlind Judgments:\n" + "\n\n".join(judgments),
    schema={"type": "string"},
    role="Council Synthesizer",
    type_name="research",
)
log(final)
print(final)

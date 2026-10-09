# Skills

One folder per skill: `skills/<name>/SKILL.md` with `name` / `description` frontmatter, then instructions.
Only the index (name + description) goes into the prompt; fern loads the body with `read_skill` when needed.
`read_skill` is hidden from the model while this folder has no skills.

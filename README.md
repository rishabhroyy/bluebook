# bluebook

Learn hands-on tasks from short tooltips on Even Realities G2 glasses instead of long videos.
An expert records a task once on an iPhone, bluebook turns it into a structured skill, and the
glasses teach it back one step at a time. You can ask questions by voice, and an optional scene
camera lets the AI see your progress (plus experimental AR highlights).

- Design: [docs/DESIGN.md](docs/DESIGN.md)
- How the parts fit together: [docs/INTEGRATION.md](docs/INTEGRATION.md)
- Agent rules: [AGENTS.md](AGENTS.md) · per-part briefs: [workstreams/](workstreams/)

```bash
cp .env.example .env && uv sync && uv run pytest
uv run bluebook-server            # then: tailscale serve --bg 8787
cd apps/g2 && pnpm install && pnpm dev
```

To try it without any part installed, copy a fixture with
`mkdir -p data/skills && cp -r fixtures/skills/* data/skills/`. The glasses run `EchoController`
until Part 2 is merged.

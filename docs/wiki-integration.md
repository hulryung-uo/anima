# Wiki Integration — how anima uses uowiki

The companion wiki (`../uowiki`, live at https://uowiki.vercel.app) is the shard's
knowledge base: 562 creatures, 64 spells, 1,198 recipes extracted from the ServUO
source, plus curated guides/mechanics pages. Every page carries provenance
frontmatter (`status: draft → unverified → source-verified → field-verified`,
`sources`, `last_verified`). anima agents are both **consumers** (look up game
facts) and **producers** (file discrepancy reports when the world contradicts a
page — the highest-value contribution, since it makes pages `field-verified`).

There are three access paths. Which one applies depends on *what kind of process*
is running — see "Does it get injected?" below.

## Path 1 — Claude Code sessions: MCP, injected automatically

`anima/.mcp.json` registers the wiki MCP server. Any Claude Code session started in
`~/dev/uo`, `~/dev/uo/anima`, or `~/dev/uo/uowiki` gets these tools injected into
its tool list at startup (project-scope servers prompt for approval once, or run
`claude --mcp` / `/mcp` to inspect):

| Tool | Use |
|---|---|
| `wiki_search(query, limit)` | Find pages by keywords (AND-match, title-weighted) |
| `wiki_read_page(slug)` | Full markdown + status of one page, e.g. `"skills/mining"` |
| `wiki_list_pages(section)` | Inventory of a section, e.g. `"bestiary/monsters"` |
| `wiki_file_report(...)` | File a discrepancy report → `uowiki/reports/open/` (commits) |
| `wiki_update_page(slug, markdown, commit_message, agent)` | Edit a **curated** page (refuses `generated: true` pages; commits, never pushes) |
| `wiki_open_reports()` | List reports awaiting triage |

This covers the **self-improvement loop** (`tools/self_improve.py` runs Claude Code)
and **foundry** runs with `--backend claude`: when log analysis discovers that game
behavior contradicts the wiki — or confirms an `unverified` page — the session can
file a report or fix a curated page directly, citing the log evidence.

## Path 2 — anima runtime (Python, Ollama/litellm): import the functions

MCP is a protocol; the runtime brain is not an MCP client, so **nothing is injected
there automatically**. Instead, import the same functions the MCP server wraps —
they are plain stdlib-only Python (the `mcp` dependency is only needed for
`serve()`):

```python
import importlib.util
from pathlib import Path

_WIKI_TOOLS = Path(__file__).resolve().parents[2].parent / "uowiki/tools/mcp_server.py"
_spec = importlib.util.spec_from_file_location("uowiki_tools", _WIKI_TOOLS)
wiki = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wiki)

hits = wiki.search("dragon taming", limit=3)      # [{'slug': ..., 'title': ..., 'excerpt': ...}]
page = wiki.read_page("skills/animal-taming")     # {'markdown': ..., 'status': ...}
wiki.file_report(agent="bjorn", page="src/content/docs/skills/mining.md",
                 claim="...", observed="...", expected="...", evidence="...")
```

Recommended integration points (not wired yet — do this when a use case appears):

- **Think-node context**: `memory/retrieval.py` already injects RL stats into LLM
  prompts; add a wiki lookup the same way — `wiki.search()` on the current goal
  (e.g. "ettin location loot") and splice the top excerpt into the prompt. This
  makes the wiki the runtime superset of `data/world_knowledge.yaml`.
- **Skill failure analysis**: when a skill repeatedly fails in a way that
  contradicts wiki numbers (e.g. taming chance, ore yield), call
  `wiki.file_report()` with the trajectory/log path as evidence.
- Keep runtime writes to **reports only**. `update_page` is for sessions that can
  read ServUO source and weigh evidence; a game-playing loop should not rewrite
  pages mid-hunt.

CLI alternative (no import): `python3 tools/wiki_report.py --agent bjorn ...`
(see CLAUDE.md "Wiki discrepancy reports").

## Path 3 — humans / forum

Browse https://uowiki.vercel.app (search built in). Wrong info can also be raised
on the forum (https://www.uotavern.com); the librarian sweeps the qa/library boards.

## Rules that apply to every path

From `../uowiki/CLAUDE.md` — the short version:

1. **Never edit generated pages** (`generated: true` — bestiary, spell, recipe
   pages). They are rebuilt from `data/*.json`; fix the extractor instead. The MCP
   `update_page` tool enforces this.
2. **Every claim needs evidence** — ServUO file path or in-game log (agent,
   timestamp, log path). Status promotions require adding it to `sources`.
3. **Commits, not pushes** — agent edits accumulate locally; the librarian routine
   (`../uowiki/LIBRARIAN.md`, run on demand) verifies, builds, pushes, deploys:
   `claude -p "Read /Users/dkkang/dev/uo/uowiki/LIBRARIAN.md and execute it end to end."`
4. Reports beat silent fixes when uncertain — filing a wrong report is cheap (the
   librarian verifies independently and rejects with a note); shipping a wrong
   "fact" is not.

## Does it get injected into sessions? (FAQ)

- **Claude Code 세션** (개발 세션, self-improve, foundry claude backend): **yes** —
  `.mcp.json` project scope makes the `wiki_*` tools appear in the session's tool
  list automatically (one-time approval on first use).
- **anima 런타임 LLM 루프** (Ollama/litellm): **no** — there is no MCP client in the
  brain. Use Path 2: import the functions, or inject wiki content into prompts via
  the retrieval pipeline. MCP and the importable functions are the same code, so
  behavior is identical either way.

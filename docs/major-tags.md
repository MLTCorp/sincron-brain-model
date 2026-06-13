# Major Tags

Major Tags are the primary retrieval routes for Sincron Brain memories.

They are not free-form labels and should not be used as multiple facets for the
same small memory. A good Major Tag answers:

> Where should the AI first search for this memory in the future?

## Default Categories

Use these defaults whenever possible:

| Major Tag | Use for |
|---|---|
| `user_profile` | Stable facts about the user, role, responsibilities, and personal/professional context. |
| `preferences` | How the user wants the AI to answer, behave, decide, format, avoid, or prioritize work. |
| `projects` | Projects, products, repositories, initiatives, systems, or named workstreams. |
| `technical_context` | Architecture, code, commands, stack, implementation details, and technical behavior. |
| `external_access` | APIs, API keys, tokens, credentials, integrations, external platforms, and access rules. Never store secret values. |
| `workflows` | Recurring processes such as deploy, testing, review, publishing, support, and operations. |
| `decisions` | Decisions already made, criteria, tradeoffs, and agreements that should not be rediscussed from scratch. |
| `business_context` | Business rules, offers, contracts, pricing, strategy, customers as commercial context, and operations. |
| `people` | Individual people: users, authors, collaborators, contacts, stakeholders, and responsible persons. |
| `organizations` | Companies, clients, vendors, institutions, communities, teams, and platforms as entities. |
| `references` | Links, documents, files, sources, citations, and external materials used as reference. |
| `schedule` | Dates, deadlines, cadence, routines tied to time, reminders, and recurring temporal commitments. |

## Primary Rule

Use one primary Major Tag per memory whenever possible.

Bad:

```json
{
  "major_tags": ["external_access", "technical_context", "preferences"]
}
```

Good:

```json
{
  "major_tags": ["external_access"]
}
```

The detailed context belongs in normal tags, synopsis, and content.

Example:

```json
{
  "major_tags": ["external_access"],
  "tags": ["service_x", "api_key", "env_local", "do_not_ask_again"]
}
```

## Preferences Are Special

`preferences` stores behavior the user expects from the AI. These memories should
be consulted whenever the agent uses memory in a user-facing conversation.

Examples:

- preferred answer length
- preferred language or tone
- repeated corrections about how the AI should work
- rules the user expects the AI to follow
- things the user does not want to repeat

`constraints` is intentionally not a default Major Tag. User constraints and
rules belong in `preferences` unless they clearly fit another functional route.

## Creating A New Major Tag

There is no separate registry command.

A new Major Tag is created when the sleep/indexing judge stores or updates a
memory with that value in `major_tags`.

Create a new Major Tag only when all of these are true:

- none of the defaults fit
- the category is generic, not a specific project, person, client, file, or tool
- the category can group many future memories
- the category is useful as a future search route
- the name uses `snake_case`
- the name is short and understandable without extra context

Avoid Major Tags based on emotion, feedback type, source type, or one-off facts.
Those belong in score, synopsis, content, or normal tags.

## Examples

User says:

> The API key for service X is in `.env.local`; do not ask again.

Use:

```json
{
  "major_tags": ["external_access"]
}
```

User says:

> Prefer short answers with no long technical explanation.

Use:

```json
{
  "major_tags": ["preferences"]
}
```

User says:

> We decided that reused memories return to score 100.

Use:

```json
{
  "major_tags": ["decisions"]
}
```

User says:

> Every Friday we review the weekly content calendar.

Use:

```json
{
  "major_tags": ["schedule"]
}
```

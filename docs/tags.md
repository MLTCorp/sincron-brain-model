# Common Tags

Common tags are retrieval labels inside a Major Tag.

They are allowed to be more specific than Major Tags, and a memory can have
multiple common tags. They still need a controlled vocabulary so the vault does
not fill up with duplicate forms.

## Core Rule

Tags must be nouns, named entities, or nominal concepts.

Good:

```text
api_key
env_file
matheus_massari
sincron_ia
memory_viewer
payment_delay
weekly_review
```

Bad:

```text
matheus_falou_da_piq_no_dia_13
perguntar_de_novo
coisa_importante
cliente_estava_frustrado
```

## Creation Policy

Use existing tags when they are good enough, but do not make the system overly
restrictive. Create a new tag when it adds a useful future retrieval route.

Rules:

- use `snake_case`
- avoid accents, spaces, and uppercase
- prefer singular forms
- avoid singular/plural duplicates
- avoid redundant synonyms
- avoid verbs, phrases, mini-summaries, and one-off dated events
- prefer 3 to 8 tags per memory
- make tags specific enough to filter but generic enough to reuse

## Reuse Before Creating

Before creating a tag, inspect existing tags in the relevant Major Tag. If an
existing tag already represents the concept well, reuse it.

Examples:

- use `api_key`, not both `api_key` and `api_keys`
- use `env_file`, not both `env_file` and `environment_file`
- use `memory_viewer`, not `viewer_da_memoria`

New tags are allowed when they complement existing tags.

Example:

Existing tags:

```text
api_key
env_file
openai
```

New memory:

> The Anthropic key is in `.env.local`.

Useful tags:

```text
api_key
env_file
anthropic
env_local
```

`anthropic` and `env_local` are useful additions because they create real future
search routes.

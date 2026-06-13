"""Default Major Tag taxonomy and prompt guidance."""

from __future__ import annotations

DEFAULT_MAJOR_TAGS = (
    {
        "name": "soul",
        "description": (
            "Durable AI identity, mission, posture, personality, principles, and "
            "cognitive style."
        ),
    },
    {
        "name": "user_profile",
        "description": (
            "Stable facts about the user, role, responsibilities, and "
            "personal/professional context."
        ),
    },
    {
        "name": "preferences",
        "description": (
            "How the user wants the AI to answer, behave, decide, format, avoid, "
            "or prioritize work."
        ),
    },
    {
        "name": "projects",
        "description": (
            "Projects, products, repositories, initiatives, systems, or named "
            "workstreams."
        ),
    },
    {
        "name": "technical_context",
        "description": (
            "Architecture, code, commands, stack, implementation details, and "
            "technical behavior."
        ),
    },
    {
        "name": "external_access",
        "description": (
            "APIs, API keys, tokens, credentials, integrations, external platforms, "
            "and access rules. Never store secret values."
        ),
    },
    {
        "name": "workflows",
        "description": (
            "Recurring processes such as deploy, testing, review, publishing, "
            "support, and operations."
        ),
    },
    {
        "name": "decisions",
        "description": (
            "Decisions already made, criteria, tradeoffs, and agreements that "
            "should not be rediscussed from scratch."
        ),
    },
    {
        "name": "business_context",
        "description": (
            "Business rules, offers, contracts, pricing, strategy, customers as "
            "commercial context, and operations."
        ),
    },
    {
        "name": "people",
        "description": (
            "Individual people: users, authors, collaborators, contacts, stakeholders, "
            "and responsible persons."
        ),
    },
    {
        "name": "organizations",
        "description": (
            "Companies, clients, vendors, institutions, communities, teams, and "
            "platforms as entities."
        ),
    },
    {
        "name": "references",
        "description": (
            "Links, documents, files, sources, citations, and external materials "
            "used as reference."
        ),
    },
    {
        "name": "schedule",
        "description": (
            "Dates, deadlines, cadence, routines tied to time, reminders, and "
            "recurring temporal commitments."
        ),
    },
)

DEFAULT_MAJOR_TAG_NAMES = tuple(tag["name"] for tag in DEFAULT_MAJOR_TAGS)


def default_major_tag_names_csv() -> str:
    return ", ".join(f"`{name}`" for name in DEFAULT_MAJOR_TAG_NAMES)


def major_tag_prompt_guide() -> str:
    """Return compact instructions for LLM prompts."""
    lines = [
        "Major Tags default:",
        *(
            f"- {tag['name']}: {tag['description']}"
            for tag in DEFAULT_MAJOR_TAGS
        ),
        "",
        "Regras de Major Tag:",
        "- Major Tag e uma rota principal de consulta, nao uma lista de facetas.",
        "- Retorne uma unica major_tag por memoria sempre que possivel.",
        "- Use a categoria em que a IA primeiro deveria procurar essa memoria no futuro.",
        "- Use tags comuns/sinopse/conteudo para detalhes secundarios.",
        "- Nao crie Major Tag baseada em emocao, elogio, reclamacao, origem da "
        "mensagem, arquivo, pessoa, cliente ou projeto especifico.",
        "- Crie uma nova Major Tag somente se nenhuma default servir e se ela puder "
        "agrupar varias memorias futuras.",
        "- Nova Major Tag deve usar snake_case, ser generica, curta, funcional e reutilizavel.",
        "- soul e especial: use quando a informacao recomendar uma postura, "
        "personalidade, missao, identidade, principio ou estilo cognitivo duravel da IA.",
        "- preferences e especial: memorias nessa categoria moldam o comportamento "
        "do agente para as preferencias do usuario.",
        "- soul e preferences devem ser consultadas em conversas que usam memoria.",
    ]
    return "\n".join(lines)

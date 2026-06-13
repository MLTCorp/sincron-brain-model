"""Common tag normalization and prompt guidance."""

from __future__ import annotations

import re
import unicodedata

MAX_TAGS_PER_MEMORY = 8


def normalize_tag(value: str) -> str:
    """Normalize one common tag to a compact snake_case noun-like label."""
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s-]+", "_", text).strip("_")
    if not text:
        return ""
    parts = [_singularize(part) for part in text.split("_") if part]
    return "_".join(parts)


def normalize_tags(values: list[str], limit: int = MAX_TAGS_PER_MEMORY) -> list[str]:
    """Normalize, deduplicate, and cap common tags while preserving order."""
    out = []
    seen = set()
    for value in values:
        tag = normalize_tag(str(value))
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
        if len(out) >= limit:
            break
    return out


def tag_policy_prompt_guide() -> str:
    """Return compact instructions for common tag creation."""
    return "\n".join(
        [
            "Regras de Tags comuns:",
            "- Tags comuns detalham a memoria dentro da Major Tag; elas nao substituem a sinopse.",
            "- Use varias tags quando forem uteis, mas evite inflar. Alvo: 3 a 8 tags.",
            "- Tags devem ser substantivos, entidades nomeadas ou conceitos nominais.",
            "- Use snake_case, sem acentos, sem espacos e preferencialmente no singular.",
            "- Reutilize tags existentes quando elas representarem bem o conceito.",
            "- Crie tag nova quando ela acrescentar uma rota util de recuperacao.",
            "- Evite sinonimos redundantes, singular/plural duplicado, verbos, "
            "frases e mini-resumos.",
            "- Bons exemplos: api_key, env_file, matheus_massari, sincron_ia, memory_viewer.",
            "- Ruins: matheus_falou_da_piq_no_dia_13, perguntar_de_novo, coisa_importante.",
        ]
    )


def _singularize(part: str) -> str:
    if len(part) <= 3:
        return part
    if part.endswith("ies") and len(part) > 4:
        return part[:-3] + "y"
    if part.endswith("s") and not part.endswith(("ss", "us", "is")):
        return part[:-1]
    return part

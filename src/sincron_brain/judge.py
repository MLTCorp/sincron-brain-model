"""LLM-as-judge: the real Decider for the sleep job.

Given a draft and the candidate memories it might belong to, the judge does the
indexing work (Major Tag, synopsis, Go Deeper, emotional flag) and decides
whether to create a new memory or enrich an existing one.

The completion call is injected (`Completion`) so prompt construction and
response parsing are testable without an API key. The default completion routes
through litellm to the configured provider. Completion and parsing are defensive:
provider failures or malformed output fall back to a safe `create`, so a bad LLM
response never breaks sleep.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from sincron_brain import reconcile
from sincron_brain.config import VaultConfig
from sincron_brain.models import DraftItem
from sincron_brain.reconcile import Candidate, Decider, Decision

Completion = Callable[[list[dict]], str]

SYSTEM_PROMPT = (
    "Você organiza a memória de longo prazo de um agente, no eixo "
    "Major Tag → Tag → sinopse → conteúdo.\n"
    "Recebe uma NOVA INFORMAÇÃO e uma lista de MEMÓRIAS EXISTENTES CANDIDATAS.\n\n"
    "Decida UMA ação e responda APENAS com JSON, sem texto fora dele:\n"
    '- Mesmo assunto de uma candidata → {"action":"merge","target_id":"<id>",'
    '"synopsis":"<sinopse enriquecida>","content_append":"<texto novo a anexar>",'
    '"go_deeper":["<ids relacionados>"],"major_tags":["<tags a adicionar>"],'
    '"emotional":<bool>}\n'
    '- Assunto novo → {"action":"create","major_tags":["<tema>"],'
    '"synopsis":"<~300-400 chars>","content":"<memória contextual consolidada>",'
    '"go_deeper":["<ids relacionados>"],'
    '"emotional":<bool>}\n\n'
    "Regras:\n"
    "- Nunca transforme turnos de conversa em transcrição crua por padrão. Recompile "
    "mensagens de usuário e resposta da IA em uma memória contextual, curta e acionável.\n"
    "- Preserve o fato durável e o contexto de uso. Ex: em vez de copiar 'Usuário: já "
    "falei...' / 'IA: desculpe...', grave que o usuário já corrigiu a IA sobre X e que "
    "da próxima vez deve-se usar Y.\n"
    "- Sinais emocionais devem virar prioridade/contexto, não repetição literal da fala "
    "emocional na memória final.\n"
    "- A sinopse é um convite para ler o conteúdo: densa, e PRESERVANDO palavras-chave "
    "e nomes próprios (sistemas, pessoas, produtos) que tornam a memória localizável.\n"
    "- Prefira LINKAR (go_deeper) a duplicar conteúdo, e a inchar uma memória grande.\n"
    "- emotional=true somente quando houver feedback, correção ou cobrança do usuário "
    "sobre a IA, a resposta, ou a capacidade de lembrar/usar a memória. Feedback positivo "
    "e negativo têm o mesmo peso de prioridade.\n"
    "- emotional=false quando a emoção estiver apenas dentro do fato narrado. Ex: "
    "'Esse cliente me deixou frustrado porque atrasou o pagamento' é conteúdo da memória, "
    "não reforço emocional do sistema.\n"
    "- target_id só pode ser o id de uma candidata listada."
)


def parse_decision(raw: str, candidates: list[Candidate]) -> Decision:
    """Parse the LLM's JSON into a Decision. Any failure yields a safe create."""
    try:
        data = json.loads(_strip_fences(raw))
        if data.get("action") == "merge" and data.get("target_id") in {c.id for c in candidates}:
            return Decision(
                action="merge",
                target_id=data["target_id"],
                synopsis=data.get("synopsis") or "",
                content=data.get("content_append") or data.get("content") or "",
                go_deeper=list(data.get("go_deeper") or []),
                major_tags=list(data.get("major_tags") or []),
                emotional=bool(data.get("emotional", False)),
            )
        return Decision(
            action="create",
            major_tags=list(data.get("major_tags") or []),
            synopsis=data.get("synopsis") or "",
            content=data.get("content") or "",
            go_deeper=list(data.get("go_deeper") or []),
            emotional=bool(data.get("emotional", False)),
        )
    except (json.JSONDecodeError, TypeError, AttributeError):
        return Decision(action="create")


def build_messages(draft: DraftItem, candidates: list[Candidate]) -> list[dict]:
    cand_lines = "\n".join(f"- id={c.id}: {c.synopsis}" for c in candidates) or "(nenhuma)"
    if draft.source_type == "conversation_turn" and (
        draft.user_message or draft.agent_response or draft.memory_reason
    ):
        user = (
            "NOVA INFORMAÇÃO (source=conversation_turn):\n"
            f"MOTIVO PARA MEMÓRIA:\n{draft.memory_reason or '(não informado)'}\n\n"
            f"MENSAGEM DO USUÁRIO (material bruto, não copie como transcrição):\n"
            f"{draft.user_message or '(vazia)'}\n\n"
            f"RESPOSTA DA IA (material bruto, não copie como transcrição):\n"
            f"{draft.agent_response or '(vazia)'}\n\n"
            f"FALLBACK CONTEXTUAL JÁ COMPILADO:\n{draft.content}\n\n"
            f"MEMÓRIAS EXISTENTES CANDIDATAS:\n{cand_lines}"
        )
    else:
        user = (
            f"NOVA INFORMAÇÃO (source={draft.source_type}):\n{draft.content}\n\n"
            f"MEMÓRIAS EXISTENTES CANDIDATAS:\n{cand_lines}"
        )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def make_judge(config: VaultConfig, complete: Completion | None = None) -> Decider:
    """Build the judge Decider. `complete` is injectable for testing."""
    do_complete = complete or _litellm_completion(config)

    def decide(draft: DraftItem, candidates: list[Candidate]) -> Decision:
        try:
            raw = do_complete(build_messages(draft, candidates))
        except Exception:
            return Decision(action="create")
        return parse_decision(raw, candidates)

    return decide


def default_decider(config: VaultConfig) -> Decider:
    """The judge when an API key is configured, else the no-LLM create-only default."""
    if config.judge_api_key():
        return make_judge(config)
    return reconcile.create_only


def _litellm_completion(config: VaultConfig) -> Completion:
    def complete(messages: list[dict]) -> str:
        import litellm

        response = litellm.completion(
            model=f"{config.judge.provider}/{config.judge.model}",
            messages=messages,
            api_key=config.judge_api_key(),
            max_tokens=config.judge.max_tokens,
            temperature=0,
        )
        return response.choices[0].message.content or ""

    return complete


def _strip_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()

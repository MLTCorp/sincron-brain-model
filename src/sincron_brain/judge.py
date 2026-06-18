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
import time
from collections.abc import Callable
from typing import Any

from sincron_brain import reconcile, storage
from sincron_brain.config import VaultConfig
from sincron_brain.major_tags import major_tag_prompt_guide
from sincron_brain.models import DraftItem
from sincron_brain.reconcile import Candidate, Decider, Decision
from sincron_brain.tags import tag_policy_prompt_guide

JUDGE_TIMEOUT_SECONDS = 30
JUDGE_MAX_RETRIES = 1

Completion = Callable[[list[dict]], str]

SYSTEM_PROMPT = (
    "Você organiza a memória de longo prazo de um agente, no eixo "
    "Major Tag → Tag → sinopse → conteúdo.\n"
    "Recebe uma NOVA INFORMAÇÃO e uma lista de MEMÓRIAS EXISTENTES CANDIDATAS.\n\n"
    "Responda APENAS com JSON no formato:\n"
    '{"decisions":[{...}, {...}]}\n\n'
    "Cada item em `decisions` é UMA ação. Tipos válidos de item:\n"
    '- Mesmo assunto de uma candidata → {"action":"merge","target_id":"<id>",'
    '"synopsis":"<sinopse enriquecida>","content_append":"<texto novo a anexar>",'
    '"go_deeper":["<ids relacionados>"],"major_tags":["<tags a adicionar>"],'
    '"tags":["<tags comuns a adicionar>"],'
    '"emotional":<bool>}\n'
    '- Assunto novo → {"action":"create","major_tags":["<tema>"],'
    '"tags":["<tags comuns>"],'
    '"synopsis":"<~300-400 chars>","content":"<memória contextual consolidada>",'
    '"go_deeper":["<ids relacionados>"],'
    '"emotional":<bool>}\n\n'
    "Decomposição em múltiplas decisões:\n"
    "- Se a NOVA INFORMAÇÃO cobre N major_tags canônicas distintas COM FATOS "
    "DURÁVEIS INDEPENDENTES, retorne N decisões create — uma por categoria. "
    "Cada decisão tem synopsis e content focados naquele recorte. NUNCA duplique "
    "texto entre decisões — se você se vê reescrevendo a mesma frase em duas "
    "categorias, é sinal de que era uma decisão só.\n"
    "- Exemplo: 'Olá, sou Massari, quero que sejas Adamastor sempre bem-humorado' "
    "vira DUAS decisões: (a) create user_profile sobre o nome do usuário; "
    "(b) create soul sobre a persona e tom do agente. Não junte em uma só, e não "
    "crie uma terceira em preferences repetindo o mesmo tom.\n"
    "- Múltiplos fatos sobre o MESMO sujeito ficam em UMA decisão só "
    "('Massari mora em SP e gosta de café' = uma decisão em user_profile).\n"
    "- Máximo de 4 decisões por draft. Não use a lista para fragmentar artificialmente.\n\n"
    "Regras gerais:\n"
    f"{major_tag_prompt_guide()}\n"
    f"{tag_policy_prompt_guide()}\n"
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
    "- target_id só pode ser o id de uma candidata listada.\n"
    "- HINT_TAGS do draft (se houver) são sugestões de tags COMUNS feitas pelo host. "
    "Trate-as como candidatas a `tags`, NUNCA como major_tag. Promover um hint a "
    "major_tag (ex: 'name', 'identity') é proibido — esses valores ficam em `tags`, e "
    "o major_tag certo vem dos defaults canônicos (ex: 'user_profile' para identidade do usuário).\n"
    "- SUJEITOS DISTINTOS NÃO COMPARTILHAM MEMÓRIA. Identidade do agente (IA), do "
    "usuário humano, de clientes/terceiros, e de organizações são MEMÓRIAS SEPARADAS, "
    "mesmo quando o tema é parecido (nome, papel, função). Ex: 'usuário se chama "
    "Massari' (user_profile) e 'agente se chama Adamastor' (soul) NUNCA viram a "
    "mesma memória — são duas memórias distintas, opcionalmente ligadas por go_deeper.\n"
    "- soul é o agente (a IA). user_profile é o humano que conversa. people é um "
    "terceiro mencionado. organizations é entidade jurídica. Esses major_tags "
    "marcam sujeitos diferentes e jamais coexistem numa única memória.\n"
    "- MERGE preserva a major_tag da candidata. Se você acha que o assunto novo "
    "pertence a outro major_tag, então NÃO é merge — é create + opcionalmente "
    "go_deeper apontando para a candidata. Em merge, omita `major_tags` ou "
    "repita a major_tag da candidata; nunca proponha uma diferente.\n"
    "- go_deeper SÓ pode citar IDs que aparecem na lista MEMÓRIAS EXISTENTES "
    "CANDIDATAS. Nunca invente IDs, nunca reescreva IDs, nunca cite o ID da "
    "própria memória sendo criada. IDs inventados são descartados e geram "
    "alerta no audit.\n"
    "- Use go_deeper quando: (a) o assunto continua/desenvolve um fato já "
    "presente numa candidate de major_tag DIFERENTE (alternativa ao merge "
    "cross-major-tag); (b) há candidate de major_tag diferente cujo contexto "
    "enriquece a nova memória sem caber dentro dela. Não use como rede de "
    "associações fracas.\n"
    "- Cap sugerido: até 3 IDs por go_deeper. Vizinhos semânticos restantes "
    "são preenchidos automaticamente pelo sistema (auto-FTS), então prefira "
    "poucos links de alta relevância a muitos links fracos.\n"
    "- FEEDBACK DIRECIONADO: quando o draft é reação do usuário ao USO da "
    "memória (elogio como 'lembrou direitinho', correção como 'já te falei "
    "isso', cobrança como 'como não lembrou?', OU frustração/raiva/xingamento "
    "endereçados ao desempenho como 'você esqueceu de novo, droga!'), além de "
    "marcar `emotional`: true, identifique quais memórias receberam o "
    "feedback usando a lista MEMÓRIAS USADAS RECENTEMENTE fornecida no prompt "
    "e liste os IDs em `feedback_targets` (cap 3). Indique também "
    "`feedback_sentiment` (`positive`, `negative` ou `neutral`). Os IDs em "
    "feedback_targets ganham boost durável de emotion_floor — positivo e "
    "negativo têm o MESMO peso, conforme a base teórica do projeto. Se a "
    "raiva/frustração é parte do fato narrado ('esse cliente me frustrou'), "
    "isso é conteúdo, NÃO feedback de memória — não acione feedback_targets. "
    "Se o xingamento não tem alvo identificável (desabafo geral), deixe "
    "`feedback_targets` vazio."
)


def parse_decisions(raw: str, candidates: list[Candidate]) -> list[Decision]:
    """Parse the LLM's JSON into a list of Decisions.

    Accepts three input shapes:
      - canonical multi: {"decisions": [{...}, {...}]}
      - single legacy:   {"action": "create"|"merge", ...}  (wrapped in a list)
      - malformed/empty: returns [Decision(action="create")] as a safe fallback,
        mirroring the historical contract that a bad LLM response never breaks
        the sleep job.
    """
    try:
        data = json.loads(_strip_fences(raw))
    except (json.JSONDecodeError, TypeError, AttributeError):
        return [Decision(action="create")]

    if isinstance(data, dict) and isinstance(data.get("decisions"), list):
        items = data["decisions"]
    elif isinstance(data, dict) and "action" in data:
        items = [data]
    elif isinstance(data, list):
        items = data
    else:
        return [Decision(action="create")]

    candidate_ids = {c.id for c in candidates}
    decisions: list[Decision] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        feedback_targets = [
            tid
            for tid in (item.get("feedback_targets") or [])
            if isinstance(tid, str) and tid.strip()
        ]
        feedback_sentiment = item.get("feedback_sentiment") or ""
        if feedback_sentiment not in {"positive", "negative", "neutral", ""}:
            feedback_sentiment = ""
        if item.get("action") == "merge" and item.get("target_id") in candidate_ids:
            decisions.append(
                Decision(
                    action="merge",
                    target_id=item["target_id"],
                    synopsis=item.get("synopsis") or "",
                    content=item.get("content_append") or item.get("content") or "",
                    go_deeper=list(item.get("go_deeper") or []),
                    major_tags=list(item.get("major_tags") or []),
                    tags=list(item.get("tags") or []),
                    emotional=bool(item.get("emotional", False)),
                    feedback_targets=feedback_targets,
                    feedback_sentiment=feedback_sentiment,
                )
            )
        else:
            decisions.append(
                Decision(
                    action="create",
                    major_tags=list(item.get("major_tags") or []),
                    tags=list(item.get("tags") or []),
                    synopsis=item.get("synopsis") or "",
                    content=item.get("content") or "",
                    go_deeper=list(item.get("go_deeper") or []),
                    emotional=bool(item.get("emotional", False)),
                    feedback_targets=feedback_targets,
                    feedback_sentiment=feedback_sentiment,
                )
            )

    if not decisions:
        return [Decision(action="create")]
    return decisions


def parse_decision(raw: str, candidates: list[Candidate]) -> Decision:
    """Back-compat single-decision parse — returns the first decision only.

    New code paths should use parse_decisions. Kept so existing test fixtures
    and any external integration that imported this name keep working.
    """
    return parse_decisions(raw, candidates)[0]


def build_messages(
    draft: DraftItem,
    candidates: list[Candidate],
    recent_use_memories: list[dict] | None = None,
) -> list[dict]:
    cand_lines = "\n".join(
        (
            f"- id={c.id}; major_tags={c.major_tags or []}; "
            f"tags={c.tags or []}: {c.synopsis}"
        )
        for c in candidates
    ) or "(nenhuma)"
    hint_line = (
        f"HINT_TAGS (candidatas a `tags` comuns, nunca a major_tag): {list(draft.hint_tags)}\n\n"
        if draft.hint_tags
        else ""
    )
    recent_lines = "\n".join(
        f"- id={m['id']}; major_tags={m.get('major_tags') or []}: {m.get('synopsis', '')}"
        for m in (recent_use_memories or [])
    ) or "(nenhuma)"
    recent_section = (
        "MEMÓRIAS USADAS RECENTEMENTE (alvos potenciais de feedback_targets):\n"
        f"{recent_lines}\n\n"
    )
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
            f"{hint_line}"
            f"{recent_section}"
            f"MEMÓRIAS EXISTENTES CANDIDATAS:\n{cand_lines}"
        )
    else:
        user = (
            f"NOVA INFORMAÇÃO (source={draft.source_type}):\n{draft.content}\n\n"
            f"{hint_line}"
            f"{recent_section}"
            f"MEMÓRIAS EXISTENTES CANDIDATAS:\n{cand_lines}"
        )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def make_judge(config: VaultConfig, complete: Completion | None = None) -> Decider:
    """Build the judge Decider. `complete` is injectable for testing."""
    do_complete = complete or _litellm_completion(config)

    def decide(
        draft: DraftItem,
        candidates: list[Candidate],
        *,
        recent_use_memories: list[dict] | None = None,
    ) -> list[Decision]:
        start = time.monotonic()
        try:
            raw = do_complete(
                build_messages(draft, candidates, recent_use_memories=recent_use_memories)
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            storage.write_audit(
                config,
                "judge.completion_failed",
                draft_id=draft.id,
                duration_ms=duration_ms,
                error=type(exc).__name__,
                error_message=str(exc)[:200],
                provider=config.judge.provider,
                model=config.judge.model,
                candidates=len(candidates),
            )
            return [Decision(action="create")]
        duration_ms = int((time.monotonic() - start) * 1000)
        decisions = parse_decisions(raw, candidates)
        storage.write_audit(
            config,
            "judge.completion",
            draft_id=draft.id,
            duration_ms=duration_ms,
            provider=config.judge.provider,
            model=config.judge.model,
            candidates=len(candidates),
            decisions_count=len(decisions),
        )
        return decisions

    return decide


def default_decider(config: VaultConfig) -> Decider:
    """The judge when an API key is configured, else the no-LLM create-only default."""
    if config.judge_api_key():
        return make_judge(config)
    return reconcile.create_only


def judge_available(config: VaultConfig) -> bool:
    """Whether the configured judge can actually run, i.e. its API key is set."""
    return bool(config.judge_api_key())


def judge_status(config: VaultConfig) -> dict:
    """Surface judge readiness for stats/viewer/connect — never returns the key value."""
    return {
        "provider": config.judge.provider,
        "model": config.judge.model,
        "api_key_env": config.judge.api_key_env,
        "api_key_present": judge_available(config),
        "api_key_source": config.judge_api_key_source(),
        "ready": judge_available(config),
    }


def _litellm_completion(config: VaultConfig) -> Completion:
    def complete(messages: list[dict]) -> str:
        import litellm

        response: Any = litellm.completion(
            model=f"{config.judge.provider}/{config.judge.model}",
            messages=messages,
            api_key=config.judge_api_key(),
            max_tokens=config.judge.max_tokens,
            temperature=0,
            timeout=JUDGE_TIMEOUT_SECONDS,
            num_retries=JUDGE_MAX_RETRIES,
        )
        return response.choices[0].message.content or ""

    return complete


def _strip_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()

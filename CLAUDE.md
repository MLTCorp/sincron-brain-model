# Sincron Brain Model

Modelo de memória plug-and-play para agentes de IA. Distribuído como MCP server.
Qualquer projeto com IA pluga e ganha memória estruturada de longo prazo, inspirada em Obsidian + estrutura cognitiva humana.

---

## Identidade e propósito

- **O que é**: uma camada de memória organizada, indexada e recuperável por tags hierárquicas + pontuação cognitiva. Plug-in para projetos de IA existentes.
- **Como se instala**: MCP server. Desenvolvedor pluga no cliente dele (Claude Code, Claude Desktop, Cursor, app próprio via SDK MCP) e expõe as tools de memória pro agente.
- **Inspiração**: Obsidian (vault de .md com links entre notas) + ponderação de relevância do cérebro humano (memórias decaem, sobem por frequência e por emoção).

---

## Princípio arquitetural central

**O único eixo de recuperação é Major Tag → Tag → sinopse → conteúdo.**

- **Major Tag** = tema amplo (ex: "Pessoas", "Trabalho", "Família").
- **Tag** = tópico dentro do tema (ex: "Mateus Massari - Cofundador").
- **Sinopse** = descrição curta (~300-400 chars) no topo do .md, no estilo de description de skill. Permite ao agente decidir se vale aprofundar sem ler o arquivo todo.
- **Conteúdo** = corpo do .md.
- **Go Deeper** = referências cruzadas entre memórias (estilo wikilink Obsidian).

Uma memória pode pertencer a múltiplas Major Tags.

A IA do agente usa o próprio raciocínio dela pra navegar essa estrutura. Ela sabe que "sócio" é parente de "cofundador" sem precisar de embedding pra calcular isso.

---

## O que está DENTRO do escopo

- Receber conteúdo **textualizado** + metadados via tool `remember(...)`.
- Indexar em estrutura .md + SQLite (FTS5 para fallback de busca textual).
- Sistema de pontuação 1-100 com decaimento temporal, bônus por acesso, bônus por emoção.
- Sono noturno (cron customizável, default 03:00) que processa rascunhos via LLM-as-judge.
- Sugerir/manter Go Deeper entre memórias relacionadas.
- Tools de leitura/navegação pelo agente do host.
- CLI auxiliar (`init`, `sleep-now`, `stats`).

## O que está FORA do escopo (não engordar o projeto)

- ❌ Transcrição de áudio (responsabilidade do app host).
- ❌ Processamento de imagens / visão (responsabilidade do app host).
- ❌ Fetch/scraping de páginas web (responsabilidade do app host).
- ❌ Gerenciamento da janela de contexto do host.
- ❌ Funcionar como provider de LLM ou de chat.
- ❌ Embeddings vetoriais (rejeitado por decisão de design, ver "Decisões travadas").

O app host pode lidar com qualquer modalidade — quando converter para texto, chama `remember(...)` passando o texto + um `asset_ref` opaco apontando pro binário dele.

---

## Decisões travadas (não relitigar sem motivo forte)

1. **Distribuição**: MCP Server. Não plugin Claude Code (limitaria portabilidade), não lib pura (mais fricção de integração).
2. **Storage**: `.md` no disco como fonte da verdade + SQLite como índice (scores, timestamps, FTS5). Vault legível no Obsidian.
3. **Sem embeddings**. A IA do agente faz similaridade semântica raciocinando sobre sinopses. Major Tag → Tag é o eixo, FTS é a rede de segurança textual. Rejeitado porque:
   - Major Tag → Tag resolve ~70% dos casos sozinho.
   - FTS cobre mais ~20%.
   - LLM raciocinando sobre sinopses cobre o restante com qualidade superior a similaridade vetorial.
   - Elimina infra de embedding, dimensão fixa, reindex em troca de provider, custo de indexação.
4. **Sono cron** (default 03:00, customizável). Não eager indexing durante conversa — economiza token e mantém conversa fluida.
5. **Cascata de custo no sono**: heurística barata (palavras-chave, regex) marca candidatos; LLM-as-judge revisa só os marcados. Otimização de custo sem perder precisão.
6. **Provider configurável** (OpenAI, Anthropic, Voyage, Cohere, Gemini, Mistral, Jina, Azure, Bedrock, Ollama local, custom OpenAI-compatible). Camada de abstração via `litellm` ou equivalente. Mesma regra de provider serve pra qualquer uso de LLM no sistema (atualmente só o judge — sem embedding).
7. **Uma chave de API só** pra rodar tudo (a do judge). Detecta chaves no ambiente (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.) e usa o que tiver.
8. **Reindex incremental por score descendente**: se trocar provider/modelo do judge, re-processa memórias de score alto primeiro. Sistema fica usável durante migração.
9. **Decaimento de score**: piso = 1, nunca 0. Nenhuma memória é apagada sozinha — só perde superficialidade.

---

## Contrato — tools expostas via MCP

### Escrita
```
remember(
  content: str,                  # texto da memória (obrigatório)
  source_type: str = "text",     # rótulo livre: "user_message", "voice_transcript",
                                 # "image_description", "web_article", etc.
  asset_ref: str | None = None,  # path/URL opcional, opaco pra gente
  hint_tags: list[str] = [],     # dica de tags, judge no sono valida/refina
  timestamp: datetime | None,
  metadata: dict = {},           # campo livre pro app host
)
```
→ enfileira no rascunho. Indexação real acontece no próximo sono.

### Leitura
- `list_major_tags()` → árvore enxuta dos temas.
- `list_tags(major_tag, min_score=0)` → tags + sinopses + scores, ordenado por score DESC.
- `read_memory(tag_id)` → conteúdo completo + asset_ref + go_deeper.
- `search(query)` → conveniência que combina os 3 num caminho otimizado.

### Operação
- `sleep_now()` → força processamento manual do rascunho.
- `stats()` → contagem de memórias, distribuição de score, último sono, custo estimado.

---

## Estrutura do vault

```
memory/
├── _draft/                          ← fila aguardando o próximo sono
│   └── 2026-05-13-turn-abc.json
├── _index.sqlite                    ← scores, FTS, metadados
├── _config.toml                     ← provider, schedule, vault config
├── pessoas/
│   ├── mateus-massari-cofundador.md
│   └── luizao-socio.md
├── trabalho/
│   ├── sincron-auto.md
│   └── reuniao-cliente-acme.md
└── ...
```

Cada `.md` segue o formato:

```markdown
---
id: mateus-massari-cofundador
major_tags: [pessoas, empresas/sincron]
score: 87
created: 2026-05-13T14:32:00Z
last_accessed: 2026-05-13T20:11:00Z
access_count: 12
emotion_floor: 20
source_type: user_message
asset_ref: null
go_deeper: [luizao-socio, sincron-auto, mateus-massari-familia]
---

# Sinopse

Cofundador da Sincron Digital, sócio do Luizão. Atua na liderança técnica e
produto. Inspirou o modelo de memória plug-and-play descrito neste vault.
Casado com Cacau, pai do Pedro.

# Conteúdo

[corpo livre da memória, formatado em markdown]
```

---

## Sono — o que o judge faz

Quando o cron dispara (default 03:00) ou `sleep_now()` é chamado:

1. **Lê o rascunho** acumulado desde o último sono.
2. **Heurística barata** primeiro: regex de palavras-chave emocionais, marca candidatos.
3. **LLM-as-judge** então processa cada item do rascunho:
   - Escolhe Major Tag(s) certa(s).
   - Decide se cria memória nova ou atualiza existente (compara com sinopses dos candidatos).
   - Escreve/refina a sinopse (~300-400 chars).
   - Sugere Go Deeper olhando sinopses de memórias com proximidade temática.
   - Atribui peso emocional (se a heurística marcou).
4. **Recalcula scores**:
   - Decaimento temporal aplicado a todas as memórias.
   - Bônus de acesso (memórias lidas desde último sono sobem).
   - Bônus emocional (positivo OU negativo — ambos os polos pesam).
5. **Limpa rascunho processado**.
6. **Atualiza `_index.sqlite`** (FTS, scores, timestamps).

---

## Decisões ainda em aberto

- **Nome do pacote** (atual diretório: `sincron-brain-model`).
- **Linguagem do MCP server**: Python (FastMCP) vs TypeScript (`@modelcontextprotocol/sdk`). Python é o caminho de menos atrito pra LLM/litellm.
- **Fórmulas exatas** de: taxa de decaimento, valor do bônus por acesso, valor do bônus emocional. Começar com defaults conservadores e ajustar empiricamente.

---

## Base teórica — "A arte de esquecer" (Izquierdo et al.)

Arquivo: [`docs/zeluiz,+22.pdf`](docs/zeluiz,+22.pdf) — Iván Izquierdo, Lia R. M. Bevilaqua e Martín Cammarota, *Estudos Avançados* 20 (58), 2006.

**Consulte este artigo sempre que for analisar ou decidir qualquer coisa sobre scoring, decaimento, esquecimento, consolidação, peso emocional ou granularidade de memória.** Ele é a fundamentação neurocientífica do nosso modelo cognitivo e já se mostrou útil em várias decisões.

Resumo operacional (o que mapeia pro nosso design):

- **Esquecer é função, não falha.** Lembrar tudo impede pensar (paradoxo de Funes: para generalizar é preciso esquecer detalhes). → justifica decaimento + sinopse abstrativa-mas-com-keywords + ramificar via Go Deeper em vez de inchar uma memória.
- **Quatro mecanismos distintos**, só um é esquecimento real:
  1. *Extinção* — inibição ativa, reversível.
  2. *Repressão* — inibição ativa (voluntária/inconsciente), reversível. → embasa a **supressão a pedido do usuário** (soft delete, recuperável no banco).
  3. *Memória de curta duração* — efêmera por design fisiológico.
  4. *Esquecimento real* — atrofia sináptica por desuso, irreversível. → justifica decaimento por falta de uso.
- **Emoção forma sinapses duráveis** (via PKA), positiva OU negativa, e a memória persiste meses/anos. → embasa o **piso emocional não-decaente** (+10 por gatilho, cap em `emotion_bonus_max`).
- **Uso/recuperação mantém a memória** ("a função faz o órgão"; leitura é o melhor exercício geral). → embasa o **bônus de acesso** e a reconciliação que reforça memórias retocadas em vez de duplicá-las.
- **Janela de consolidação**: síntese protéica em 3-6h + segunda onda às 12h (BDNF) decide se a memória passa de 48h. → conhecido, mas **decidimos não implementar duas ondas** (complexidade > ganho).

---

## Princípios de trabalho neste projeto

- **Não engordar escopo.** Se aparecer ideia tipo "e se a gente também transcrevesse áudio?", a resposta é: não, isso é responsabilidade do app host. A gente só recebe texto.
- **Major Tag → Tag é o coração.** Qualquer feature nova passa pelo crivo: "isso fortalece ou compete com o eixo Major Tag → Tag?". Se compete, repensar.
- **Custo de operação importa.** Cada decisão de design avalia o gasto de token no sono e no uso normal.
- **Plug-and-play de verdade.** Instalação tem que ser: 1 comando + colar config MCP + 1 chave de API. Se ficar mais complexo, voltar pra prancheta.
- **Sem comentários óbvios no código.** O nome das coisas explica. Comentário só pra o "porquê" não-óbvio.
- **Consultar a base teórica.** Antes de mexer em scoring/decaimento/esquecimento/consolidação/emoção, reler o resumo de "A arte de esquecer" (seção acima) e, se necessário, o PDF em `docs/`.

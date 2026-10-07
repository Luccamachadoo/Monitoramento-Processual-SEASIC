# Arquitetura — Monitoramento Processual SEASIC (v3)

Esta nota alinha o documento *Arquitetura do Projeto — Versão 2* ao que está
implementado. Onde não há menção aqui, a v2 continua valendo (objetivo, escopo,
fora do escopo, conformidade, proteção de dados, MVP e critérios de aceitação).

## O que muda em relação à v2

| Tema | v2 | v3 (implementado) |
|---|---|---|
| Tabelas | 3: cadastro, fotografias, ocorrências | 4: `processes`, `executions`, `snapshots`, `occurrences`. `executions` sustenta o carimbo e o Monitor de Execução (4.9). |
| Cadastro Mestre | `config/processos.xlsx` | CSV importado com `import-catalog`; o SQLite é a fonte. A importação só insere/atualiza; inativar é marcar `ativo = não` com motivo. |
| Configuração | `seletores.yaml` + `execucao.yaml` | Um `config.toml` local (não versionado); `config.example.toml` é o modelo. |
| Primeiro sistema | SEI (2439/2026) | e-DOC. O mesmo coletor atende o SEI. |
| Stack | Python + Playwright + SQLite | Igual; núcleo só com biblioteca padrão, Playwright como extra `browser`. |

## Modelo de dados

- `processes` — chave (`system`, `number`); `system` ∈ {`SEI`, `EDOC`}.
- `executions` — início, fim, `status` (`EM_ANDAMENTO`, `OK`, `PARCIAL`,
  `FALHOU`), consultados, êxitos, falhas, observações e `scope` (sistema da rodada).
- `snapshots` — uma por processo consultado em cada execução, inclusive falhas
  (`valid = 0`). Guarda o conjunto de unidades, andamento, data ISO, `content_hash`,
  `comparison_status` (`BASELINE`, `SEM_MUDANCA`, `ALTERACAO`, `INVALIDA`) e o
  número como o sistema exibiu (`display_number`).
- `occurrences` — eventos com `dedupe_key` única e `communicated`.

## Classificação

A v2 misturava resultado da consulta e evento. Na v3:

- **Status da consulta** (na fotografia): `OK`, `NAO_LOCALIZADO`,
  `SESSAO_EXPIRADA`, `ERRO_EXTRACAO`, `INDISPONIVEL`.
- **Ocorrências** (o que o Gabinete lê): `MUDANCA_DE_UNIDADE`, `NOVO_ANDAMENTO`,
  `PARADO`, `FALHA`.

Regras:

- **Hash** = unidades (ordenadas, sem diferenciar maiúsculas) + andamento
  normalizado + data do andamento. `setor_exibicao` fica fora.
- **Mudança de unidade / novo andamento** — só quando a fotografia válida atual
  difere da última válida; repetir a execução no mesmo dia não duplica avisos.
- **Parado** — regra temporal, independente do hash: **dias úteis** entre a data
  do andamento e a data de Brasília da coleta ≥ `stagnant_after_days`. Descontam-se
  fins de semana, feriados nacionais e os de `[monitor].holidays`. Um aviso por
  andamento.
- **Falha** — um aviso quando o status de falha de um processo muda; falhas
  repetidas iguais não geram novo aviso.
- Falhas globais (sessão expirada no início, navegador não abriu) não geram
  fotografia por processo: a execução fica `FALHOU` com a causa.

## Número do processo

O e-DOC exibe números com sufixo de classificação (`2439/2026-COMPR-SEASIC`). O
cadastro deve trazer o número completo; se trouxer só `NNNN/AAAA`, o coletor
aceita a linha cujo número começa por esse prefixo seguido de hífen e recusa como
ambíguo quando houver mais de uma.

## Rodada de coleta

Em série, um processo por vez, com:

- teto por execução e **teto diário** por sistema (soma das execuções do dia);
- janela de horário opcional;
- **processo de referência** (`canary`) consultado primeiro — se falhar, a rodada
  para antes de gerar falhas em massa;
- interrupção quando a sessão expira ou após N falhas técnicas seguidas;
- execução sempre finalizada, inclusive em interrupção manual; execuções que
  ficaram abertas são fechadas como `FALHOU` na rodada seguinte.

## Saídas

- `report` — relatório técnico de uma execução.
- `status` — carimbo e falhas atuais.
- `visao` — visão executiva da seção 7 da v2 (Markdown ou CSV), com carimbo.
- `resumo` — ocorrências não comunicadas por área e programa; `--marcar-comunicado`.
- `history` — histórico completo de um processo, sem depender de planilha.
- `planilha` — publica a visão atual no **Google Sheets**, numa aba própria do robô,
  com conta de serviço (escopo só de planilhas) e escrita como texto puro.

## Operação

- `diagnostico` confere a implantação e, com um processo conhecido, mostra cada
  passo da consulta (seletor por seletor) sem gravar nada — apoio ao mapeamento
  de seletores no ambiente institucional.
- Nenhuma unidade aberta encontrada é falha de extração, nunca "conjunto vazio".

- `rotina` executa o dia inteiro (coleta → resumo e visão em arquivo → planilha →
  backup com rotação → limpeza de logs), com etapas isoladas e código de saída 1
  quando alguma falha.
- Log técnico diário com retenção (`[logs]`), só com identificador, status e
  código de erro.

## Decisões pendentes

Decididas: planilha no Google Sheets; "parado" em dias úteis.

1. Se o limite de "parado" varia por área.
2. Feriados estaduais, municipais e pontos facultativos a cadastrar em `holidays`.
3. Regra do `setor_exibicao` quando o processo está aberto em várias unidades
   (hoje a visão mostra todas).
4. Confirmar os prazos de retenção (padrão: logs 180 dias, 30 backups diários) e o
   local institucional de execução e de cópia dos backups.
5. Canal e formato do resumo para o Gabinete (o texto Markdown já serve para colar
   em e-mail ou mensagem).

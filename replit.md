# SEASIC — Monitoramento Processual

Base local de desenvolvimento para registrar consultas, preservar histórico e destacar mudanças e exceções em processos SEI/e-DOC.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `tools/seasic-processos/` — CLI Python, SQLite, comparação, relatórios, documentação e testes.
- `tools/seasic-processos/src/seasic_monitor/database.py` — fonte do esquema SQLite e persistência.
- `tools/seasic-processos/docs/operacao.md` — limites e procedimentos operacionais.
- `lib/api-spec/openapi.yaml` e `artifacts/api-server/` — scaffold compartilhado do workspace; não são a fonte dos dados do monitor.

## Architecture decisions

- O monitor começa como ferramenta Python local e usa SQLite; não depende do scaffold web/Postgres do workspace.
- A demonstração contém somente registros sintéticos; coletores reais SEI/e-DOC permanecem bloqueados até autorização e configuração no ambiente institucional.
- e-DOC é o primeiro sistema escolhido para integrar; a configuração real segue desativada até haver URL e seletores aprovados.
- Execuções, fotografias e ocorrências ficam separadas do cadastro mestre; falhas não substituem o último estado válido.
- O banco local, credenciais futuras, perfis de navegador e dados reais nunca devem ser adicionados ao Git ou a esta hospedagem sem autorização institucional.
- O formato da planilha de destino (`.xlsx` ou nuvem) continua pendente; não manter os dois formatos em paralelo.

## Product

O MVP de linha de comando cadastra processos, grava fotografias e falhas, compara unidades e andamento, gera resumo por execução e mantém histórico por processo. A integração real e a atualização de planilhas ainda não estão habilitadas.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

_Populate as you build — sharp edges, "always run X before Y" rules._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details

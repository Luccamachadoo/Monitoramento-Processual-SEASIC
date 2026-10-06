# Operação — Monitoramento Processual SEASIC

## Limites desta entrega

O pacote ainda é uma base local para desenvolvimento. `demo` consulta apenas
dados sintéticos; `run` só acessa um sistema depois que o coletor for habilitado
e configurado no ambiente institucional. Este projeto Replit
não deve receber credenciais, sessões autenticadas, perfis de navegador,
planilhas internas ou processos reais sem autorização institucional explícita.

O sistema é somente de leitura. Não assina, movimenta, despacha ou altera
processos. Não contorna MFA, CAPTCHA, bloqueios, limites ou controles de acesso.

## Preparação local

1. Obtenha aprovação para o ambiente institucional onde código, banco e backups
   serão mantidos.
2. Instale Python 3.11 ou superior nesse ambiente.
3. Faça uma cópia institucional do repositório e proteja a pasta de dados.
4. Copie `config.example.toml` para `config.toml`; ajuste somente caminhos e
   parâmetros aprovados.
5. Inicialize o banco com `python -m seasic_monitor.cli init`.
6. Importe o Cadastro Mestre autorizado por CSV.
7. Faça backup antes de uma mudança de versão ou manutenção.

Não use sincronização pública. O diretório SQLite contém informação institucional
e deve ter acesso restrito à equipe autorizada. Os backups precisam de controle
de acesso e teste periódico de restauração, não apenas de criação.

## Execução de demonstração

`python -m seasic_monitor.cli demo` cria um banco sintético separado
(`data/demo.sqlite`) quando nenhum banco foi indicado. O relatório apresenta
casos fictícios de baseline, processo parado e falha de coleta. Não execute a
demonstração apontando para o banco institucional.

## Estados e interpretação

- **Sem execução registrada:** a rotina não rodou; não é evidência de ausência
  de movimentação.
- **BASELINE:** primeira fotografia válida; ainda não existe estado anterior
  para comparar.
- **SEM_MUDANCA:** hash dos campos acompanhados igual ao da fotografia válida
  anterior. Não quer dizer que todos os campos ou documentos do processo foram
  lidos.
- **ALTERACAO:** houve mudança nos campos comparáveis.
- **INVALIDA:** tentativa preservada, mas não serve de base para comparação.
- **Falha atual:** última tentativa do processo não foi válida. A fotografia
  válida anterior continua disponível no histórico.
- **PARADO:** o último movimento atingiu o limite em dias úteis definido na
  configuração (`stagnant_after_days`). Fins de semana, feriados nacionais
  (inclusive Sexta-feira Santa) e os feriados de `[monitor].holidays` não contam.
  Cadastre ali feriados estaduais e municipais (ex.: 8 de julho em Sergipe) e os
  pontos facultativos adotados. Avisado uma vez por andamento.
- **Execução FALHOU sem fotografias:** a sessão estava expirada, a tela de consulta
  não apareceu ou o navegador não abriu. Nada foi consultado; o último estado
  válido continua valendo e as observações da execução dizem a causa.
- **Execução PARCIAL com "não consultados":** a rodada foi interrompida (sessão
  expirou, processo de referência falhou ou falhas técnicas seguidas). Os
  processos restantes não têm dado novo — não é ausência de movimentação.
- **Execução interrompida:** se a rotina cair sem finalizar, a próxima execução a
  marca como FALHOU com a nota "Execução interrompida antes de ser finalizada".

Datas são armazenadas em ISO/UTC; o resumo é exibido no horário de São Paulo.
Dias sem movimento são calculados na exibição e não gravados como estado.

## Coleta real — pendente

A ativação depende de autorização institucional, mapeamento dos dois sistemas e
execução no ambiente aprovado. O primeiro coletor deverá:

- reutilizar uma sessão permitida e interromper-se quando ela expirar;
- solicitar reautenticação humana, sem automação de MFA/CAPTCHA;
- consultar um processo por vez e respeitar o intervalo e teto diário aprovados;
- falhar explicitamente se os seletores ou campos mínimos não forem encontrados;
- registrar uma fotografia inválida em caso de erro, sem apagar estado válido;
- não extrair conteúdo integral de documentos nem incluí-lo em logs.

### e-DOC — primeiro alvo de integração

O fluxo `login` / `consultar` / `run` serve aos dois sistemas (`--sistema e-DOC`
ou `--sistema SEI`); `login-edoc` e `run-edoc` são atalhos. A configuração de
exemplo está desativada e não tem endereço nem seletores. Antes de habilitá-la:

1. obtenha ciência/autorização da ASSTI/STI e confirme a conta de consulta;
2. execute em máquina institucional com navegador visível e terminal interativo;
3. mapeie e valide os seletores para busca, resultado, unidades, andamento, data,
   processo não localizado e sessão expirada;
4. configure URL e seletores no `config.toml` local (nunca no arquivo de exemplo
   versionado). Como a consulta e os detalhes são telas separadas, preencha
   `result_row`, `process_number_cell`, `detail_link` e `detail_ready` para abrir
   apenas o detalhe da linha cujo número corresponde ao processo consultado.
   O e-DOC exibe números com sufixo (`2439/2026-COMPR-SEASIC`); prefira cadastrar
   o número completo. Com só `NNNN/AAAA`, duas linhas com o mesmo prefixo geram
   `RESULTADO_AMBIGUO` em vez de um palpite. A URL inicial deve abrir a tela de
   busca após a autenticação;
5. use `login --sistema e-DOC` para abrir o perfil local e autenticar-se na janela
   oficial. Esse comando precisa apenas de URL e `enabled = true`; ele não lê nem
   preenche usuário, senha, MFA ou CAPTCHA;
6. valide um processo conhecido com `consultar e-DOC NUMERO` (não grava no banco)
   e confira manualmente no sistema oficial;
7. defina esse processo como `canary` em `[collectors.EDOC]` antes de qualquer lote.

### SEI

O mesmo coletor atende o SEI. Campos exibidos dentro de iframes (árvore do
processo, visualização) precisam de `[collectors.SEI.frames]`, indicando para cada
seletor o iframe que o contém. Valide com `consultar SEI NUMERO` antes do lote.

### Ritmo e proteção da rodada

`run` consulta um processo por vez e respeita, no `config.toml`:

- `min_interval_seconds` — intervalo entre consultas;
- `max_processes_per_run` e `max_processes_per_day` — o teto diário soma todas as
  execuções do dia (horário de Brasília) para o sistema; acima dele nada é consultado;
- `allowed_hours` — janela permitida, ex.: `"05:00-08:00"`;
- `max_consecutive_failures` — falhas técnicas seguidas que interrompem a rodada
  ("não localizado" não conta);
- `canary` — processo de referência consultado primeiro; se falhar, a rodada para.

Se a sessão expirar no meio da rodada, ela para e os restantes ficam como "não
consultados". Refaça `login` e rode de novo.

Para instalar o navegador no computador institucional, a equipe responsável
precisa aprovar e executar a instalação do extra Playwright e do Chromium. Não
copie o perfil persistente para o Replit nem para pastas sincronizadas.
`headless = true` pode ser usado em `run`/`consultar` depois que a sessão estiver
salva; `login` sempre abre a janela.

Antes de lote, validar manualmente de 8 a 12 processos durante cinco dias úteis,
conforme o critério do projeto. O lote não deve atualizar planilhas até essa
validação terminar.

## Rotina diária

`rotina` faz tudo numa chamada, na ordem:

1. coleta de cada sistema com `enabled = true` (e-DOC, depois SEI);
2. resumo executivo gravado em `[rotina].reports_dir` como
   `resumo-AAAA-MM-DD-HHMM.md` — com `mark_communicated = true`, o que foi gravado
   não se repete no dia seguinte;
3. visão atual em CSV na mesma pasta (`visao-AAAA-MM-DD-HHMM.csv`);
4. planilha no Google Sheets, se `[sheets] enabled = true`;
5. backup `processos-AAAA-MM-DD.sqlite` em `[rotina].backup_dir`, mantendo os
   `backup_keep` mais recentes;
6. remoção dos logs mais antigos que `[logs].retention_days`.

Cada etapa é independente: se a coleta falhar (sessão expirada, layout mudou), o
resumo, a visão e a planilha ainda são gerados — e mostram que **não houve coleta
válida**, em vez de parecerem "sem movimentação". A rotina imprime uma linha por
etapa e termina com código 1 se alguma falhou.

A rotina é **assistida**: quando a sessão expira, alguém precisa rodar `login` e
autenticar-se na janela oficial. Confira o resultado todo dia.

### Agendador de Tarefas do Windows

Exemplo para rodar de segunda a sexta às 06:30 (ajuste os caminhos; use a conta
autorizada, com sessão do Windows aberta para o navegador persistente):

```bat
schtasks /Create /TN "SEASIC Monitoramento" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 06:30 ^
  /TR "cmd /c cd /d C:\SEASIC\seasic-processos && set PYTHONPATH=src && python -m seasic_monitor.cli rotina >> logs\agendador.txt 2>&1"
```

Na tarefa, marque "Executar somente quando o usuário estiver conectado": o
navegador usa o perfil do operador. O resultado de cada execução fica no
histórico da tarefa (código 0 = tudo OK, 1 = alguma etapa falhou) e no log do dia.

### Logs técnicos

`logs/seasic-AAAA-MM-DD.log` registra início e fim de cada execução, uma linha por
processo (`processo=SISTEMA:NUMERO status=… comparacao=… erro=…`) e o resultado de
cada etapa da rotina. Não registra andamento, unidade, interessado nem conteúdo
de documento. Arquivos mais antigos que `retention_days` (padrão 180) são
apagados pela rotina.

### Backups

A rotina mantém `backup_keep` cópias diárias (padrão 30) na máquina. Copie-as
periodicamente para um local institucional fora dela e teste a restauração.

## Cadastro Mestre

`import-catalog cadastro.csv` valida o arquivo inteiro antes de gravar: uma linha
inválida ou um processo repetido cancelam a importação toda. Processos ativos no
banco que não aparecem no CSV são listados como aviso e continuam monitorados;
com `--inativar-ausentes`, são inativados com o motivo "Ausente do cadastro
importado em DD/MM/AAAA". O histórico deles é preservado.

## Planilha no Google Sheets

O comando `planilha` publica a visão atual numa aba do Google Sheets. O robô
reescreve apenas a aba configurada (padrão "Visão atual — robô"); as demais abas
e anotações da equipe ficam intactas. Layout da aba:

- **A1:** título; **A2:** carimbo da última execução bem-sucedida e quantos
  processos foram consultados com êxito — sempre nessa posição;
- **A3:** alerta quando a execução mais recente falhou ou foi parcial;
- **A4:** horário da publicação; **linha 6:** cabeçalho; da linha 7 em diante, um
  processo por linha.

O texto é gravado como texto puro (RAW): nada vindo do SEI/e-DOC vira fórmula.

### Preparação (uma vez, com a TI)

1. Criar no Google Cloud do órgão um projeto com a API do Google Sheets habilitada
   e uma **conta de serviço** só para esta rotina; gerar a chave JSON.
2. Guardar o JSON em pasta restrita da máquina de execução, fora do repositório
   e de pastas sincronizadas. Ele é segredo, como uma senha.
3. Criar a planilha e compartilhá-la **somente** com o e-mail da conta de serviço
   (editor) e com a equipe do Gabinete. Nunca usar "qualquer pessoa com o link".
4. Preencher `[sheets]` no `config.toml`: `enabled = true`, `spreadsheet_id` (trecho
   da URL entre `/d/` e `/edit`) e `credentials_path` — ou definir
   `SEASIC_SHEETS_CREDENTIALS` com o caminho do JSON.
5. Instalar o extra: `python3 -m pip install -e '.[sheets]'`.
6. Rodar `planilha` uma vez numa planilha de teste e conferir o layout.

A conta de serviço usa apenas o escopo de planilhas (sem acesso ao Drive). Se a
chave vazar, revogue-a no Google Cloud e gere outra.

A publicação na planilha oficial só deve começar depois da validação de 5 dias
úteis sem divergência (critério de aceitação do projeto). O histórico continua
no SQLite; a planilha é só a camada de visualização.

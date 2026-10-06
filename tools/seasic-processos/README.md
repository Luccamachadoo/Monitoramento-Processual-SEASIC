# Monitoramento Processual SEASIC

Ferramenta de linha de comando para cadastro, histórico e comparação de consultas
processuais. Por padrão usa apenas dados sintéticos e não acessa SEI/e-DOC. O
fluxo opcional e-DOC permanece desligado e só deve ser configurado e executado
em ambiente institucional autorizado. Nunca movimenta processos.

## Começar

Python 3.11 ou superior é suficiente; o núcleo usa apenas a biblioteca padrão.

```bash
cd tools/seasic-processos
cp config.example.toml config.toml
PYTHONPATH=src python3 -m seasic_monitor.cli init
PYTHONPATH=src python3 -m seasic_monitor.cli demo
```

O comando `demo` usa `data/demo.sqlite` e cria apenas os registros fictícios
`DEMO-001`, `DEMO-002` e `DEMO-003`. Para usar outro banco de forma intencional,
passe `--database CAMINHO` antes do comando. Evite apontar demonstrações ou
testes para um banco institucional.

Para consultar novamente o banco da demonstração:

```bash
PYTHONPATH=src python3 -m seasic_monitor.cli --database data/demo.sqlite status
PYTHONPATH=src python3 -m seasic_monitor.cli --database data/demo.sqlite report
```

## Comandos disponíveis

```bash
PYTHONPATH=src python3 -m seasic_monitor.cli status
PYTHONPATH=src python3 -m seasic_monitor.cli report --format markdown
PYTHONPATH=src python3 -m seasic_monitor.cli report --format json
PYTHONPATH=src python3 -m seasic_monitor.cli visao                       # estado atual (Markdown)
PYTHONPATH=src python3 -m seasic_monitor.cli visao --format csv --saida data/visao.csv
PYTHONPATH=src python3 -m seasic_monitor.cli resumo                      # novidades ainda não comunicadas
PYTHONPATH=src python3 -m seasic_monitor.cli resumo --marcar-comunicado
PYTHONPATH=src python3 -m seasic_monitor.cli history SEI DEMO-001
PYTHONPATH=src python3 -m seasic_monitor.cli import-catalog cadastro.csv
PYTHONPATH=src python3 -m seasic_monitor.cli backup data/backup/processos.sqlite
```

- `visao` mostra, para cada processo ativo, unidades abertas, último trâmite, dias
  sem movimento (calculados na exibição), situação da última consulta e a nota do
  cadastro, com o carimbo da última execução bem-sucedida no topo. O CSV (UTF-8 com
  BOM, abre direto no Excel) é a ponte para as planilhas até a decisão do formato.
- `resumo` lista as ocorrências ainda não comunicadas, agrupadas por área e
  programa. Com `--marcar-comunicado`, elas não voltam no próximo resumo.

Após autorização e configuração local, a coleta real é:

```bash
PYTHONPATH=src python3 -m seasic_monitor.cli login --sistema e-DOC
PYTHONPATH=src python3 -m seasic_monitor.cli consultar e-DOC 2439/2026   # um processo, não grava
PYTHONPATH=src python3 -m seasic_monitor.cli run --sistema e-DOC
```

`login-edoc` e `run-edoc` continuam funcionando como atalhos. `login` abre um perfil
persistente do navegador para o operador autenticar-se manualmente; com URL e
`enabled = true`, pode ser usado antes de mapear os seletores. `consultar` serve para
validar seletores num processo conhecido sem tocar no banco. `run` percorre apenas
os processos ativos do sistema indicado, em série, e:

- recusa começar se exceder o teto por execução, o teto diário
  (`max_processes_per_day`, somando as execuções do dia) ou a janela `allowed_hours`;
- consulta primeiro o processo de referência (`canary`), se configurado, e para a
  rodada se ele falhar — sinal de layout alterado;
- para a rodada quando a sessão expira ou após `max_consecutive_failures` falhas
  técnicas seguidas; os restantes aparecem como "não consultados";
- quando a sessão já está expirada no início, não grava fotografia por processo: a
  execução fica `FALHOU` com a causa, e o último estado válido é preservado.

Na estação institucional autorizada, instale o extra opcional do navegador após
aprovação da equipe de TI:

```bash
python3 -m pip install -e '.[browser]'
python3 -m playwright install chromium
```

O fluxo do e-DOC tem busca e detalhe em telas separadas. Se forem configurados
`detail_link`, `result_row`, `process_number_cell` e `detail_ready`, o coletor
exige os quatro e abre o detalhe apenas quando há uma única linha correspondente.
O e-DOC exibe números com sufixo (`2439/2026-COMPR-SEASIC`): o cadastro pode trazer
o número completo ou só `NNNN/AAAA`; nesse caso, mais de uma linha com o mesmo
prefixo é tratada como resultado ambíguo. O número exibido é gravado na fotografia.

No SEI, a árvore e o conteúdo do processo ficam em iframes: use
`[collectors.SEI.frames]` para indicar o iframe de cada seletor. Os seletores e a URL
ficam apenas no `config.toml` local, nunca em `config.example.toml`.

`--database` e `--config` são opções globais e devem vir antes do comando.
Exemplo:

```bash
PYTHONPATH=src python3 -m seasic_monitor.cli \
  --config config.toml --database data/processos.sqlite status
```

### Importar o Cadastro Mestre

O CSV deve estar em UTF-8 e conter os cabeçalhos `system` e `numero`. Os demais
campos são opcionais:

```csv
system,numero,area,programa,descricao,ativo,motivo_inativacao
SEI,DEMO-010,DSAN,Banco de Alimentos,Exemplo fictício,sim,
e-DOC,DEMO-011,DSAN,Banco de Alimentos,Outro exemplo,não,Encerrado
```

Use apenas um cadastro autorizado no ambiente institucional escolhido. A opção
`ativo` aceita `sim/não`, `true/false` ou `1/0`. O par sistema+número é a chave;
um número igual em SEI e e-DOC representa dois processos distintos.

## O que já funciona

- Cadastro mestre com sistema, número, área, programa, descrição e inativação.
- SQLite local com tabelas separadas para cadastro, execuções, fotografias e
  ocorrências.
- Gravação de falhas sem substituir a última fotografia válida.
- Hash dos campos comparáveis, com normalização da ordem das unidades.
- Detecção de mudança de unidade, novo andamento, processo parado e falha.
- Supressão de ocorrências repetidas durante a mesma situação.
- Resumo por execução, histórico por processo, carimbo e backup SQLite.
- Permissões restritas para diretório e arquivo do banco em sistemas POSIX.
- Adaptador Playwright genérico por seletores (SEI e e-DOC), com perfil
  persistente, suporte a iframes e falha explícita; desativado por padrão.
- Rodada protegida: teto por execução e diário, janela de horário, processo de
  referência e interrupção por sessão expirada ou falhas técnicas seguidas.
- Visão atual (Markdown/CSV) e resumo executivo com controle do que já foi
  comunicado.

O limite de processo parado é configurável em dias corridos. A decisão entre dias
corridos e dias úteis ainda precisa ser confirmada pelo Gabinete.

## O que não está habilitado

Os coletores vêm desativados. Seletores, fluxo de navegação e comportamento de
cada sistema precisam ser configurados e validados com conta autorizada no
ambiente institucional. Não foram incluídos URLs, credenciais, perfis de navegador
nem dados reais.

Antes de habilitar uma coleta real, o operador institucional precisa confirmar:

1. autorização e conta de consulta para cada sistema;
2. local institucional de execução e armazenamento, retenção e backup;
3. ciência formal à ASSTI/STI;
4. forma de autenticação e re-login manual, sem contornar MFA, CAPTCHA ou
   controles de segurança;
5. seletores e campos mínimos validados em processo de teste;
6. intervalo e teto diário aprovados.

Planilhas e serviços em nuvem também não estão conectados. A escolha entre
`.xlsx` e planilha em nuvem permanece pendente; até essa decisão, os relatórios
são Markdown/JSON e o SQLite é a fonte do histórico.

## Desenvolvimento

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

`tests/test_playwright_local.py` roda o coletor com Chromium contra páginas
sintéticas em `tests/fixtures/paginas/` (nunca contra SEI/e-DOC); é pulado quando o
extra `browser` não está instalado.

A arquitetura atual está em [`docs/arquitetura.md`](docs/arquitetura.md). As decisões operacionais e a configuração de execução estão em
[`docs/operacao.md`](docs/operacao.md).


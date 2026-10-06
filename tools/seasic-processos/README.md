# Monitoramento Processual SEASIC

Ferramenta de linha de comando para cadastro, histórico e comparação de consultas
processuais. A primeira entrega opera somente com dados sintéticos: não acessa
SEI/e-DOC, não abre navegador e não movimenta processos.

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
PYTHONPATH=src python3 -m seasic_monitor.cli history SEI DEMO-001
PYTHONPATH=src python3 -m seasic_monitor.cli import-catalog cadastro.csv
PYTHONPATH=src python3 -m seasic_monitor.cli backup data/backup/processos.sqlite
```

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
- Adaptador Playwright genérico por seletores, com perfil persistente e
  comportamento de falha explícita; desativado por padrão.

O limite de processo parado é configurável em dias corridos. A decisão entre dias
corridos e dias úteis ainda precisa ser confirmada pelo Gabinete.

## O que não está habilitado

O comando `run` bloqueia explicitamente a consulta real. O adaptador Playwright é
apenas uma base genérica: seletores, fluxo de navegação e comportamento de cada
sistema precisam ser configurados e validados com conta autorizada no ambiente
institucional. O extra Playwright não está instalado neste ambiente. Não foram
incluídos URLs, credenciais, perfis de navegador nem dados reais.

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

As decisões operacionais e a configuração de execução estão em
[`docs/operacao.md`](docs/operacao.md).


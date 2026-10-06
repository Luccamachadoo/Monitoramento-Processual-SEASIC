# Operação — Monitoramento Processual SEASIC

## Limites desta entrega

O pacote ainda é uma base local para desenvolvimento. `demo` consulta apenas
dados sintéticos; `run` não acessa sistemas institucionais. Este projeto Replit
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
- **PARADO:** data de último movimento atingiu o limite em dias corridos
  definido na configuração.

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

Já existe um fluxo configurável `login-edoc` / `run-edoc`, mas a configuração de
exemplo está desativada e não tem endereço nem seletores. Antes de habilitá-lo:

1. obtenha ciência/autorização da ASSTI/STI e confirme a conta de consulta;
2. execute em máquina institucional com navegador visível e terminal interativo;
3. mapeie e valide os seletores para busca, resultado, unidades, andamento, data,
   processo não localizado e sessão expirada;
4. configure URL e seletores no `config.toml` local (nunca no arquivo de exemplo
   versionado). Como a consulta e os detalhes são telas separadas, preencha
   `result_row`, `process_number_cell`, `detail_link` e `detail_ready` para abrir
   apenas o detalhe da linha cujo número corresponde exatamente ao processo
   consultado. A URL inicial deve abrir a tela de busca após a autenticação;
5. se necessário, use `login-edoc` para abrir o perfil local e autenticar-se na
   janela oficial. Esse comando precisa apenas de URL e `enabled = true`; ele
   não lê nem preenche usuário, senha, MFA ou CAPTCHA;
6. valide um processo conhecido antes de qualquer lote.

`run-edoc` consulta apenas processos e-DOC ativos, um por vez, aplicando o
intervalo e o teto de processos do TOML. Se a sessão não estiver pronta ou o
layout não corresponder aos seletores, registra a falha sem substituir a última
fotografia válida.

Para instalar o navegador no computador institucional, a equipe responsável
precisa aprovar e executar a instalação do extra Playwright e do Chromium. Não
copie o perfil persistente para o Replit nem para pastas sincronizadas.

Antes de lote, validar manualmente de 8 a 12 processos durante cinco dias úteis,
conforme o critério do projeto. O lote não deve atualizar planilhas até essa
validação terminar.

## Atualização de planilhas

Ainda não implementada. O formato `.xlsx` versus planilha em nuvem precisa ser
decidido antes da integração. A saída futura deve atualizar apenas campos
gerenciados pelo robô, usar a chave sistema+número e mostrar horário da última
execução bem-sucedida e contagem de sucessos/falhas. O histórico oficial desta
ferramenta continuará no banco, não na planilha.


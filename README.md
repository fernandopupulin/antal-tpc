# antl

O antl recebe diffs da base de código analisada e gera um plano de execução para o pytest. A análise cruza as linhas alteradas com o mapa de cobertura da revisão base e distribui os testes selecionados de acordo com suas durações. O modo `--verify` também executa verificações direcionadas durante a análise para reduzir o plano quando isso compensa.

A imagem usa a revisão `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. Cada diff deve ser gerado contra essa revisão. O mapa e o inventário precisam ser atualizados antes de apontar a ferramenta para outra revisão ou outro projeto.

## Gerar um plano

Construa a imagem na pasta extraída do antl com o Docker em modo Linux. A construção baixa a revisão base e as dependências.

```powershell
docker build --platform linux/amd64 -t antl:local .
```

No checkout que contém a mudança, gere o diff e rode o planejador.

```powershell
New-Item -ItemType Directory -Force patches | Out-Null
git diff --binary --full-index eb11aec4be8cb2122afe713a4816662c3f0fb2fa HEAD --output=patches/change.patch
$patchDir = (Resolve-Path patches).Path
$plan = docker run --rm --network none --cpus 2 --memory 4g --pids-limit 256 --read-only `
  --tmpfs /tmp:rw,size=256m,mode=1777 --cap-drop ALL --security-opt no-new-privileges `
  --user 65534:65534 --env HOME=/tmp `
  --mount "type=bind,source=$patchDir,target=/input,readonly" `
  antl:local --verify /input/change.patch
if ($LASTEXITCODE -ne 0) { throw "Falha ao gerar o plano do antl" }
$plan | Set-Content -Encoding utf8 plan.json
```

Passe vários caminhos para analisar diffs independentes no mesmo lote. Sem `--verify`, a análise é estática. No modo verificado, o contêiner pode executar código alterado. Mantenha o isolamento do comando acima e monte apenas os diffs.

Para usar a saída JSONL do motor em lote, monte os diffs em `/data/commits` e o catálogo em `/data/tests.json`, e execute a imagem sem argumentos. O catálogo usa números consecutivos para identificar os testes.

## Usar no CI

Cada item em `plans` corresponde a um diff. Rode cada vetor de argumentos de `commands` em um job separado com o checkout da revisão que gerou aquele diff e as dependências de teste instaladas. Passe o vetor diretamente ao executor do processo, com qualquer código de saída diferente de zero falhando o job. `workers` lista os IDs de teste agrupados e `test_count` informa quantos foram selecionados.

Use `--max-workers` para ajustar o limite de jobs concorrentes e `--worker-cost-ms` para indicar quanto custa abrir mais um job. O planejador minimiza a duração estimada do grupo mais lento somada ao custo de todos os jobs. Os padrões são até oito jobs e 2.000 ms por job.

- `plano` com comandos executa os grupos selecionados.
- `plano` com lista de comandos vazia indica que a análise não selecionou testes.
- `suite-completa` manda rodar `python -m pytest -q tests`. Esse status cobre erros de aplicação do diff, alterações na configuração de testes e falhas do planejador. O campo `reason` registra o motivo.

Agende também a suíte completa em seu fluxo de integração. A seleção por impacto depende da cobertura coletada na revisão base.

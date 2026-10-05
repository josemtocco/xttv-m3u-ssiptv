# XTTV → M3U para SS IPTV — versão definitiva

Projeto GitHub Actions para coletar o catálogo público do **XTTV**, percorrer os gêneros e paginações, descobrir dados carregados por JavaScript/API, abrir as estações e capturar URLs públicas de reprodução expostas pela própria página, gerando `lista.m3u` para SS IPTV.

## O que esta versão faz

- abre `https://xttv.com.br/generos`;
- descobre as categorias/gêneros disponíveis;
- percorre cada categoria;
- percorre todas as páginas disponíveis, procurando o controle **Próxima/Next**;
- captura respostas JSON feitas pelo navegador para descobrir estações quando elas não aparecem como links no HTML;
- abre cada página `/station/...` encontrada;
- observa requisições de rede durante a abertura e após clicar em **Tocar / Play / Ao Vivo / Assistir**;
- coleta URLs públicas HLS (`.m3u8`) e outros endpoints de mídia expostos pela página;
- grava nome, logo e gênero;
- gera M3U UTF-8 com `tvg-id`, `tvg-name`, `tvg-logo` e `group-title`;
- mantém estado em `canais.json`;
- não apaga imediatamente um canal por uma falha temporária: são necessárias `FAILS_TO_REMOVE` falhas consecutivas;
- executa automaticamente a cada 6 horas;
- usa `ubuntu-24.04`, evitando a futura mudança automática do `ubuntu-latest`.

## Arquivos

- `gerar_m3u.py` — coletor completo e gerador M3U.
- `canais.json` — estado persistente dos canais válidos.
- `lista.m3u` — playlist para SS IPTV.
- `requirements.txt` — Playwright.
- `.github/workflows/atualizar.yml` — atualização automática.
- `xttv_debug/` — inventário de respostas de API/rede gerado durante cada execução.

## Publicar no GitHub

Coloque os arquivos na raiz do repositório e mantenha `.github/workflows/atualizar.yml` no local indicado. Faça o primeiro `Run workflow` manualmente em **Actions → Atualizar M3U XTTV**.

Depois, se o GitHub Pages estiver habilitado para `main/root`, a playlist poderá ser usada em uma URL como:

`https://SEU_USUARIO.github.io/SEU_REPOSITORIO/lista.m3u`

## Atualização

O cron é `0 */6 * * *`, portanto roda a cada 6 horas em UTC. Também é possível executar manualmente.

## Segurança e limites

O projeto trabalha somente com páginas e recursos públicos expostos pelo XTTV. Não tenta contornar login, DRM, paywall ou controles de acesso.

## Diagnóstico

A pasta `xttv_debug` contém `api_respostas.json` e `streams_rede.json`. Se uma mudança no XTTV impedir a descoberta, esses arquivos mostram quais endpoints foram observados na execução e permitem adaptar o coletor sem voltar ao método antigo de procurar apenas links no HTML.

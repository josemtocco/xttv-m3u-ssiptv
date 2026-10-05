# XTTV → M3U para SS IPTV

Scraper para `https://xttv.com.br/generos`.

## Como funciona

1. Abre a página **Gêneros**.
2. Descobre os gêneros disponíveis.
3. Entra em cada gênero, um por vez.
4. Analisa **100% da página atual** e coleta todos os links de estações.
5. Procura o controle **Próxima/Next** e **clica nele**.
6. Confirma que a página/conteúdo mudou antes de continuar.
7. Repete até não existir mais próxima página.
8. Só depois passa ao próximo gênero.
9. Abre cada estação e tenta acionar **Tocar/Play/Ao Vivo** para capturar streams carregados dinamicamente.
10. Gera `lista.m3u` com `tvg-name` e nome de exibição iguais ao nome da estação.
11. Mantém o estado em `canais.json`, preservando temporariamente canais que falharem em uma execução e removendo-os após `FAILS_TO_REMOVE` falhas consecutivas.

### Paginação

A paginação é deliberadamente sequencial. O scraper não assume que `?page=2`, `?page=3` etc. sejam suficientes: ele deve analisar a página visível e acionar o controle de próxima página do próprio site. Também existe proteção contra página repetida e limite de segurança configurável.

## GitHub Actions

Atualização automática a cada 6 horas e também manual pelo botão **Run workflow**.

## Arquivos

- `gerar_m3u.py` — scraper e gerador da M3U
- `lista.m3u` — playlist gerada
- `canais.json` — estado incremental
- `.github/workflows/atualizar.yml` — atualização automática
- `requirements.txt` — dependência Playwright

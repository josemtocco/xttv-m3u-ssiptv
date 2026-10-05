# XTTV → M3U para SS IPTV

Projeto GitHub para coletar os canais disponíveis no XTTV por **todos os gêneros e todas as páginas de cada gênero**, gerar uma playlist M3U compatível com SS IPTV e atualizar automaticamente a cada 6 horas.

## Como a coleta funciona

1. Abre `https://xttv.com.br/generos`.
2. Descobre as categorias reais e elimina URLs duplicadas/variações de codificação.
3. Abre uma categoria.
4. Aguarda o conteúdo dinâmico carregar e força o carregamento de cartões lazy-load.
5. Coleta as estações da página atual em `href`, `data-*`, HTML/JSON renderizado e recursos carregados.
6. Procura o controle **Próxima/Next/Seguinte** e clica nele.
7. Confirma que a página/conteúdo mudou.
8. Repete a coleta até não existir mais Próxima, até a paginação repetir ou atingir o limite de segurança.
9. Só depois passa para a próxima categoria.
10. Abre cada estação, tenta iniciar **Tocar/Play/Ao Vivo** e captura URLs de transmissão carregadas pelo navegador.
11. Gera `lista.m3u` com `tvg-name` e nome exibido iguais ao nome do canal e `group-title` da categoria.

### Proteção contra falhas

- Se uma categoria retornar zero estações por falha de carregamento, isso é registrado.
- Se **todas** as categorias retornarem zero, a execução falha e a playlist anterior não é apagada.
- Canais antigos são preservados temporariamente por `FAILS_TO_REMOVE` execuções antes de serem removidos.
- Páginas repetidas não causam loop infinito.

## Arquivos

- `gerar_m3u.py` — coletor e gerador da playlist.
- `canais.json` — estado incremental.
- `lista.m3u` — playlist final.
- `.github/workflows/atualizar.yml` — atualização automática.
- `requirements.txt` — dependências.

## Execução manual

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium
python gerar_m3u.py
```

## Configurações opcionais

- `MAX_CATEGORY_PAGES=200`
- `CONCURRENCY=4`
- `FAILS_TO_REMOVE=2`
- `PAGE_TIMEOUT_MS=60000`
- `WAIT_CATEGORY_MS=3500`
- `WAIT_NEXT_MS=1800`

O projeto não substitui uma playlist válida quando uma execução sofre uma falha de descoberta.

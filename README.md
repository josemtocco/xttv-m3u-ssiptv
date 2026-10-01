# XTTV → M3U para SS IPTV

Projeto para gerar automaticamente uma lista M3U a partir do catálogo público do XTTV (`https://xttv.com.br/explorar`), pronta para uso no SS IPTV.

## O que faz

- varre o catálogo e paginações, sem limitar a descoberta aos primeiros 8 canais;
- abre as páginas individuais das estações e tenta localizar os streams públicos expostos pela própria página;
- usa o nome da estação e as categorias/gêneros encontrados no site;
- valida os streams antes de publicar;
- mantém canais anteriormente válidos quando uma execução sofre uma falha temporária de descoberta;
- remove canais que falham consecutivamente conforme `FAILS_TO_REMOVE`;
- adiciona automaticamente canais novos;
- gera `lista.m3u` em UTF-8, com `tvg-name`, `tvg-logo`, `group-title` e `tvg-id`;
- ordena a lista por categoria e nome, formato adequado para SS IPTV;
- roda automaticamente a cada 6 horas pelo GitHub Actions.

> O coletor não tenta contornar login, DRM, paywall ou qualquer mecanismo de controle de acesso. Ele usa somente URLs públicas encontradas no catálogo/páginas da fonte.

## Arquivos

- `gerar_m3u.py` — coletor e gerador.
- `requirements.txt` — dependências.
- `lista.m3u` — lista publicada para o SS IPTV.
- `canais.json` — estado persistente usado para evitar perda de canais por falhas temporárias.
- `.github/workflows/atualizar.yml` — execução automática a cada 6 horas.

## GitHub Pages

Ative **Settings → Pages → Deploy from a branch → main/root**. Depois, a URL da lista será:

`https://SEU_USUARIO.github.io/SEU_REPOSITORIO/lista.m3u`

Essa URL pode ser cadastrada no SS IPTV como lista externa.

## Execução local

```bash
python -m pip install -r requirements.txt
python gerar_m3u.py
```

## Configurações por variável de ambiente

- `VALIDATE_STREAMS=true` — valida os streams (`false` para apenas descobrir).
- `FAILS_TO_REMOVE=2` — número de falhas consecutivas para remover um canal.
- `MAX_PAGES=250` — limite de páginas/rotas de catálogo exploradas.
- `MAX_STATIONS=10000` — limite de estações descobertas.
- `VALIDATION_WORKERS=12` — reservado para futuras versões paralelas; a versão atual usa processamento sequencial para reduzir carga na fonte.
- `HTTP_TIMEOUT=15` — timeout das páginas.
- `STREAM_TIMEOUT=12` — timeout da validação dos streams.

## Observação sobre as 6 horas

O agendamento está configurado para `00:00, 06:00, 12:00 e 18:00 UTC`. O GitHub Actions usa UTC; isso equivale a 21:00, 03:00, 09:00 e 15:00 no horário de Brasília durante UTC-3. A execução manual também fica disponível em **Actions → Atualizar M3U → Run workflow**.

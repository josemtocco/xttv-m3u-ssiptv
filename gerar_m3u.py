import asyncio, json, os, re, time, hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

BASE = os.getenv('XTTV_BASE', 'https://xttv.com.br').rstrip('/')
GENRES_URL = f'{BASE}/generos'
OUT = Path('lista.m3u')
STATE = Path('canais.json')
DEBUG = Path('xttv_debug')
DEBUG.mkdir(exist_ok=True)

MAX_CATEGORY_PAGES = int(os.getenv('MAX_CATEGORY_PAGES', '200'))
MAX_STATIONS = int(os.getenv('MAX_STATIONS', '10000'))
MAX_STREAM_CANDIDATES = int(os.getenv('MAX_STREAM_CANDIDATES', '20'))
PAGE_TIMEOUT = int(os.getenv('PAGE_TIMEOUT_MS', '45000'))
STREAM_WAIT = int(os.getenv('STREAM_WAIT_MS', '10000'))
HEADLESS = os.getenv('HEADLESS', '1') != '0'
FAILS_TO_REMOVE = int(os.getenv('FAILS_TO_REMOVE', '2'))
VALIDATE_STREAMS = os.getenv('VALIDATE_STREAMS', 'true').lower() != 'false'

STREAM_RE = re.compile(r'(?i)https?://[^\s"\'<>]+(?:m3u8|mpd)(?:\?[^\s"\'<>]*)?')
STATION_RE = re.compile(r'https?://(?:www\.)?xttv\.com\.br/station/[A-Za-z0-9._~:/?#\[\]@!$&\'()*+,;=%-]+', re.I)


def clean(s):
    return re.sub(r'\s+', ' ', str(s or '')).strip()


def slug(s):
    s = clean(s).lower()
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s or 'xttv'


def norm_url(u):
    if not u: return ''
    u = urljoin(BASE + '/', str(u))
    p = urlparse(u)
    return urlunparse((p.scheme, p.netloc, p.path, '', p.query, ''))


def is_stream(u):
    u = str(u or '')
    return bool(re.search(r'(?i)(\.m3u8(?:$|\?)|\.mpd(?:$|\?)|/hls(?:/|\?)|/stream(?:/|\?)|/live(?:/|\?))', u))


def is_station(u):
    return bool(re.search(r'xttv\.com\.br/station/', u, re.I))


def plausible_name(s):
    s = clean(s)
    if not s or len(s) > 180: return False
    bad = {'tocar','play','ao vivo','assistir','carregar mais','próxima','proxima','voltar','login','buscar'}
    return s.lower() not in bad and len(s) >= 2


def extract_strings(obj, key=''):
    out=[]
    if isinstance(obj, dict):
        for k,v in obj.items():
            out.extend(extract_strings(v, str(k)))
    elif isinstance(obj, list):
        for v in obj: out.extend(extract_strings(v, key))
    elif isinstance(obj, str):
        if obj.startswith('http'): out.append((key,obj))
    return out


def walk_records(obj, category_hint=''):
    """Find likely station records in arbitrary API JSON."""
    found=[]
    if isinstance(obj, dict):
        keys={str(k).lower() for k in obj}
        station_url=''
        for k,v in obj.items():
            if isinstance(v,str) and is_station(v): station_url=norm_url(v)
        # nested slug/id/path fields
        if not station_url:
            for k,v in obj.items():
                if isinstance(v,str) and ('station/' in v or k.lower() in {'url','link','href','path'}):
                    cand=norm_url(v)
                    if is_station(cand): station_url=cand
        name=''
        for k in ('name','station_name','title','channel_name','display_name','nome','label'):
            if k in obj and isinstance(obj[k],str) and plausible_name(obj[k]): name=clean(obj[k]); break
        logo=''
        for k in ('logo','logo_url','image','image_url','thumbnail','thumbnail_url','stream_icon','icon'):
            if k in obj and isinstance(obj[k],str) and obj[k].startswith('http'):
                logo=norm_url(obj[k]); break
        genres=[]
        for k in ('genres','genre','categories','category','tags'):
            v=obj.get(k)
            if isinstance(v,str): genres += [clean(x) for x in re.split(r'[,|;/]',v) if clean(x)]
            elif isinstance(v,list):
                for x in v:
                    if isinstance(x,str): genres.append(clean(x))
                    elif isinstance(x,dict):
                        for kk in ('name','title','label'):
                            if isinstance(x.get(kk),str): genres.append(clean(x[kk])); break
        streams=[]
        for k,v in extract_strings(obj):
            if is_stream(v): streams.append(norm_url(v))
        if station_url or (name and any(k in keys for k in {'stream','stream_url','streamurl','hls','m3u8','url','link'})):
            found.append({'url':station_url,'name':name,'logo':logo,'genres':list(dict.fromkeys(genres+[category_hint] if category_hint else genres)),'streams':list(dict.fromkeys(streams))})
        for v in obj.values(): found.extend(walk_records(v, category_hint))
    elif isinstance(obj,list):
        for v in obj: found.extend(walk_records(v, category_hint))
    return found


async def main():
    state = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    stations={}
    api_log=[]
    stream_events=[]

    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=HEADLESS, args=['--disable-dev-shm-usage','--no-sandbox'])
        context=await browser.new_context(viewport={'width':1440,'height':1000}, user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36')
        page=await context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)

        async def on_response(resp):
            u=resp.url
            ct=(resp.headers.get('content-type') or '').lower()
            if is_stream(u):
                stream_events.append({'url':u,'status':resp.status,'type':'response','content_type':ct})
            if 'xttv.com.br' not in u: return
            if 'json' not in ct and not any(x in u.lower() for x in ('api','graphql','trpc','station','genre','gener')): return
            rec={'url':u,'status':resp.status,'content_type':ct}
            try:
                txt=await resp.text()
                rec['body_hash']=hashlib.sha1(txt.encode('utf-8','ignore')).hexdigest()
                if len(txt)<2_000_000:
                    data=json.loads(txt)
                    recs=walk_records(data)
                    for r in recs:
                        if r.get('url'):
                            stations.setdefault(r['url'], {'url':r['url'],'name':r['name'],'logo':r['logo'],'genres':r['genres'],'streams':r['streams']})
                            if r['name'] and not stations[r['url']].get('name'): stations[r['url']]['name']=r['name']
                        elif r.get('name') and r.get('streams'):
                            key='stream:'+hashlib.sha1((r['name']+'|'+r['streams'][0]).encode()).hexdigest()
                            stations.setdefault(key, {'url':'','name':r['name'],'logo':r['logo'],'genres':r['genres'],'streams':r['streams']})
                    rec['records']=len(recs)
            except Exception:
                pass
            api_log.append(rec)
        page.on('response', on_response)

        print(f'XTTV definitivo | {GENRES_URL}')
        await page.goto(GENRES_URL, wait_until='domcontentloaded', timeout=PAGE_TIMEOUT)
        await page.wait_for_timeout(2500)
        await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
        await page.wait_for_timeout(1000)

        cats=await page.locator('a[href],button').evaluate_all('els => els.map(e => ({t:(e.innerText||e.textContent||\'\').trim(),h:e.href||\'\'}))')
        categories=[]; seen=set()
        for x in cats:
            u=norm_url(x.get('h'))
            if u and '/generos' in u and u.rstrip('/') != GENRES_URL.rstrip('/') and u not in seen:
                seen.add(u); categories.append((clean(x.get('t')),u))
        # fallback: any same-origin link whose path is a genre-like route
        if not categories:
            for x in cats:
                u=norm_url(x.get('h'))
                if u.startswith(BASE) and u not in seen and x.get('t'):
                    seen.add(u); categories.append((clean(x['t']),u))
        print(f'Categorias descobertas: {len(categories)}')

        for ci,(cat,caturl) in enumerate(categories,1):
            print(f'[{ci}/{len(categories)}] {cat} -> {caturl}')
            current=caturl; fingerprints=set()
            for pn in range(1,MAX_CATEGORY_PAGES+1):
                before=len(stations)
                try:
                    await page.goto(current, wait_until='domcontentloaded', timeout=PAGE_TIMEOUT)
                    await page.wait_for_timeout(1800)
                    for _ in range(3):
                        await page.mouse.wheel(0, 1800); await page.wait_for_timeout(500)
                    html=await page.content()
                    fp=hashlib.sha1(re.sub(r'\d+','',html).encode()).hexdigest()
                    if fp in fingerprints: print('  página repetida; fim.'); break
                    fingerprints.add(fp)
                    # Parse rendered HTML for station hrefs as a bonus.
                    for u in STATION_RE.findall(html):
                        stations.setdefault(norm_url(u), {'url':norm_url(u),'name':'','logo':'','genres':[cat],'streams':[]})
                    # Inspect anchors/buttons; if a station is represented directly, collect it.
                    links=await page.locator('a[href]').evaluate_all('els=>els.map(e=>({t:(e.innerText||e.textContent||\'\').trim(),h:e.href||\'\'}))')
                    for x in links:
                        u=norm_url(x['h'])
                        if is_station(u):
                            rec=stations.setdefault(u, {'url':u,'name':clean(x['t']),'logo':'','genres':[cat],'streams':[]})
                            if plausible_name(x['t']) and not rec.get('name'): rec['name']=clean(x['t'])
                            if cat not in rec['genres']: rec['genres'].append(cat)
                    print(f'  página {pn}: estações/API={len(stations)-before:+d}, total={len(stations)}')

                    # Find next page from hrefs first.
                    nxt=None
                    for x in links:
                        t=clean(x['t']).lower()
                        u=norm_url(x['h'])
                        if u and (t in {'próxima','proxima','next','›','→'} or 'próxima' in t or 'proxima' in t):
                            if u != current: nxt=u; break
                    if not nxt:
                        # common query pagination: increment page if current page exposes a page number or API-backed paginator.
                        parsed=urlparse(current); q=parse_qs(parsed.query)
                        if 'page' in q:
                            try:
                                n=int(q['page'][0])+1; q['page']=[str(n)]; nxt=urlunparse((parsed.scheme,parsed.netloc,parsed.path,parsed.params,urlencode(q,doseq=True),''))
                            except: pass
                    if not nxt: break
                    current=nxt
                except Exception as e:
                    print(f'  erro página {pn}: {e}')
                    break
            if len(stations)>=MAX_STATIONS: break

        # Persist raw network inventory for troubleshooting.
        (DEBUG/'api_respostas.json').write_text(json.dumps(api_log,ensure_ascii=False,indent=2),encoding='utf-8')
        (DEBUG/'streams_rede.json').write_text(json.dumps(stream_events,ensure_ascii=False,indent=2),encoding='utf-8')

        # Station inspection. Network capture is the primary stream discovery mechanism.
        station_items=list(stations.values())[:MAX_STATIONS]
        print(f'Estações para inspeção: {len(station_items)}')
        for i,rec in enumerate(station_items,1):
            if not rec.get('url'): continue
            sp=await context.new_page(); sp.set_default_timeout(PAGE_TIMEOUT)
            found=list(rec.get('streams') or [])
            local_events=[]
            async def sr(resp):
                u=resp.url
                if is_stream(u): local_events.append(u)
                if 'xttv.com.br' in u and any(k in u.lower() for k in ('stream','play','player','media','hls','m3u8')):
                    if is_stream(u): local_events.append(u)
            sp.on('response', sr)
            try:
                await sp.goto(rec['url'],wait_until='domcontentloaded',timeout=PAGE_TIMEOUT)
                await sp.wait_for_timeout(1800)
                title=clean(await sp.locator('h1').first.text_content()) if await sp.locator('h1').count() else ''
                if title and not rec.get('name'): rec['name']=title
                # capture links / source attributes
                html=await sp.content()
                found += [norm_url(x) for x in STREAM_RE.findall(html)]
                # click likely playback controls, at most a few
                selectors=['button','[role="button"]','a']
                clicked=0
                for sel in selectors:
                    els=sp.locator(sel)
                    n=min(await els.count(),15)
                    for j in range(n):
                        try:
                            txt=clean(await els.nth(j).inner_text())
                            if re.search(r'(?i)^(tocar|play|ao vivo|assistir|ouvir)$',txt):
                                await els.nth(j).click(timeout=2500)
                                clicked+=1
                                await sp.wait_for_timeout(STREAM_WAIT)
                                break
                        except Exception: pass
                    if clicked: break
                found += [norm_url(x) for x in local_events]
                found=list(dict.fromkeys(x for x in found if is_stream(x)))
                if found: rec['streams']=found[:MAX_STREAM_CANDIDATES]
                if not rec.get('name'):
                    rec['name']=clean(await sp.title())
            except Exception as e:
                rec['inspect_error']=str(e)[:300]
            finally:
                await sp.close()
            if i%25==0 or i==len(station_items): print(f'  inspeção {i}/{len(station_items)}')

        await browser.close()

    # Enrich names/categories from existing state when current discovery is incomplete.
    now=int(time.time()); valid={}
    for key,rec in stations.items():
        if not rec.get('name'): continue
        name=clean(rec['name'])
        genres=[clean(x) for x in (rec.get('genres') or []) if clean(x)]
        streams=list(dict.fromkeys(rec.get('streams') or []))
        # only stations with a real stream become publishable; old valid records survive temporary failure.
        if streams:
            sid=rec.get('url') or ('stream:'+hashlib.sha1((name+'|'+streams[0]).encode()).hexdigest())
            old=state.get(sid,{})
            valid[sid]={**old, **rec, 'name':name, 'genres':genres or old.get('genres') or ['XTTV'], 'streams':streams, 'fail_count':0, 'last_ok':now}
        else:
            sid=rec.get('url')
            if sid in state:
                old=state[sid]; old['fail_count']=int(old.get('fail_count',0))+1
                if old['fail_count'] < FAILS_TO_REMOVE and old.get('streams'):
                    valid[sid]=old

    # Preserve old channels if discovery was globally empty or partially broken.
    if not valid and state:
        valid=state
        print('Nenhum stream novo validado; estado anterior preservado integralmente.')
    else:
        for sid,old in state.items():
            if sid not in valid and old.get('streams') and int(old.get('fail_count',0))+1 < FAILS_TO_REMOVE:
                old['fail_count']=int(old.get('fail_count',0))+1; valid[sid]=old

    if not valid:
        raise SystemExit('Nenhum canal com stream público foi encontrado e não existe estado anterior.')

    STATE.write_text(json.dumps(valid,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8')
    lines=['#EXTM3U']
    for sid,rec in sorted(valid.items(), key=lambda kv: ((kv[1].get('genres') or ['ZZZ'])[0].lower(), kv[1].get('name','').lower())):
        name=clean(rec.get('name')) or 'Canal XTTV'
        group=clean((rec.get('genres') or ['XTTV'])[0]) or 'XTTV'
        logo=rec.get('logo') or ''
        attrs=[f'tvg-id="{slug(name)}"',f'tvg-name="{name.replace(chr(34), chr(39))}"']
        if logo: attrs.append(f'tvg-logo="{logo}"')
        attrs.append(f'group-title="{group.replace(chr(34), chr(39))}"')
        stream=rec['streams'][0]
        lines.append('#EXTINF:-1 '+ ' '.join(attrs) + ',' + name)
        lines.append(stream)
    OUT.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(f'FINAL: {len(valid)} canais -> {OUT}')

if __name__=='__main__':
    asyncio.run(main())

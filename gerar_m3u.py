import asyncio, json, logging, os, re, time, html as htmlmod
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
from playwright.async_api import async_playwright

BASE='https://xttv.com.br'
GENRES_URL=f'{BASE}/generos'
STATE=Path('canais.json')
M3U=Path('lista.m3u')
MAX_CATEGORY_PAGES=int(os.getenv('MAX_CATEGORY_PAGES','200'))
CONCURRENCY=int(os.getenv('CONCURRENCY','4'))
HEADLESS=os.getenv('HEADLESS','1') != '0'
FAILS_TO_REMOVE=int(os.getenv('FAILS_TO_REMOVE','2'))
TIMEOUT=int(os.getenv('PAGE_TIMEOUT_MS','60000'))
WAIT_CATEGORY_MS=int(os.getenv('WAIT_CATEGORY_MS','3500'))
WAIT_NEXT_MS=int(os.getenv('WAIT_NEXT_MS','1800'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log=logging.getLogger('xttv')

STATION_PATH_RE=re.compile(r'(?:https?:\/\/xttv\.com\.br)?\/station\/([A-Za-z0-9._~%+\-]+)',re.I)
STATION_PATH_RE2=re.compile(r'(?:https?:)?//xttv\.com\.br/station/[A-Za-z0-9._~/%+\-]+',re.I)
STREAM_RE=re.compile(r'https?://[^\s"\'<>\\]+(?:\.m3u8(?:\?[^\s"\'<>\\]*)?|\.mpd(?:\?[^\s"\'<>\\]*)?|\.mp3(?:\?[^\s"\'<>\\]*)?|\.aac(?:\?[^\s"\'<>\\]*)?|\.aacp(?:\?[^\s"\'<>\\]*)?)',re.I)
NEXT_WORDS=('próxima','proxima','próximo','proximo','next','seguinte','›','»','→')
PREV_WORDS=('anterior','previous','voltar','back')
PLAY_WORDS=('tocar','play','ao vivo','ouvir','assistir','iniciar')


def clean_url(u): return (u or '').replace('\\/','/').strip().rstrip('.,);]}>\'"')
def norm_url(u):
    if not u: return ''
    u=htmlmod.unescape(u).replace('\\/','/')
    if u.startswith('//'): return 'https:'+u
    return urljoin(BASE,u)
def station_key(u): return norm_url(u).rstrip('/').lower()
def looks_station(u):
    p=urlparse(norm_url(u)); return p.netloc.endswith('xttv.com.br') and p.path.lower().startswith('/station/')
def looks_stream(u):
    x=(u or '').lower()
    return any(k in x for k in ('.m3u8','.mpd','.mp3','.aac','.aacp')) or any(k in x for k in ('stream','live','playlist','hls'))
def esc(s): return str(s or '').replace('\\',' ').replace('"','').replace('\r',' ').replace('\n',' ').strip()

def load_state():
    if not STATE.exists(): return {}
    try: return json.loads(STATE.read_text(encoding='utf-8'))
    except Exception: return {}
def save_state(data):
    tmp=STATE.with_suffix('.tmp'); tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8'); tmp.replace(STATE)

async def wait_rendered(page, ms=WAIT_CATEGORY_MS):
    try: await page.wait_for_load_state('domcontentloaded',timeout=15000)
    except Exception: pass
    try: await page.wait_for_load_state('networkidle',timeout=10000)
    except Exception: pass
    await page.wait_for_timeout(ms)

async def station_links_from_page(page):
    out=set()
    # 1) All href/src/data-* attributes, not only anchors.
    try:
        vals=await page.locator('a,button,[role="link"],[role="button"],[data-href],[data-url],[data-link],[data-route],*').evaluate_all("""
        els => els.flatMap(e => Array.from(e.attributes || []).map(a => a.value)).filter(Boolean)
        """)
        for v in vals:
            for m in STATION_PATH_RE2.findall(v): out.add(clean_url(norm_url(m)))
            for m in STATION_PATH_RE.findall(v): out.add(f'{BASE}/station/{m}')
    except Exception: pass
    # 2) Rendered DOM / JSON / escaped HTML.
    try:
        raw=await page.content()
        raw=htmlmod.unescape(raw).replace('\\/','/')
        for m in STATION_PATH_RE2.findall(raw): out.add(clean_url(m))
        for m in STATION_PATH_RE.findall(raw): out.add(f'{BASE}/station/{m}')
    except Exception: pass
    # 3) Performance resource URLs sometimes expose an API endpoint containing station data.
    try:
        resources=await page.evaluate("performance.getEntriesByType('resource').map(x=>x.name)")
        for v in resources:
            for m in STATION_PATH_RE2.findall(v): out.add(clean_url(m))
            for m in STATION_PATH_RE.findall(v): out.add(f'{BASE}/station/{m}')
    except Exception: pass
    return {u for u in out if looks_station(u)}

async def discover_categories(page):
    await page.goto(GENRES_URL,wait_until='domcontentloaded',timeout=TIMEOUT); await wait_rendered(page,3000)
    cats={}
    for _ in range(10):
        try: await page.mouse.wheel(0,1800)
        except Exception: pass
        await page.wait_for_timeout(400)
    try:
        elements=await page.locator('a,button,[role="link"],[role="button"]').all()
        for el in elements:
            try:
                txt=re.sub(r'\s+',' ',(await el.inner_text()).strip())
                if not txt or len(txt)>100: continue
                href=await el.get_attribute('href') or ''
                vals=[href]
                for a in ('data-href','data-url','data-link','data-route','onclick'):
                    vals.append(await el.get_attribute(a) or '')
                for raw in vals:
                    for m in re.findall(r'https?://xttv\.com\.br[^\s"\'<>]+|/genero[s]?/[^\s"\'<>]+',raw,re.I):
                        u=clean_url(norm_url(m)); path=urlparse(u).path.lower()
                        if '/genero/' in path and u.rstrip('/')!=GENRES_URL.rstrip('/'):
                            cats[u]=re.sub(r'\s+',' ',txt)
            except Exception: pass
    except Exception: pass
    # Fallback: extract every genre route from rendered source and canonicalize by URL.
    try:
        raw=htmlmod.unescape(await page.content()).replace('\\/','/')
        for m in re.findall(r'(?:https?://xttv\.com\.br)?/genero/[^\s"\'<>]+',raw,re.I):
            u=clean_url(norm_url(m)); path=unquote(urlparse(u).path)
            if u.rstrip('/')!=GENRES_URL.rstrip('/'):
                label=unquote(path.rstrip('/').split('/')[-1]).replace('-',' ')
                cats.setdefault(u,label.title())
    except Exception: pass
    # Canonicalize duplicate URL variants; the user's log showed malformed encoded labels as duplicates.
    by_url={}
    for u,n in cats.items(): by_url[u]=n
    log.info('Categorias encontradas: %d',len(by_url))
    for u,n in by_url.items(): log.info('Categoria: %s -> %s',n,u)
    return {n:u for u,n in by_url.items()}

async def disabled(el):
    try:
        if await el.is_disabled(): return True
    except Exception: pass
    for a in ('disabled','aria-disabled','data-disabled'):
        try:
            v=await el.get_attribute(a)
            if v is not None and str(v).lower() in ('','true','1','disabled'): return True
        except Exception: pass
    try:
        c=(await el.get_attribute('class') or '').lower()
        if 'disabled' in c or 'cursor-not-allowed' in c: return True
    except Exception: pass
    return False

async def find_next(page):
    # Prefer explicit pagination controls; inspect text + accessibility metadata + href.
    candidates=[]
    try: candidates=await page.locator('a,button,[role="link"],[role="button"]').all()
    except Exception: return None
    for el in candidates:
        try:
            if await disabled(el): continue
            txt=re.sub(r'\s+',' ',(await el.inner_text()).strip()).lower()
            aria=(await el.get_attribute('aria-label') or '').lower()
            title=(await el.get_attribute('title') or '').lower()
            test=(await el.get_attribute('data-testid') or '').lower()
            href=(await el.get_attribute('href') or '').lower()
            marker=' '.join((txt,aria,title,test,href))
            if any(x in marker for x in PREV_WORDS): continue
            if any(x in marker for x in NEXT_WORDS): return el
        except Exception: pass
    return None

async def page_signature(page,links):
    try: body=await page.locator('body').inner_text()
    except Exception: body=''
    return re.sub(r'\s+',' ',body)[:5000]+'|'+'|'.join(sorted(links))

async def click_next(page,before_url,before_sig):
    el=await find_next(page)
    if not el: return False
    try: await el.scroll_into_view_if_needed()
    except Exception: pass
    try: await el.click(timeout=10000)
    except Exception:
        try: await el.click(force=True,timeout=10000)
        except Exception: return False
    await page.wait_for_timeout(WAIT_NEXT_MS)
    try: await page.wait_for_load_state('domcontentloaded',timeout=10000)
    except Exception: pass
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        await page.wait_for_timeout(700)
        links=await station_links_from_page(page); sig=await page_signature(page,links)
        if page.url!=before_url or sig!=before_sig: return True
    return False

async def collect_category(page,name,url):
    found=set(); seen=set(); pages=0
    await page.goto(url,wait_until='domcontentloaded',timeout=TIMEOUT); await wait_rendered(page)
    while pages<MAX_CATEGORY_PAGES:
        # Give dynamic station cards time to render; scroll to trigger lazy loading.
        for _ in range(8):
            try: await page.mouse.wheel(0,1400)
            except Exception: pass
            await page.wait_for_timeout(350)
        links=await station_links_from_page(page); sig=await page_signature(page,links)
        page_id=page.url+'|'+sig
        if page_id in seen:
            log.warning('Categoria %s: página repetida; encerrando.',name); break
        seen.add(page_id); pages+=1; found|=links
        log.info('Categoria %s | página %d | %d estações encontradas nesta página | %d acumuladas',name,pages,len(links),len(found))
        moved=await click_next(page,page.url,sig)
        if not moved: break
    if pages>=MAX_CATEGORY_PAGES: log.warning('Categoria %s atingiu limite de %d páginas.',name,MAX_CATEGORY_PAGES)
    log.info('Categoria %s concluída: %d páginas analisadas | %d estações únicas',name,pages,len(found))
    return found

async def station_details(browser,url,categories):
    page=await browser.new_page(); streams=set(); network=set()
    try:
        async def on_response(resp):
            try:
                if looks_stream(resp.url): network.add(clean_url(resp.url))
            except Exception: pass
        page.on('response',on_response)
        await page.goto(url,wait_until='domcontentloaded',timeout=TIMEOUT); await wait_rendered(page,1800)
        # Start playback when the site exposes it as a button/link.
        try:
            for el in await page.locator('button,a,[role="button"]').all():
                marker=' '.join([(await el.inner_text()).strip().lower(),(await el.get_attribute('aria-label') or '').lower(),(await el.get_attribute('title') or '').lower()])
                if any(w in marker for w in PLAY_WORDS) and not await disabled(el):
                    try: await el.click(timeout=7000)
                    except Exception: continue
                    await page.wait_for_timeout(3500); break
        except Exception: pass
        html=htmlmod.unescape(await page.content()).replace('\\/','/')
        streams |= {clean_url(x) for x in STREAM_RE.findall(html)}
        for pat in (r'"(?:stream|streamUrl|stream_url|url|source|src|hls|m3u8)"\s*:\s*"([^"]+)"',r"'(?:stream|streamUrl|stream_url|url|source|src|hls|m3u8)'\s*:\s*'([^']+)'"):
            for x in re.findall(pat,html,re.I):
                x=norm_url(x)
                if looks_stream(x): streams.add(clean_url(x))
        streams |= network
        title=''
        for sel in ('h1','meta[property="og:title"]'):
            try:
                if sel.startswith('meta'): title=(await page.locator(sel).get_attribute('content') or '').strip()
                else: title=(await page.locator(sel).first.inner_text()).strip()
                if title: break
            except Exception: pass
        return {'url':url,'name':re.sub(r'\s+',' ',title).strip(),'categories':sorted(categories),'streams':sorted(streams)}
    except Exception as e:
        return {'url':url,'name':'','categories':sorted(categories),'streams':[],'error':str(e)}
    finally: await page.close()

async def main():
    old=load_state(); log.info('Fonte: %s',GENRES_URL); log.info('Estado anterior: %d canais',len(old))
    async with async_playwright() as pw:
        executable=os.getenv('CHROMIUM_EXECUTABLE','/usr/bin/chromium')
        browser=await pw.chromium.launch(headless=HEADLESS,executable_path=executable,args=['--disable-dev-shm-usage','--no-sandbox'])
        page=await browser.new_page(viewport={'width':1440,'height':1100})
        cats=await discover_categories(page)
        if not cats: log.error('Nenhuma categoria encontrada.'); await browser.close(); return 2
        allstations={}
        for cname,curl in cats.items():
            urls=await collect_category(page,cname,curl)
            for u in urls:
                k=station_key(u); allstations.setdefault(k,{'url':u,'categories':set()}); allstations[k]['categories'].add(cname)
        log.info('Descoberta concluída: %d categorias | %d estações únicas',len(cats),len(allstations))
        if not allstations:
            # Never erase a valid previous playlist because of a scraper regression.
            await browser.close(); log.error('ZERO estações descobertas. Lista anterior preservada.'); return 2
        sem=asyncio.Semaphore(CONCURRENCY)
        async def one(item):
            async with sem: return await station_details(browser,item['url'],item['categories'])
        results=[]; items=list(allstations.values())
        for i in range(0,len(items),CONCURRENCY*3):
            results.extend(await asyncio.gather(*(one(x) for x in items[i:i+CONCURRENCY*3])))
            log.info('Estações analisadas: %d/%d',len(results),len(items))
        await browser.close()
    current={}; streams_total=0
    for r in results:
        valid=[u for u in r.get('streams',[]) if looks_stream(u)]
        if not valid: continue
        k=station_key(r['url']); prev=old.get(k,{})
        name=r.get('name') or prev.get('name') or unquote(k.rsplit('/',1)[-1]).replace('-',' ').title()
        current[k]={'url':r['url'],'name':name,'categories':r.get('categories') or prev.get('categories') or ['Outros'],'stream':valid[0],'logo':prev.get('logo',''),'failures':0,'last_ok':int(time.time())}; streams_total+=1
    for k,p in old.items():
        if k in current: continue
        failures=int(p.get('failures',0))+1
        if failures<FAILS_TO_REMOVE and p.get('stream'):
            q=dict(p); q['failures']=failures; current[k]=q; log.warning('Preservando temporariamente: %s (falha %d/%d)',p.get('name',k),failures,FAILS_TO_REMOVE)
    if not current: log.error('Nenhum canal utilizável; lista anterior preservada.'); return 2
    save_state(current)
    lines=['#EXTM3U','']
    for k,p in sorted(current.items(),key=lambda kv:(str((kv[1].get('categories') or ['Outros'])[0]).lower(),str(kv[1].get('name','')).lower())):
        name=esc(p.get('name') or k.rsplit('/',1)[-1]); group=esc((p.get('categories') or ['Outros'])[0]); logo=esc(p.get('logo','')); attrs=f'tvg-name="{name}" group-title="{group}"'
        if logo: attrs+=f' tvg-logo="{logo}"'
        lines += [f'#EXTINF:-1 {attrs},{name}',p['stream'],'']
    M3U.write_text('\n'.join(lines),encoding='utf-8'); log.info('Streams utilizáveis: %d | Canais na M3U: %d',streams_total,len(current)); return 0

if __name__=='__main__': raise SystemExit(asyncio.run(main()))

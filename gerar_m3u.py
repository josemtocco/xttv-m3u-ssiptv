import asyncio, json, logging, os, re, time
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

from playwright.async_api import async_playwright

BASE='https://xttv.com.br'
GENRES_URL=f'{BASE}/generos'
STATE=Path('canais.json')
M3U=Path('lista.m3u')
MAX_CATEGORY_PAGES=int(os.getenv('MAX_CATEGORY_PAGES','200'))
CONCURRENCY=int(os.getenv('CONCURRENCY','4'))
HEADLESS=os.getenv('HEADLESS','1') != '0'
FAILS_TO_REMOVE=int(os.getenv('FAILS_TO_REMOVE','2'))
TIMEOUT=int(os.getenv('PAGE_TIMEOUT_MS','45000'))
NEXT_WAIT_MS=int(os.getenv('NEXT_WAIT_MS','1200'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log=logging.getLogger('xttv')

STREAM_RE=re.compile(r'https?://[^\s"\'<>\\]+(?:\.m3u8(?:\?[^\s"\'<>\\]*)?|\.mpd(?:\?[^\s"\'<>\\]*)?|\.mp3(?:\?[^\s"\'<>\\]*)?|\.aac(?:\?[^\s"\'<>\\]*)?|\.aacp(?:\?[^\s"\'<>\\]*)?)', re.I)
STATION_RE=re.compile(r'https?://xttv\.com\.br/station/[A-Za-z0-9._~/%+-]+', re.I)
NEXT_WORDS={'próxima','proxima','próximo','proximo','next','seguinte','›','»','→'}
PLAY_WORDS=('tocar','play','ao vivo','ouvir','assistir','iniciar')


def clean_url(u):
    return (u or '').rstrip('.,);]}>\'"')

def norm_url(u):
    if not u: return ''
    if u.startswith('//'): return 'https:'+u
    return urljoin(BASE, u)

def load_state():
    if not STATE.exists(): return {}
    try: return json.loads(STATE.read_text(encoding='utf-8'))
    except Exception: return {}

def save_state(data):
    tmp=STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8')
    tmp.replace(STATE)

def esc(s):
    return str(s or '').replace('\\',' ').replace('"','').replace('\r',' ').replace('\n',' ').strip()

def station_key(url): return url.rstrip('/').lower()

def looks_station(u): return '/station/' in u and urlparse(u).netloc.endswith('xttv.com.br')

def looks_stream(u):
    x=u.lower()
    return any(k in x for k in ('.m3u8','.mpd','.mp3','.aac','.aacp')) or any(k in x for k in ('stream','live','playlist','hls'))

def page_signature(text, links):
    names=' '.join(sorted(links))
    return re.sub(r'\s+',' ',text or '')[:3000]+'|'+names

async def extract_station_links(page):
    out=set()
    for a in await page.locator('a').all():
        try:
            href=await a.get_attribute('href')
            if href:
                u=clean_url(norm_url(href))
                if looks_station(u): out.add(u)
        except Exception: pass
    try:
        html=await page.content()
        for u in STATION_RE.findall(html): out.add(clean_url(u))
    except Exception: pass
    return out

async def discover_categories(page):
    await page.goto(GENRES_URL,wait_until='domcontentloaded',timeout=TIMEOUT)
    await page.wait_for_timeout(2500)
    for _ in range(8):
        await page.mouse.wheel(0,1800); await page.wait_for_timeout(350)
    cats={}
    for el in await page.locator('a,button,[role="button"]').all():
        try:
            txt=re.sub(r'\s+',' ',(await el.inner_text()).strip())
            if not txt or len(txt)>100: continue
            href=await el.get_attribute('href')
            candidates=[]
            if href: candidates.append(href)
            for attr in ('data-href','data-url','data-link','data-route','onclick'):
                v=await el.get_attribute(attr)
                if v: candidates += re.findall(r'https?://xttv\.com\.br[^\s"\'<>]+|/[^\s"\'<>]+',v)
            for raw in candidates:
                u=clean_url(norm_url(raw))
                lowtxt=txt.lower()
                if (urlparse(u).netloc.endswith('xttv.com.br') and '/station/' not in u
                    and u.rstrip('/') != GENRES_URL.rstrip('/')
                    and not any(x in lowtxt for x in ('início','inicio','explorar','buscar','login','contato','sugerir','termos','política','politica'))):
                    # Only accept likely genre routes or buttons without a normal route.
                    path=urlparse(u).path.lower()
                    if '/genero' in path or '/generos/' in path:
                        cats.setdefault(txt,u)
        except Exception: pass
    try:
        html=await page.content()
        for m in re.finditer(r'(?:href|data-href|data-url|data-route)=["\']([^"\']+)["\']',html,re.I):
            u=clean_url(norm_url(m.group(1)))
            if '/genero' in urlparse(u).path.lower() and u.rstrip('/') != GENRES_URL.rstrip('/'):
                label=urlparse(u).path.rstrip('/').split('/')[-1].replace('-',' ').strip().title()
                if label: cats.setdefault(label,u)
    except Exception: pass
    log.info('Categorias encontradas: %d',len(cats))
    for n,u in cats.items(): log.info('Categoria: %s -> %s',n,u)
    return cats

async def element_disabled(el):
    try:
        if await el.is_disabled(): return True
    except Exception: pass
    for attr in ('disabled','aria-disabled','data-disabled'):
        try:
            v=await el.get_attribute(attr)
            if v is not None and str(v).lower() in ('','true','1','disabled'): return True
        except Exception: pass
    try:
        cls=(await el.get_attribute('class') or '').lower()
        if any(x in cls for x in ('disabled','is-disabled','cursor-not-allowed')): return True
    except Exception: pass
    return False

async def find_next_control(page):
    selectors='a,button,[role="button"]'
    best=None
    for el in await page.locator(selectors).all():
        try:
            txt=re.sub(r'\s+',' ',(await el.inner_text()).strip()).lower()
            aria=(await el.get_attribute('aria-label') or '').strip().lower()
            title=(await el.get_attribute('title') or '').strip().lower()
            data=(await el.get_attribute('data-testid') or '').strip().lower()
            marker=' '.join(x for x in (txt,aria,title,data) if x)
            if any(w in marker for w in NEXT_WORDS) and not await element_disabled(el):
                # Ignore previous/back controls.
                if any(w in marker for w in ('anterior','previous','voltar','back')): continue
                best=el; break
        except Exception: pass
    return best

async def click_next_and_wait(page, before_url, before_sig):
    nxt=await find_next_control(page)
    if not nxt: return False
    try:
        await nxt.scroll_into_view_if_needed()
    except Exception: pass
    try:
        await nxt.click(timeout=TIMEOUT)
    except Exception:
        try: await nxt.click(force=True,timeout=TIMEOUT)
        except Exception: return False
    # The site may navigate or update the listing in place.
    try: await page.wait_for_load_state('domcontentloaded',timeout=8000)
    except Exception: pass
    deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        await page.wait_for_timeout(NEXT_WAIT_MS)
        try:
            links=await extract_station_links(page)
            body=await page.locator('body').inner_text()
            sig=page_signature(body,links)
            if page.url != before_url or sig != before_sig:
                return True
        except Exception: pass
    return False

async def collect_category(page,name,url):
    found=set(); seen_pages=set(); pages=0
    await page.goto(url,wait_until='domcontentloaded',timeout=TIMEOUT)
    await page.wait_for_timeout(1200)
    while pages < MAX_CATEGORY_PAGES:
        current_url=page.url
        links=await extract_station_links(page)
        body=''
        try: body=await page.locator('body').inner_text()
        except Exception: pass
        sig=page_signature(body,links)
        # Guard against looping on the same page.
        page_id=current_url+'|'+sig
        if page_id in seen_pages:
            log.warning('Categoria %s: página repetida detectada; encerrando.',name)
            break
        seen_pages.add(page_id); pages += 1; found |= links
        log.info('Categoria %s | página %d | %d estações encontradas nesta página | %d acumuladas',name,pages,len(links),len(found))
        moved=await click_next_and_wait(page,current_url,sig)
        if not moved:
            break
    if pages >= MAX_CATEGORY_PAGES:
        log.warning('Categoria %s atingiu MAX_CATEGORY_PAGES=%d; verifique se há paginação maior.',name,MAX_CATEGORY_PAGES)
    log.info('Categoria %s concluída: %d páginas analisadas | %d estações únicas',name,pages,len(found))
    return found

async def station_details(browser,url,category_names):
    page=await browser.new_page()
    streams=set(); network=set()
    try:
        async def response_handler(resp):
            u=resp.url
            if looks_stream(u): network.add(clean_url(u))
        page.on('response',response_handler)
        await page.goto(url,wait_until='domcontentloaded',timeout=TIMEOUT)
        await page.wait_for_timeout(1200)
        # XTTV commonly exposes a Tocar/Play control. Trigger it so dynamically loaded streams appear.
        for el in await page.locator('button,a,[role="button"]').all():
            try:
                marker=' '.join([(await el.inner_text()).strip().lower(),(await el.get_attribute('aria-label') or '').lower(),(await el.get_attribute('title') or '').lower()])
                if any(w in marker for w in PLAY_WORDS):
                    if not await element_disabled(el):
                        await el.click(timeout=5000)
                        await page.wait_for_timeout(2500)
                        break
            except Exception: pass
        for _ in range(3): await page.mouse.wheel(0,1400); await page.wait_for_timeout(250)
        html=await page.content()
        streams |= {clean_url(x) for x in STREAM_RE.findall(html)}
        for pat in [r'(?:stream|streamUrl|stream_url|url|source|src|hls|m3u8)["\']?\s*[:=]\s*["\']([^"\']+)',r'(https?://[^"\'<> ]+\.(?:m3u8|mpd)(?:\?[^"\'<> ]*)?)']:
            for x in re.findall(pat,html,re.I):
                x=norm_url(x.replace('\\/','/'))
                if looks_stream(x): streams.add(clean_url(x))
        title=''
        for sel in ('h1','meta[property="og:title"]'):
            try:
                if sel.startswith('meta'): title=(await page.locator(sel).get_attribute('content') or '').strip()
                else: title=(await page.locator(sel).first.inner_text()).strip()
                if title: break
            except Exception: pass
        if not title:
            title=(await page.title()).strip()
        return {'url':url,'name':re.sub(r'\s+',' ',title).strip(),'categories':sorted(set(category_names)),'streams':sorted(streams|network)}
    except Exception as e:
        return {'url':url,'name':'','categories':sorted(set(category_names)),'streams':[],'error':str(e)}
    finally: await page.close()

async def main():
    old=load_state(); log.info('Fonte: %s',GENRES_URL); log.info('Estado anterior: %d canais',len(old))
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=HEADLESS, executable_path=os.getenv('CHROMIUM_EXECUTABLE','/usr/bin/chromium'))
        page=await browser.new_page(viewport={'width':1440,'height':1000})
        cats=await discover_categories(page)
        if not cats:
            log.error('Nenhuma categoria encontrada em /generos.')
            await browser.close(); return 2
        allstations={}
        for cname,curl in cats.items():
            urls=await collect_category(page,cname,curl)
            for u in urls:
                k=station_key(u); allstations.setdefault(k,{'url':u,'categories':set()}); allstations[k]['categories'].add(cname)
        log.info('Descoberta concluída: %d categorias | %d estações únicas',len(cats),len(allstations))
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
        streams_total += len(valid); k=station_key(r['url']); prev=old.get(k,{})
        name=r.get('name') or prev.get('name') or k.rsplit('/',1)[-1].replace('-',' ').title()
        current[k]={'url':r['url'],'name':name,'categories':r.get('categories') or prev.get('categories') or ['Outros'],'stream':valid[0],'logo':prev.get('logo',''),'failures':0,'last_ok':int(time.time())}
    for k,p in old.items():
        if k in current: continue
        failures=int(p.get('failures',0))+1
        if failures<FAILS_TO_REMOVE and p.get('stream'):
            q=dict(p); q['failures']=failures; current[k]=q; log.warning('Preservando temporariamente: %s (falha %d/%d)',p.get('name',k),failures,FAILS_TO_REMOVE)
        else: log.info('Removendo inativo: %s',p.get('name',k))
    if not current:
        log.error('Nenhum canal utilizável encontrado; lista anterior preservada.'); return 2
    save_state(current)
    lines=['#EXTM3U','']
    for k,p in sorted(current.items(),key=lambda kv:(str(kv[1].get('categories',['Outros'])[0]).lower(),str(kv[1].get('name','')).lower())):
        name=esc(p.get('name') or k.rsplit('/',1)[-1]); cats=p.get('categories') or ['Outros']; group=esc(cats[0]); logo=esc(p.get('logo','')); stream=p.get('stream','')
        attrs=f'tvg-name="{name}" group-title="{group}"'
        if logo: attrs += f' tvg-logo="{logo}"'
        lines += [f'#EXTINF:-1 {attrs},{name}',stream,'']
    M3U.write_text('\n'.join(lines),encoding='utf-8')
    log.info('Streams utilizáveis: %d | Canais na M3U: %d',streams_total,len(current)); log.info('Gerado: %s',M3U)
    return 0

if __name__=='__main__': raise SystemExit(asyncio.run(main()))

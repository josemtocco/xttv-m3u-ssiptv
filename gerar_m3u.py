#!/usr/bin/env python3
"""Discover XTTV stations, validate public streams and generate an SS IPTV M3U.

The collector intentionally does not bypass authentication, DRM, paywalls or access controls.
It only republishes public stream URLs exposed by XTTV pages/API responses.
"""
from __future__ import annotations
import json, logging, os, re, sys, time
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

BASE = os.getenv("XTTV_BASE_URL", "https://xttv.com.br").rstrip("/")
EXPLORE = f"{BASE}/explorar"
OUTPUT = Path(os.getenv("M3U_OUTPUT", "lista.m3u"))
STATE = Path(os.getenv("STATE_FILE", "canais.json"))
TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15"))
STREAM_TIMEOUT = float(os.getenv("STREAM_TIMEOUT", "12"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "250"))
MAX_STATIONS = int(os.getenv("MAX_STATIONS", "10000"))
WORKERS = int(os.getenv("VALIDATION_WORKERS", "12"))
FAILS_TO_REMOVE = int(os.getenv("FAILS_TO_REMOVE", "2"))
VALIDATE = os.getenv("VALIDATE_STREAMS", "true").lower() not in {"0", "false", "no"}
USER_AGENT = os.getenv("USER_AGENT", "XTTV-M3U-Collector/1.0 (+GitHub Actions)")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("xttv-m3u")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"})

URL_RE = re.compile(r'https?://[^\s"\'<>\\]+', re.I)
STREAM_RE = re.compile(r'https?://[^\s"\'<>\\]+(?:\.m3u8(?:\?[^\s"\'<>\\]*)?|\.mpd(?:\?[^\s"\'<>\\]*)?|\.m3u(?:\?[^\s"\'<>\\]*)?|\.ts(?:\?[^\s"\'<>\\]*)?|\.aac(?:\?[^\s"\'<>\\]*)?|\.mp4(?:\?[^\s"\'<>\\]*)?)', re.I)
IMAGE_RE = re.compile(r'https?://[^\s"\'<>\\]+\.(?:png|jpe?g|webp|gif|svg)(?:\?[^\s"\'<>\\]*)?$', re.I)

@dataclass
class Channel:
    id: str
    name: str
    stream_url: str
    categories: list[str]
    logo: str = ""
    station_url: str = ""
    tvg_id: str = ""
    failures: int = 0
    last_ok: str = ""


def clean(s: Any) -> str:
    if s is None:
        return ""
    s = unescape(str(s)).strip()
    return re.sub(r"\s+", " ", s)


def unique(values):
    out=[]; seen=set()
    for v in values:
        v=clean(v)
        if not v: continue
        k=v.casefold()
        if k not in seen:
            seen.add(k); out.append(v)
    return out


def normalize_url(url: str) -> str:
    url = unescape(url).strip().rstrip(".,;)")
    if url.startswith("//"): url = "https:" + url
    return url


def internal(url: str) -> bool:
    try: return urlparse(url).netloc.lower() == urlparse(BASE).netloc.lower()
    except: return False


def fetch(url: str):
    try:
        r=SESSION.get(url,timeout=TIMEOUT,allow_redirects=True)
        if r.status_code >= 400:
            log.debug("HTTP %s %s",r.status_code,url)
            return None
        return r
    except requests.RequestException as e:
        log.debug("Falha HTTP %s: %s",url,e)
        return None


def station_links(html: str, page_url: str) -> set[str]:
    soup=BeautifulSoup(html,"html.parser")
    found=set()
    for a in soup.select("a[href]"):
        u=urljoin(page_url,a.get("href"))
        if internal(u) and "/station/" in urlparse(u).path:
            found.add(u.split("#")[0])
    # Some SPAs put routes in script text rather than anchors.
    for m in re.findall(r'(?:(?:href|url|path|stationUrl|station_url)\s*[:=]\s*["\'])([^"\']+)',html,re.I):
        u=urljoin(page_url,m)
        if internal(u) and "/station/" in urlparse(u).path: found.add(u)
    return found


def next_pages(html: str, page_url: str) -> set[str]:
    soup=BeautifulSoup(html,"html.parser"); out=set()
    for a in soup.select("a[href]"):
        href=a.get("href"); label=clean(a.get_text(" ",strip=True)).lower()
        if not href: continue
        u=urljoin(page_url,href)
        if not internal(u): continue
        p=urlparse(u); path=p.path.lower(); qs=parse_qs(p.query)
        if any(k in qs for k in ("page","offset","cursor","limit")) or any(x in label for x in ("próxima","proxima","next","mais","carregar")):
            if path.startswith(("/explorar","/genero","/generos")): out.add(u)
    return out


def synthetic_pages() -> list[str]:
    # Handles catalogs rendered from pagination/infinite-scroll APIs even when pagination links are hidden.
    urls=[]
    for page in range(1, MAX_PAGES+1):
        for key in ("page", "pagina"):
            urls.append(f"{EXPLORE}?{key}={page}")
    for offset in range(0, MAX_PAGES*50, 50):
        urls.append(f"{EXPLORE}?offset={offset}&limit=50")
    return urls


def walk_catalog() -> tuple[set[str], dict[str,str]]:
    queue=deque([EXPLORE]); seen=set(); stations=set(); pages=0
    while queue and pages < MAX_PAGES:
        url=queue.popleft()
        if url in seen: continue
        seen.add(url); pages += 1
        r=fetch(url)
        if not r: continue
        sl=station_links(r.text,r.url); stations |= sl
        for p in next_pages(r.text,r.url):
            if p not in seen: queue.append(p)
        # Explore all hidden pagination offsets only until no new station is found for a stretch.
        if url == EXPLORE:
            queue.extend(synthetic_pages())
        if len(stations) >= MAX_STATIONS: break
        if pages % 25 == 0: log.info("Descoberta: %d páginas | %d estações",pages,len(stations))
    log.info("Descoberta concluída: %d páginas | %d estações",pages,len(stations))
    return stations, {}


def walk_json(obj: Any, path=""):
    if isinstance(obj, dict):
        for k,v in obj.items():
            yield k,v,path
            yield from walk_json(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i,v in enumerate(obj): yield from walk_json(v,f"{path}[{i}]")


def likely_stream(value: str, key: str="") -> bool:
    v=value.lower(); k=key.lower()
    if not v.startswith(("http://","https://")): return False
    if any(x in k for x in ("stream","play","source","video","hls","m3u8","manifest","liveurl","media")): return True
    return bool(re.search(r'\.(m3u8|mpd|m3u|ts|aac|mp4)(?:$|\?)',v))


def parse_station(url: str) -> list[Channel]:
    r=fetch(url)
    if not r: return []
    html=r.text; soup=BeautifulSoup(html,"html.parser")
    title=clean((soup.find("meta",property="og:title") or {}).get("content") if soup.find("meta",property="og:title") else "")
    if not title:
        title=clean(soup.title.get_text() if soup.title else "")
        title=re.sub(r"\s*[|\-–].*$", "", title).strip()
    if not title or title.lower() in {"xttv","explorar"}: 
        h=soup.find(["h1","h2"]); title=clean(h.get_text(" ",strip=True) if h else "Estação XTTV")

    logos=[]
    for sel in [("meta",{"property":"og:image"}),("meta",{"name":"twitter:image"})]:
        tag=soup.find(*sel)
        if tag and tag.get("content"): logos.append(urljoin(url,tag["content"]))
    for im in soup.find_all("img",src=True):
        u=urljoin(url,im.get("src"));
        if IMAGE_RE.match(u): logos.append(u)
    logo=next(iter(unique(logos)),"")

    cats=[]
    # Visible badges/categories plus structured data.
    for tag in soup.select("[class*=genre i], [class*=categor i], [class*=tag i]"):
        t=clean(tag.get_text(" ",strip=True));
        if 1 <= len(t) <= 40: cats.append(t)

    stream_candidates=[]; tvg_id=""
    scripts=soup.find_all("script")
    raw_parts=[html]
    for s in scripts:
        if s.string: raw_parts.append(s.string)
        elif s.get_text(): raw_parts.append(s.get_text())
    for raw in raw_parts:
        for m in STREAM_RE.findall(raw): stream_candidates.append(normalize_url(m))
        for m in URL_RE.findall(raw):
            m=normalize_url(m)
            if likely_stream(m): stream_candidates.append(m)
        try:
            data=json.loads(raw)
            for k,v,_ in walk_json(data):
                if isinstance(v,str) and likely_stream(v,k): stream_candidates.append(normalize_url(v))
                elif isinstance(v,list) and any(isinstance(x,str) and likely_stream(x,k) for x in v):
                    stream_candidates.extend(normalize_url(x) for x in v if isinstance(x,str) and likely_stream(x,k))
                if isinstance(v,str) and k.lower() in {"name","title","stationname","station_name"} and not title: title=clean(v)
                if isinstance(v,str) and k.lower() in {"tvgid","tvg_id","id","slug"}: tvg_id=clean(v)
                if isinstance(v,list) and k.lower() in {"genres","genre","categories","tags"}: cats.extend(str(x) for x in v if isinstance(x,(str,int,float)))
        except Exception:
            pass
    # HTML-encoded JSON and escaped URLs.
    for raw in raw_parts:
        raw=raw.replace('\\/','/')
        for m in STREAM_RE.findall(raw): stream_candidates.append(normalize_url(m))

    stream_candidates=unique(stream_candidates)
    cats=unique([c for c in cats if c.lower() not in {title.lower(),"xttv","ao vivo"}])
    if not cats: cats=["Sem categoria"]
    if not stream_candidates: return []

    sid=urlparse(url).path.rstrip('/').split('/')[-1]
    return [Channel(id=f"{sid}|{u}",name=title,stream_url=u,categories=cats,logo=logo,station_url=url,tvg_id=tvg_id) for u in stream_candidates]


def load_state() -> dict[str,Channel]:
    if not STATE.exists(): return {}
    try:
        data=json.loads(STATE.read_text(encoding="utf-8"))
        return {c["id"]:Channel(**c) for c in data if c.get("stream_url") and c.get("name")}
    except Exception as e:
        log.warning("Não foi possível ler %s: %s",STATE,e); return {}


def validate(url: str) -> tuple[bool,str]:
    try:
        # GET is used because several streaming servers reject HEAD.
        r=SESSION.get(url,timeout=STREAM_TIMEOUT,allow_redirects=True,stream=True,headers={"Range":"bytes=0-2047"})
        ok=r.status_code in {200,206,301,302,307,308}
        ctype=(r.headers.get("content-type") or "").lower()
        if ok and (url.lower().split("?",1)[0].endswith((".m3u8",".m3u")) or "mpegurl" in ctype or "x-mpegurl" in ctype):
            chunk=next(r.iter_content(8192),b"")
            text=chunk.decode("utf-8","ignore")
            ok=ok and ("#EXTM3U" in text or "#EXT-X-" in text)
        r.close(); return ok, f"HTTP {r.status_code}"
    except requests.RequestException as e: return False, type(e).__name__


def save_state(channels: list[Channel]):
    channels=sorted(channels,key=lambda c:(c.categories[0].casefold() if c.categories else "",c.name.casefold(),c.stream_url))
    STATE.write_text(json.dumps([asdict(c) for c in channels],ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


def m3u_quote(s: str) -> str: return clean(s).replace('"',"'")


def write_m3u(channels: list[Channel]):
    bycat=defaultdict(list)
    for c in channels:
        # Preserve every site category while using one M3U entry per category.
        for cat in c.categories or ["Sem categoria"]: bycat[cat].append(c)
    lines=["#EXTM3U"]
    for cat in sorted(bycat,key=str.casefold):
        seen=set()
        for c in sorted(bycat[cat],key=lambda x:x.name.casefold()):
            key=(c.name.casefold(),c.stream_url)
            if key in seen: continue
            seen.add(key)
            attrs=[f'tvg-id="{m3u_quote(c.tvg_id or c.id.split("|")[0])}"',f'tvg-name="{m3u_quote(c.name)}"']
            if c.logo: attrs.append(f'tvg-logo="{m3u_quote(c.logo)}"')
            attrs.append(f'group-title="{m3u_quote(cat)}"')
            lines.append(f'#EXTINF:-1 {" ".join(attrs)},{m3u_quote(c.name)}')
            lines.append(c.stream_url)
    tmp=OUTPUT.with_suffix(OUTPUT.suffix+".tmp")
    tmp.write_text("\n".join(lines)+"\n",encoding="utf-8",newline="\n")
    tmp.replace(OUTPUT)


def main():
    log.info("Fonte: %s",EXPLORE)
    old=load_state(); log.info("Estado anterior: %d canais",len(old))
    stations,_=walk_catalog()
    discovered=[]
    for i,url in enumerate(sorted(stations),1):
        cs=parse_station(url)
        if cs: discovered.extend(cs)
        if i % 50 == 0: log.info("Estações analisadas: %d/%d | streams: %d",i,len(stations),len(discovered))
    # De-duplicate by station+stream while keeping the newest metadata.
    merged={c.id:c for c in discovered}
    log.info("Streams descobertos: %d",len(merged))

    if not merged and not old:
        log.error("Nenhum canal descoberto na fonte; abortando para não gerar uma lista vazia.")
        return 2

    now=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
    if VALIDATE:
        ok_count=0; fail_count=0
        for idx,c in enumerate(list(merged.values()),1):
            ok,why=validate(c.stream_url)
            if ok:
                c.failures=0; c.last_ok=now; ok_count+=1
            else:
                c.failures=old.get(c.id, c).failures + 1; fail_count+=1
                log.debug("Inativo? %s -> %s",c.name,why)
        log.info("Validação: %d OK | %d falharam",ok_count,fail_count)

    # Start from newly discovered channels. For channels temporarily absent from discovery,
    # keep them only if they were previously active; they are validated before retention.
    final={}
    for cid,c in merged.items():
        if c.failures < FAILS_TO_REMOVE: final[cid]=c
    # Preserve old active channels when the catalog page/API temporarily fails to expose them.
    for cid,oldc in old.items():
        if cid in final: continue
        if cid not in merged:
            if VALIDATE:
                ok,why=validate(oldc.stream_url)
                if ok:
                    oldc.failures=0; oldc.last_ok=now; final[cid]=oldc
                else:
                    oldc.failures += 1
                    if oldc.failures < FAILS_TO_REMOVE: final[cid]=oldc
                    else: log.info("Removido por inatividade: %s",oldc.name)
            else:
                final[cid]=oldc
    channels=list(final.values())
    if not channels:
        log.error("Após validação não restou nenhum canal; preservando o estado anterior para segurança.")
        return 3
    save_state(channels); write_m3u(channels)
    log.info("M3U gerada: %d canais | %d categorias | %s",len(channels),len({x for c in channels for x in c.categories}),OUTPUT)
    return 0

if __name__ == "__main__": sys.exit(main())

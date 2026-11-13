"""Публикации о TikTok-мемах; не имитирует просмотр видео."""
import hashlib
import html
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from studio.comfort import local_now
from studio.config import flag

FEEDS=('https://knowyourmeme.com/memes.rss','https://knowyourmeme.com/editorials.rss')


def clean(text):
    return re.sub(r'\s+',' ',html.unescape(re.sub(r'<[^>]*>',' ',text or ''))).strip()


def parse_feed(content,now=None):
    now=now or datetime.now(timezone.utc)
    if len(content)>2_000_000:
        raise ValueError('Лента превышает лимит')
    result=[]
    for item in ET.fromstring(content).findall('.//item')[:100]:
        title=clean(item.findtext('title'))[:250]
        description=clean(item.findtext('description'))[:1500]
        if not re.search(r'tik\s?tok|тикток|тик\s?ток',title+' '+description,re.I):
            continue
        try:
            stamp=parsedate_to_datetime(item.findtext('pubDate',''))
            if stamp.tzinfo is None or not 0<=(now-stamp).total_seconds()<=7*86400:
                continue
            url=item.findtext('link','').strip()
            parsed=urlparse(url)
            if parsed.scheme!='https' or parsed.hostname not in ('knowyourmeme.com','www.knowyourmeme.com') or parsed.username or parsed.password or parsed.port not in (None,443):
                continue
        except (ValueError,TypeError,OverflowError):
            continue
        result.append({'title':title,'summary':description[:700],'url':url,'published':stamp.isoformat(),'observed':now.isoformat(),
                       'source':'knowyourmeme_rss','verification':'Свежая публикация о TikTok; возраст и популярность мема не подтверждены','video_watched':False,'popularity':None})
    return result


def save_signal(store,body):
    id='meme-'+hashlib.sha256(body['url'].encode()).hexdigest()[:20]
    if not store.get('meme_signal',id):
        store.put('meme_signal',id,body)
    return id


async def refresh_memes(store):
    if not flag('STUDIO_MEME_RADAR',True):
        return {'status':'disabled','new':[]}
    local=local_now(store)
    if local.hour<13:
        return {'status':'not_due','new':[]}
    day=local.date().isoformat()
    if store.get('settings','meme_scan',{}).get('day')==day:
        return {'status':'cached','new':[]}
    store.put('settings','meme_scan',{'day':day,'attempt':time.time(),'status':'checking'})
    ids=[]
    failures=0
    async with httpx.AsyncClient(timeout=20,follow_redirects=False,trust_env=False) as client:
        for source in FEEDS:
            try:
                async with client.stream('GET',source,headers={'User-Agent':'CartoonStudio/0.1 RSS'}) as response:
                    response.raise_for_status()
                    content=bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content)>2_000_000:
                            raise ValueError('Превышен размер ленты')
                for signal in parse_feed(content):
                    id='meme-'+hashlib.sha256(signal['url'].encode()).hexdigest()[:20]
                    if not store.get('meme_signal',id):
                        ids.append(save_signal(store,signal))
            except (httpx.HTTPError,ValueError,ET.ParseError):
                failures+=1
    status='unavailable' if failures==len(FEEDS) else 'partial' if failures else 'checked'
    store.put('settings','meme_scan',{'day':day,'attempt':time.time(),'status':status,'new_count':len(ids),'scope':'rss_descriptions_not_video'})
    return {'status':status,'new':ids[:10]}


def submit_meme(store,url,description):
    parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.hostname not in ('www.tiktok.com','tiktok.com','vm.tiktok.com','vt.tiktok.com') or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ValueError('Нужна HTTPS-ссылка TikTok без постороннего домена')
    if not 10<=len(description)<=1500:
        raise ValueError('Добавьте описание мема от 10 до 1500 символов после ссылки')
    return save_signal(store,{'title':clean(description)[:120],'summary':clean(description),'url':url,'published':None,
                             'observed':datetime.now(timezone.utc).isoformat(),'source':'owner_tiktok_link',
                             'verification':'Описание владельца; свежесть и метрики не подтверждены','video_watched':False,'popularity':None})


def recent_signals(store):
    now=datetime.now(timezone.utc)
    result=[]
    for row in store.objects('meme_signal'):
        stamp=datetime.fromisoformat(row['body'].get('published') or row['body']['observed'])
        if 0<=(now-stamp).total_seconds()<=7*86400:
            result.append({'id':row['id'],**row['body']})
    return result[:6]

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from studio.db import Store
from studio.memes import parse_feed, recent_signals, refresh_memes, submit_meme


def feed(stamp,title='TikTok new joke'):
    return ('<rss><channel><item><title>'+title+'</title><description>A funny TikTok format</description><pubDate>'+format_datetime(stamp)+'</pubDate><link>https://knowyourmeme.com/memes/test</link></item></channel></rss>').encode()


def test_fresh_publication_is_not_claimed_as_watched_or_viral():
    now=datetime.now(timezone.utc)
    signal=parse_feed(feed(now-timedelta(hours=1)),now)[0]
    assert signal['video_watched'] is False and signal['popularity'] is None
    assert 'не подтверждены' in signal['verification']
    assert not parse_feed(feed(now-timedelta(days=8)),now)
    assert not parse_feed(feed(now+timedelta(days=1)),now)


def test_manual_tiktok_link_keeps_description_without_request(tmp_path):
    store=Store(tmp_path)
    id=submit_meme(store,'https://www.tiktok.com/@creator/video/123','Персонаж драматично спорит со своим отражением')
    assert recent_signals(store)[0]['id']==id
    assert store.get('meme_signal',id)['published'] is None
    with pytest.raises(ValueError):
        submit_meme(store,'https://tiktok.com.evil.test/video/1','Описание длиннее десяти символов')


def test_scan_is_deduplicated_and_rate_limited(tmp_path,monkeypatch):
    monkeypatch.setattr('studio.memes.local_now',lambda _:datetime(2026,11,13,13,tzinfo=timezone.utc))
    monkeypatch.setenv('STUDIO_MEME_RADAR','true')
    store=Store(tmp_path)
    calls=[]
    def handle(request):
        calls.append(request.url)
        return httpx.Response(200,content=feed(datetime.now(timezone.utc)-timedelta(hours=1)))
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    assert len(asyncio.run(refresh_memes(store))['new'])==1
    assert asyncio.run(refresh_memes(store))['status']=='cached'
    assert len(calls)==2 and len(store.objects('meme_signal'))==1


def test_failure_does_not_create_fake_memes(tmp_path,monkeypatch):
    monkeypatch.setattr('studio.memes.local_now',lambda _:datetime(2026,11,13,13,tzinfo=timezone.utc))
    monkeypatch.setenv('STUDIO_MEME_RADAR','true')
    def handle(request):
        return httpx.Response(403)
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    store=Store(tmp_path)
    assert asyncio.run(refresh_memes(store))['status']=='unavailable'
    assert not store.objects('meme_signal')


def test_no_scan_before_daily_time(tmp_path,monkeypatch):
    monkeypatch.setenv('STUDIO_MEME_RADAR','true')
    monkeypatch.setattr('studio.memes.local_now',lambda _:datetime(2026,11,13,12,59,tzinfo=timezone.utc))
    assert asyncio.run(refresh_memes(Store(tmp_path)))['status']=='not_due'

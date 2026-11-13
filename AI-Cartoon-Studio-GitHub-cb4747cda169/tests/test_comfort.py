import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
import pytest

from studio.bot import Bot
from studio.bot_comfort import (
    accept_feedback,
    callback,
    daily_summary,
    deliver_video,
    progress_notifications,
)
from studio.comfort import quiet_now, render_action, worker_problem
from studio.config import Settings
from studio.db import Store
from studio.media import sha256


def setup(tmp_path):
    store=Store(tmp_path)
    store.put('settings','comfort',{'quiet_enabled':False})
    return store,Bot(store,Settings(data=tmp_path,owner_id='123',bot_token='fixture'))


def test_render_buttons_preserve_task_and_rearm_warning(tmp_path):
    store,_=setup(tmp_path)
    task=store.enqueue('render',{'episode':'ep','package':'pkg'},'render-test')
    render_action(store,task,'30')
    assert store.task(task)['available']>time.time()+1190
    assert store.get('render_warning',task)['status']=='deferred'
    assert store.claim(['render'],'pc',require_render_notice=True) is None
    render_action(store,task,'now')
    assert store.claim(['render'],'pc',require_render_notice=True)['id']==task
    with pytest.raises(ValueError,match='уже начат'):
        render_action(store,task,'60')


def test_quiet_hours_cross_midnight_and_offset(tmp_path):
    store,_=setup(tmp_path)
    store.put('settings','comfort',{'quiet_enabled':True,'quiet_start':23,'quiet_end':9,'utc_offset':3})
    assert quiet_now(store,datetime(2026,11,13,21,tzinfo=timezone.utc).timestamp())
    assert not quiet_now(store,datetime(2026,11,13,7,tzinfo=timezone.utc).timestamp())
    with pytest.raises(ValueError,match='тихие часы'):
        from unittest.mock import patch
        with patch('studio.comfort.quiet_now',return_value=True):
            render_action(store,'missing','now')


def test_worker_disk_and_missing_encoder_are_reported(tmp_path):
    store,_=setup(tmp_path)
    assert 'недоступен' in worker_problem(store)
    store.put('worker','latest',{'last_seen':time.time(),'free_bytes':1024,'ffmpeg_ready':True})
    assert 'мало места' in worker_problem(store)
    store.put('worker','latest',{'last_seen':time.time(),'free_bytes':10*1024**3,'ffmpeg_ready':False})
    assert 'FFmpeg' in worker_problem(store)


def test_daily_summary_is_once_per_local_day(tmp_path,monkeypatch):
    store,bot=setup(tmp_path)
    store.put('settings','comfort',{'quiet_enabled':False,'summary_hour':0})
    messages=[]
    async def send(*args):
        messages.append(args[1])
    monkeypatch.setattr(bot,'send',send)
    asyncio.run(daily_summary(bot,None))
    asyncio.run(daily_summary(bot,None))
    assert len(messages)==1 and 'Идей на выбор' in messages[0]


def test_progress_edits_existing_message(tmp_path,monkeypatch):
    store,bot=setup(tmp_path)
    task=store.enqueue('render',{'episode':'ep','package':'pkg'},'render')
    store.claim(['render'],'pc')
    store.put('render_progress',task,{'stage':'rendering','scene':3,'scenes':8})
    sent=[]
    async def send(*args):
        sent.append(args[1])
        return {'message_id':42}
    monkeypatch.setattr(bot,'send',send)
    asyncio.run(progress_notifications(bot,None))
    assert '3 из 8' in sent[0]
    store.put('render_progress',task,{'stage':'uploading','scene':0,'scenes':0})
    previous=store.get('notification','progress:'+task)
    store.put('notification','progress:'+task,previous|{'at':0})
    edits=[]
    async def call(*args):
        edits.append(args[2])
    monkeypatch.setattr(bot,'call',call)
    asyncio.run(progress_notifications(bot,None))
    assert edits[0]['message_id']==42 and len(sent)==1


def test_video_upload_is_hash_bound_and_not_repeated(tmp_path):
    store,bot=setup(tmp_path)
    (tmp_path/'results').mkdir()
    video=tmp_path/'results'/'test.mp4'
    video.write_bytes(b'fixture-video')
    body={'status':'awaiting_review','script':{'episode_title':'Тест'},'result':video.name,'qc':{'sha256':sha256(video),'pass':True}}
    requests=[]
    def handler(request):
        requests.append(request)
        assert request.url.path.endswith('/sendVideo') and b'fixture-video' in request.content
        return httpx.Response(200,json={'ok':True,'result':{'message_id':1}})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await deliver_video(bot,client,{'id':'ep','body':body})
            await deliver_video(bot,client,{'id':'ep','body':body})
    asyncio.run(scenario())
    assert len(requests)==1


def test_review_cannot_approve_without_seven_explicit_checks(tmp_path):
    store,bot=setup(tmp_path)
    store.put('episode','ep',{'status':'awaiting_review','qc':{'sha256':'a'*64,'pass':True}})
    store.put('telegram_review','token',{'episode':'ep','sha256':'a'*64,'checks':[],'created':time.time()})
    with pytest.raises(ValueError,match='семь'):
        asyncio.run(callback(bot,None,'v:token:confirm'))


def test_feedback_queues_new_revision_and_retains_original(tmp_path):
    store,bot=setup(tmp_path)
    original={'status':'awaiting_review','script':{'original':True},'qc':{'sha256':'a'*64}}
    store.put('episode','ep',original)
    store.put('telegram_review','token',{'episode':'ep','sha256':'a'*64,'checks':[],'created':time.time()})
    store.put('settings','review_feedback',{'token':'token','status':'waiting','created':time.time()})
    assert 'Новая редакция' in accept_feedback(bot,'Сделай реплики короче')
    assert store.get('episode','ep')['script']==original['script']
    task=next(t for t in store.tasks() if t['kind']=='script_revision')
    assert json.loads(store.task(task['id'])['payload'])['issues']==['Сделай реплики короче']
    assert accept_feedback(bot,'Ещё сообщение') is None


def test_quiet_command_saves_preferences(tmp_path):
    store,bot=setup(tmp_path)
    assert 'сохранены' in asyncio.run(bot.command('/quiet 22 8 3'))
    assert store.get('settings','comfort')['quiet_start']==22
    assert 'Ежедневная сводка' in asyncio.run(bot.command('/summary 18'))


def test_api_blocks_low_disk_and_records_real_progress(tmp_path):
    from fastapi.testclient import TestClient

    from studio.api import create_app
    store,_=setup(tmp_path)
    task=store.enqueue('render',{'episode':'ep','package':'pkg'},'test')
    store.put('render_warning',task,{'status':'sent','ready_at':0})
    settings=Settings(data=tmp_path,api_token='o'*40,worker_token='w'*40,server_enabled=True,owner_id='123',bot_token='fixture')
    with TestClient(create_app(settings,store)) as client:
        headers={'Authorization':'Bearer '+settings.worker_token}
        assert client.post('/worker/claim',headers=headers,json={'worker_id':'pc','free_bytes':1,'ffmpeg_ready':True}).json() is None
        claimed=client.post('/worker/claim',headers=headers,json={'worker_id':'pc','free_bytes':10*1024**3,'ffmpeg_ready':True}).json()
        assert claimed['id']==task
        result=client.post(f'/worker/{task}/heartbeat',headers=headers,json={'lease_token':claimed['lease_token'],'stage':'rendering','scene':3,'scenes':8})
        assert result.status_code==200
        assert store.get('render_progress',task)['scene']==3
        assert store.get('worker','latest')['free_bytes']==10*1024**3


def test_quiet_period_invalidates_old_countdown(tmp_path):
    from studio.comfort import pause_notices
    store,_=setup(tmp_path)
    task=store.enqueue('render',{'episode':'ep','package':'pkg'},'test')
    store.put('render_warning',task,{'status':'sent','ready_at':0})
    pause_notices(store)
    assert store.get('render_warning',task)['status']=='quiet'
    assert store.claim(['render'],'pc',require_render_notice=True) is None

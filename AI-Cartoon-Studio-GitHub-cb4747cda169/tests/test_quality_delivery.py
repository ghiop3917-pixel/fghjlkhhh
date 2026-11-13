import asyncio
import wave
from array import array
from types import SimpleNamespace

import httpx
import pytest

from studio.db import Store
from studio.quality import inspect_audio, schedule_repair
from studio.subtitles import AlignmentMismatch, Captioner
from studio.tiktok import TikTokConsent, upload_draft
from studio.trend_sources import telegram_channel_post, youtube_trends


def test_silent_audio_is_rejected(tmp_path):
    path = tmp_path/'silent.wav'
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(array('h', [0]*16000).tobytes())
    assert not inspect_audio(path)['pass']


@pytest.mark.parametrize('failed,kind', [('audio','materials'), ('visual','materials'), ('subtitle','script_revision')])
def test_repair_queue_is_bounded_and_preserves_original(tmp_path, failed, kind):
    store=Store(tmp_path)
    store.put('episode','ep',{'status':'qc_failed','package':'pkg','script':{'original':True}})
    report={'package':{'checks':{failed:{'pass':False,'issues':['Слишком длинное слово' if failed=='subtitle' else 'Дефект']}}}}
    for attempt in range(1,4):
        result=schedule_repair(store,'ep',report)
        assert result['kind']==kind and result['attempt']==attempt
    assert schedule_repair(store,'ep',report) is None
    assert store.get('episode','ep')['script']=={'original':True}


def test_bad_asr_match_is_a_defect_in_strict_mode(monkeypatch):
    monkeypatch.setenv('STUDIO_ALIGNMENT_REQUIRED','true')
    class Model:
        def transcribe(self,*args,**kwargs):
            return iter([SimpleNamespace(words=[])]),None
    with pytest.raises(AlignmentMismatch):
        Captioner(Model()).align('unused','Текст не совпал',3)


def test_tiktok_disabled_never_opens_network(tmp_path, monkeypatch):
    monkeypatch.setenv('TIKTOK_UPLOAD_ENABLED','false')
    consent=TikTokConsent(sha256='a'*64,approve_exact_upload=True,understand_creator_must_complete_post=True)
    with pytest.raises(ValueError,match='выключена'):
        asyncio.run(upload_draft(Store(tmp_path),'ep',consent))


def test_tiktok_draft_is_not_publication_and_blocks_duplicates(tmp_path, monkeypatch):
    from studio.media import sha256
    store=Store(tmp_path)
    (tmp_path/'results').mkdir()
    video=tmp_path/'results'/'test.mp4'
    video.write_bytes(b'test fixture')
    digest=sha256(video)
    store.put('episode','ep',{'status':'reviewed','result':video.name,'qc':{'sha256':digest}})
    for key in ('TIKTOK_UPLOAD_ENABLED','TIKTOK_AUTHORIZED_APP'):
        monkeypatch.setenv(key,'true')
    monkeypatch.setenv('TIKTOK_ACCESS_TOKEN','fixture-token')
    def handle(request):
        if request.method=='PUT':
            assert 'authorization' not in request.headers
            assert request.headers['content-range']=='bytes 0-11/12'
            return httpx.Response(201)
        return httpx.Response(200,json={'error':{'code':'ok'},'data':{'publish_id':'fixture','upload_url':'https://open-upload.tiktokapis.com/video/?fixture=1'}})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    consent=TikTokConsent(sha256=digest,approve_exact_upload=True,understand_creator_must_complete_post=True)
    assert asyncio.run(upload_draft(store,'ep',consent))['status']=='awaiting_creator_post'
    assert not store.get('publication','ep')
    with pytest.raises(ValueError,match='повтор'):
        asyncio.run(upload_draft(store,'ep',consent))


def test_sources_remain_disabled_without_explicit_access(tmp_path,monkeypatch):
    monkeypatch.delenv('YOUTUBE_DATA_API_KEY',raising=False)
    store=Store(tmp_path)
    assert asyncio.run(youtube_trends(store))==[]
    assert telegram_channel_post(store,{'chat':{'id':123,'type':'channel'},'text':'Пост'},set()) is None
    assert not store.objects('trend')


def test_speech_duration_failure_requests_script_revision_without_package(tmp_path):
    store=Store(tmp_path)
    store.put('episode','ep',{'status':'materials_approved','script':{'original':True}})
    report={'package':{'checks':{'subtitle':{'pass':False,'issues':['Реальная озвучка 65 с; требуется 30–60 с']}}}}
    assert schedule_repair(store,'ep',report)['kind']=='script_revision'

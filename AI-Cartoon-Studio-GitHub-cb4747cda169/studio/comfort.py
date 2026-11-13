"""Настройки удобства и безопасное управление ожидающим рендером."""
import json
import os
import time
from datetime import datetime, timedelta, timezone


def preferences(store):
    return {'quiet_start':23,'quiet_end':9,'utc_offset':3,'summary_hour':19,'quiet_enabled':True, **store.get('settings','comfort',{})}


def local_now(store, stamp=None):
    return datetime.fromtimestamp(time.time() if stamp is None else stamp,timezone(timedelta(hours=preferences(store)['utc_offset'])))


def quiet_now(store, stamp=None):
    p=preferences(store)
    if not p['quiet_enabled']:
        return False
    hour=local_now(store,stamp).hour
    start,end=p['quiet_start'],p['quiet_end']
    return (hour>=start or hour<end) if start>end else start<=hour<end


def render_action(store, task_id, action):
    now=time.time()
    if action=='evening':
        local=local_now(store,now)
        target=local.replace(hour=18,minute=0,second=0,microsecond=0)
        if target<=local:
            target+=timedelta(days=1)
        available=target.timestamp()-600
    elif action in ('30','60'):
        available=now+int(action)*60-600
    elif action=='now':
        if quiet_now(store):
            raise ValueError('Сейчас тихие часы. Измените /quiet или дождитесь утра')
        available=now
    else:
        raise ValueError('Неизвестное действие рендера')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute("SELECT status FROM tasks WHERE id=? AND kind='render'",(task_id,)).fetchone()
        if not row or row['status']!='pending':
            raise ValueError('Рендер уже начат или завершён; эта кнопка устарела')
        db.execute('UPDATE tasks SET available=? WHERE id=?',(available,task_id))
        body={'status':'sent','sent_at':now,'ready_at':now,'owner_started':True} if action=='now' else {'status':'deferred','until':available+600}
        db.execute("INSERT INTO objects(category,id,revision,body,updated) VALUES('render_warning',?,1,?,?) ON CONFLICT(category,id) DO UPDATE SET revision=revision+1,body=excluded.body,updated=excluded.updated",(task_id,json.dumps(body),now))
    return 'Запуск разрешён; Lenovo заберёт задачу при следующем подключении' if action=='now' else 'Рендер отложен. Перед новым стартом придёт предупреждение за 10 минут'


def worker_status(root):
    import shutil
    return {'free_bytes':shutil.disk_usage(root).free,'ffmpeg_ready':bool(shutil.which('ffmpeg'))}


def pause_notices(store):
    # Утром ожидающий рендер получит свежее предупреждение вместо ночного таймера.
    with store.connect() as db:
        db.execute("UPDATE objects SET body=json_set(body,'$.status','quiet'),revision=revision+1,updated=? WHERE category='render_warning' AND json_extract(body,'$.status')='sent' AND id IN (SELECT id FROM tasks WHERE kind='render' AND status='pending')",(time.time(),))


def worker_problem(store):
    data=store.get('worker','latest',{})
    if time.time()-data.get('last_seen',0)>90:
        return 'Lenovo недоступен. Включите ноутбук и окно Worker'
    if data.get('free_bytes') is not None and data['free_bytes']<int(os.getenv('STUDIO_WORKER_MIN_FREE_GB','2'))*1024**3:
        return 'На Lenovo мало места. Нужно минимум '+os.getenv('STUDIO_WORKER_MIN_FREE_GB','2')+' ГБ; автоматического удаления нет'
    if data.get('ffmpeg_ready') is False:
        return 'На Lenovo недоступен FFmpeg; рендер ожидает исправления'
    return None

"""Кнопки рендера, просмотр, сводка и измеримый прогресс в Telegram."""
import json
import secrets
import time

from studio.comfort import (
    local_now,
    preferences,
    render_action,
    worker_problem,
)
from studio.media import safe_file, sha256


def controls(task):
    return {'inline_keyboard':[[{'text':'▶️ Я готов — запускай','callback_data':'r:now:'+task}],
        [{'text':'Через 30 минут','callback_data':'r:30:'+task},{'text':'Через час','callback_data':'r:60:'+task}],
        [{'text':'До вечера','callback_data':'r:evening:'+task}]]}


def summary(store):
    episodes=store.objects('episode')
    proposed=sum(r['body'].get('status')=='proposed' for r in store.objects('trend'))
    waiting=sum(r['body'].get('status') in ('awaiting_script_approval','awaiting_review') for r in episodes)
    ready=sum(r['body'].get('status') in ('awaiting_review','reviewed') for r in episodes)
    with store.connect() as db:
        counts=dict(db.execute('SELECT status,COUNT(*) FROM tasks GROUP BY status').fetchall())
    return f"📋 Сводка студии\nИдей на выбор: {proposed}\nЖдут вашего решения: {waiting}\nГотовых видео: {ready}\nЗадач в очереди: {counts.get('pending',0)}, в работе: {counts.get('running',0)}, с ошибкой: {counts.get('failed',0)}\nLenovo: {worker_problem(store) or 'на связи'}\n/app — открыть студию"


async def daily_summary(bot,client):
    now=local_now(bot.store)
    key='daily-summary:'+now.date().isoformat()
    if now.hour<preferences(bot.store)['summary_hour'] or bot.store.get('notification',key,{}).get('status')=='sent':
        return
    await bot.send(client,summary(bot.store))
    bot.store.put('notification',key,{'status':'sent'})


async def progress_notifications(bot,client):
    store=bot.store
    for task in store.tasks():
        if task['kind']!='render' or task['status']!='running':
            continue
        progress=store.get('render_progress',task['id'],{})
        if not progress:
            continue
        names={'downloading':'Загружаются материалы на Lenovo','rendering':f"Рендерится сцена {progress['scene']} из {progress['scenes']}",'assembling':'Собирается итоговое видео','uploading':'Готовое видео отправляется на сервер'}
        text='🎬 '+names[progress['stage']]
        key='progress:'+task['id']
        previous=store.get('notification',key,{})
        if text==previous.get('text') or time.time()-previous.get('at',0)<15:
            continue
        if previous.get('message_id'):
            await bot.call(client,'editMessageText',{'chat_id':bot.settings.owner_id,'message_id':previous['message_id'],'text':text})
            message_id=previous['message_id']
        else:
            sent=await bot.send(client,text)
            message_id=sent.get('message_id') if isinstance(sent,dict) else None
        store.put('notification',key,{'status':'sent','text':text,'message_id':message_id,'at':time.time()})


async def deliver_video(bot,client,row):
    store=bot.store
    episode=row['body']
    path=safe_file(store.root/'results',episode['result'])
    digest=sha256(path)
    if digest!=episode['qc']['sha256']:
        raise ValueError('Видео изменилось после проверки; отправка остановлена')
    key='video:'+row['id']+':'+digest
    if store.get('notification',key,{}).get('status')=='sent':
        return
    token=secrets.token_hex(10)
    store.put('telegram_review',token,{'episode':row['id'],'sha256':digest,'checks':[],'created':time.time()})
    markup={'inline_keyboard':[[{'text':'Одобрить: семь проверок','callback_data':f'v:{token}:open'},{'text':'Исправить','callback_data':f'v:{token}:fix'}]]}
    if path.stat().st_size>49_000_000:
        await bot.send(client,'Видео готово, но превышает лимит отправки Telegram. Посмотрите его в /app; затем можно пройти проверки кнопками ниже.',markup)
    else:
        with path.open('rb') as video:
            response=await client.post(f'https://api.telegram.org/bot{bot.settings.bot_token}/sendVideo',data={'chat_id':bot.settings.owner_id,'caption':('Готово: '+episode['script']['episode_title']+'\nПросмотрите и прослушайте весь ролик перед одобрением.')[:1000],'supports_streaming':'true','reply_markup':json.dumps(markup,ensure_ascii=False)},files={'video':(row['id']+'.mp4',video,'video/mp4')},timeout=180)
        response.raise_for_status()
        if not response.json().get('ok'):
            raise ValueError('Telegram не подтвердил доставку видео')
    store.put('notification',key,{'status':'sent','sha256':digest})


CHECKS={'script':'Сценарий и логика','characters':'Внешность персонажей','audio':'Голоса и звук','visual':'Качество изображения','subtitles':'Субтитры и синхронизация','trend':'Достоверность темы','producer':'Итоговое одобрение'}


async def callback(bot,client,data):
    if data.startswith('r:'):
        _,action,task=data.split(':',2)
        return render_action(bot.store,task,action)
    _,token,action=data.split(':',2)
    review=bot.store.get('telegram_review',token)
    if not review or time.time()-review['created']>86400:
        raise ValueError('Кнопка устарела. Откройте текущую серию в /app')
    episode=bot.store.get('episode',review['episode'],{})
    if episode.get('status')!='awaiting_review' or episode.get('qc',{}).get('sha256')!=review['sha256']:
        raise ValueError('Версия видео или статус изменились; нужна новая проверка')
    if action=='fix':
        bot.store.put('settings','review_feedback',{'token':token,'created':time.time(),'status':'waiting'})
        return 'Напишите следующим сообщением, что исправить. Будет создана отдельная редакция; исходное видео сохраняется. /cancel — отменить ввод'
    if action=='confirm':
        from studio.production import archive_reviewed
        archive_reviewed(bot.store,review['episode'],{k:k in review['checks'] for k in CHECKS})
        return 'Семь проверок подтверждены. Серия одобрена и сохранена в архиве. Публикация отдельно в /app'
    if action in CHECKS:
        selected=set(review['checks'])
        selected.symmetric_difference_update({action})
        review['checks']=sorted(selected)
        bot.store.put('telegram_review',token,review)
    elif action!='open':
        raise ValueError('Неизвестная проверка')
    buttons=[[{'text':('✅ ' if k in review['checks'] else '⬜ ')+label,'callback_data':f'v:{token}:{k}'}] for k,label in CHECKS.items()]
    buttons.append([{'text':'Подтвердить все отмеченные проверки','callback_data':f'v:{token}:confirm'}])
    await bot.send(client,'Отметьте каждую проверку после просмотра. Отправка на платформы этим не разрешается.',{'inline_keyboard':buttons})
    return 'Отмечено '+str(len(review['checks']))+' из 7'


def accept_feedback(bot,text):
    request=bot.store.get('settings','review_feedback',{})
    if request.get('status')!='waiting':
        return None
    if text=='/cancel':
        bot.store.put('settings','review_feedback',request|{'status':'cancelled'})
        return 'Ввод замечаний отменён'
    if text.startswith('/'):
        return None
    if time.time()-request['created']>3600:
        raise ValueError('Время ввода замечаний истекло; нажмите «Исправить» снова')
    if len(text)>3000:
        raise ValueError('Сократите замечания до 3000 символов')
    review=bot.store.get('telegram_review',request['token'])
    episode=bot.store.get('episode',review['episode'])
    if episode['status']!='awaiting_review' or episode['qc']['sha256']!=review['sha256']:
        raise ValueError('Видео изменилось; выберите актуальный ролик')
    if episode.get('repair_attempts',0)>=3:
        raise ValueError('Лимит трёх исправлений исчерпан; создайте редакцию в /app')
    task=bot.store.enqueue('script_revision',{'episode':review['episode'],'issues':[text]},'owner-fix:'+request['token'])
    bot.store.put('episode',review['episode'],episode|{'status':'awaiting_script_repair','repair_attempts':episode.get('repair_attempts',0)+1})
    bot.store.put('settings','review_feedback',request|{'status':'accepted','task':task})
    return 'Замечания приняты. Новая редакция потребует согласования. Исходные материалы сохранены'

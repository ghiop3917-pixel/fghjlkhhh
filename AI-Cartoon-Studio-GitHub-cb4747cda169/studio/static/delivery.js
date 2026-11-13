"use strict";
function enhanceDeliveryUI(){
  if(view==='analytics'){
    const section=document.createElement('div');$('#content').append(section);
    api('/api/analytics/summary').then(r=>r.json()).then(data=>{if(section.isConnected)section.innerHTML=panel('Истории и персонажи',`<p>${esc(data.note)}</p><pre>${esc(JSON.stringify({series:data.series,characters:data.characters},null,2))}</pre>`);}).catch(e=>notify(e.message));
  }
  if(view!=='episodes')return;
  document.querySelectorAll('[data-action="video"]').forEach(video=>{
    const row=state.episodes.find(r=>r.id===video.dataset.id);
    if(row?.body.status!=='reviewed')return;
    const button=document.createElement('button');button.className='secondary';button.textContent='Публикация и файлы';
    button.onclick=()=>deliveryDialog(row);video.parentElement.append(button);
  });
}
async function deliveryDialog(row){
  const id=row.id,base='/api/episodes/'+id;
  try{
    const status=await(await api(base+'/delivery')).json();
    dialog('<h2>Публикация и сохранность</h2><p>Открытие окна ничего не отправляет.</p><pre>'+esc(JSON.stringify(status,null,2))+'</pre><div class="row"><button id="deliveryPlan">Согласовать YouTube</button><button id="deliveryCheck">Сверить YouTube</button><button id="deliveryTikTok">Доставка в TikTok</button><button id="deliveryCleanup">Предпросмотр очистки</button></div><div id="deliveryForm"></div>');
    const run=async fn=>{try{await fn();}catch(e){notify(e.message);}};
    $('#deliveryCheck').onclick=()=>run(async()=>{const d=await(await api(base+'/youtube-readback','POST')).json();notify('YouTube: '+d.status);});
    $('#deliveryPlan').onclick=()=>run(async()=>{
      $('#deliveryForm').innerHTML='<form id="planForm"><label>Название<input name="title" maxlength="100" required></label><label>Описание<textarea name="description" required></textarea></label><label>Доступ<select name="privacy"><option value="private">Приватно</option><option value="unlisted">По ссылке</option><option value="public">Публично</option></select></label><label>Дата выпуска (для публичного доступа, местное время)<input name="when" type="datetime-local"></label><p>SHA256: '+esc(row.body.qc.sha256)+'</p><label class="check"><input type="checkbox" required>Разрешаю отправку этого файла с указанными метаданными и доступом</label><button>Сохранить план</button></form>';
      const f=$('#planForm');f.elements.title.value=row.body.script.episode_title;f.elements.description.value=row.body.script.synopsis+'\n\nВымышленная анимационная история.';
      if(row.body.script.format==='emergency_story'){const s=await(await api(base+'/safety-review')).json();f.elements.description.value+='\n'+s.disclaimer+'\nИсточники советов:\n'+[...new Set(s.advice.map(a=>a.reference.url))].join('\n');}
      f.onsubmit=e=>{e.preventDefault();run(async()=>{await api(base+'/publication-plan','POST',{sha256:row.body.qc.sha256,title:f.elements.title.value,description:f.elements.description.value,privacy:f.elements.privacy.value,publish_at:f.elements.when.value?new Date(f.elements.when.value).toISOString():null,approve_exact_upload:true});$('#dialog').close();notify('План сохранён; отключённый YouTube ожидает подключения.');});};
    });
    $('#deliveryTikTok').onclick=()=>{
      $('#deliveryForm').innerHTML='<form id="tikForm"><p>Нужны разрешённое приложение TikTok и OAuth. Публикацию автор завершает во входящих уведомлениях TikTok.</p><label class="check"><input type="checkbox" required>Разрешаю передать именно проверенный MP4</label><label class="check"><input type="checkbox" required>Понимаю, что публикацию нужно завершить в TikTok</label><button>Передать</button></form>';
      $('#tikForm').onsubmit=e=>{e.preventDefault();run(async()=>{await api(base+'/tiktok-draft','POST',{sha256:row.body.qc.sha256,approve_exact_upload:true,understand_creator_must_complete_post:true});notify('Доставлено. Завершите публикацию в TikTok.');});};
    };
    $('#deliveryCleanup').onclick=()=>run(async()=>{
      const p=await(await api(base+'/cleanup-plan')).json();
      $('#deliveryForm').innerHTML='<p>'+esc(p.reason)+'</p><pre>'+esc(JSON.stringify(p.files,null,2))+'</pre>'+(p.eligible?'<form id="cleanForm"><label class="check"><input type="checkbox" required>Разрешаю удалить только перечисленные файлы после резервного копирования</label><label class="check"><input type="checkbox" required>Считаю подтверждённое публичное видео YouTube достаточным условием</label><button>Разрешить удаление списка</button></form>':'');
      if($('#cleanForm'))$('#cleanForm').onsubmit=e=>{e.preventDefault();run(async()=>{await api(base+'/cleanup','POST',{plan_sha256:p.plan_sha256,confirm_delete_listed_files:true,confirm_youtube_is_sufficient:true});$('#dialog').close();notify('Разрешение записано; перед очисткой выполнятся новая сверка и резервное копирование.');});};
    });
    status.plans.filter(p=>p.status==='approved').forEach(p=>{const b=document.createElement('button');b.className='secondary';b.textContent='Отменить план: '+p.title;b.onclick=()=>run(async()=>{await api('/api/publication-plans/'+p.id+'/cancel','POST');b.disabled=true;notify('План отменён');});$('#dialogBody').append(b);});
  }catch(e){notify(e.message);}
}

/** Веб-панель JobHunter: одна страница, без сборки и внешних зависимостей. */
export const PAGE = /* html */ `<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JobHunter</title>
<meta name="description" content="Автономный ИИ-охотник за работой: 24 площадки, автоотклики, отчёты в Telegram">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🎯</text></svg>">
<style>
:root{--bg:#f6f7fb;--panel:#fff;--ink:#14161f;--muted:#6b7084;--line:#e4e6ef;--accent:#4f46e5;--accent-ink:#fff;--good:#0f9d58;--warn:#d97706;--bad:#dc2626;--hot:#ea580c;--chip:#eef0f8;--shadow:0 1px 2px rgba(20,22,31,.06),0 4px 16px rgba(20,22,31,.05)}
@media (prefers-color-scheme:dark){:root{--bg:#0e1016;--panel:#161922;--ink:#e8eaf2;--muted:#9097ad;--line:#262a37;--accent:#818cf8;--accent-ink:#0e1016;--good:#34d399;--warn:#fbbf24;--bad:#f87171;--hot:#fb923c;--chip:#1f2330;--shadow:none}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1200px;margin:0 auto;padding:16px}
header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px}
header h1{font-size:20px;margin:0;font-weight:700;letter-spacing:-.01em}header .sub{color:var(--muted);font-size:13px}
.spacer{flex:1}
.btn{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:10px;padding:8px 14px;font:inherit;cursor:pointer;white-space:nowrap}
.btn:hover{border-color:var(--accent)}.btn.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}
.btn.big{padding:12px 22px;font-size:16px}.btn.sm{padding:4px 10px;font-size:13px;border-radius:8px}.btn:disabled{opacity:.5;cursor:wait}
nav{display:flex;gap:6px;overflow-x:auto;margin-bottom:16px;padding-bottom:2px}
nav button{border:0;background:transparent;color:var(--muted);padding:8px 12px;border-radius:8px;font:inherit;cursor:pointer;white-space:nowrap}
nav button.on{background:var(--panel);color:var(--ink);box-shadow:var(--shadow);font-weight:600}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;box-shadow:var(--shadow)}
.grid{display:grid;gap:12px}.kpis{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.kpi .n{font-size:28px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.1}.kpi .l{color:var(--muted);font-size:13px}
.kpi.hot .n{color:var(--hot)}
.two{grid-template-columns:1.3fr 1fr}@media (max-width:860px){.two{grid-template-columns:1fr}}
h2{font-size:16px;margin:0 0 12px}
.funnel .row{display:grid;grid-template-columns:140px 1fr 48px;align-items:center;gap:8px;margin:6px 0;font-size:14px}
.funnel .bar{height:10px;background:var(--chip);border-radius:6px;overflow:hidden}.funnel .bar i{display:block;height:100%;background:var(--accent);border-radius:6px}
.events{max-height:420px;overflow:auto;font-size:13px}.events div{padding:6px 0;border-bottom:1px solid var(--line)}.events .t{color:var(--muted);margin-right:6px;font-variant-numeric:tabular-nums}
.events .hot{color:var(--hot);font-weight:600}.events .error{color:var(--bad)}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
input,select,textarea{font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 10px;min-width:0}
textarea{width:100%;min-height:90px;resize:vertical}
.list{display:grid;gap:10px}
.item{display:grid;grid-template-columns:56px 1fr auto;gap:12px;align-items:start}
@media (max-width:640px){.item{grid-template-columns:44px 1fr}.item .acts{grid-column:1/-1;justify-content:flex-start}}
.score{width:48px;height:48px;border-radius:12px;display:grid;place-items:center;font-weight:700;font-size:17px;background:var(--chip);font-variant-numeric:tabular-nums}
.score.s3{background:color-mix(in srgb,var(--good) 18%,transparent);color:var(--good)}.score.s2{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn)}
.item .t{font-weight:600}.item .m{color:var(--muted);font-size:13px}.item .v{font-size:13px;margin-top:4px}
.chip{display:inline-block;background:var(--chip);border-radius:999px;padding:1px 8px;font-size:12px;margin-right:4px;color:var(--muted)}
.chip.hot{background:color-mix(in srgb,var(--hot) 18%,transparent);color:var(--hot);font-weight:600}
.acts{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.letter{white-space:pre-wrap;font-size:13px;background:var(--chip);border-radius:10px;padding:10px;margin-top:8px}
.form{display:grid;gap:14px}.form label{display:grid;gap:6px;font-size:14px;font-weight:600}.form .hint{font-weight:400;color:var(--muted);font-size:12px}
.form>*{min-width:0}input,select,textarea{max-width:100%}
.inline{display:flex;gap:10px 16px;flex-wrap:wrap}.inline label{display:flex;gap:8px;align-items:center;font-weight:400;flex-wrap:wrap;min-width:0}
.inline input:not([type=checkbox]){flex:1 1 140px}
section,.card{min-width:0}
.src{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px}.src label{font-weight:400;display:flex;gap:8px;align-items:center}
.conn{display:grid;gap:10px}.conn .row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:10px 0;border-bottom:1px solid var(--line)}
.dot{width:10px;height:10px;border-radius:50%;background:var(--bad);flex:none}.dot.ok{background:var(--good)}
.login{max-width:360px;margin:12vh auto}.login .card{display:grid;gap:12px}
.hidden{display:none!important}.muted{color:var(--muted)}.toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:10px 16px;border-radius:10px;font-size:14px;z-index:9;max-width:90vw}
.hero{display:flex;gap:16px;align-items:center;flex-wrap:wrap;justify-content:space-between}
.hero p{margin:4px 0 0;color:var(--muted);font-size:14px;max-width:640px}
</style>
</head>
<body>
<div id="login" class="login hidden"><div class="card">
  <h1 style="margin:0">🎯 JobHunter</h1><div class="muted">Введите пароль панели</div>
  <input id="pw" type="password" placeholder="Пароль" autocomplete="current-password">
  <button class="btn primary" onclick="login()">Войти</button><div id="lerr" class="muted"></div>
</div></div>

<div id="app" class="wrap hidden">
  <header>
    <div><h1>🎯 JobHunter</h1><div class="sub" id="sub">автономный охотник за работой</div></div>
    <div class="spacer"></div>
    <button class="btn" id="pauseBtn" onclick="togglePause()">⏸ Пауза</button>
    <button class="btn primary" id="runBtn" onclick="run()">🚀 Запустить</button>
  </header>
  <nav id="tabs">
    <button data-t="overview" class="on">Обзор</button><button data-t="vacancies">Вакансии</button><button data-t="apps">Отклики</button>
    <button data-t="settings">Настройки</button><button data-t="connections">Подключения</button>
  </nav>

  <section id="t-overview">
    <div class="card hero" style="margin-bottom:12px">
      <div><h2 style="margin:0">Охота идёт сама</h2><p>Каждые 2 часа (и сразу по кнопке) движок обходит 24 площадки (hh.ru, Хабр Карьера, Telegram-каналы, LinkedIn, Indeed и другие), ИИ оценивает вакансии, пишет письмо на языке вакансии и откликается. Ответы работодателей приходят в Telegram с готовым черновиком ответа.</p></div>
      <button class="btn primary big" onclick="run()">🚀 Запустить сейчас</button>
    </div>
    <div class="grid kpis" id="kpis"></div>
    <div class="grid two" style="margin-top:12px">
      <div class="card"><h2>Воронка</h2><div class="funnel" id="funnel"></div><h2 style="margin-top:18px">Источники</h2><div class="funnel" id="sources"></div></div>
      <div class="card"><h2>Журнал</h2><div class="events" id="events"></div></div>
    </div>
  </section>

  <section id="t-vacancies" class="hidden">
    <div class="filters">
      <input id="fq" placeholder="Поиск по названию/компании" oninput="debounce(loadVacancies)">
      <select id="fs" onchange="loadVacancies()"><option value="">Все статусы</option><option value="scored">Подходят</option><option value="new">Ждут оценки</option><option value="manual">Ждут касания</option><option value="applied">Отклик отправлен</option><option value="skipped">Отсеяны</option></select>
      <select id="fsrc" onchange="loadVacancies()"><option value="">Все площадки</option></select>
    </div>
    <div class="list" id="vlist"></div>
  </section>

  <section id="t-apps" class="hidden"><div class="list" id="alist"></div></section>

  <section id="t-settings" class="hidden"><div class="card"><div class="form" id="sform"></div>
    <div style="margin-top:16px"><button class="btn primary" onclick="saveSettings()">Сохранить</button></div></div></section>

  <section id="t-connections" class="hidden"><div class="card conn" id="conn"></div></section>
</div>
<div id="toast" class="toast hidden"></div>

<script>
const $=s=>document.querySelector(s);const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const SRC={hh:'hh.ru',habr:'Хабр Карьера',getmatch:'getmatch',trudvsem:'Работа России',fl:'FL.ru',telegram:'Telegram-каналы',vk:'VK',linkedin:'LinkedIn',indeed:'Indeed',glassdoor:'Glassdoor',zip_recruiter:'ZipRecruiter',google:'Google Jobs',bayt:'Bayt',naukri:'Naukri',bdjobs:'BDJobs',hellowork:'HelloWork',remoteok:'RemoteOK',remotive:'Remotive',himalayas:'Himalayas',wwr:'We Work Remotely',workingnomads:'Working Nomads',jobicy:'Jobicy',arbeitnow:'Arbeitnow',hn:'Hacker News'};
const APP={draft:'Черновик',waiting_user:'Ждёт касания',sent:'Отправлен',viewed:'Просмотрен',invited:'Приглашение',interview:'Созвон',offer:'Оффер',rejected:'Отказ'};
let S=null,timer=null;
function toast(t){const e=$('#toast');e.textContent=t;e.classList.remove('hidden');clearTimeout(e._t);e._t=setTimeout(()=>e.classList.add('hidden'),4000)}
function debounce(f){clearTimeout(timer);timer=setTimeout(f,300)}
async function api(p,body){const r=await fetch(p,body?{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)}:{});if(r.status===401){show(false);throw new Error('auth')}return r.json()}
function show(ok){$('#login').classList.toggle('hidden',ok);$('#app').classList.toggle('hidden',!ok)}
async function login(){const r=await fetch('/api/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({password:$('#pw').value})});if(r.ok){show(true);init()}else $('#lerr').textContent='Неверный пароль'}
$('#pw').addEventListener('keydown',e=>{if(e.key==='Enter')login()});
document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>tab(b.dataset.t));
function tab(t){document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));document.querySelectorAll('section').forEach(s=>s.classList.toggle('hidden',s.id!=='t-'+t));location.hash=t;({overview:loadOverview,vacancies:loadVacancies,apps:loadApps,settings:loadSettings,connections:loadConn})[t]()}
async function run(){document.querySelectorAll('[onclick="run()"]').forEach(b=>b.disabled=true);toast('Запускаю поиск по всем площадкам…');try{const r=await api('/api/run',{});toast(r.engine);loadOverview()}finally{document.querySelectorAll('[onclick="run()"]').forEach(b=>b.disabled=false)}}
async function togglePause(){const s=await api('/api/settings');s.settings.paused=!s.settings.paused;await api('/api/settings',{settings:s.settings});toast(s.settings.paused?'Пауза':'Продолжаю');loadOverview()}
function kpi(n,l,c=''){return '<div class="card kpi '+c+'"><div class="n">'+n+'</div><div class="l">'+l+'</div></div>'}
function bars(rows,max){return rows.map(([l,n])=>'<div class="row"><span>'+esc(l)+'</span><div class="bar"><i style="width:'+(max?Math.max(2,n/max*100):0)+'%"></i></div><b>'+n+'</b></div>').join('')}
async function loadOverview(){const d=await api('/api/overview');const s=d.stats;
 $('#pauseBtn').textContent=d.paused?'▶️ Продолжить':'⏸ Пауза';
 $('#sub').textContent=(d.paused?'на паузе':'работает')+(d.engine?' · последний сбор '+new Date(d.engine.at).toLocaleString('ru'):'');
 $('#kpis').innerHTML=kpi(s.vacancies,'вакансий найдено')+kpi('+'+s.newToday,'за сутки')+kpi(s.applied,'откликов отправлено')+kpi(s.waitingUser,'ждут одного касания')+kpi(s.replies,'ответили')+kpi(s.hot,'🔥 хотят созвониться','hot');
 const st=Object.fromEntries(s.byStatus.map(r=>[r.status,r.n]));
 const f=[['Найдено',s.vacancies],['Оценено ИИ',s.scored],['Отклики',s.applied+s.waitingUser],['Ответили',s.replies],['Созвоны',s.invited],['Офферы',st.offer||0]];
 $('#funnel').innerHTML=bars(f,s.vacancies);
 const mx=Math.max(1,...s.bySource.map(r=>r.n));$('#sources').innerHTML=bars(s.bySource.map(r=>[SRC[r.source]||r.source,r.n]),mx)||'<div class="muted">Пока пусто: нажмите «Запустить»</div>';
 $('#events').innerHTML=d.events.map(e=>'<div class="'+e.level+'"><span class="t">'+esc((e.at||'').slice(5,16))+'</span>'+esc(e.text)+'</div>').join('')||'<div class="muted">Событий пока нет</div>';
 const sel=$('#fsrc');if(sel.options.length<2)s.bySource.forEach(r=>sel.add(new Option(SRC[r.source]||r.source,r.source)));
}
function sc(n){return n==null?'':(n>=75?'s3':n>=55?'s2':'')}
function money(v){if(!v.salary_from&&!v.salary_to)return'';const c=(v.currency||'').replace(/RUR|RUB/,'₽').replace('USD','$').replace('EUR','€');return (v.salary_from?Number(v.salary_from).toLocaleString('ru'):'')+(v.salary_to?'–'+Number(v.salary_to).toLocaleString('ru'):'')+' '+c}
async function loadVacancies(){const q=new URLSearchParams({q:$('#fq').value,status:$('#fs').value,source:$('#fsrc').value});const rows=await api('/api/vacancies?'+q);
 $('#vlist').innerHTML=rows.map(v=>'<div class="card item"><div class="score '+sc(v.score)+'">'+(v.score??'·')+'</div><div><div class="t"><a href="'+esc(v.url)+'" target="_blank" rel="noopener">'+esc(v.title)+'</a></div>'+
 '<div class="m">'+esc(v.company||'')+' · '+esc(SRC[v.source]||v.source)+(v.remote?' · 🌍 удалённо':v.location?' · '+esc(v.location):'')+(money(v)?' · 💰 '+esc(money(v)):'')+(v.lang==='en'?' · EN':'')+'</div>'+
 (v.fit!=null?'<div class="v"><span class="chip">соответствие '+v.fit+'</span><span class="chip">условия '+v.conditions+'</span><span class="chip">шанс '+v.chance+'</span>'+(v.app_status?'<span class="chip hot">'+(APP[v.app_status]||v.app_status)+'</span>':'')+'</div>':'')+
 (v.verdict?'<div class="v muted">'+esc(v.verdict)+'</div>':'')+'</div><div class="acts">'+(v.app_status&&v.app_status!=='waiting_user'?'':'<button class="btn sm primary" onclick="applyV(this,\\''+esc(v.id)+'\\')">Откликнуться</button>')+'<button class="btn sm" onclick="skipV(\\''+esc(v.id)+'\\',this)">Скрыть</button></div></div>').join('')||'<div class="card muted">Ничего не найдено</div>'}
async function applyV(b,id){b.disabled=true;b.textContent='Пишу письмо…';const r=await api('/api/apply',{id});toast(r.note||r.status);b.textContent=r.status==='sent'?'Отправлено ✓':'Готово — см. Отклики'}
async function skipV(id,b){await api('/api/skip',{id});b.closest('.item').remove()}
async function loadApps(){const rows=await api('/api/applications');
 $('#alist').innerHTML=rows.map(a=>'<div class="card item"><div class="score '+sc(a.score)+'">'+(a.score??'·')+'</div><div><div class="t"><a href="'+esc(a.url)+'" target="_blank" rel="noopener">'+esc(a.title)+'</a> '+(a.hot?'<span class="chip hot">🔥 горячо</span>':'')+'</div>'+
 '<div class="m">'+esc(a.company||'')+' · '+esc(SRC[a.source]||a.source)+' · канал: '+esc(a.channel)+' · '+esc((a.updated_at||'').slice(0,16))+'</div>'+
 (a.last_reply?'<div class="letter"><b>Ответ работодателя:</b>\\n'+esc(a.last_reply)+(a.suggested_answer?'\\n\\n<b>Черновик ответа:</b>\\n'+esc(a.suggested_answer):'')+'</div>':'')+
 '<details><summary class="muted" style="cursor:pointer;font-size:13px">Письмо</summary><div class="letter">'+esc(a.letter)+'</div></details></div>'+
 '<div class="acts"><select onchange="setApp('+a.id+',this.value)">'+Object.entries(APP).map(([k,l])=>'<option value="'+k+'"'+(k===a.status?' selected':'')+'>'+l+'</option>').join('')+'</select></div></div>').join('')||'<div class="card muted">Откликов пока нет</div>'}
async function setApp(id,status){await api('/api/app-status',{id,status});toast('Статус обновлён')}
const lines=a=>(a||[]).join('\\n');const unl=s=>s.split('\\n').map(x=>x.trim()).filter(Boolean);
async function loadSettings(){S=await api('/api/settings');const s=S.settings,p=S.profile;
 $('#sform').innerHTML=
 '<div class="inline"><label><input type="checkbox" id="autoApply" '+(s.autoApply?'checked':'')+'> Автоотклики</label><label><input type="checkbox" id="remoteOnly" '+(s.remoteOnly?'checked':'')+'> Только удалёнка</label><label><input type="checkbox" id="paused" '+(s.paused?'checked':'')+'> Пауза</label></div>'+
 '<div class="inline"><label>Порог автоотклика <input type="number" id="minScoreAuto" value="'+s.minScoreAuto+'" style="width:90px"></label><label>Порог показа <input type="number" id="minScoreNotify" value="'+s.minScoreNotify+'" style="width:90px"></label><label>Откликов в сутки <input type="number" id="dailyLimit" value="'+s.dailyLimit+'" style="width:90px"></label><label>Мин. зарплата, $/мес <input type="number" id="minSalaryUsd" value="'+s.minSalaryUsd+'" style="width:110px"></label></div>'+
 '<label>Площадки<div class="src">'+Object.keys(SRC).map(k=>'<label><input type="checkbox" data-src="'+k+'" '+(s.sources[k]!==false?'checked':'')+'> '+SRC[k]+'</label>').join('')+'</div></label>'+
 '<label>Запросы на русском <span class="hint">по одному в строке</span><textarea id="queriesRu">'+esc(lines(s.queriesRu))+'</textarea></label>'+
 '<label>Запросы на английском<textarea id="queriesEn">'+esc(lines(s.queriesEn))+'</textarea></label>'+
 '<label>Telegram-каналы с вакансиями <span class="hint">username без @, по одному в строке</span><textarea id="telegramChannels">'+esc(lines(s.telegramChannels))+'</textarea></label>'+
 '<label>VK-сообщества <span class="hint">короткое имя или ссылка, нужен VK_SERVICE_TOKEN</span><textarea id="vkGroups">'+esc(lines(s.vkGroups))+'</textarea></label>'+
 '<label>Стоп-слова в названии<textarea id="stopWords">'+esc(lines(s.stopWords))+'</textarea></label>'+
 '<label>Стоп-компании<textarea id="stopCompanies">'+esc(lines(s.stopCompanies))+'</textarea></label>'+
 '<div class="inline"><label>Email отправителя <span class="hint">домен, подтверждённый в Resend</span><input id="emailFrom" value="'+esc(s.emailFrom)+'" placeholder="Влад <vlad@bazadigital.ru>"></label><label>Reply-To<input id="emailReplyTo" value="'+esc(s.emailReplyTo)+'" placeholder="jobs@bazadigital.ru"></label><label>ID резюме hh.ru<input id="hhResumeId" value="'+esc(s.hhResumeId)+'"></label></div>'+
 '<label>Резюме (RU) <span class="hint">ИИ берёт факты только отсюда</span><textarea id="resumeRu" style="min-height:220px">'+esc(p.resumeRu)+'</textarea></label>'+
 '<label>Резюме (EN)<textarea id="resumeEn" style="min-height:220px">'+esc(p.resumeEn)+'</textarea></label>'+
 '<label>Чего хочу<textarea id="wishes">'+esc(p.wishes)+'</textarea></label>'+
 '<div class="inline"><label>Портфолио RU<input id="portfolioRu" value="'+esc(p.portfolioRu)+'"></label><label>Портфолио EN<input id="portfolioEn" value="'+esc(p.portfolioEn)+'"></label><label>Telegram<input id="ptg" value="'+esc(p.telegram)+'"></label><label>Email<input id="pemail" value="'+esc(p.email)+'"></label></div>'}
async function saveSettings(){const s=S.settings,p=S.profile;
 ['autoApply','remoteOnly','paused'].forEach(k=>s[k]=$('#'+k).checked);['minScoreAuto','minScoreNotify','dailyLimit','minSalaryUsd'].forEach(k=>s[k]=Number($('#'+k).value));
 ['queriesRu','queriesEn','telegramChannels','vkGroups','stopWords','stopCompanies'].forEach(k=>s[k]=unl($('#'+k).value));['emailFrom','emailReplyTo','hhResumeId'].forEach(k=>s[k]=$('#'+k).value.trim());
 document.querySelectorAll('[data-src]').forEach(c=>s.sources[c.dataset.src]=c.checked);
 ['resumeRu','resumeEn','wishes','portfolioRu','portfolioEn'].forEach(k=>p[k]=$('#'+k).value);p.telegram=$('#ptg').value;p.email=$('#pemail').value;
 await api('/api/settings',{settings:s,profile:p});toast('Сохранено')}
async function loadConn(){const c=await api('/api/connections');const row=(ok,name,desc,btn='')=>'<div class="row"><span class="dot '+(ok?'ok':'')+'"></span><div style="flex:1;min-width:220px"><b>'+name+'</b><div class="muted" style="font-size:13px">'+desc+'</div></div>'+btn+'</div>';
 $('#conn').innerHTML='<h2>Подключения</h2>'+
 row(true,'ИИ',esc(c.ai))+
 row(c.hh.connected,'hh.ru — автоотклики',c.hh.connected?'Подключено · резюме '+esc(c.hh.resumeId||'не выбрано'):c.hh.configured?'Нажмите, чтобы войти через hh.ru':'Нужны HH_CLIENT_ID и HH_CLIENT_SECRET (dev.hh.ru)',c.hh.authUrl?'<a class="btn sm primary" href="'+esc(c.hh.authUrl)+'">'+(c.hh.connected?'Переподключить':'Подключить hh.ru')+'</a>':'')+
 row(c.telegram.configured&&c.telegram.owner,'Telegram-бот',c.telegram.owner?'Привязан к вашему чату':c.telegram.configured?'Нажмите «Настроить», затем отправьте боту /start &lt;пароль&gt;':'Нужен TELEGRAM_BOT_TOKEN',c.telegram.configured?'<button class="btn sm" onclick="tgSetup()">Настроить вебхук</button>':'')+
 row(c.github.configured,'Движок сбора (GitHub Actions)',c.github.configured?'Репозиторий '+esc(c.github.repo)+(c.engine?' · последний сбор '+new Date(c.engine.at).toLocaleString('ru'):''):'Нужны GITHUB_TOKEN и GITHUB_REPO для кнопки «Запустить»; по расписанию работает и без них')+
 row(c.email.configured,'Email-отклики','Resend: RESEND_API_KEY + email отправителя в настройках')+
 row(c.vk.configured,'VK-сообщества','VK_SERVICE_TOKEN — сервисный ключ VK-приложения')+
 (c.engine?'<h2 style="margin-top:12px">Последний сбор по площадкам</h2><div class="muted" style="font-size:13px">'+Object.entries(c.engine.stats||{}).map(([k,v])=>esc(SRC[k]||k)+': '+esc(v)).join(' · ')+'</div>':'')}
async function tgSetup(){const r=await api('/api/telegram/setup',{});toast(r===true?'Вебхук установлен. Отправьте боту /start <пароль>':'Ответ Telegram: '+JSON.stringify(r))}
async function init(){tab((location.hash||'#overview').slice(1));setInterval(()=>{if(!$('#t-overview').classList.contains('hidden'))loadOverview()},30000)}
fetch('/api/overview').then(r=>{if(r.ok){show(true);init()}else show(false)});
</script>
</body>
</html>`;

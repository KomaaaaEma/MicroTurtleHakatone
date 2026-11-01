import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';

const source=name=>readFileSync(new URL('../static/'+name,import.meta.url),'utf8');
function context(){
 const elements=new Map();
 const ctx=vm.createContext({S:{data:{devices:[],health:{},stats:{}},locationFilters:{},sessionEpoch:1,user:{id:'old',role:'admin'}},labels:{},Date,URLSearchParams,JSON,console,setTimeout:()=>{},structuredClone,location:{hash:'#overview'},document:{},renderPage:()=>{},refresh:()=>{},$:selector=>{if(!elements.has(selector))elements.set(selector,{hidden:false,textContent:'',innerHTML:'',open:false,classList:{toggle(){}},close(){}});return elements.get(selector);},$$:()=>[],esc:String,icon:()=>'',toast:()=>{}});
 vm.runInContext(source('microturtles.js'),ctx);
 vm.runInContext(source('operations.js'),ctx);
 ctx.S.user={id:'old',role:'admin'};
 return ctx;
}
test('late bootstrap cannot resurrect a logged out session',async()=>{
 const ctx=context();let resolve;ctx.api=()=>new Promise(r=>resolve=r);
 const pending=ctx.microRefresh(false);
 ctx.S.sessionEpoch++;ctx.S.user=null;
 resolve({user:{id:'old',role:'admin'},devices:[{id:'private-old'}]});
 await pending;
 assert.equal(ctx.S.user,null);
 assert.deepEqual(ctx.S.data.devices,[]);
});
test('bootstrap from previous location is discarded',async()=>{
 const ctx=context();let resolve;ctx.api=()=>new Promise(r=>resolve=r);
 const pending=ctx.microRefresh(false);
 ctx.S.locationFilters.building_id='new-building';
 resolve({user:ctx.S.user,devices:[{id:'old-building-device'}]});
 await pending;
 assert.deepEqual(ctx.S.data.devices,[]);
});
test('late ticket list is not cached under another account',async()=>{
 const ctx=context();let resolve;ctx.api=()=>new Promise(r=>resolve=r);
 const pending=ctx.loadModule('tickets');
 ctx.S.sessionEpoch++;ctx.S.user={id:'new',role:'resident'};
 vm.runInContext('for(const key of Object.keys(moduleCache))delete moduleCache[key]',ctx);
 resolve([{id:'old-private-ticket'}]);await pending;
 assert.equal(vm.runInContext('moduleCache.tickets',ctx),undefined);
});
test('late incident list is not cached under another selected house',async()=>{
 const ctx=context();let resolve;ctx.api=()=>new Promise(r=>resolve=r);
 const pending=ctx.loadModule('incidents');
 ctx.S.locationFilters.building_id='other';
 resolve([{id:'old-house-incident'}]);await pending;
 assert.equal(vm.runInContext('moduleCache.incidents.status',ctx),'loading');
 assert.equal(vm.runInContext('moduleCache.incidents.data',ctx),undefined);
});
test('private bootstrap does not erase allowed common-device detail',async()=>{
 const ctx=context();ctx.S.user={id:'resident',role:'resident'};
 ctx.S.visibleCommon=[{id:'barrier',location:{scope:'common'}}];
 ctx.S.selected='barrier';ctx.S.page='detail';ctx.S.polling=false;
 ctx.api=async path=>path.startsWith('/bootstrap')?{user:ctx.S.user,devices:[],stats:{},notifications:[],integration:{}}:[];
 ctx.updateChrome=()=>{};ctx.renderPage=()=>{};ctx.loadModule=async()=>{};
 await ctx.microRefresh(false);
 assert.equal(ctx.S.visibleCommon[0].id,'barrier');
});

function pageContext(role='resident'){
 const ctx=context();
 ctx.S.user={id:'current',username:'current',display_name:'Текущий пользователь',role,location:{building_id:'home',apartment:'3007'}};
 ctx.heading=(eyebrow,title,description,actions='')=>`<h1>${title}</h1><p>${description}</p>${actions}`;
 ctx.date=String;ctx.valueLabel=String;ctx.building=()=>({address:'Адрес квартиры'});
 return ctx;
}
function cacheModule(ctx,key,data,status='success'){
 ctx.fixtureData=data;
 vm.runInContext(`moduleCache[${JSON.stringify(key)}]={status:${JSON.stringify(status)},scope:scopeKey(),data:fixtureData,loaded:Date.now()}`,ctx);
}
const meterFixture=[
 {id:'private',name:'Личный ХВС',common:false,value:12,unit:'м³',freshness:{status:'fresh'},readings:[{value:11},{value:12}],manual_allowed:true},
 {id:'common',name:'Общедомовой учёт',common:true,value:123,unit:'м³',freshness:{status:'fresh'},readings:[{value:122},{value:123}]},
];

test('resident meters stay private even when an old common scope is cached',()=>{
 const ctx=pageContext();ctx.S.meterScope='common';cacheModule(ctx,'meters',{meters:meterFixture});
 const html=ctx.metersPage();
 assert.match(html,/Личный ХВС/);
 assert.doesNotMatch(html,/Общедомовой учёт|Общедомовые|meterScope|общие зоны/);
 assert.match(html,/Передать показание/);
 assert.match(html,/История/);
});

for(const role of ['resident','admin']){
 test(`${role} meters show readings and history without graphs`,()=>{
  const ctx=pageContext(role);cacheModule(ctx,'meters',{meters:meterFixture});
  const html=ctx.metersPage();
  assert.match(html,/Последнее показание/);
  assert.match(html,/История/);
  assert.doesNotMatch(html,/<svg|meter-chart|chart-line|Для графика/);
  if(role==='admin')assert.match(html,/Общедомовой учёт/);
 });
 test(`${role} profile has account details without notification settings`,()=>{
  const ctx=pageContext(role);cacheModule(ctx,'profile',{user:ctx.S.user,notification_preferences:{news:true,tickets:true}});
  const html=ctx.profilePage();
  assert.match(html,/<h1>Профиль<\/h1>/);
  assert.match(html,/Текущий пользователь|Логин/);
  assert.doesNotMatch(html,/Настройки уведомлений|data-preference|saveProfilePreferences|Профиль и уведомления/);
 });
}

test('resident requests are usable in overview through loading, error, empty and success',()=>{
 const ctx=pageContext();
 assert.match(ctx.residentTicketsOverview(),/Загружаем данные/);
 vm.runInContext("moduleCache.tickets={status:'error',message:'Сервер недоступен'}",ctx);
 assert.match(ctx.residentTicketsOverview(),/Сервер недоступен/);
 assert.match(ctx.residentTicketsOverview(),/Повторить загрузку/);
 cacheModule(ctx,'tickets',[]);
 assert.match(ctx.residentTicketsOverview(),/пока нет заявок/);
 assert.match(ctx.residentTicketsOverview(),/editTicket\(\)/);
 cacheModule(ctx,'tickets',[{id:'own-request',title:'Проверить клапан',status:'in_progress',created_at:'2026-11-01'}]);
 const html=ctx.residentTicketsOverview();
 assert.match(html,/id="resident-tickets"/);
 assert.match(html,/Проверить клапан/);
 assert.match(html,/В работе/);
 assert.match(html,/openTicket\('own-request'\)/);
 assert.doesNotMatch(html,/#tickets|location.hash='tickets'/);
 ctx.S.user.role='admin';
 assert.equal(ctx.residentTicketsOverview(),'');
});

test('overview stops loading news and retains only relevant operational data',()=>{
 for(const role of ['resident','admin']){
  const ctx=pageContext(role),requested=[];ctx.loadModule=key=>requested.push(key);
  ctx.refreshOperationsOverview();
  assert.deepEqual(requested,role==='resident'?['tickets']:['incidents','tickets']);
  assert.doesNotMatch(ctx.overviewModules(),/Новостей УК|location.hash='news'/);
 }
});

test('resident ticket and purchase submissions return to overview, admin to ticket list',async()=>{
 for(const role of ['resident','admin']){
  const ctx=pageContext(role);ctx.mutateModule=async()=>({id:'request'});ctx.handleModuleNavigation=()=>{};
  ctx.$=()=>({value:'test',open:false});ctx.S.page='market';ctx.location.hash='#market';
  await ctx.orderProduct('product');
  assert.equal(ctx.location.hash,role==='resident'?'overview':'tickets');
  ctx.location.hash='#devices';
  await ctx.submitTicket('device','');
  assert.equal(ctx.location.hash,role==='resident'?'overview':'tickets');
 }
});

test('removed routes stay hidden for both roles and resident-only navigation is enforced',()=>{
 for(const role of ['resident','admin']){
  const ctx=pageContext(role);
  for(const page of ['news','audit','backups'])assert.equal(ctx.canOpenPage(page),false,`${role}: ${page}`);
  for(const page of ['journal','common','tickets'])assert.equal(ctx.canOpenPage(page),role==='admin',`${role}: ${page}`);
  for(const page of ['overview','devices','scenarios','meters','market','profile','notifications'])assert.equal(ctx.canOpenPage(page),true,`${role}: ${page}`);
 }
 const html=source('index.html');
 for(const page of ['news','audit','backups'])assert.doesNotMatch(html,new RegExp(`data-page="${page}"`));
 for(const page of ['journal','common','tickets'])assert.match(html,new RegExp(`<a[^>]+data-admin-only[^>]+data-page="${page}"`));
});

test('both stylesheets use flat surfaces and keep a visible keyboard focus indicator',()=>{
 for(const name of ['style.css','microturtles.css'])assert.doesNotMatch(source(name),/(?:box-shadow|text-shadow)\s*:/);
 assert.match(source('microturtles.css'),/:focus-visible\{outline:2px solid/);
});

test('resident overview retains events and requests without separate journal or news links',()=>{
 const ctx=pageContext();
 Object.assign(ctx.S.data,{devices:[],rules:[],events:[],notifications:[],stats:{}});
 Object.assign(ctx,{residentComfort:()=>'',integrationOverview:()=>'',addButton:()=>'',eventRows:()=>'<p>Последние события</p>'});
 const html=ctx.microOverviewPage();
 assert.match(html,/Последние события/);
 assert.match(html,/id="resident-tickets"/);
 assert.doesNotMatch(html,/#journal|#news|Новости вашего дома|Новостей УК/);
});

test('manual scenario checks return residents to overview and admins to the journal',async()=>{
 const code=source('app.js');
 const fn=code.slice(code.indexOf('async function manualRun('),code.indexOf('\n',code.indexOf('async function manualRun(')));
 for(const role of ['resident','admin']){
  const ctx=pageContext(role);ctx.act=async fn=>fn();ctx.api=async()=>({status:'success'});
  vm.runInContext(fn,ctx);await ctx.manualRun('own-rule');
  assert.equal(ctx.location.hash,role==='admin'?'journal':'overview');
 }
});

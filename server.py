"""Аренда электровелосипедов: сервер Telegram mini app.
Вся бизнес-логика (периоды, неустойка, отсрочки, оплаты, возвраты) считается здесь, интерфейс только показывает и вызывает действия."""
import os,re,json,hmac,hashlib,sqlite3,threading,time,secrets,urllib.request,urllib.error,urllib.parse,datetime as dt
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
TOK=os.environ['BOT_TOKEN'];OWNER=int(os.environ['OWNER_ID'])
MSK=dt.timezone(dt.timedelta(hours=3));L=threading.RLock()
DATA=os.environ.get('DATA_DIR','/data');BK=DATA+'/backups';os.makedirs(BK,exist_ok=True)
TGB=os.environ.get('TG_BASE','https://api.telegram.org')
TQB=os.environ.get('TOCHKA_BASE','https://enter.tochka.com/uapi');MOCK=os.environ.get('TOCHKA_MOCK')=='1'
db=sqlite3.connect(DATA+'/app.db',check_same_thread=False)
db.executescript("create table if not exists docs(id text primary key,kind text,data text);create table if not exists log(ts text,act text,id text,old text,new text);"
"create table if not exists inv(tok text primary key,client text,exp real);create table if not exists bind(tg integer primary key,client text);create table if not exists ntf(k text primary key)")
FAIL={};TRY={};BOT=[os.environ.get('BOT_USERNAME','').lstrip('@') or None]
HD={'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Strict-Transport-Security':'max-age=31536000',
'Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline' https://telegram.org; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self' https://*.telegram.org"}
KINDS=('cfg','admin','client','bike','rent','exp','qr','unk','req')
PERMS=('pay','defer','rents','seize','clients','bikes','exp','reports')
LEN={'w':7,'m':30}
DEF={'week':3500,'month':11000,'pen':2,'tax':7,'acq':0,'cdef':3,'nown':False,'ncli':False,
'cats':['Ремонт','Запчасти','Обслуживание','Реклама','Зарплата','Аренда помещения','Прочее']}
ID=re.compile(r'[a-z0-9]{1,16}')
D=dt.date.fromisoformat
class Bad(Exception):pass
def now():return dt.datetime.now(MSK)
def today():return now().date()
def nid():return secrets.token_hex(5)
def rub(n):return f'{round(n):,}'.replace(',',' ')+' ₽'
def dm(d):return (D(d) if isinstance(d,str) else d).strftime('%d.%m')
def digits(p):return re.sub(r'\D','',p or '')[-10:]
def num(x,lo=0,hi=10**8):
    try:v=float(x)
    except Exception:raise Bad('Неверное число')
    if not lo<=v<=hi:raise Bad('Число вне допустимого диапазона')
    return round(v,2)
def day(x):
    try:return D(str(x))
    except Exception:raise Bad('Неверная дата')
def txt(x,n=200):return str(x or '').strip()[:n]
# ---------- хранилище ----------
def row(i):
    r=db.execute('select kind,data from docs where id=?',(i,)).fetchone();return r and (r[0],json.loads(r[1]))
def doc(i):
    r=row(i);return r and r[1]
def kind(k):return [(i,json.loads(d)) for i,d in db.execute('select id,data from docs where kind=?',(k,))]
def save(i,k,d):db.execute('insert or replace into docs values(?,?,?)',(i,k,json.dumps(d,ensure_ascii=False)))
def put(i,k,d,act,who,old=None):
    d['_v']=(old or {}).get('_v',0)+1;save(i,k,d)
    lj=lambda x:x and json.dumps({a:b for a,b in x.items() if a!='img'},ensure_ascii=False)
    db.execute('insert into log values(?,?,?,?,?)',(now().isoformat(),act+(who and '@'+who or ''),i,lj(old),lj(d)))
def cfg():return {**DEF,**(doc('cfg') or {})}
# ---------- Telegram ----------
def who(init):
    try:
        q=dict(urllib.parse.parse_qsl(init));h=q.pop('hash')
        s='\n'.join(f'{k}={q[k]}' for k in sorted(q))
        key=hmac.new(b'WebAppData',TOK.encode(),hashlib.sha256).digest()
        if hmac.compare_digest(hmac.new(key,s.encode(),hashlib.sha256).hexdigest(),h) and time.time()-int(q['auth_date'])<86400:return json.loads(q['user'])['id']
    except Exception:pass
def tg(method,data):
    return json.load(urllib.request.urlopen(urllib.request.Request(f'{TGB}/bot{TOK}/{method}',json.dumps(data).encode(),{'Content-Type':'application/json'}),timeout=20))
def _send(chat,t):
    try:tg('sendMessage',{'chat_id':chat,'text':t})
    except Exception as e:print('send',e)
def push(chat,t):threading.Thread(target=_send,args=(chat,t),daemon=True).start()
def say(t):
    if cfg().get('nown'):push(OWNER,t)
def tell(cid,t):
    if cfg().get('ncli'):
        for (g,) in db.execute('select tg from bind where client=?',(cid,)).fetchall():push(g,t)
def botname():
    if not BOT[0]:
        try:BOT[0]=tg('getMe',{})['result']['username']
        except Exception as e:print('getMe',e)
    return BOT[0]
# ---------- расчёт аренды ----------
# Аренда: start — начало первого оплачиваемого периода; base — периоды, оплаченные до переноса;
# plan — смены цены/периода с номера периода k; once — цена на один период; defer — отсрочки {from,until}.
def plan(r,k):
    t,p=r['type'],r['price']
    for c in r.get('plan') or []:
        if c['k']<=k:t=c.get('type') or t;p=c['price']
    o=(r.get('once') or {}).get(str(k))
    return t,(p if o is None else o)
def pstart(r,k):
    s=D(r['start'])+dt.timedelta(days=r.get('shift',0))
    for j in range(1,k):s+=dt.timedelta(days=LEN[plan(r,j)[0]])
    return s
def paidn(r):return r.get('base',0)+sum(p.get('n',0) for p in r.get('pays') or [])
def isfree(r,d):return any(D(x['from'])<=d<=D(x['until']) for x in r.get('defer') or [])
def pen(r,due,price,on):
    """Неустойка за период с днём оплаты due на дату on: ставка% от цены периода за каждый день просрочки, кроме дней отсрочки."""
    n=0;d=due+dt.timedelta(days=1)
    while d<=on:
        if not isfree(r,d):n+=1
        d+=dt.timedelta(days=1)
    return round(price*cfg()['pen']/100*n),n
def owed(r,on,cnt=None):
    """Неоплаченные периоды, начиная с первого. cnt=None: все наступившие (минимум один, следующий); иначе ровно cnt."""
    k=paidn(r)+1;s=pstart(r,k);out=[]
    while (len(out)<cnt) if cnt else (s<=on or not out):
        t,p=plan(r,k);pe,dd=pen(r,s,p,on)
        out.append({'k':k,'start':str(s),'type':t,'price':p,'pen':pe,'days':dd,'due':s<=on})
        s+=dt.timedelta(days=LEN[t]);k+=1
        if len(out)>400:break
    return out
def rstate(r,on=None):
    on=on or today()
    if r.get('status')!='active':return {'debt':r.get('debt',0)}
    o=owed(r,on);du=[x for x in o if x['due']];n=o[0];ns=D(n['start'])
    df=next((x['until'] for x in r.get('defer') or [] if D(x['from'])<=on<=D(x['until'])),None)
    return {'next':n['start'],'nprice':n['price'],'ntype':n['type'],'late':max(0,(on-ns).days),'left':(ns-on).days,
            'pen':sum(x['pen'] for x in du),'debt':sum(x['price']+x['pen'] for x in du),'due_n':len(du),'defer':df,'paid':paidn(r),
            'until':str(ns-dt.timedelta(days=1))}
def quote(r,n,on=None):
    o=owed(r,on or today(),n);return {'amt':sum(x['price']+x['pen'] for x in o),'pen':sum(x['pen'] for x in o),'items':o}
def addpay(r,n,amt,pe,method,by,qr=None,ts=None,kind=None):
    p={'ts':ts or now().isoformat(timespec='seconds'),'n':n,'amt':amt,'pen':pe,'method':method,'by':by}
    if qr:p['qr']=qr
    if kind:p['kind']=kind
    r.setdefault('pays',[]).append(p)
def active_of(field,val):
    for i,r in kind('rent'):
        if r.get('status')=='active' and r.get(field)==val:return i,r
# ---------- Точка (СБП) ----------
# Подключение: в Настройках владелец вписывает JWT-токен, ID торговой точки (merchantId) и ID счёта (accountId).
# Запросы сделаны по открытому API Точки (раздел СБП); при подключении сверяем с актуальной документацией.
def tq():
    t=cfg().get('tochka') or {}
    if MOCK:return {'mock':1,**t}
    return t if t.get('jwt') and t.get('merchant') and t.get('account') else None
def tochka(method,path,body=None):
    t=tq();rq=urllib.request.Request(TQB+path,json.dumps(body).encode() if body is not None else None,
        {'Authorization':'Bearer '+t['jwt'],'Content-Type':'application/json'},method=method)
    return json.load(urllib.request.urlopen(rq,timeout=25))
def qr_make(amt,purpose):
    t=tq()
    if not t:raise Bad('Оплата по QR не подключена: впишите данные Точки в Настройках.')
    if MOCK:return {'id':'mock'+nid(),'payload':'https://qr.nspk.ru/MOCK'+nid(),'img':None}
    try:
        r=tochka('POST',f"/sbp/v1.0/qr-code/merchant/{t['merchant']}/{t['account']}",{'Data':{'amount':int(round(amt*100)),'currency':'RUB',
            'paymentPurpose':purpose[:140],'qrcType':'02','ttl':int((dt.datetime.combine(today()+dt.timedelta(days=1),dt.time(),MSK)-now()).total_seconds()//60),
            'imageParams':{'width':400,'height':400,'mediaType':'image/png'}}})['Data']
    except Exception as e:print('tochka qr',e);raise Bad('Точка не ответила, попробуйте ещё раз.')
    return {'id':r['qrcId'],'payload':r['payload'],'img':(r.get('image') or {}).get('content')}
def qr_paid(ids):
    """Какие из QR оплачены (по ответу Точки)."""
    if MOCK or not ids:return set()
    r=tochka('GET','/sbp/v1.0/qr-codes/'+','.join(ids)+'/payment-status')['Data']
    return {x.get('qrcId') for x in r.get('paymentList',[]) if x.get('status')=='Accepted'}
def new_qr(rid,r,n,by):
    """QR на n периодов (или на долг закрытой аренды при n=0). Сумма фиксируется на сегодня, QR действует до конца дня."""
    if n:q=quote(r,n);amt=q['amt'];pe=q['pen']
    else:amt=r.get('debt',0);pe=0
    if amt<=0:raise Bad('Нечего оплачивать')
    c=doc(r['client']) or {};b=doc(r.get('bike') or '') or {}
    m=qr_make(amt,f"Аренда {b.get('num','')} {c.get('name','')}".strip())
    qid=nid();put(qid,'qr',{'rent':rid,'n':n,'amt':amt,'pen':pe,'base':paidn(r),'date':str(today()),'status':'open','tid':m['id'],'payload':m['payload'],'img':m['img'],'ts':now().isoformat(timespec='seconds')},'qr',by)
    return qid
def on_paid(qid,q):
    """Банк подтвердил оплату QR. Засчитываем, только если ничего не изменилось с момента создания QR, иначе — в «Не распознано»."""
    if q.get('status')!='open':return
    old=dict(q);q['status']='paid';put(qid,'qr',q,'qr-paid','',old)
    r=doc(q['rent']);ok=False
    if r and q['n'] and r.get('status')=='active' and paidn(r)==q['base'] and str(today())==q['date']:
        o=dict(r);addpay(r,q['n'],q['amt'],q['pen'],'qr','СБП',qid);put(q['rent'],'rent',r,'pay-qr','',o);ok=True
    elif r and not q['n'] and r.get('status')!='active' and abs(r.get('debt',0)-q['amt'])<1:
        o=dict(r);addpay(r,0,q['amt'],0,'qr','СБП',qid,kind='debt');r['debt']=0;put(q['rent'],'rent',r,'pay-qr','',o);ok=True
    c=(doc((r or {}).get('client','')) or {}).get('name','')
    if ok:say(f"Оплата по QR: {c}, {rub(q['amt'])}.")
    else:
        put(nid(),'unk',{'ts':now().isoformat(timespec='seconds'),'amt':q['amt'],'qr':qid,'rent':q['rent'],'status':'open',
            'why':'Оплата пришла после изменений в аренде или не в тот день'},'unk','')
        say(f"Платёж по QR не распознан: {c}, {rub(q['amt'])}. Разберите в приложении.")
def poll():
    if not tq():return
    with L:
        op={x['tid']:(i,x) for i,x in kind('qr') if x.get('status')=='open' and D(x['date'])>=today()-dt.timedelta(days=2)}
    if not op:return
    try:paid=qr_paid(list(op))
    except Exception as e:print('poll',e);return
    with L:
        for t in paid:
            i,_=op[t];q=doc(i)
            if q:on_paid(i,q)
        db.commit()
# ---------- права и представление ----------
def adm_of(u):
    for i,d in kind('admin'):
        if d.get('tg')==u:return i,d
def role(u):
    if u==OWNER:return 'owner',None
    a=adm_of(u)
    if a:return ('off',a) if a[1].get('disabled') else ('admin',a)
    b=db.execute('select client from bind where tg=?',(u,)).fetchone()
    return ('client',b[0]) if b else ('new',None)
def snapshot(ro,P):
    """Все данные для владельца или администратора; аренды и велосипеды с готовыми расчётами."""
    fin=ro=='owner' or 'reports' in P;out={k:{} for k in KINDS if k!='cfg'};t=today()
    busy={r['bike']:i for i,r in kind('rent') if r.get('status')=='active'}
    for i,k,d in db.execute('select id,kind,data from docs'):
        if k=='cfg':continue
        d=json.loads(d)
        if k=='admin' and ro!='owner':continue
        if k=='exp' and not(fin or 'exp' in P):continue
        if k=='rent':d['st']=rstate(d,t)
        if k=='bike':
            d['rent']=busy.get(i)
            if not fin:d.pop('cost',None);d.pop('sale',None)
        if k=='qr':d.pop('img',None)
        out[k][i]=d
    c=cfg();tc=c.pop('tochka',{}) or {}
    c['tochka']={'on':bool(tq()),'merchant':tc.get('merchant',''),'account':tc.get('account',''),'jwt':bool(tc.get('jwt')),'hook':tc.get('hook','')} if ro=='owner' else {'on':bool(tq())}
    if not fin:c={x:c[x] for x in('week','month','pen','cdef','cats','tochka')}
    return {'docs':out,'cfg':c,'today':str(t)}
def mine(cid):
    c=doc(cid) or {};a=active_of('client',cid);cf=cfg()
    r=None
    if a:
        i,d=a;st=rstate(d);b=doc(d['bike']) or {}
        req=next(({'days':x['days'],'ts':x['ts']} for _,x in kind('req') if x.get('rent')==i and x.get('status')=='pending'),None)
        q1=quote(d,1)
        qr=next(({'id':j,'amt':x['amt'],'n':x['n'],'payload':x['payload'],'img':x.get('img')} for j,x in kind('qr') if x.get('rent')==i and x.get('status')=='open' and x['date']==str(today()) and x['base']==paidn(d)),None)
        r={'id':i,'bike':b.get('name',''),'num':b.get('num',''),'issued':d.get('issued'),'type':d['type'],'st':st,'q1':q1['amt'],
           'pays':[{k:p.get(k) for k in('ts','n','amt','pen','method')} for p in d.get('pays') or []][-30:],'req':req,'qr':qr,
           'defers':[x for x in d.get('defer') or []][-5:]}
    debt=sum(d.get('debt',0) for _,d in kind('rent') if d.get('client')==cid and d.get('status')!='active')
    return {'role':'client','name':c.get('name',''),'rent':r,'debt':debt,'cdef':cf['cdef'],'pen':cf['pen'],'qr':bool(tq())}
# ---------- действия ----------
NEED={'quote':'pay','issue':'rents','return':'rents','price':'rents','contract':'rents','pay':'pay','paydebt':'pay','qr':'pay','qrcancel':'pay','unk':'pay',
'defer':'defer','undefer':'defer','req':'defer','seize':'seize','client':'clients','black':'clients','bike':'bikes','bikestat':'bikes','exp':'exp','expdel':'exp'}
OWNERONLY={'cfg','admin','admindel','fix','paydel'}
def need_v(b,d):
    if b.get('v') is not None and b.get('v')!=d.get('_v'):raise Bad('Данные только что изменились. Обновите экран.')
def get(k,i):
    r=ID.fullmatch(str(i or '')) and row(str(i))
    if not r or r[0]!=k:raise Bad('Запись не найдена')
    return str(i),r[1]
def act(a,b,ro,P,by):
    """Выполняет действие владельца или администратора. Возвращает ответ (dict). Вызывать под замком."""
    if a in OWNERONLY and ro!='owner':raise PermissionError
    if a in NEED and ro=='admin' and NEED[a] not in P:raise PermissionError
    if a not in NEED and a not in OWNERONLY:raise Bad('Неизвестное действие')
    t=today()
    if a=='client':
        i=str(b.get('id') or nid());old=doc(i) if b.get('id') else None
        if b.get('id'):i,old=get('client',i);need_v(b,old)
        d=dict(old or {'created':str(t)})
        d.update(name=txt(b.get('name'),80),phone=txt(b.get('phone'),30),contract=txt(b.get('contract'),40),cdate=txt(b.get('cdate'),10),src=txt(b.get('src'),40),note=txt(b.get('note'),500))
        if not d['name']:raise Bad('Укажите ФИО')
        put(i,'client',d,'client',by,old);return {'id':i}
    if a=='black':
        i,d=get('client',b.get('id'));o=dict(d);d['black']=bool(b.get('on'));d['bnote']=txt(b.get('note'),300);put(i,'client',d,'black',by,o);return {}
    if a=='bike':
        i=str(b.get('id') or nid());old=None
        if b.get('id'):i,old=get('bike',i);need_v(b,old)
        d=dict(old or {'status':'free','created':str(t)})
        d.update(num=txt(b.get('num'),20),name=txt(b.get('name'),80),vin=txt(b.get('vin'),40),note=txt(b.get('note'),500),point=txt(b.get('point'),60))
        if ro=='owner' or 'reports' in P:d.update(cost=num(b.get('cost') or 0),bought=txt(b.get('bought'),10))
        if not d['name']:raise Bad('Укажите название')
        put(i,'bike',d,'bike',by,old);return {'id':i}
    if a=='bikestat':
        i,d=get('bike',b.get('id'));need_v(b,d);s=b.get('status');o=dict(d)
        if s not in('free','repair','sold','off'):raise Bad('Неверный статус')
        if active_of('bike',i):raise Bad('Велосипед сейчас в аренде')
        d['status']=s;d.pop('sale',None);d.pop('gone',None)
        if s=='sold':d['sale']={'amt':num(b.get('amt')),'date':str(day(b.get('date') or t))}
        if s in('sold','off'):d['gone']=str(day(b.get('date') or t))
        put(i,'bike',d,'bikestat',by,o);return {}
    if a=='exp':
        i=str(b.get('id') or nid());old=None
        if b.get('id'):i,old=get('exp',i);need_v(b,old)
        bk=str(b.get('bike') or '')
        if bk:get('bike',bk)
        d={'date':str(day(b.get('date'))),'amt':num(b.get('amt'),0.01),'cat':txt(b.get('cat'),40) or 'Прочее','bike':bk,'title':txt(b.get('title'),120)}
        put(i,'exp',d,'exp',by,old);return {'id':i}
    if a=='expdel':
        i,d=get('exp',b.get('id'));db.execute('delete from docs where id=?',(i,))
        db.execute('insert into log values(?,?,?,?,?)',(now().isoformat(),'expdel@'+by,i,json.dumps(d,ensure_ascii=False),None));return {}
    if a=='issue':
        ci,c=get('client',b.get('client'));bi,bk=get('bike',b.get('bike'))
        if active_of('client',ci):raise Bad('У клиента уже есть активная аренда')
        if active_of('bike',bi) or bk.get('status')!='free':raise Bad('Велосипед не свободен')
        ty=b.get('type')
        if ty not in LEN:raise Bad('Выберите неделю или месяц')
        iss=day(b.get('date') or t)
        r={'client':ci,'bike':bi,'type':ty,'price':num(b.get('price'),1),'issued':str(iss),'free':bool(b.get('free')),
           'start':str(iss+dt.timedelta(days=1 if b.get('free') else 0)),'status':'active','pays':[],'by':by,'contract':txt(b.get('contract'),40)}
        if b.get('first') is not None:r['once']={'1':num(b.get('first'),1)}
        i=nid();n=int(b.get('payn') or 0)
        if n:
            q=quote(r,n,iss);addpay(r,n,q['amt'],q['pen'],'cash' if b.get('method')!='transfer' else 'transfer',by)
        put(i,'rent',r,'issue',by);return {'id':i}
    if a=='quote':
        i,r=get('rent',b.get('id'));n=int(b.get('n') or 1)
        if r.get('status')!='active' or not 1<=n<=24:raise Bad('Нечего оплачивать')
        q=quote(r,n);return {'amt':q['amt'],'pen':q['pen'],'from':q['items'][0]['start'],'to':str(D(q['items'][-1]['start'])+dt.timedelta(days=LEN[q['items'][-1]['type']]-1))}
    if a in('pay','paydebt','qr','qrcancel','return','seize','price','defer','undefer','contract','fix','paydel'):
        i,r=get('rent',b.get('id'));need_v(b,r);o=json.loads(json.dumps(r))
        act_=r.get('status')=='active'
        if a in('pay','qr','return','seize','price','defer','undefer','fix') and not act_ and not(a=='qr' and r.get('debt')):raise Bad('Аренда уже закрыта')
        if a=='pay':
            n=int(b.get('n') or 1)
            if not 1<=n<=24:raise Bad('Неверное число периодов')
            m=b.get('method')
            if m not in('cash','transfer','qr'):raise Bad('Выберите способ оплаты')
            q=quote(r,n);addpay(r,n,q['amt'],q['pen'],m,by);put(i,'rent',r,'pay',by,o)
            # открытые QR этой аренды больше не действуют
            for j,x in kind('qr'):
                if x.get('rent')==i and x.get('status')=='open':ox=dict(x);x['status']='void';put(j,'qr',x,'qr-void',by,ox)
            return {'amt':q['amt']}
        if a=='paydebt':
            if act_ or not r.get('debt'):raise Bad('Долга нет')
            m=b.get('method') if b.get('method') in('cash','transfer','qr') else 'cash'
            addpay(r,0,r['debt'],0,m,by,kind='debt');r['debt']=0;put(i,'rent',r,'paydebt',by,o);return {}
        if a=='qr':
            n=0 if not act_ else int(b.get('n') or 1)
            if act_ and not 1<=n<=12:raise Bad('Неверное число периодов')
            return {'qr':new_qr(i,r,n,by)}
        if a=='qrcancel':
            j,x=get('qr',b.get('qr'))
            if x.get('status')=='open':ox=dict(x);x['status']='void';put(j,'qr',x,'qr-void',by,ox)
            return {}
        if a=='paydel':
            k=int(b.get('k',-1));ps=r.get('pays') or []
            if not 0<=k<len(ps):raise Bad('Платёж не найден')
            ps.pop(k);put(i,'rent',r,'paydel',by,o);return {}
        if a=='price':
            p=num(b.get('price'),1);k=paidn(r)+1;ty=b.get('type') or plan(r,k)[0]
            if ty not in LEN:raise Bad('Неверный период')
            if b.get('once'):
                if ty!=plan(r,k)[0]:raise Bad('Разовая цена не меняет период')
                r.setdefault('once',{})[str(k)]=p
            else:
                r['plan']=[c for c in r.get('plan') or [] if c['k']<k]+[{'k':k,'type':ty,'price':p,'ts':str(t),'by':by}]
                r.get('once',{}).pop(str(k),None)
            put(i,'rent',r,'price',by,o);return {}
        if a=='defer':
            u=day(b.get('until'))
            if u<t:raise Bad('Дата уже прошла')
            r.setdefault('defer',[]).append({'from':str(t),'until':str(u),'by':by,'ts':now().isoformat(timespec='seconds')})
            put(i,'rent',r,'defer',by,o);tell(r['client'],f'Вам дана отсрочка без неустойки до {dm(u)}.');return {}
        if a=='undefer':
            r['defer']=[x for x in r.get('defer') or [] if D(x['until'])<t];put(i,'rent',r,'undefer',by,o);return {}
        if a=='contract':
            r['contract']=txt(b.get('contract'),40);put(i,'rent',r,'contract',by,o);return {}
        if a=='fix':
            nx=day(b.get('next'));cur=D(rstate(r,t)['next']);r['shift']=r.get('shift',0)+(nx-cur).days
            put(i,'rent',r,'fix',by,o);return {}
        st=rstate(r,t)
        if a=='return':
            debt=num(b.get('debt') if b.get('debt') is not None else st['debt']);ref=num(b.get('refund') or 0)
            r.update(status='closed',closed=str(t),debt=debt,refund={'amt':ref,'date':str(t)} if ref else None,debt0=st['debt'])
            put(i,'rent',r,'return',by,o);return {}
        if a=='seize':
            if b.get('confirm')!='ИЗЪЯТЬ':raise Bad('Нужно подтверждение')
            r.update(status='seized',closed=str(t),debt=0,writeoff=st['debt'])
            put(i,'rent',r,'seize',by,o);return {}
    if a=='unk':
        i,u=get('unk',b.get('id'));o=dict(u)
        if u.get('status')!='open':raise Bad('Уже разобрано')
        if b.get('do')=='apply':
            ri,r=get('rent',b.get('rent'));n=int(b.get('n') or 0);ro_=json.loads(json.dumps(r))
            if r.get('status')=='active':
                if not 1<=n<=24:raise Bad('Укажите число периодов')
                addpay(r,n,u['amt'],0,'qr',by,u.get('qr'))
            else:addpay(r,0,u['amt'],0,'qr',by,u.get('qr'),kind='debt');r['debt']=max(0,r.get('debt',0)-u['amt'])
            put(ri,'rent',r,'unk-apply',by,ro_);u['status']='applied';u['to']=ri
        else:u['status']='done';u['note']=txt(b.get('note'),200)
        put(i,'unk',u,'unk',by,o);return {}
    if a=='req':
        i,q=get('req',b.get('id'));o=dict(q)
        if q.get('status')!='pending':raise Bad('Заявка уже рассмотрена')
        ri,r=get('rent',q['rent'])
        if b.get('ok') and r.get('status')=='active':
            ro_=json.loads(json.dumps(r));ns=D(rstate(r,t)['next'])
            u=max(ns,t)+dt.timedelta(days=q['days'])
            r.setdefault('defer',[]).append({'from':str(t),'until':str(u),'by':by,'ts':now().isoformat(timespec='seconds'),'req':i})
            put(ri,'rent',r,'defer',by,ro_);q['status']='ok';q['until']=str(u);tell(r['client'],f'Отсрочка одобрена: без неустойки до {dm(u)}.')
        else:q['status']='no';tell(r['client'],'Заявка на отсрочку отклонена.')
        q['by']=by;put(i,'req',q,'req',by,o);return {}
    if a=='cfg':
        old=doc('cfg');c=cfg();o=json.loads(json.dumps(old or {}))
        for k in('week','month'):c[k]=num(b.get(k,c[k]),1)
        c['pen']=num(b.get('pen',c['pen']),0,100);c['tax']=num(b.get('tax',c['tax']),0,100);c['acq']=num(b.get('acq',c['acq']),0,100)
        c['cdef']=int(num(b.get('cdef',c['cdef']),0,3))
        c['nown']=bool(b.get('nown',c['nown']));c['ncli']=bool(b.get('ncli',c['ncli']))
        if isinstance(b.get('cats'),list):c['cats']=[txt(x,40) for x in b['cats'] if txt(x,40)][:30] or DEF['cats']
        if isinstance(b.get('tochka'),dict):
            tc=dict(c.get('tochka') or {});x=b['tochka']
            tc['merchant']=txt(x.get('merchant'),60);tc['account']=txt(x.get('account'),80)
            if x.get('jwt'):tc['jwt']=str(x['jwt']).strip()[:5000]
            if x.get('clear'):tc={}
            tc.setdefault('hook',secrets.token_urlsafe(18)) if tc else None
            c['tochka']=tc
        c['_v']=(old or {}).get('_v',0)+1;save('cfg','cfg',c)
        if o.get('tochka',{}).get('jwt'):o['tochka']['jwt']='***'
        lg=dict(c)
        if lg.get('tochka',{}).get('jwt'):lg['tochka']=dict(lg['tochka'],jwt='***')
        db.execute('insert into log values(?,?,?,?,?)',(now().isoformat(),'cfg',  'cfg',json.dumps(o,ensure_ascii=False),json.dumps(lg,ensure_ascii=False)));return {}
    if a=='admin':
        i=str(b.get('id') or nid());old=None
        if b.get('id'):i,old=get('admin',i)
        nm=txt(b.get('name'),60);ph=txt(b.get('phone'),30)
        if not nm or not digits(ph):raise Bad('Укажите имя и телефон')
        pl=b.get('perms') if isinstance(b.get('perms'),list) else []
        d={'name':nm,'phone':ph,'perms':[q for q in PERMS if q in pl],'tg':(old or {}).get('tg'),'disabled':bool(b.get('disabled')),'created':(old or {}).get('created',str(t))}
        put(i,'admin',d,'admin',by,old);return {'id':i}
    if a=='admindel':
        i,d=get('admin',b.get('id'));db.execute('delete from docs where id=?',(i,))
        db.execute('insert into log values(?,?,?,?,?)',(now().isoformat(),'admindel',i,json.dumps(d,ensure_ascii=False),None));return {}
    raise Bad('Неизвестное действие')
def client_act(cid,b):
    a=active_of('client',cid)
    if not a:raise Bad('Активной аренды нет')
    i,r=a
    if b.get('a')=='quote':
        n=int(b.get('n') or 1)
        if not 1<=n<=6:raise Bad('Неверное число периодов')
        q=quote(r,n);return {'amt':q['amt'],'pen':q['pen'],'to':str(D(q['items'][-1]['start'])+dt.timedelta(days=LEN[q['items'][-1]['type']]-1))}
    if b.get('a')=='qr':
        n=int(b.get('n') or 1)
        if not 1<=n<=6:raise Bad('Неверное число периодов')
        for j,x in kind('qr'):
            if x.get('rent')==i and x.get('status')=='open' and x['date']==str(today()) and x['base']==paidn(r) and x['n']==n:return {'qr':j}
        return {'qr':new_qr(i,r,n,'клиент')}
    if b.get('a')=='req':
        n=int(b.get('days') or 0);mx=cfg()['cdef']
        if not 1<=n<=mx:raise Bad(f'Отсрочку можно попросить на 1–{mx} дн.')
        if any(x.get('rent')==i and x.get('status')=='pending' for _,x in kind('req')):raise Bad('Заявка уже отправлена')
        put(nid(),'req',{'rent':i,'client':cid,'days':n,'ts':now().isoformat(timespec='seconds'),'status':'pending'},'req-new','клиент')
        say(f"Заявка на отсрочку: {(doc(cid) or {}).get('name','')}, {n} дн. Одобрите или отклоните в приложении.");return {}
    raise Bad('Неизвестное действие')
# ---------- копии ----------
def dump():
    with L:
        ds=[]
        for i,k,x in db.execute('select id,kind,data from docs'):
            d=json.loads(x)
            if k=='cfg' and d.get('tochka',{}).get('jwt'):d=dict(d,tochka=dict(d['tochka'],jwt=''))
            ds.append({'id':i,'kind':k,'data':d})
        return json.dumps({'v':1,'app':'rent','ts':now().isoformat(),'docs':ds,'bind':[[a,c] for a,c in db.execute('select tg,client from bind')],
            'log':[list(r) for r in db.execute('select ts,act,id,old,new from log')]},ensure_ascii=False).encode()
DL={};TTL=int(os.environ.get('DL_TTL','120'));BS=DATA+'/last_download.json'
def bget():
    try:return json.load(open(BS))
    except Exception:return None
def bset():
    try:open(BS,'w').write(json.dumps({'ts':now().isoformat()}))
    except Exception as e:print('bset',e)
def snap(pre):
    b=sqlite3.connect(f'{BK}/{pre}-{now():%Y-%m-%d-%H%M%S}.db');db.backup(b);b.close()
    for f in sorted(x for x in os.listdir(BK) if x.startswith(pre+'-'))[:-(30 if pre=='app' else 5)]:os.remove(f'{BK}/{f}')
def restore(b):
    """Заменяет все данные содержимым файла (копия или файл переноса). Журнал сохраняется. Ключ Точки остаётся текущий."""
    try:
        if not isinstance(b,dict) or b.get('v')!=1 or b.get('app')!='rent':return 400,'Это не файл копии приложения аренды.'
        docs=b['docs'];bind=b.get('bind',[]);lg=b.get('log',[])
        if not isinstance(docs,list) or not isinstance(bind,list) or not isinstance(lg,list) or len(docs)>50000:return 400,'Файл повреждён.'
        seen=set();rows=[];cur=doc('cfg') or {}
        for x in docs:
            i,k,d=x['id'],x['kind'],x['data']
            if not isinstance(i,str) or not (ID.fullmatch(i) or i=='cfg') or k not in KINDS or not isinstance(d,dict) or i in seen:return 400,'Файл повреждён: неверная запись.'
            if k=='cfg':d=dict(d,tochka=cur.get('tochka',{}) if not d.get('tochka',{}).get('jwt') else d['tochka'])
            seen.add(i);rows.append((i,k,json.dumps(d,ensure_ascii=False)))
        bi=[(int(a),str(c)) for a,c in bind if ID.fullmatch(str(c))]
        lg=[tuple(None if y is None else str(y) for y in r) for r in lg if isinstance(r,list) and len(r)==5][-100000:]
    except Exception:return 400,'Файл повреждён.'
    with L:
        snap('pre-restore')
        try:
            empty=not db.execute('select 1 from log limit 1').fetchone()
            db.execute('delete from docs');db.execute('delete from bind');db.execute('delete from inv')
            db.executemany('insert into docs values(?,?,?)',rows);db.executemany('insert or replace into bind values(?,?)',bi)
            if empty:db.executemany('insert into log values(?,?,?,?,?)',lg)
            db.execute('insert into log values(?,?,?,?,?)',(now().isoformat(),'restore','',None,json.dumps({'docs':len(rows),'from':b.get('ts')})));db.commit()
        except Exception as e:
            db.rollback();print('restore',e);return 500,'Не удалось загрузить, данные не изменены.'
    return 200,len(rows)
# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def log_message(s,*a):pass
    def send(s,c,b,t='application/json',x=None):
        if isinstance(b,(dict,list)):b=json.dumps(b,ensure_ascii=False).encode()
        s.send_response(c);s.send_header('Content-Type',t);s.send_header('Content-Length',str(len(b)))
        for k,v in {**HD,**(x or {})}.items():s.send_header(k,v)
        s.end_headers();s.wfile.write(b)
    def ip(s):return s.headers.get('X-Forwarded-For','').split(',')[0].strip() or s.client_address[0]
    def user(s):
        ip=s.ip();t=time.time()
        FAIL[ip]=[x for x in FAIL.get(ip,[]) if x>t-600]
        if len(FAIL[ip])>=10:return None
        u=who(s.headers.get('X-Init',''))
        if u is None:FAIL[ip].append(t)
        return u
    def do_GET(s):
        p=s.path.split('?')[0].split('#')[0]
        if p in('/api/all','/api/me','/api/log'):
            u=s.user()
            if u is None:return s.send(403,{})
            with L:
                ro,x=role(u)
                if p=='/api/all':
                    if ro not in('owner','admin'):return s.send(403,{})
                    r=snapshot(ro,set(x[1].get('perms',[])) if ro=='admin' else set(PERMS))
                elif p=='/api/log':
                    if ro!='owner':return s.send(403,{})
                    r=[list(q) for q in db.execute('select ts,act,id,old,new from log order by rowid desc limit 300')]
                elif ro=='owner':r={'role':'owner','backup':bget(),'empty':not db.execute("select 1 from docs where kind in('rent','bike','client') limit 1").fetchone()}
                elif ro=='admin':r={'role':'admin','name':x[1].get('name',''),'perms':[q for q in x[1].get('perms',[]) if q in PERMS]}
                elif ro=='client':r=mine(x)
                else:r={'role':ro}
            return s.send(200,r)
        if p.startswith('/api/qr/'):
            u=s.user()
            if u is None:return s.send(403,{})
            with L:
                ro,x=role(u);q=ID.fullmatch(p[8:]) and doc(p[8:])
                if not q or 'payload' not in q:return s.send(404,{})
                if ro=='client' and (doc(q['rent']) or {}).get('client')!=x:return s.send(403,{})
                if ro not in('owner','admin','client'):return s.send(403,{})
                return s.send(200,{k:q.get(k) for k in('amt','n','pen','payload','img','status','date')})
        if p.startswith('/dl/'):
            ip=('dl',s.ip());t=time.time();FAIL[ip]=[x for x in FAIL.get(ip,[]) if x>t-600]
            for a in [a for a,e in DL.items() if e<t]:DL.pop(a,None)
            if len(FAIL[ip])>=10 or DL.get(p[4:],0)<t:FAIL[ip].append(t);return s.send(404,b'')
            d=dump();bset()
            return s.send(200,d,'application/octet-stream',{'Content-Disposition':f'attachment; filename="arenda-{now():%Y-%m-%d-%H%M}.json"','Cache-Control':'no-store'})
        f={'/':'index.html'}.get(p)
        if not f:return s.send(404,b'')
        s.send(200,open(os.path.join(os.path.dirname(os.path.abspath(__file__)),'static',f),'rb').read(),'text/html; charset=utf-8',{'Cache-Control':'no-cache'})
    def do_POST(s):
        p=s.path.split('?')[0]
        if p.startswith('/hook/tochka/'):
            # Точка сообщает о входящем платеже. Тело не проверяем, а сразу сверяем статусы открытых QR через API.
            tc=(cfg().get('tochka') or {}).get('hook')
            if not tc or not hmac.compare_digest(p[13:],tc):return s.send(404,b'')
            s.rfile.read(min(int(s.headers.get('Content-Length',0)),100000));threading.Thread(target=poll,daemon=True).start();return s.send(200,{})
        u=s.user()
        if u is None:return s.send(403,{})
        n=int(s.headers.get('Content-Length',0))
        if n>(40000000 if p=='/api/restore' and u==OWNER else 200000):return s.send(413,{})
        try:b=json.loads(s.rfile.read(n))
        except Exception:return s.send(400,{})
        if not isinstance(b,dict):return s.send(400,{})
        t=time.time()
        if u==OWNER and p=='/api/dl':
            k=secrets.token_urlsafe(16);DL[k]=t+TTL;return s.send(200,{'url':'/dl/'+k})
        if u==OWNER and p=='/api/restore':
            c,r=restore(b);return s.send(c,{'docs':r} if c==200 else {'err':r})
        with L:
            ro,x=role(u)
            try:
                if ro in('owner','admin') and p=='/api/act':
                    P=set(x[1].get('perms',[])) if ro=='admin' else set(PERMS)
                    by='Владелец' if ro=='owner' else str(x[1].get('name',''))[:40]
                    r=act(str(b.get('a','')),b,ro,P,by);db.commit();return s.send(200,r)
                if ro=='owner' and p=='/api/mockpay' and MOCK:
                    i,q=get('qr',b.get('qr'));on_paid(i,q);db.commit();return s.send(200,{})
                if ro in('owner','admin') and p in('/api/invite','/api/unbind'):
                    c=str(b.get('client',''));adm=c.startswith('adm:')
                    a=ID.fullmatch(c[4:] if adm else c) and row(c[4:] if adm else c)
                    if adm and (ro!='owner' or not a or a[0]!='admin'):return s.send(403,{})
                    if not adm and ((ro=='admin' and 'clients' not in x[1].get('perms',[])) or not a or a[0]!='client'):return s.send(403,{})
                    if p=='/api/invite':
                        if not botname():return s.send(500,{'err':'Бот недоступен'})
                        k=secrets.token_urlsafe(9);db.execute('insert into inv values(?,?,?)',(k,c,t+7*86400));db.commit()
                        return s.send(200,{'tok':k,'bot':BOT[0]})
                    if adm:a[1]['tg']=None;save(c[4:],'admin',a[1])
                    else:db.execute('delete from bind where client=?',(c,))
                    db.commit();return s.send(200,{})
                if ro=='new' and p=='/api/claim':
                    TRY[u]=[q for q in TRY.get(u,[]) if q>t-3600]
                    if len(TRY[u])>=5:return s.send(429,{})
                    TRY[u].append(t)
                    i=db.execute('select client from inv where tok=? and exp>?',(str(b.get('tok')),t)).fetchone();ph=digits(b.get('phone'));tgt=i and i[0]
                    if tgt and tgt.startswith('adm:'):
                        a=row(tgt[4:]);c=a and a[0]=='admin' and not a[1].get('disabled') and a[1]
                        if c and ph and digits(c.get('phone'))==ph:
                            c['tg']=u;save(tgt[4:],'admin',c);db.execute('delete from inv where tok=?',(str(b.get('tok')),));db.commit()
                            say(f"Администратор {c.get('name')} вошёл в приложение аренды.");return s.send(200,{})
                    else:
                        c=tgt and doc(tgt)
                        if c and ph and digits(c.get('phone'))==ph:
                            db.execute('insert or replace into bind values(?,?)',(u,tgt));db.execute('delete from inv where tok=?',(str(b.get('tok')),));db.commit()
                            say(f"Клиент {c.get('name')} вошёл в приложение аренды.");return s.send(200,{})
                    if len(TRY[u])==3:say(f'Неудачные попытки входа (Telegram ID {u}).')
                    return s.send(403,{})
                if ro=='client' and p=='/api/c':
                    r=client_act(x,b);db.commit();return s.send(200,r)
            except Bad as e:
                db.rollback();return s.send(400,{'err':str(e)})
            except PermissionError:
                db.rollback();return s.send(403,{'err':'Нет прав'})
            except Exception as e:
                db.rollback();print('act',repr(e));return s.send(500,{'err':'Ошибка сервера'})
        return s.send(403,{})
# ---------- расписание ----------
def morning(t):
    """10:00: сводка владельцу и напоминания клиентам (накануне и в день оплаты). Только если включено в Настройках."""
    out=[]
    with L:
        cf=cfg()
        def once(k):
            if db.execute('select 1 from ntf where k=?',(k,)).fetchone():return False
            db.execute('insert into ntf values(?)',(k,));return True
        rs=[(i,r,rstate(r,t)) for i,r in kind('rent') if r.get('status')=='active']
        if cf['nown'] and once(f'{t}:daily'):
            late=sum(1 for _,_,s in rs if s['late']>0);due=sum(1 for _,_,s in rs if s['left']==0)
            rq=sum(1 for _,x in kind('req') if x.get('status')=='pending')
            out.append((OWNER,f'Сегодня платят: {due}. Просрочено: {late}.'+(f' Заявок на отсрочку: {rq}.' if rq else '')))
        if cf['ncli']:
            for i,r,s in rs:
                if s['left'] in(0,1) and once(f"{t}:rem:{i}:{s['next']}"):
                    m=(f"Напоминание: завтра, {dm(s['next'])}, оплата аренды {rub(s['nprice'])}." if s['left'] else f"Сегодня день оплаты аренды: {rub(s['nprice'])}.")
                    for (g,) in db.execute('select tg from bind where client=?',(r['client'],)).fetchall():out.append((g,m))
        db.execute('delete from ntf where k<?',(str(t-dt.timedelta(days=60)),));db.commit()
    for c,x in out:push(c,x)
def job():
    last=None;lp=0
    while 1:
        n=now();key=(n.date(),n.hour)
        if key!=last and n.hour in(3,10):
            last=key
            try:
                if n.hour==3:
                    with L:snap('app')
                else:morning(n.date())
            except Exception as e:print(e)
        if time.time()-lp>150:
            lp=time.time()
            try:poll()
            except Exception as e:print('poll',e)
        time.sleep(20)
if __name__=='__main__':
    threading.Thread(target=job,daemon=True).start()
    threading.Thread(target=botname,daemon=True).start()
    ThreadingHTTPServer(('',int(os.environ.get('PORT','8080'))),H).serve_forever()

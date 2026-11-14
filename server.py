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
FAIL={};TRY={};UN={};BOT=[os.environ.get('BOT_USERNAME','').lstrip('@') or None]
HD={'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Strict-Transport-Security':'max-age=31536000',
'Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline' https://telegram.org; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self' https://*.telegram.org"}
KINDS=('cfg','admin','client','bike','rent','exp','qr','unk','req','bill')
PERMS=('pay','partial','bill','defer','pen','rents','seize','clients','bikes','exp','reports')
LEN={'w':7,'m':30}
DEF={'week':3500,'month':11000,'pen':5,'grace':2,'ddays':3,'archdays':30,'cphone':'','ctg':'','tax':7,'acq':0,'cdef':3,'nown':False,'ncli':False,
'cats':['Ремонт','Запчасти','Обслуживание','Реклама','Зарплата','Аренда помещения','Прочее']}
ID=re.compile(r'[a-z0-9]{1,16}')
D=dt.date.fromisoformat
class Bad(Exception):pass
class Dup(Exception):pass
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
        if hmac.compare_digest(hmac.new(key,s.encode(),hashlib.sha256).hexdigest(),h) and time.time()-int(q['auth_date'])<86400:
            u=json.loads(q['user']);UN[u['id']]=str(u.get('username') or '')[:40];return u['id']
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
def due_of(r,k,s):
    """День оплаты периода k, который начинается s: последний оплаченный день перед ним.
    Первый период оплачивается в день выдачи (не раньше)."""
    d=s-dt.timedelta(days=1)
    return max(d,D(r.get('issued') or r['start'])) if k==1 else d
def pen(r,due,price,on):
    """Неустойка за период с днём оплаты due на дату on: ставка% от цены периода за каждый день просрочки,
    начиная с (льгота+1)-го дня. Не считаются дни отсрочки, списанные дни и аренды с отключённой неустойкой."""
    if r.get('nopen'):return 0,0
    c=cfg();g=int(c.get('grace',2));w=D(r['waive']) if r.get('waive') else None
    n=0;d=due+dt.timedelta(days=g+1)
    while d<=on:
        if not isfree(r,d) and not(w and d<=w):n+=1
        d+=dt.timedelta(days=1)
    return round(price*c['pen']/100*n),n
def owed(r,on,cnt=None):
    """Неоплаченные периоды, начиная с первого. cnt=None: все, чей день оплаты наступил (минимум один); иначе ровно cnt."""
    k=paidn(r)+1;s=pstart(r,k);out=[]
    while len(out)<400:
        t,p=plan(r,k);du=due_of(r,k,s)
        if (len(out)>=cnt) if cnt else (out and du>on):break
        pe,dd=pen(r,du,p,on)
        out.append({'k':k,'start':str(s),'type':t,'price':p,'pen':pe,'days':dd,'due':du<=on,'pay':str(du)})
        s+=dt.timedelta(days=LEN[t]);k+=1
    return out
def rstate(r,on=None):
    on=on or today()
    if r.get('status')!='active':return {'debt':r.get('debt',0)}
    o=owed(r,on);du=[x for x in o if x['due']];n=o[0];nd=D(n['pay'])
    df=next((x['until'] for x in r.get('defer') or [] if D(x['from'])<=on<=D(x['until'])),None)
    return {'next':n['pay'],'start':n['start'],'nprice':n['price'],'ntype':n['type'],'late':max(0,(on-nd).days),'left':(nd-on).days,
            'pen':sum(x['pen'] for x in du),'debt':sum(x['price']+x['pen'] for x in du),'due_n':len(du),'defer':df,'paid':paidn(r),
            'until':str(D(n['start'])-dt.timedelta(days=1)),'pdays':max(0,(D(n['start'])-on).days),'pfrom':str(nd+dt.timedelta(days=int(cfg().get('grace',2))+1)),'nopen':bool(r.get('nopen')),
            **dstate(r,on)}
# Долг активной аренды (частичная оплата): r['dl'] — список {id,amt,due,pen,pdate}. pen — уже начисленная и зафиксированная неустойка,
# pdate — день последней фиксации (дальше неустойка считается с pdate+1).
def daccr(r,it,on):
    """Неустойка по долгу, набежавшая после последней фиксации: ставка% от остатка за каждый день после срока и льготных дней."""
    if r.get('nopen'):return 0
    c=cfg();g=int(c.get('grace',2));w=D(r['waive']) if r.get('waive') else None
    d=D(it['due'])+dt.timedelta(days=g+1)
    if it.get('pdate'):d=max(d,D(it['pdate'])+dt.timedelta(days=1))
    n=0
    while d<=on:
        if not isfree(r,d) and not(w and d<=w):n+=1
        d+=dt.timedelta(days=1)
    return round(it['amt']*c['pen']/100*n)
def dstate(r,on):
    its=r.get('dl') or []
    if not its:return {'dl':0,'dpen':0,'dprin':0}
    pen=sum(it.get('pen',0)+daccr(r,it,on) for it in its);amt=sum(it['amt'] for it in its);due=min(D(it['due']) for it in its)
    return {'dl':round(amt+pen,2),'dpen':pen,'dprin':round(amt,2),'ddue':str(due),'dlate':max(0,(on-due).days),
            'dpfrom':str(due+dt.timedelta(days=int(cfg().get('grace',2))+1))}
def pay_dl(r,amt,on):
    """Гасит долг суммой amt: сначала фиксирует неустойку, затем гасит неустойку и сам долг (самые ранние первыми). Возвращает остаток."""
    its=sorted(r.get('dl') or [],key=lambda x:x['due'])
    for it in its:it['pen']=it.get('pen',0)+daccr(r,it,on);it['pdate']=str(on)
    for it in its:
        x=min(amt,it['pen']);it['pen']=round(it['pen']-x,2);amt-=x
        x=min(amt,it['amt']);it['amt']=round(it['amt']-x,2);amt-=x
    r['dl']=[it for it in its if it['amt']>0.005 or it['pen']>0.005]
    return round(amt,2)
def add_dl(r,amt,due,by):
    r.setdefault('dl',[]).append({'id':nid(),'amt':round(amt,2),'orig':round(amt,2),'due':str(due),'pen':0,'ts':now().isoformat(timespec='seconds'),'by':by})
def apply_money(r,kind_,amt,method,by,due=None,qr=None,on=None):
    """Зачисляет деньги по выставленному платежу. kind_='period': за следующий период (меньше полной суммы — частичная оплата, остаток в долг);
    'debt': в счёт долга. Возвращает лишнее (если заплатили больше, чем нужно)."""
    on=on or today()
    if kind_=='period':
        q=quote(r,1,on)
        if amt+0.005<q['amt']:
            addpay(r,1,amt,0,method,by,qr,kind='part');add_dl(r,q['amt']-amt,due or on+dt.timedelta(days=int(cfg()['ddays'])),by);return 0
        addpay(r,1,q['amt'],q['pen'],method,by,qr);amt=round(amt-q['amt'],2)
        if amt<=0.005:return 0
    left=pay_dl(r,amt,on)
    if amt-left>0.005:addpay(r,0,round(amt-left,2),0,method,by,qr,kind='debt')
    return left
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
# Подключение: в Настройках вписываются JWT-токен, ID торговой точки (merchantId) и ID счёта (accountId).
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
def new_qr(rid,r,n,by,debt=False,bill=None):
    """QR: на выставленный платёж (bill); на n периодов (+ долг, если debt); на долг активной аренды (n=0); на долг закрытой аренды.
    Сумма фиксируется на сегодня, QR действует до конца дня."""
    pe=0;dp=0
    if bill:amt=bill[1]['amt']
    elif n:
        q=quote(r,n);amt=q['amt'];pe=q['pen']
        if debt:dp=dstate(r,today())['dl'];amt+=dp
    elif r.get('status')=='active':amt=dstate(r,today())['dl'];dp=amt
    else:amt=r.get('debt',0)
    if amt<=0:raise Bad('Нечего оплачивать')
    c=doc(r['client']) or {}
    m=qr_make(amt,('Аренда электровелосипеда'+(f", договор {r['contract']}" if r.get('contract') else '')+f", {c.get('name','')}").strip(', '))
    d={'rent':rid,'n':n,'amt':amt,'pen':pe,'dpart':dp,'base':paidn(r),'date':str(today()),'status':'open','tid':m['id'],'payload':m['payload'],'img':m['img'],'ts':now().isoformat(timespec='seconds')}
    if bill:d['bill']=bill[0]
    qid=nid();put(qid,'qr',d,'qr',by);return qid
def on_paid(qid,q):
    """Банк подтвердил оплату QR. Засчитываем автоматически, если это ещё имеет смысл; иначе — в «Не распознано»."""
    if q.get('status')!='open':return
    old=dict(q);q['status']='paid';put(qid,'qr',q,'qr-paid','',old)
    r=doc(q['rent']);ok=False;left=0;o=json.loads(json.dumps(r)) if r else None;t=today()
    act_=r and r.get('status')=='active'
    if r and q.get('bill'):
        b=doc(q['bill'])
        if b and b.get('status')=='open' and act_:
            left=apply_money(r,b['kind'],q['amt'],'qr','СБП',D(b['due']) if b.get('due') else None,qid)
            ob=dict(b);b['status']='paid';b['paid']=now().isoformat(timespec='seconds');put(q['bill'],'bill',b,'bill-paid','',ob);ok=True
    elif act_ and q['n'] and paidn(r)==q['base'] and str(t)==q['date']:
        if q.get('dpart'):left=pay_dl(r,q['dpart'],t);addpay(r,0,q['dpart']-left,0,'qr','СБП',qid,kind='debt')
        addpay(r,q['n'],q['amt']-q.get('dpart',0),q['pen'],'qr','СБП',qid);ok=True
    elif act_ and not q['n'] and r.get('dl'):
        left=apply_money(r,'debt',q['amt'],'qr','СБП',qr=qid);ok=True
    elif r and not q['n'] and not act_ and abs(r.get('debt',0)-q['amt'])<1:
        addpay(r,0,q['amt'],0,'qr','СБП',qid,kind='debt');r['debt']=0;ok=True
    if ok:put(q['rent'],'rent',r,'pay-qr','',o)
    c=(doc((r or {}).get('client','')) or {}).get('name','')
    if ok and left<=0.005:say(f"Оплата по QR: {c}, {rub(q['amt'])}.")
    else:
        put(nid(),'unk',{'ts':now().isoformat(timespec='seconds'),'amt':left if ok else q['amt'],'qr':qid,'rent':q['rent'],'status':'open',
            'why':'Переплата сверх долга' if ok else 'Оплата пришла после изменений в аренде или не в тот день'},'unk','')
        say(f"Платёж по QR не распознан: {c}, {rub(left if ok else q['amt'])}. Разберите в приложении.")
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
    """Все данные для главного аккаунта или администратора; аренды и велосипеды с готовыми расчётами."""
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
    if not fin:c={x:c[x] for x in('week','month','pen','grace','ddays','cdef','cats','tochka')}
    return {'docs':out,'cfg':c,'today':str(t)}
def mine(cid):
    c=doc(cid) or {};a=active_of('client',cid);cf=cfg()
    r=None
    if a:
        i,d=a;st=rstate(d)
        req=next(({'days':x['days'],'ts':x['ts']} for _,x in kind('req') if x.get('rent')==i and x.get('status')=='pending'),None)
        q1=quote(d,1)
        qr=next(({'id':j,'amt':x['amt'],'n':x['n'],'payload':x['payload'],'img':x.get('img')} for j,x in kind('qr') if x.get('rent')==i and x.get('status')=='open' and x['date']==str(today()) and x['base']==paidn(d)),None)
        bills=[{'id':j,'amt':x['amt'],'kind':x['kind'],'due':x.get('due'),'ts':x['ts']} for j,x in kind('bill') if x.get('rent')==i and x.get('status')=='open']
        r={'id':i,'issued':d.get('issued'),'type':d['type'],'st':st,'q1':q1['amt'],'bills':bills,
           'pays':[{k:p.get(k) for k in('ts','n','amt','pen','method','kind')} for p in d.get('pays') or []][-30:],'req':req,'qr':qr,
           'defers':[x for x in d.get('defer') or []][-5:]}
    debt=sum(d.get('debt',0) for _,d in kind('rent') if d.get('client')==cid and d.get('status')!='active')
    return {'role':'client','name':c.get('name',''),'rent':r,'debt':debt,'cdef':cf['cdef'],'pen':cf['pen'],'grace':cf['grace'],'qr':bool(tq()),'fio':c.get('name',''),'cphone':cf['cphone'],'ctg':cf['ctg']}
# ---------- действия ----------
NEED={'quote':'pay','issue':'rents','return':'rents','price':'rents','contract':'rents','pay':'pay','paydebt':'pay','qr':'pay','qrcancel':'pay','unk':'pay',
'defer':'defer','undefer':'defer','nopen':'pen','waive':'pen','paypart':'partial','paydl':'pay','bill':'bill','billvoid':'bill','billpaid':'pay','req':'defer','seize':'seize','client':'clients','black':'clients','archive':'clients','unarchive':'clients','bike':'bikes','bikestat':'bikes','exp':'exp','expdel':'exp'}
OWNERONLY={'cfg','admin','admindel','fix','paydel','purge'}
def archive(i,d,why,by,o):
    """Клиент уходит в архив: скрыт из списков и выдачи, вход и ссылки сброшены. Аренды и платежи остаются."""
    d['arch']=str(today());d['archwhy']=why;put(i,'client',d,'archive',by,o)
    db.execute('delete from bind where client=?',(i,));db.execute('delete from inv where client=?',(i,))
    for j,x in kind('req'):
        if x.get('client')==i and x.get('status')=='pending':ox=dict(x);x['status']='no';put(j,'req',x,'req',by,ox)
def autoarch(t):
    """Ночью: клиенты без аренды дольше N дней (от возврата или от добавления) уходят в архив."""
    n=int(cfg().get('archdays',30))
    if n<=0:return
    rs=kind('rent')
    for i,c in kind('client'):
        if c.get('arch'):continue
        mine_=[r for _,r in rs if r.get('client')==i]
        if any(r.get('status')=='active' for r in mine_):continue
        last=max([c.get('created') or '2000-01-01']+[r.get('closed') or r.get('issued') or '' for r in mine_])
        if D(last[:10])<=t-dt.timedelta(days=n):archive(i,c,'auto','',dict(c))
def need_v(b,d):
    if b.get('v') is not None and b.get('v')!=d.get('_v'):raise Bad('Данные только что изменились. Обновите экран.')
def get(k,i):
    r=ID.fullmatch(str(i or '')) and row(str(i))
    if not r or r[0]!=k:raise Bad('Запись не найдена')
    return str(i),r[1]
def void_bills(rid,by):
    for j,x in kind('bill'):
        if x.get('rent')==rid and x.get('status')=='open':ox=dict(x);x['status']='void';put(j,'bill',x,'bill-void',by,ox)
def act(a,b,ro,P,by):
    """Выполняет действие главного аккаунта или администратора. Возвращает ответ (dict). Вызывать под замком."""
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
        ph=digits(d['phone'])
        if ph and not b.get('force') and (not old or digits(old.get('phone'))!=ph):
            for j,x in kind('client'):
                if j!=i and not x.get('purged') and digits(x.get('phone'))==ph:raise Dup(j,x.get('name',''),bool(x.get('arch')))
        put(i,'client',d,'client',by,old);return {'id':i}
    if a=='archive':
        i,d=get('client',b.get('id'))
        if active_of('client',i):raise Bad('Сначала завершите аренду клиента')
        o=dict(d);archive(i,d,'manual',by,o);return {}
    if a=='unarchive':
        i,d=get('client',b.get('id'))
        if d.get('purged'):raise Bad('Клиент удалён навсегда')
        o=dict(d);d.pop('arch',None);d.pop('archwhy',None);d['created']=str(t);put(i,'client',d,'unarchive',by,o);return {}
    if a=='purge':
        i,d=get('client',b.get('id'))
        if b.get('confirm')!='УДАЛИТЬ':raise Bad('Нужно подтверждение')
        if not d.get('arch'):raise Bad('Сначала отправьте клиента в архив')
        if active_of('client',i):raise Bad('У клиента активная аренда')
        nd={'name':'Удалённый клиент','phone':'','purged':str(t),'arch':d.get('arch'),'archwhy':d.get('archwhy'),'created':d.get('created'),'_v':d.get('_v',0)+1}
        save(i,'client',nd);db.execute('delete from bind where client=?',(i,));db.execute('delete from inv where client=?',(i,))
        db.execute('update log set old=NULL,new=NULL where id=?',(i,))
        db.execute('insert into log values(?,?,?,?,?)',(now().isoformat(),'purge@'+by,i,None,None));return {}
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
        if c.get('arch'):raise Bad('Клиент в архиве. Сначала верните его из архива.')
        if active_of('client',ci):raise Bad('У клиента уже есть активная аренда')
        if active_of('bike',bi) or bk.get('status')!='free':raise Bad('Велосипед не свободен')
        ty=b.get('type')
        if ty not in LEN:raise Bad('Выберите неделю или месяц')
        iss=day(b.get('date') or t)
        if iss>t:raise Bad('Дата выдачи не может быть в будущем')
        if iss<t-dt.timedelta(days=60):raise Bad('Дата выдачи слишком давняя')
        r={'client':ci,'bike':bi,'type':ty,'price':num(b.get('price'),1),'issued':str(iss),'free':bool(b.get('free')),
           'start':str(iss+dt.timedelta(days=1 if b.get('free') else 0)),'status':'active','pays':[],'by':by,'contract':txt(b.get('contract'),40)}
        if b.get('first') is not None:r['once']={'1':num(b.get('first'),1)}
        i=nid();n=int(b.get('payn') or 0)
        if n:
            q=quote(r,n,iss);addpay(r,n,q['amt'],q['pen'],'cash' if b.get('method')!='transfer' else 'transfer',by,
                ts=None if iss==t else dt.datetime.combine(iss,now().time(),MSK).isoformat(timespec='seconds'))
        put(i,'rent',r,'issue',by);return {'id':i}
    if a=='quote':
        i,r=get('rent',b.get('id'));n=int(b.get('n') or 1)
        if r.get('status')!='active' or not 1<=n<=24:raise Bad('Нечего оплачивать')
        q=quote(r,n);return {'amt':q['amt'],'pen':q['pen'],'from':q['items'][0]['start'],'to':str(D(q['items'][-1]['start'])+dt.timedelta(days=LEN[q['items'][-1]['type']]-1))}
    if a in('pay','paydebt','qr','qrcancel','return','seize','price','defer','undefer','contract','fix','paydel','nopen','waive','paypart','paydl','bill','billvoid','billpaid'):
        i,r=get('rent',b.get('id'));need_v(b,r);o=json.loads(json.dumps(r))
        act_=r.get('status')=='active'
        if a in('pay','qr','return','seize','price','defer','undefer','fix','nopen','waive','paypart','paydl','bill','billvoid','billpaid') and not act_ and not(a=='qr' and r.get('debt')):raise Bad('Аренда уже закрыта')
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
        if a=='paypart':
            # частичная оплата следующего периода: период засчитан, остаток — долг со сроком доплаты
            m=b.get('method')
            if m not in('cash','transfer'):raise Bad('Выберите способ оплаты')
            amt=num(b.get('amt'),1);q=quote(r,1)
            if amt+0.005>=q['amt']:raise Bad('Это полная сумма — примите обычную оплату')
            du=day(b.get('due') or t+dt.timedelta(days=int(cfg()['ddays'])))
            if du<t:raise Bad('Срок доплаты уже прошёл')
            apply_money(r,'period',amt,m,by,du);put(i,'rent',r,'paypart',by,o);return {'debt':round(q['amt']-amt,2)}
        if a=='paydl':
            m=b.get('method')
            if m not in('cash','transfer'):raise Bad('Выберите способ оплаты')
            amt=num(b.get('amt'),1);tot=dstate(r,t)['dl']
            if not tot:raise Bad('Долга нет')
            if amt>tot+0.005:raise Bad(f'Долг всего {rub(tot)}')
            apply_money(r,'debt',amt,m,by);put(i,'rent',r,'paydl',by,o);return {'left':dstate(r,t)['dl']}
        if a=='bill':
            k=b.get('kind');amt=num(b.get('amt'),1)
            if k=='period':
                q=quote(r,1)
                if amt>q['amt']+0.005:raise Bad(f"Платёж за период {rub(q['amt'])}, больше выставить нельзя")
                if amt+0.005<q['amt'] and ro=='admin' and 'partial' not in P:raise PermissionError
                du=day(b.get('due') or t+dt.timedelta(days=int(cfg()['ddays'])))
                if du<t:raise Bad('Срок доплаты уже прошёл')
            elif k=='debt':
                tot=dstate(r,t)['dl']
                if not tot:raise Bad('Долга нет')
                if amt>tot+0.005:raise Bad(f'Долг всего {rub(tot)}')
                du=None
            else:raise Bad('Выберите, за что платёж')
            for j,x in kind('bill'):
                if x.get('rent')==i and x.get('status')=='open':raise Bad('У клиента уже есть неоплаченный выставленный платёж. Отмените его или отметьте оплату.')
            bi=nid();put(bi,'bill',{'rent':i,'client':r['client'],'kind':k,'amt':amt,'due':str(du) if du else None,'status':'open','ts':now().isoformat(timespec='seconds'),'by':by},'bill',by)
            tell(r['client'],f'Вам выставлен платёж {rub(amt)}. Оплатить можно в приложении.');return {'id':bi}
        if a in('billvoid','billpaid'):
            j,x=get('bill',b.get('bill'))
            if x.get('rent')!=i or x.get('status')!='open':raise Bad('Платёж уже закрыт')
            ox=dict(x)
            if a=='billvoid':x['status']='void';put(j,'bill',x,'bill-void',by,ox);return {}
            m=b.get('method')
            if m not in('cash','transfer'):raise Bad('Выберите способ оплаты')
            left=apply_money(r,x['kind'],x['amt'],m,by,D(x['due']) if x.get('due') else None)
            x['status']='paid';x['paid']=now().isoformat(timespec='seconds');put(j,'bill',x,'bill-paid',by,ox);put(i,'rent',r,'billpaid',by,o);return {'left':left}
        if a=='paydebt':
            if act_ or not r.get('debt'):raise Bad('Долга нет')
            m=b.get('method') if b.get('method') in('cash','transfer','qr') else 'cash'
            addpay(r,0,r['debt'],0,m,by,kind='debt');r['debt']=0;put(i,'rent',r,'paydebt',by,o);return {}
        if a=='qr':
            if b.get('bill'):
                j,x=get('bill',b['bill'])
                if x.get('rent')!=i or x.get('status')!='open':raise Bad('Платёж уже закрыт')
                return {'qr':new_qr(i,r,0,by,bill=(j,x))}
            n=0 if (not act_ or b.get('debtonly')) else int(b.get('n') or 1)
            if act_ and not b.get('debtonly') and not 1<=n<=12:raise Bad('Неверное число периодов')
            return {'qr':new_qr(i,r,n,by,debt=bool(b.get('debt')))}
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
        if a=='nopen':
            r['nopen']=bool(b.get('on'));put(i,'rent',r,'nopen',by,o);return {}
        if a=='waive':
            st=rstate(r,t);amt=st['pen']+st['dpen'];r['waive']=str(t);r.setdefault('waived',[]).append({'date':str(t),'amt':amt,'by':by})
            for it in r.get('dl') or []:it['pen']=0;it['pdate']=str(t)
            r['dl']=[it for it in r.get('dl') or [] if it['amt']>0.005]
            put(i,'rent',r,'waive',by,o);return {'amt':amt}
        if a=='undefer':
            r['defer']=[x for x in r.get('defer') or [] if D(x['until'])<t];put(i,'rent',r,'undefer',by,o);return {}
        if a=='contract':
            r['contract']=txt(b.get('contract'),40);put(i,'rent',r,'contract',by,o);return {}
        if a=='fix':
            nx=day(b.get('next'));cur=D(rstate(r,t)['next']);r['shift']=r.get('shift',0)+(nx-cur).days
            put(i,'rent',r,'fix',by,o);return {}
        st=rstate(r,t)
        if a=='return':
            debt=num(b.get('debt') if b.get('debt') is not None else st['debt']+st['dl']);ref=num(b.get('refund') or 0)
            r.update(status='closed',closed=str(t),debt=debt,refund={'amt':ref,'date':str(t)} if ref else None,debt0=st['debt']+st['dl'],dl=[])
            void_bills(i,by)
            put(i,'rent',r,'return',by,o);return {}
        if a=='seize':
            if b.get('confirm')!='ИЗЪЯТЬ':raise Bad('Нужно подтверждение')
            r.update(status='seized',closed=str(t),debt=0,writeoff=st['debt']+st['dl'],dl=[])
            void_bills(i,by)
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
        c['cdef']=int(num(b.get('cdef',c['cdef']),0,3));c['grace']=int(num(b.get('grace',c['grace']),0,10));c['ddays']=int(num(b.get('ddays',c['ddays']),1,60));c['archdays']=int(num(b.get('archdays',c['archdays']),0,365))
        c['cphone']=txt(b.get('cphone',c['cphone']),30);c['ctg']=txt(b.get('ctg',c['ctg']),40).lstrip('@').replace('https://t.me/','')
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
        d={'name':nm,'phone':ph,'perms':[q for q in PERMS if q in pl],'tg':(old or {}).get('tg'),'tgu':(old or {}).get('tgu',''),'disabled':bool(b.get('disabled')),'created':(old or {}).get('created',str(t)),'m2':1}
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
        q=quote(r,n);dp=dstate(r,today())['dl'] if b.get('debt') else 0
        return {'amt':q['amt']+dp,'pen':q['pen'],'dl':dp,'to':str(D(q['items'][-1]['start'])+dt.timedelta(days=LEN[q['items'][-1]['type']]-1))}
    if b.get('a')=='qr' and b.get('bill'):
        j,x=get('bill',b['bill'])
        if x.get('rent')!=i or x.get('status')!='open':raise Bad('Платёж уже закрыт')
        for k,y in kind('qr'):
            if y.get('bill')==j and y.get('status')=='open' and y['date']==str(today()):return {'qr':k}
        return {'qr':new_qr(i,r,0,'клиент',bill=(j,x))}
    if b.get('a')=='qr' and b.get('debtonly'):
        if not r.get('dl'):raise Bad('Долга нет')
        return {'qr':new_qr(i,r,0,'клиент')}
    if b.get('a')=='qr':
        n=int(b.get('n') or 1)
        if not 1<=n<=6:raise Bad('Неверное число периодов')
        for j,x in kind('qr'):
            if x.get('rent')==i and x.get('status')=='open' and x['date']==str(today()) and x['base']==paidn(r) and x['n']==n and not x.get('bill') and bool(x.get('dpart'))==bool(b.get('debt')):return {'qr':j}
        return {'qr':new_qr(i,r,n,'клиент',debt=bool(b.get('debt')))}
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
IMG={};DL={};TTL=int(os.environ.get('DL_TTL','120'));BS=DATA+'/last_download.json'
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
            migrate()
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
                elif ro=='client':
                    c=doc(x);un=UN.get(u,'')
                    if c and c.get('tgu','')!=un:c['tgu']=un;save(x,'client',c);db.commit()
                    r=mine(x)
                else:cf=cfg();r={'role':ro,'cphone':cf['cphone'],'ctg':cf['ctg']}
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
        if p.startswith('/img/'):
            t=time.time()
            for a in [a for a,(e,_) in IMG.items() if e<t]:IMG.pop(a,None)
            x=IMG.get(p[5:])
            if not x:return s.send(404,b'')
            return s.send(200,x[1],'image/png',{'Content-Disposition':'attachment; filename="qr-oplata.png"','Cache-Control':'no-store'})
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
        if p=='/api/qrimg':
            # картинка QR, нарисованная в приложении: храним 10 минут, чтобы Telegram мог её скачать
            import base64
            with L:ro_,_=role(u)
            if ro_ not in('owner','admin','client'):return s.send(403,{})
            try:png=base64.b64decode(str(b.get('png','')).split(',')[-1],validate=True)
            except Exception:return s.send(400,{})
            if not png.startswith(b'\x89PNG') or len(png)>400000:return s.send(400,{})
            if len(IMG)>200:IMG.clear()
            k=secrets.token_urlsafe(16);IMG[k]=(t+600,png);return s.send(200,{'url':'/img/'+k})
        if u==OWNER and p=='/api/dl':
            k=secrets.token_urlsafe(16);DL[k]=t+TTL;return s.send(200,{'url':'/dl/'+k})
        if u==OWNER and p=='/api/restore':
            c,r=restore(b);return s.send(c,{'docs':r} if c==200 else {'err':r})
        with L:
            ro,x=role(u)
            try:
                if ro in('owner','admin') and p=='/api/act':
                    P=set(x[1].get('perms',[])) if ro=='admin' else set(PERMS)
                    by='Вы' if ro=='owner' else str(x[1].get('name',''))[:40]
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
                        q=bool(b.get('qr'));k=('qq'+secrets.token_hex(12)) if q else secrets.token_urlsafe(9)
                        db.execute('insert into inv values(?,?,?)',(k,c,t+(600 if q else 7*86400)));db.commit()
                        return s.send(200,{'tok':k,'bot':BOT[0]})
                    if adm:a[1]['tg']=None;save(c[4:],'admin',a[1])
                    else:db.execute('delete from bind where client=?',(c,))
                    db.commit();return s.send(200,{})
                if ro=='new' and p=='/api/claim':
                    TRY[u]=[q for q in TRY.get(u,[]) if q>t-3600]
                    if len(TRY[u])>=5:return s.send(429,{})
                    TRY[u].append(t)
                    tk=str(b.get('tok'));i=db.execute('select client from inv where tok=? and exp>?',(tk,t)).fetchone();tgt=i and i[0]
                    isq=tk.startswith('qq') and len(tk)==26;ph=digits(b.get('phone'))
                    if tgt and tgt.startswith('adm:'):
                        a=row(tgt[4:]);c=a and a[0]=='admin' and not a[1].get('disabled') and a[1]
                        if c and (isq or (ph and digits(c.get('phone'))==ph)):
                            c['tg']=u;c['tgu']=UN.get(u,'');save(tgt[4:],'admin',c);db.execute('delete from inv where tok=?',(str(b.get('tok')),));db.commit()
                            say(f"Администратор {c.get('name')} вошёл в приложение аренды.");return s.send(200,{})
                    else:
                        c=tgt and doc(tgt)
                        if c and (isq or (ph and digits(c.get('phone'))==ph)):
                            c['tgu']=UN.get(u,'');save(tgt,'client',c)
                            db.execute('insert or replace into bind values(?,?)',(u,tgt));db.execute('delete from inv where tok=?',(str(b.get('tok')),));db.commit()
                            say(f"Клиент {c.get('name')} вошёл в приложение аренды.");return s.send(200,{})
                    if len(TRY[u])==3:say(f'Неудачные попытки входа (Telegram ID {u}).')
                    return s.send(403,{})
                if ro=='client' and p=='/api/c':
                    r=client_act(x,b);db.commit();return s.send(200,r)
            except Dup as e:
                db.rollback();j,nm,ar=e.args;return s.send(409,{'err':f'Клиент с этим номером уже есть: {nm}'+(' (в архиве)' if ar else ''),'dup':j,'arch':ar})
            except Bad as e:
                db.rollback();return s.send(400,{'err':str(e)})
            except PermissionError:
                db.rollback();return s.send(403,{'err':'Нет прав'})
            except Exception as e:
                db.rollback();print('act',repr(e));return s.send(500,{'err':'Ошибка сервера'})
        return s.send(403,{})
# ---------- расписание ----------
def morning(t):
    """10:00: сводка вам и напоминания клиентам (накануне и в день оплаты). Только если включено в Настройках."""
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
                    with L:
                        snap('app')
                        try:autoarch(n.date());db.commit()
                        except Exception as e:print('autoarch',e);db.rollback()
                else:morning(n.date())
            except Exception as e:print(e)
        if time.time()-lp>150:
            lp=time.time()
            try:poll()
            except Exception as e:print('poll',e)
        time.sleep(20)
def migrate():
    """Перенесённые аренды: день оплаты = дата окончания в старом сервисе (новый период начинается на день позже).
    Настройки: неустойка 5% с 3-го дня, если льгота ещё не задана."""
    with L:
        for i,r in kind('rent'):
            if r.get('imported') and not r.get('m1'):
                o=dict(r);r['m1']=1
                if r.get('status')=='active':r['shift']=r.get('shift',0)+1
                put(i,'rent',r,'migrate','',o)
        for i,a in kind('admin'):
            if not a.get('m2'):
                p=a.get('perms') or []
                if 'pay' in p:p=p+[x for x in('partial','bill') if x not in p]
                if 'defer' in p and 'pen' not in p:p=p+['pen']
                a['perms']=[x for x in PERMS if x in p];a['m2']=1;save(i,'admin',a)
        c=doc('cfg')
        if c is not None and 'grace' not in c:c['grace']=2;c['pen']=5;save('cfg','cfg',c)
        db.commit()
migrate()
if __name__=='__main__':
    threading.Thread(target=job,daemon=True).start()
    threading.Thread(target=botname,daemon=True).start()
    ThreadingHTTPServer(('',int(os.environ.get('PORT','8080'))),H).serve_forever()

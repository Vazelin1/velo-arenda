"""Аренда электровелосипедов: сервер Telegram mini app.
Вся бизнес-логика (периоды, неустойка, отсрочки, оплаты, возвраты) считается здесь, интерфейс только показывает и вызывает действия."""
import os,re,json,hmac,hashlib,sqlite3,threading,time,secrets,urllib.request,urllib.error,urllib.parse,datetime as dt
from decimal import Decimal,ROUND_HALF_UP,InvalidOperation
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
PERMS=('rent_issue','rent_return','rent_rates','rent_note','rent_contract','rent_date','seize',
       'pay_accept','partial','bill','pay_qr','pay_review','pay_delete','defer_manual','defer_requests','pen_toggle','pen_waive',
       'client_edit','client_archive','client_block','client_access','bike_edit','bike_status','exp','reports')
LEGACY_PERMS={
    'rents':('rent_issue','rent_return','rent_rates','rent_note','rent_contract'),
    'pay':('pay_accept','pay_qr','pay_review'),
    'defer':('defer_manual','defer_requests'),
    'pen':('pen_toggle','pen_waive'),
    'clients':('client_edit','client_archive','client_block','client_access'),
    'bikes':('bike_edit','bike_status'),
}
BASE_TERMS=('w','m')
TERM_DAYS={'w':7,'w2':14,'w3':21,'m':30}
DEFAULT_RATES={'w':3700,'w2':7300,'w3':10800,'m':15000}
DEF={'week':DEFAULT_RATES['w'],'month':DEFAULT_RATES['m'],'renewal_rates':DEFAULT_RATES,
'pen':5,'grace':2,'ddays':3,'archdays':30,'cphone':'','ctg':'','cmax':'','tax':7,'acq':0,'receipt':0,'cdef':3,'nown':False,'ncli':False,
'cats':['Ремонт','Запчасти','Обслуживание','Реклама','Зарплата','Аренда помещения','Прочее']}
ID=re.compile(r'[a-z0-9]{1,16}')
D=dt.date.fromisoformat
class Bad(Exception):pass
class Dup(Exception):pass
def now():return dt.datetime.now(MSK)
def today():return now().date()
def nid():return secrets.token_hex(5)
def cash(n):return float(Decimal(str(n)).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP))
def percent(amount,rate,days=1):return cash(Decimal(str(amount))*Decimal(str(rate))*days/100)
def rub(n):return f'{cash(n):,.2f}'.rstrip('0').rstrip('.').replace(',',' ').replace('.',',')+' ₽'
def dm(d):return (D(d) if isinstance(d,str) else d).strftime('%d.%m')
def digits(p):return re.sub(r'\D','',p or '')[-10:]
def num(x,lo=0,hi=10**8):
    try:v=Decimal(str(x))
    except (InvalidOperation,ValueError):raise Bad('Неверное число')
    if not v.is_finite():raise Bad('Неверное число')
    if not lo<=v<=hi:raise Bad('Число вне допустимого диапазона')
    return cash(v)
def integer(x,lo=0,hi=400):
    try:v=Decimal(str(x))
    except (InvalidOperation,ValueError):raise Bad('Нужно целое число')
    if not v.is_finite() or not lo<=v<=hi or v!=v.to_integral_value():raise Bad('Нужно целое число в допустимом диапазоне')
    return int(v)
def rate_prices(values):
    if not isinstance(values,dict) or set(values)!=set(TERM_DAYS):raise Bad('Укажите все четыре цены')
    return {term:num(values[term],1) for term in TERM_DAYS}
def day(x):
    try:return D(str(x))
    except Exception:raise Bad('Неверная дата')
def txt(x,n=200):return str(x or '').strip()[:n]
def admin_permissions(values):
    return [p for p in PERMS if p in values or any(k in values and p in group for k,group in LEGACY_PERMS.items())]
def chat_url(raw,telegram=False):
    value=txt(raw,2048)
    if not value:return ''
    if telegram and '://' not in value:
        if value.lower().startswith(('t.me/','telegram.me/')):value='https://'+value
        elif re.fullmatch(r'@?[\w+/-]+(?:\?[^\s:]*)?',value):value='https://t.me/'+value.lstrip('@')
    try:
        p=urllib.parse.urlsplit(value)
        if p.scheme=='https' and p.hostname and p.port!=0 and p.username is None and not re.search(r'[\s\x00-\x1f\\]',value):
            return value
    except ValueError:pass
    return ''
def contact_data(c):
    return {'cphone':c['cphone'],'ctg':chat_url(c['ctg'],telegram=True),'cmax':chat_url(c['cmax'])}
# ---------- хранилище ----------
def row(i):
    r=db.execute('select kind,data from docs where id=?',(i,)).fetchone();return r and (r[0],json.loads(r[1]))
def doc(i):
    r=row(i);return r and r[1]
def kind(k):return [(i,json.loads(d)) for i,d in db.execute('select id,data from docs where kind=?',(k,))]
def save(i,k,d):db.execute('insert or replace into docs values(?,?,?)',(i,k,json.dumps(d,ensure_ascii=False)))
def write_log(action,by,i,old=None,new=None):
    def encode(x):
        if not x:return None
        d={a:b for a,b in x.items() if a!='img'}
        if i=='cfg' and d.get('tochka'):d['tochka']={**d['tochka'],'jwt':'***','hook':'***'}
        return json.dumps(d,ensure_ascii=False)
    db.execute('insert into log values(?,?,?,?,?)',(now().isoformat(),action+('@'+by if by else ''),i,encode(old),encode(new)))
def put(i,k,d,act,who,old=None,audit=None):
    d['_v']=(old or {}).get('_v',0)+1;save(i,k,d)
    write_log(act,who,i,old,{**d,'_audit':audit} if audit else d)
def cfg():return {**DEF,**(doc('cfg') or {})}
# ---------- Telegram ----------
def who(init):
    try:
        q=dict(urllib.parse.parse_qsl(init));h=q.pop('hash')
        s='\n'.join(f'{k}={q[k]}' for k in sorted(q))
        key=hmac.new(b'WebAppData',TOK.encode(),hashlib.sha256).digest()
        if hmac.compare_digest(hmac.new(key,s.encode(),hashlib.sha256).hexdigest(),h) and -30<=time.time()-int(q['auth_date'])<86400:
            u=json.loads(q['user']);uid=u['id']
            if type(uid) is not int or not 0<uid<2**52:return None
            UN[uid]=str(u.get('username') or '')[:40];return uid
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
def recurring_plan(r,k):
    t,p=r['type'],r['price']
    for c in r.get('plan') or []:
        if c['k']<=k:t=c.get('type') or t;p=c['price']
    return t,p
def plan(r,k):
    t,p=recurring_plan(r,k)
    initial=r.get('initial') if k==1 else None
    if initial:
        t,p=initial['term'],initial['price']
    once=(r.get('once') or {}).get(str(k))
    return t,p if once is None else once
def set_plan(r,k,period,price,date,by):
    r['plan']=[c for c in r.get('plan') or [] if c['k']<k]
    r['plan'].append({'k':k,'type':period,'price':price,'ts':str(date),'by':by})
def pstart(r,k):
    s=D(r['start'])+dt.timedelta(days=r.get('shift',0))
    durations={}
    j=r.get('base',0)+1
    for p in r.get('pays') or []:
        if p.get('term') in TERM_DAYS:durations[j]=TERM_DAYS[p['term']]
        j+=p.get('n',0)
    for j in range(1,k):
        s+=dt.timedelta(days=durations[j] if j in durations else TERM_DAYS[plan(r,j)[0]])
    return s
def paidn(r):return r.get('base',0)+sum(p.get('n',0) for p in r.get('pays') or [])
def renewed_after_first_term(r,on):
    """True/False для аренд с истёкшим первым сроком и доступной историей оплаты."""
    if r.get('imported') or r.get('base') or not r.get('start'):return None
    try:
        periods=[p['n'] for p in r.get('pays') or [] if p.get('n',0)>0]
        # До выбора срока при выдаче первая оплата могла покрывать несколько базовых периодов.
        first_periods=1 if r.get('initial') or not periods else periods[0]
        if first_periods>400 or pstart(r,first_periods+1)>on:return None
        return sum(periods)>first_periods
    except (KeyError,TypeError,ValueError,OverflowError):
        return None
def charged_days(r,first,last):
    """Дни начисления с вычетом объединения отсрочек, пауз и списанных дней."""
    if r.get('nopen') and not r.get('pen_pauses'):return 0
    start,end=first.toordinal(),last.toordinal()
    if r.get('waive'):start=max(start,D(r['waive']).toordinal()+1)
    if start>end:return 0
    excluded=[]
    for window in (r.get('defer') or [])+(r.get('pen_pauses') or []):
        left=max(start,D(window['from']).toordinal())
        right=min(end,D(window['until']).toordinal()) if window.get('until') else end
        if left<=right:excluded.append((left,right))
    count=end-start+1
    for left,right in sorted(excluded):
        left=max(left,start)
        if left<=right:
            count-=right-left+1
            start=right+1
    return count
def due_of(r,k,s):
    """День оплаты периода k, который начинается s: последний оплаченный день перед ним.
    Первый период оплачивается в день выдачи (не раньше)."""
    d=s-dt.timedelta(days=1)
    return max(d,D(r.get('issued') or r['start'])) if k==1 else d
def pen(r,due,price,on):
    """Неустойка за период с днём оплаты due на дату on: ставка% от цены периода за каждый день просрочки,
    начиная с (льгота+1)-го дня. Не считаются дни отсрочки, списанные дни и аренды с отключённой неустойкой."""
    c=cfg();g=int(c.get('grace',2));n=charged_days(r,due+dt.timedelta(days=g+1),on)
    return percent(price,c['pen'],n),n
def owed(r,on,cnt=None):
    """Неоплаченные периоды, начиная с первого. cnt=None: все, чей день оплаты наступил (минимум один); иначе ровно cnt."""
    k=paidn(r)+1;s=pstart(r,k);out=[]
    while True:
        t,p=plan(r,k);du=due_of(r,k,s)
        if cnt is None and out and du>on:break
        pe,dd=pen(r,du,p,on)
        out.append({'k':k,'start':str(s),'type':t,'price':p,'pen':pe,'days':dd,'due':du<=on,'pay':str(du)})
        if cnt and len(out)==cnt:break
        s+=dt.timedelta(days=TERM_DAYS[t]);k+=1
    return out
def rstate(r,on=None):
    on=on or today()
    if r.get('status')!='active':return {'debt':r.get('debt',0)}
    waiting=r.get('waiting')
    if waiting:on=min(on,D(waiting))
    o=owed(r,on)
    du=[x for x in o if x['due'] and (not waiting or D(x['start'])<=on)]
    n=o[0];nd=D(n['pay'])
    df=max((x['until'] for x in r.get('defer') or [] if D(x['from'])<=on<=D(x['until'])),default=None)
    basetype,baseprice=recurring_plan(r,n['k'])
    accrued=cash(sum(x['price']+x['pen'] for x in du))
    discount=min(pending_discount(r).get('amt',0),du[0]['price']) if du else 0
    debts=dstate(r,on)
    return {'next':n['pay'],'start':n['start'],'nprice':n['price'],'ntype':n['type'],
            'firstterm':n['type'] if n['k']==1 and r.get('initial') else None,'basetype':basetype,'baseprice':baseprice,
            'late':max(0,(on-nd).days),'left':(nd-on).days,
            'pen':cash(sum(x['pen'] for x in du)),'debt':accrued,'rent_due':cash(accrued-discount),
            'due_total':cash(accrued-discount+debts['dl_due']),'due_n':len(du),'defer':df,'paid':paidn(r),
            'until':str(D(n['start'])-dt.timedelta(days=1)),'pdays':max(0,(D(n['start'])-on).days),'pfrom':str(nd+dt.timedelta(days=int(cfg().get('grace',2))+1)),'nopen':bool(r.get('nopen')),
            **debts}
# Долг активной аренды (частичная оплата): r['dl'] — список {id,amt,due,pen,pdate}. pen — уже начисленная и зафиксированная неустойка,
# pdate — день последней фиксации (дальше неустойка считается с pdate+1).
def daccr(r,it,on):
    """Неустойка по долгу, набежавшая после последней фиксации: ставка% от остатка за каждый день после срока и льготных дней."""
    c=cfg();g=int(c.get('grace',2))
    d=D(it['due'])+dt.timedelta(days=g+1)
    if it.get('pdate'):d=max(d,D(it['pdate'])+dt.timedelta(days=1))
    return percent(it['amt'],c['pen'],charged_days(r,d,on))
def dstate(r,on):
    its=r.get('dl') or []
    if not its:return {'dl':0,'dl_due':0,'dpen':0,'dprin':0}
    penalties=[it.get('pen',0)+daccr(r,it,on) for it in its]
    pen=cash(sum(penalties));amt=cash(sum(it['amt'] for it in its));due=min(D(it['due']) for it in its)
    due_amount=sum(it['amt']+pe for it,pe in zip(its,penalties) if D(it['due'])<=on)
    return {'dl':cash(amt+pen),'dl_due':cash(due_amount),'dpen':pen,'dprin':amt,'ddue':str(due),'dlate':max(0,(on-due).days),
            'dpfrom':str(due+dt.timedelta(days=int(cfg().get('grace',2))+1))}
def pay_dl(r,amt,on):
    """Гасит долг суммой amt: сначала фиксирует неустойку, затем гасит неустойку и сам долг (самые ранние первыми). Возвращает остаток."""
    its=sorted(r.get('dl') or [],key=lambda x:x['due'])
    for it in its:it['pen']=cash(it.get('pen',0)+daccr(r,it,on));it['pdate']=str(on)
    for field in ('pen','amt'):
        for it in its:
            x=min(amt,it[field]);it[field]=cash(it[field]-x);amt=cash(amt-x)
    r['dl']=[it for it in its if it['amt']>0.005 or it['pen']>0.005]
    return amt
def add_dl(r,amt,due,by,pe=0):
    r.setdefault('dl',[]).append({'id':nid(),'amt':cash(amt),'orig':cash(amt),'due':str(due),'pen':cash(pe),'ts':now().isoformat(timespec='seconds'),'by':by})
def ledger(r):return json.loads(json.dumps({'status':r.get('status'),'dl':r.get('dl') or [],'debt':r.get('debt',0)}))
def payment_undo(r,p,before):
    after=ledger(r)
    if before!=after:p['undo']={'before':before,'after':after}
def legacy_undo(rid,p,current):
    """Для старых частичных оплат восстанавливаем точные состояния из журнала, не угадывая остаток."""
    for old,new in db.execute('select old,new from log where id=? order by rowid desc',(rid,)):
        try:
            a=json.loads(old);b=json.loads(new)
            added=(b.get('pays') or [])[len(a.get('pays') or []):]
            if p in added and sum(x.get('kind') in ('part','debt') for x in added)==1 and ledger(b)==current:
                return {'before':ledger(a),'after':ledger(b)}
        except (TypeError,ValueError,AttributeError):pass
    return None
def apply_money(r,kind_,amt,method,by,due=None,qr=None,on=None):
    """Зачисляет деньги по выставленному платежу. kind_='period': за следующий период (меньше полной суммы — частичная оплата, остаток в долг);
    'debt': в счёт долга. Возвращает лишнее (если заплатили больше, чем нужно)."""
    on=on or today()
    before=ledger(r)
    if kind_=='period':
        q=quote(r,1,on)
        if amt+0.005<q['amt']:
            paid_pen=min(amt,q['pen'])
            p=addpay(r,1,amt,paid_pen,method,by,qr,kind='part')
            add_dl(r,q['rent_amt']-(amt-paid_pen),due or on+dt.timedelta(days=int(cfg()['ddays'])),by,q['pen']-paid_pen)
            payment_undo(r,p,before);return 0
        addpay(r,1,q['amt'],q['pen'],method,by,qr);amt=cash(amt-q['amt'])
        if amt<=0.005:return 0
    before_pen=dstate(r,on)['dpen']
    left=pay_dl(r,amt,on)
    if amt-left>0.005:
        paid_pen=cash(before_pen-dstate(r,on)['dpen'])
        p=addpay(r,0,cash(amt-left),paid_pen,method,by,qr,kind='debt');payment_undo(r,p,before)
    return left
def pending_discount(r):
    return (doc(r.get('client')) or {}).get('discount') or {}
def discounted(r,q):
    offer=pending_discount(r);gross=cash(q['amt']);rent=cash(gross-q['pen'])
    amount=cash(min(offer.get('amt',0),max(0,rent)))
    return {**q,'amt':cash(gross-amount),'rent_amt':cash(rent-amount),'before_discount':gross,
            'discount':amount,'discount_id':offer.get('id') if amount else None}
def discount_matches(r,q):
    offer=pending_discount(r)
    return q.get('discount_id')==(offer.get('id') if offer else None)
def quote(r,n,on=None,use_discount=True):
    o=owed(r,on or today(),n);q={'amt':sum(x['price']+x['pen'] for x in o),'pen':sum(x['pen'] for x in o),'items':o}
    return discounted(r,q) if use_discount else q
def term_quote(r,n,term=None,on=None):
    on=on or today()
    if term:
        k=paidn(r)+1
        initial=r.get('initial') if k==1 else None
        if not isinstance(term,str) or term not in TERM_DAYS:raise Bad('Неверный срок оплаты')
        if initial and term!=initial['term']:raise Bad('Сначала оплатите первый срок аренды')
        if not initial and term not in (r.get('rates') or {}):
            raise Bad('Цена на этот срок не настроена')
        start=pstart(r,k)
        days=TERM_DAYS[term]
        # Начисленная неустойка относится только к дням, которые закрывает платёж.
        # График и база неустойки остаются условиями действующей аренды.
        end=start+dt.timedelta(days=days)
        pe=0
        for x in owed(r,on,(days+6)//7):
            duration=TERM_DAYS[x['type']]
            covered=min(duration,(end-D(x['start'])).days)
            if covered<=0:break
            pe+=cash(Decimal(str(x['pen']))*covered/duration)
        if initial:rent_amt=plan(r,k)[1]
        else:
            rent_amt=r['rates'][term]
            if term==recurring_plan(r,k)[0]:
                once=(r.get('once') or {}).get(str(k))
                if once is not None:rent_amt=once
    else:
        q=quote(r,n,on,use_discount=False)
        start=D(q['items'][0]['start'])
        last=q['items'][-1]
        end=D(last['start'])+dt.timedelta(days=TERM_DAYS[last['type']])
        days=(end-start).days
        pe=q['pen']
        rent_amt=q['amt']-pe
    q=discounted(r,{'amt':cash(rent_amt+pe),'pen':cash(pe),'from':str(start),'to':str(end-dt.timedelta(days=1))})
    q['weekly']=cash(Decimal(str(q['rent_amt']))*7/days)
    return q
def quote_stamp(q):
    return {key:q.get(key) for key in ('amt','pen','from','to','discount','discount_id')}
def check_amount(b,amount,q=None):
    if b.get('expected') is not None and num(b['expected'],0,10**12)!=cash(amount):
        raise Bad('Сумма изменилась. Откройте оплату заново.')
    if b.get('expected_quote') is not None and (q is None or b['expected_quote']!=quote_stamp(q)):
        raise Bad('Условия оплаты изменились. Откройте оплату заново.')
def bill_current(r,b,on=None):
    on=on or today()
    if b['kind']=='debt':return 0<b['amt']<=dstate(r,on)['dl']
    current=term_quote(r,1,on=on)
    return (b['amt']<=current['amt'] and b.get('base',paidn(r))==paidn(r) and discount_matches(r,b)
            and (not b.get('quote') or b['quote']==quote_stamp(current)))
def payment_quote(r,n,term=None,debt=False,on=None):
    on=on or today()
    q=term_quote(r,1 if term else n,term,on)
    q['dl']=dstate(r,on)['dl'] if debt else 0
    q['amt']=cash(q['amt']+q['dl'])
    q['stamp']=quote_stamp(q)
    return q
def sbp_fees(amt):
    c=cfg()
    return {key:percent(amt,c[key]) for key in ('tax','acq','receipt')}
def addpay(r,n,amt,pe,method,by,qr=None,ts=None,kind=None,term=None,discount_info=None,use_discount=True):
    p={'ts':ts or now().isoformat(timespec='seconds'),'n':n,'amt':amt,'pen':pe,'method':method,'by':by}
    if n:
        k=paidn(r)+1
        p['due']=str(due_of(r,k,pstart(r,k)))
        if not term:
            types={plan(r,j)[0] for j in range(k,k+n)}
            if len(types)==1:p['period_type']=next(iter(types))
        info=(discount_info if discount_info is not None else term_quote(r,n,term)) if use_discount else {}
        if info.get('discount'):
            ci,c=get('client',r['client']);old=json.loads(json.dumps(c))
            if not discount_matches(r,info):raise Bad('Скидка изменилась. Откройте оплату заново.')
            p.update(discount=info['discount'],discount_id=info['discount_id'],discount_offer=c.get('discount'))
            c.pop('discount',None);put(ci,'client',c,'discount-used',by,old)
    if qr:p['qr']=qr
    if kind:p['kind']=kind
    if term:p['term']=term
    if method=='qr':p['fees']=sbp_fees(amt)
    r.setdefault('pays',[]).append(p)
    return p
def active_of(field,val):
    for i,r in kind('rent'):
        if r.get('status')=='active' and r.get(field)==val:return i,r
def receipt_forecast(on=None,days=30):
    """Поступления по базовому графику активных аренд, без будущей неустойки и новых выдач."""
    on=on or today();end=on+dt.timedelta(days=days-1)
    rows=[];overdue=0;closed_debt=0;skipped=0
    for rid,r in kind('rent'):
        if r.get('status')!='active':
            closed_debt+=r.get('debt',0);continue
        if r.get('waiting'):continue
        client_name=(doc(r.get('client')) or {}).get('name') or 'Удалённый клиент'
        bike=doc(r.get('bike')) or {};bike_name=' · '.join(x for x in (bike.get('num'),bike.get('name')) if x)
        rent_rows=[];rent_overdue=0
        def append(date,amount,category):
            if amount>0:rent_rows.append({'rent':rid,'client':client_name,'bike':bike_name,'date':str(date),'amt':cash(amount),'kind':category})
        try:
            k=paidn(r)+1;start=pstart(r,k);discount=pending_discount(r).get('amt',0)
            past=[x for x in owed(r,on) if day(x['pay'])<on]
            if past:
                rent_overdue+=sum(x['price']+x['pen'] for x in past)-min(discount,past[0]['price'])
                discount=0
            planned=(r.get('return_request') or {}).get('date')
            planned=day(planned) if planned else None
            while True:
                term,price=plan(r,k);due=due_of(r,k,start)
                if due>end or (planned and start>planned):break
                applied=min(discount,price);discount=0;amount=cash(price-applied)
                if due>=on:append(due,amount,'rent')
                start+=dt.timedelta(days=TERM_DAYS[term]);k+=1
            for debt in r.get('dl') or []:
                due=day(debt['due']);amount=debt['amt']+debt.get('pen',0)
                if due<on:rent_overdue+=amount+daccr(r,debt,on)
                elif due<=end:append(due,amount,'debt')
            overdue+=rent_overdue
            rows.extend(rent_rows)
        except (Bad,KeyError,ValueError,TypeError,OverflowError):skipped+=1
    rows.sort(key=lambda x:(x['date'],x['client'],x['rent']))
    seven=on+dt.timedelta(days=6)
    return {'from':str(on),'to':str(end),'rows':rows,'total7':cash(sum(x['amt'] for x in rows if x['date']<=str(seven))),
            'total30':cash(sum(x['amt'] for x in rows)),'overdue':cash(overdue),'closed_debt':cash(closed_debt),'skipped':skipped}
# ---------- Точка (СБП) ----------
POLL=threading.Lock()
# Подключение: в Настройках вписываются JWT-токен, ID торговой точки (merchantId) и ID счёта (accountId).
# Запросы сделаны по открытому API Точки (раздел СБП); при подключении сверяем с актуальной документацией.
def tq():
    t=cfg().get('tochka') or {}
    if MOCK:return {'mock':1,**t}
    return t if t.get('jwt') and t.get('merchant') and t.get('account') else None
def tochka(method,path,body=None):
    with L:t=tq()
    if not t:raise Bad('Оплата по QR не подключена')
    rq=urllib.request.Request(TQB+path,json.dumps(body).encode() if body is not None else None,
        {'Authorization':'Bearer '+t['jwt'],'Content-Type':'application/json'},method=method)
    return json.load(urllib.request.urlopen(rq,timeout=25))
def qr_make(amt,purpose):
    t=tq()
    if not t:raise Bad('Оплата по QR не подключена: впишите данные Точки в Настройках.')
    if MOCK:return {'id':'mock'+nid(),'payload':'https://qr.nspk.ru/MOCK'+nid(),'img':None}
    seconds=(dt.datetime.combine(today()+dt.timedelta(days=1),dt.time(),MSK)-now()).total_seconds()
    if seconds<60:raise Bad('День заканчивается. Получите новый QR после 00:00 по Москве.')
    merchant,account=(urllib.parse.quote(t[k],safe='') for k in ('merchant','account'))
    try:
        r=tochka('POST',f"/sbp/v1.0/qr-code/merchant/{merchant}/{account}",{'Data':{'amount':int(round(amt*100)),'currency':'RUB',
            'paymentPurpose':purpose[:140],'qrcType':'02','ttl':max(1,int(seconds//60)),
            'imageParams':{'width':400,'height':400,'mediaType':'image/png'}}})['Data']
        payload=chat_url(r['payload'])
        if not payload or not isinstance(r['qrcId'],str) or not r['qrcId']:raise ValueError('QR response')
        return {'id':r['qrcId'],'payload':payload,'img':(r.get('image') or {}).get('content')}
    except Exception as e:print('tochka qr',e);raise Bad('Точка не ответила, попробуйте ещё раз.')
def qr_paid(ids):
    """Какие из QR оплачены (по ответу Точки)."""
    if MOCK or not ids:return set()
    r=tochka('GET','/sbp/v1.0/qr-codes/'+','.join(urllib.parse.quote(i,safe='') for i in ids)+'/payment-status')['Data']
    return {x.get('qrcId') for x in r.get('paymentList',[]) if x.get('status')=='Accepted'}
def period_qr_current(r,q,on):
    if (paidn(r)!=q['base'] or not discount_matches(r,q)):return False
    try:current=term_quote(r,q['n'],q.get('term'),on)
    except Bad:return False
    part=q.get('dpart',0)
    return (cash(current['amt']+part)==q['amt'] and current['pen']==q['pen']
            and (not q.get('quote') or q['quote']==quote_stamp(current))
            and (not q.get('start') or q['start']==current['from'])
            and (not part or part==dstate(r,on)['dl']))
def qr_available(r,q,on):
    if q.get('status')!='open' or q['date']!=str(on) or not r or r.get('waiting'):return False
    active=r['status']=='active'
    if q.get('bill'):
        b=doc(q['bill'])
        return active and b and b['status']=='open' and b['amt']==q['amt'] and bill_current(r,b,on)
    if q['n']:return active and period_qr_current(r,q,on)
    if active:return q.get('dpart',0)>0 and q['amt']==dstate(r,on)['dl']
    return not q.get('dpart') and q['amt']==r.get('debt',0)
def new_qr(rid,r,n,by,debt=False,bill=None,term=None,expected=None,expected_quote=None):
    """QR: на выставленный платёж (bill); на n периодов (+ долг, если debt); на долг активной аренды (n=0); на долг закрытой аренды.
    Сумма фиксируется на сегодня, QR действует до конца дня."""
    pe=0;dp=0;on=today();q=None
    if bill:
        if not bill_current(r,bill[1],on):raise Bad('Условия платежа изменились. Выставьте платёж заново.')
        amt=bill[1]['amt']
    elif n:
        q=term_quote(r,n,term,on);amt=q['amt'];pe=q['pen']
        if debt:dp=dstate(r,on)['dl'];amt+=dp
    elif r.get('status')=='active':amt=dstate(r,on)['dl'];dp=amt
    else:amt=r.get('debt',0)
    amt=cash(amt)
    check_amount({'expected':expected,'expected_quote':expected_quote},amt,{**q,'amt':amt} if q else None)
    if amt<=0:
        if n and not bill and q.get('discount'):
            old=json.loads(json.dumps(r));addpay(r,n,0,0,'discount',by,term=term,discount_info=q)
            void_bills(rid,by,period_only=True);put(rid,'rent',r,'pay',by,old);return None
        raise Bad('Нечего оплачивать')
    for qid,existing in kind('qr'):
        if (existing['rent']==rid and existing.get('bill')==(bill[0] if bill else None)
                and existing['n']==n and existing.get('term')==term and existing.get('dpart',0)==dp
                and existing['amt']==amt and qr_available(r,existing,on)):
            return qid
    c=doc(r['client']) or {}
    m=qr_make(amt,('Аренда электровелосипеда'+(f", договор {r['contract']}" if r.get('contract') else '')+f", {c.get('name','')}").strip(', '))
    d={'rent':rid,'n':n,'amt':amt,'pen':pe,'dpart':dp,'base':paidn(r),'date':str(on),'status':'open','tid':m['id'],'payload':m['payload'],'img':m['img'],'ts':now().isoformat(timespec='seconds')}
    if bill:
        d['bill']=bill[0]
        if bill[1]['kind']=='period':d['quote']=quote_stamp(term_quote(r,1,on=on))
    if term:d.update(term=term,start=q['from'])
    if n and not bill:d.update({key:q.get(key) for key in ('discount','discount_id')});d['quote']=quote_stamp(q)
    qid=nid();put(qid,'qr',d,'qr',by);return qid
def on_paid(qid,q):
    """Банк подтвердил оплату QR. Засчитываем автоматически, если это ещё имеет смысл; иначе — в «Не распознано»."""
    q=doc(qid)
    if not q or q.get('status') not in ('open','void'):return
    cancelled=q.get('status')=='void'
    old=dict(q)
    r=doc(q['rent']);ok=False;o=json.loads(json.dumps(r)) if r else None;t=today()
    act_=r and r.get('status')=='active' and not r.get('waiting') and not cancelled
    if act_ and q.get('bill'):
        b=doc(q['bill'])
        same_quote=not q.get('quote') or q['quote']==quote_stamp(term_quote(r,1))
        if b and b.get('status')=='open' and b['amt']==q['amt'] and bill_current(r,b) and same_quote and q['date']==str(t):
            apply_money(r,b['kind'],q['amt'],'qr','СБП',D(b['due']) if b.get('due') else None,qid,on=t)
            ob=dict(b);b['status']='paid';b['paid']=now().isoformat(timespec='seconds');put(q['bill'],'bill',b,'bill-paid','',ob);ok=True
    elif act_ and q['n'] and str(t)==q['date'] and period_qr_current(r,q,t):
        dp=q.get('dpart',0)
        if dp:apply_money(r,'debt',dp,'qr','СБП',qr=qid,on=t)
        addpay(r,q['n'],cash(q['amt']-dp),q['pen'],'qr','СБП',qid,term=q.get('term'),discount_info=q)
        ok=True
    elif act_ and not q.get('bill') and not q['n'] and q.get('dpart') and r.get('dl') and q['date']==str(t):
        apply_money(r,'debt',cash(q['amt']),'qr','СБП',qr=qid,on=t);ok=True
    elif r and not cancelled and not q.get('bill') and not q.get('dpart') and not q['n'] and r.get('status')!='active' and cash(r.get('debt',0))==q['amt']:
        before=ledger(r);p=addpay(r,0,q['amt'],0,'qr','СБП',qid,kind='debt');r['debt']=0;payment_undo(r,p,before);ok=True
    credited=0
    if ok:
        # Один банковский платёж может содержать аренду и доплату. Округляем удержания по всей операции.
        for payment in (r.get('pays') or [])[len(o.get('pays') or []):]:
            previous=sbp_fees(credited)
            credited=cash(credited+payment['amt'])
            payment['fees']={key:cash(fee-previous[key]) for key,fee in sbp_fees(credited).items()}
        if paidn(r)>paidn(o):
            void_bills(q['rent'],'СБП',period_only=True);close_requests(q['rent'],'СБП')
        put(q['rent'],'rent',r,'pay-qr','',o)
    uncredited=cash(q['amt']-credited)
    q['status']='paid';q['result']='applied' if ok and uncredited==0 else 'review';put(qid,'qr',q,'qr-paid','',old)
    c=(doc((r or {}).get('client','')) or {}).get('name','')
    if ok and uncredited<=0:say(f"Оплата по QR: {c}, {rub(q['amt'])}.")
    else:
        booked_fees=sbp_fees(credited)
        fees={key:cash(fee-booked_fees[key]) for key,fee in sbp_fees(q['amt']).items()}
        put(nid(),'unk',{'ts':now().isoformat(timespec='seconds'),'amt':uncredited,'qr':qid,'rent':q['rent'],'status':'open','fees':fees,
            'why':'Переплата сверх долга' if ok else 'Оплата по отменённому QR' if cancelled else 'Оплата пришла после изменений в аренде или не в тот день'},'unk','')
        say(f"Платёж по QR не распознан: {c}, {rub(uncredited)}. Разберите в приложении.")
def poll():
    if MOCK or not POLL.acquire(blocking=False):return
    try:
        with L:
            if not tq():return
            cutoff=today()-dt.timedelta(days=2)
            pending={q['tid']:i for i,q in kind('qr') if q.get('status') in ('open','void') and D(q['date'])>=cutoff}
        ids=list(pending)
        for start in range(0,len(ids),20):
            batch=ids[start:start+20]
            try:paid=qr_paid(batch)
            except urllib.error.HTTPError as error:
                if len(batch)==1 or error.code not in (400,404,414):
                    print('poll',error);continue
                paid=set()
                for tid in batch:
                    try:paid.update(qr_paid([tid]))
                    except Exception as error:print('poll',error)
            except Exception as error:
                print('poll',error);continue
            with L:
                for tid in paid:
                    i=pending.get(tid)
                    q=doc(i) if i else None
                    if not q or q.get('tid')!=tid:continue
                    try:
                        with db:on_paid(i,q)
                    except Exception as error:print('paid qr',i,error)
    finally:POLL.release()
# ---------- права и представление ----------
def adm_of(u):
    for i,d in kind('admin'):
        if d.get('tg')==u:return i,d
def role(u):
    if u==OWNER:return 'owner',None
    a=adm_of(u)
    if a:return ('off',a) if a[1].get('disabled') else ('admin',a)
    b=db.execute('select client from bind where tg=?',(u,)).fetchone()
    client=doc(b[0]) if b else None
    if client and not client.get('arch') and not client.get('purged'):return 'client',b[0]
    return 'new',None
def snapshot(ro,P):
    """Все данные для главного аккаунта или администратора; аренды и велосипеды с готовыми расчётами."""
    fin=ro=='owner' or 'reports' in P;out={k:{} for k in KINDS if k!='cfg'};t=today()
    busy={r['bike']:i for i,r in kind('rent') if r.get('status')=='active'}
    for i,k,d in db.execute('select id,kind,data from docs'):
        if k=='cfg':continue
        d=json.loads(d)
        if k=='admin':
            if ro!='owner':continue
            d['perms']=admin_permissions(d.get('perms') or [])
        if k=='exp' and not(fin or 'exp' in P):continue
        if k=='rent':
            d['st']=rstate(d,t)
            if fin:d['renewed']=renewed_after_first_term(d,t)
        if k=='bill' and d.get('status')=='open':
            rent=doc(d['rent'])
            d['stale']=not (rent and rent.get('status')=='active' and not rent.get('waiting') and bill_current(rent,d,t))
        if k=='req' and d.get('status')=='pending':
            rent=doc(d['rent'])
            if not rent or rent.get('status')!='active' or rent.get('waiting'):continue
        if k=='unk' and ro!='owner' and not {'reports','pay_review'}.intersection(P):continue
        if k=='bike':
            d['rent']=busy.get(i)
            if not fin:d.pop('cost',None);d.pop('sale',None)
        if k=='qr':
            d.pop('img',None)
            if ro!='owner' and 'pay_qr' not in P:d.pop('payload',None);d.pop('tid',None)
        out[k][i]=d
    c=cfg();tc=c.pop('tochka',{}) or {}
    c.update(contact_data(c))
    c['tochka']={'on':bool(tq()),'merchant':tc.get('merchant',''),'account':tc.get('account',''),'jwt':bool(tc.get('jwt')),'hook':tc.get('hook','')} if ro=='owner' else {'on':bool(tq())}
    if not fin:c={x:c[x] for x in('week','month','renewal_rates','pen','grace','ddays','cdef','archdays','cats','tochka')}
    result={'docs':out,'cfg':c,'today':str(t)}
    if fin:result['forecast']=receipt_forecast(t)
    if ro=='owner':
        reminder=backup_status()
        result.update(backup={'ts':reminder['last']} if reminder['last'] else None,backup_reminder=reminder)
    return result
def mine(cid):
    c=doc(cid) or {};a=active_of('client',cid);cf=cfg()
    r=None
    if a:
        i,d=a;st=rstate(d)
        req=next(({'days':x['days'],'ts':x['ts']} for _,x in kind('req') if x.get('rent')==i and x.get('status')=='pending'),None)
        q1=quote(d,1)
        qr=next(({'id':j,'amt':x['amt'],'n':x['n'],'payload':x['payload'],'img':x.get('img')}
                 for j,x in kind('qr') if x.get('rent')==i and qr_available(d,x,today())),None)
        bills=[{'id':j,'amt':x['amt'],'kind':x['kind'],'due':x.get('due'),'ts':x['ts']}
               for j,x in kind('bill') if x.get('rent')==i and x.get('status')=='open' and not d.get('waiting') and bill_current(d,x)]
        r={'id':i,'issued':d.get('issued'),'type':d['type'],'st':st,'q1':q1['amt'],'bills':bills,
           'waiting':d.get('waiting'),'terms':list(d.get('rates') or {}),'return_request':d.get('return_request'),
           'pays':[{k:p.get(k) for k in('ts','n','amt','pen','method','kind','term','discount')} for p in d.get('pays') or []][-30:],'req':req,'qr':None if d.get('waiting') else qr,
           'defers':[x for x in d.get('defer') or []][-5:]}
    debt=cash(sum(d.get('debt',0) for _,d in kind('rent') if d.get('client')==cid and d.get('status')!='active'))
    return {'role':'client','today':str(today()),'name':c.get('name',''),'discount':({'amt':c['discount']['amt']} if c.get('discount') else None),'rent':r,'debt':debt,'cdef':cf['cdef'],'pen':cf['pen'],'grace':cf['grace'],'qr':bool(tq()),'fio':c.get('name',''),**contact_data(cf)}
# ---------- действия ----------
NEED={'issue':'rent_issue','return':'rent_return','waitreturn':'rent_return','rates':'rent_rates','rentnote':'rent_note',
      'price':'rent_rates','contract':'rent_contract','fix':'rent_date','seize':'seize','returnack':'rent_return',
      'pay':'pay_accept','paydebt':'pay_accept','paydl':'pay_accept','billpaid':'pay_accept','paypart':'partial',
      'bill':'bill','billvoid':'bill','qr':'pay_qr','qrcancel':'pay_qr','unk':'pay_review','paydel':'pay_delete',
      'defer':'defer_manual','undefer':'defer_manual','req':'defer_requests','nopen':'pen_toggle','waive':'pen_waive',
      'client':'client_edit','black':'client_block','archive':'client_archive','unarchive':'client_archive',
      'bike':'bike_edit','bikestat':'bike_status','exp':'exp','expdel':'exp'}
QUOTE_PERMS={'pay_accept','partial','pay_qr','bill'}
OWNERONLY={'cfg','admin','admindel','purge','discount'}
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
def void_bills(rid,by,period_only=False):
    for j,x in kind('bill'):
        if x.get('rent')==rid and x.get('status')=='open' and (not period_only or x['kind']=='period'):
            ox=dict(x);x['status']='void';put(j,'bill',x,'bill-void',by,ox)
def close_requests(rid,by):
    for j,x in kind('req'):
        if x.get('rent')==rid and x.get('status')=='pending':
            ox=dict(x);x['status']='no';put(j,'req',x,'req',by,ox)
def act(a,b,ro,P,by):
    """Выполняет действие главного аккаунта или администратора. Возвращает ответ (dict). Вызывать под замком."""
    if a in OWNERONLY and ro!='owner':raise PermissionError
    if ro=='admin' and a in NEED and NEED[a] not in P:raise PermissionError
    if ro=='admin' and a=='quote' and not QUOTE_PERMS.intersection(P):raise PermissionError
    if a not in NEED and a not in OWNERONLY and a!='quote':raise Bad('Неизвестное действие')
    if a=='discount':
        i,c=get('client',b.get('id'));need_v(b,c)
        if c.get('purged'):raise Bad('Клиент удалён')
        old=json.loads(json.dumps(c));amount=num(b.get('amt') or 0)
        if amount:c['discount']={'id':nid(),'amt':amount,'note':txt(b.get('note'),200),'ts':now().isoformat(timespec='seconds'),'by':by}
        else:c.pop('discount',None)
        put(i,'client',c,'discount',by,old)
        for rid,r in kind('rent'):
            if r.get('client')==i and r.get('status')=='active':
                for bid,bill in kind('bill'):
                    if bill.get('rent')==rid and bill.get('kind')=='period' and bill.get('status')=='open':
                        previous=dict(bill);bill['status']='void';put(bid,'bill',bill,'bill-void',by,previous)
        tell(i,f'Вам назначена скидка {rub(amount)} на следующую оплату аренды. Она появится при выборе срока в приложении.' if amount else 'Скидка на следующую оплату отменена.')
        return {}
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
        write_log('purge',by,i);return {}
    if a=='black':
        i,d=get('client',b.get('id'));o=dict(d);d['black']=bool(b.get('on'));d['bnote']=txt(b.get('note'),300);put(i,'client',d,'black',by,o);return {}
    if a=='bike':
        i=str(b.get('id') or nid());old=None
        if b.get('id'):i,old=get('bike',i);need_v(b,old)
        d=dict(old or {'status':'free','created':str(t)})
        d.update(num=txt(b.get('num'),20),name=txt(b.get('name'),80),vin=txt(b.get('vin'),40),note=txt(b.get('note'),500),point=txt(b.get('point'),60))
        if ro=='owner' or 'reports' in P:d.update(cost=num(b.get('cost') or 0),bought=txt(b.get('bought'),10))
        if d.get('bought'):
            bought=day(d['bought'])
            d['bought']=str(bought)
            if bought>t:raise Bad('Дата покупки не может быть в будущем')
            if d.get('gone') and bought>D(d['gone']):raise Bad('Дата покупки позже даты архива')
        if not d['name']:raise Bad('Укажите название')
        put(i,'bike',d,'bike',by,old);return {'id':i}
    if a=='bikestat':
        i,d=get('bike',b.get('id'));need_v(b,d);s=b.get('status');o=dict(d)
        if s not in('free','repair','sold','off'):raise Bad('Неверный статус')
        if active_of('bike',i):raise Bad('Велосипед сейчас в аренде')
        archive_day=day(b.get('date') or t)
        if s in('sold','off') and archive_day>t:raise Bad('Дата архива не может быть в будущем')
        if s in('sold','off') and d.get('bought') and archive_day<D(d['bought']):raise Bad('Дата архива раньше покупки')
        last=max([r.get('closed') or r.get('issued') or r.get('start') for _,r in kind('rent') if r.get('bike')==i]+['0001-01-01'])
        if s in('sold','off') and str(archive_day)<last:raise Bad('Дата архива раньше последней аренды велосипеда')
        d['status']=s;d.pop('sale',None);d.pop('gone',None)
        if s=='sold':d['sale']={'amt':num(b.get('amt')),'date':str(archive_day)}
        if s in('sold','off'):d['gone']=str(archive_day)
        put(i,'bike',d,'bikestat',by,o);return {}
    if a=='exp':
        i=str(b.get('id') or nid());old=None
        if b.get('id'):i,old=get('exp',i);need_v(b,old)
        bk=str(b.get('bike') or '')
        if bk:get('bike',bk)
        date=day(b.get('date'))
        if date>t:raise Bad('Расход учитывается по дате оплаты. Будущая дата недоступна.')
        d={'date':str(date),'amt':num(b.get('amt'),0.01),'cat':txt(b.get('cat'),40) or 'Прочее','bike':bk,'title':txt(b.get('title'),120)}
        put(i,'exp',d,'exp',by,old);return {'id':i}
    if a=='expdel':
        i,d=get('exp',b.get('id'));db.execute('delete from docs where id=?',(i,))
        write_log('expdel',by,i,old=d);return {}
    if a=='issue':
        ci,c=get('client',b.get('client'));bi,bk=get('bike',b.get('bike'))
        if c.get('arch'):raise Bad('Клиент в архиве. Сначала верните его из архива.')
        if active_of('client',ci):raise Bad('У клиента уже есть активная аренда')
        if active_of('bike',bi) or bk.get('status')!='free':raise Bad('Велосипед не свободен')
        term=b.get('term')
        if 'term' in b:
            if not isinstance(term,str) or term not in TERM_DAYS:raise Bad('Выберите первый срок аренды')
            rates=rate_prices(b.get('rates',cfg()['renewal_rates']))
            ty='m' if term=='m' else 'w'
            price=rates[ty]
            method=b.get('method')
            if method not in ('cash','transfer','no'):raise Bad('Выберите способ оплаты')
            n=int(method!='no')
        else:
            ty=b.get('type')
            if ty not in BASE_TERMS:raise Bad('Выберите неделю или месяц')
            price=num(b.get('price'),1)
            method='transfer' if b.get('method')=='transfer' else 'cash'
            n=integer(b.get('payn') or 0,0,24)
        iss=day(b.get('date') or t)
        if iss>t:raise Bad('Дата выдачи не может быть в будущем')
        if iss<t-dt.timedelta(days=60):raise Bad('Дата выдачи слишком давняя')
        if bk.get('bought') and iss<D(bk['bought']):raise Bad('Дата выдачи раньше покупки велосипеда')
        for _,previous in kind('rent'):
            if previous.get('bike')==bi or previous.get('client')==ci:
                if previous.get('closed') and iss<D(previous['closed']):
                    raise Bad('Дата выдачи пересекается с предыдущей арендой')
        r={'client':ci,'bike':bi,'type':ty,'price':price,'issued':str(iss),'free':bool(b.get('free')),
           'start':str(iss+dt.timedelta(days=1 if b.get('free') else 0)),'status':'active','pays':[],'by':by,'contract':txt(b.get('contract'),40)}
        if term:r.update(rates=rates,initial={'term':term,'price':rates[term]})
        if b.get('first') not in (None,''):r['once']={'1':num(b.get('first'),1)}
        i=nid()
        if n:
            q=term_quote(r,n,term,iss)
            check_amount(b,q['amt'],q)
            addpay(r,n,q['amt'],q['pen'],method,by,term=term,discount_info=q,
                ts=None if iss==t else dt.datetime.combine(iss,now().time(),MSK).isoformat(timespec='seconds'))
        put(i,'rent',r,'issue',by);return {'id':i}
    if a=='quote':
        i,r=get('rent',b.get('id'));n=integer(b.get('n',1),1,24)
        if r.get('status')!='active' or r.get('waiting'):raise Bad('Нечего оплачивать')
        return payment_quote(r,n,b.get('term'),bool(b.get('debt')),t)
    if a in('pay','paydebt','qr','qrcancel','return','waitreturn','returnack','rates','rentnote','seize','price','defer','undefer','contract','fix','paydel','nopen','waive','paypart','paydl','bill','billvoid','billpaid'):
        i,r=get('rent',b.get('id'));need_v(b,r);o=json.loads(json.dumps(r))
        act_=r.get('status')=='active'
        if a in('pay','qr','return','waitreturn','rates','seize','price','defer','undefer','fix','nopen','waive','paypart','paydl','bill','billvoid','billpaid') and not act_ and not(a=='qr' and r.get('debt')):raise Bad('Аренда уже закрыта')
        if a=='rentnote':
            r['note']=txt(b.get('note'),500);put(i,'rent',r,'rentnote',by,o);return {}
        if a=='waitreturn':
            if r.get('waiting'):return {}
            r['waiting']=str(t);r.pop('return_request',None)
            void_bills(i,by)
            close_requests(i,by)
            put(i,'rent',r,'waitreturn',by,o);return {}
        if a=='returnack':
            if not act_ or not r.get('return_request'):raise Bad('Заявки на возврат нет')
            if r['return_request'].get('ack'):return {}
            r['return_request']['ack']=True;r['return_request']['by']=by
            put(i,'rent',r,'returnack',by,o)
            tell(r['client'],f"Заявка на возврат {dm(r['return_request']['date'])} просмотрена. Свяжитесь с нами для согласования времени.")
            return {}
        if act_ and r.get('waiting') and a not in ('return','contract','qrcancel','seize'):
            raise Bad('Аренда ждёт возврата')
        if a=='rates':
            values=b.get('rates')
            if values=={}:r.pop('rates',None)
            else:r['rates']=rate_prices(values)
            k=paidn(r)+1
            schedule=b.get('schedule')
            if schedule is not None:
                if not isinstance(schedule,dict):raise Bad('Укажите базовый период и цену')
                ty=schedule.get('type')
                if ty not in BASE_TERMS:raise Bad('Неверный период')
                price=num(schedule.get('price'),1)
                if (ty,price)!=recurring_plan(r,k):set_plan(r,k,ty,price,t,by)
            if 'first' in b:
                first=b['first']
                if first is None or first=='':(r.get('once') or {}).pop(str(k),None)
                else:r.setdefault('once',{})[str(k)]=num(first,1)
            put(i,'rent',r,'rates',by,o);return {}
        if a=='pay':
            n=integer(b.get('n',1),1,24)
            m=b.get('method')
            if m not in('cash','transfer','qr'):raise Bad('Выберите способ оплаты')
            term=b.get('term') or None
            q=term_quote(r,n,term,t);check_amount(b,q['amt'],q)
            addpay(r,1 if term else n,q['amt'],q['pen'],m,by,term=term,discount_info=q);put(i,'rent',r,'pay',by,o)
            void_bills(i,by,period_only=True);close_requests(i,by)
            # открытые QR этой аренды больше не действуют
            for j,x in kind('qr'):
                if x.get('rent')==i and x.get('status')=='open':ox=dict(x);x['status']='void';put(j,'qr',x,'qr-void',by,ox)
            return {'amt':q['amt']}
        if a=='paypart':
            # частичная оплата следующего периода: период засчитан, остаток — долг со сроком доплаты
            m=b.get('method')
            if m not in('cash','transfer'):raise Bad('Выберите способ оплаты')
            amt=num(b.get('amt'),0.01);q=term_quote(r,1,on=t)
            check_amount(b,q['amt'],q)
            if amt+0.005>=q['amt']:raise Bad('Это полная сумма — примите обычную оплату')
            du=day(b.get('due') or t+dt.timedelta(days=int(cfg()['ddays'])))
            if du<t:raise Bad('Срок доплаты уже прошёл')
            apply_money(r,'period',amt,m,by,du,on=t)
            void_bills(i,by,period_only=True);close_requests(i,by)
            put(i,'rent',r,'paypart',by,o);return {'debt':cash(q['amt']-amt)}
        if a=='paydl':
            m=b.get('method')
            if m not in('cash','transfer'):raise Bad('Выберите способ оплаты')
            amt=num(b.get('amt'),0.01);tot=dstate(r,t)['dl']
            if not tot:raise Bad('Долга нет')
            if amt>tot+0.005:raise Bad(f'Долг всего {rub(tot)}')
            apply_money(r,'debt',amt,m,by);put(i,'rent',r,'paydl',by,o);return {'left':dstate(r,t)['dl']}
        if a=='bill':
            k=b.get('kind');amt=num(b.get('amt'),0.01)
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
            bi=nid();put(bi,'bill',{'rent':i,'client':r['client'],'kind':k,'amt':amt,'due':str(du) if du else None,'status':'open','ts':now().isoformat(timespec='seconds'),'by':by,
                'base':paidn(r),'discount_id':q.get('discount_id') if k=='period' else None,
                'quote':quote_stamp(term_quote(r,1)) if k=='period' else None},'bill',by)
            tell(r['client'],f'Вам выставлен платёж {rub(amt)}. Оплатить можно в приложении.');return {'id':bi}
        if a in('billvoid','billpaid'):
            j,x=get('bill',b.get('bill'))
            if x.get('rent')!=i or x.get('status')!='open':raise Bad('Платёж уже закрыт')
            ox=dict(x)
            if a=='billvoid':x['status']='void';put(j,'bill',x,'bill-void',by,ox);return {}
            if not bill_current(r,x):raise Bad('Условия платежа изменились. Выставьте платёж заново.')
            m=b.get('method')
            if m not in('cash','transfer'):raise Bad('Выберите способ оплаты')
            left=apply_money(r,x['kind'],x['amt'],m,by,D(x['due']) if x.get('due') else None)
            if x['kind']=='period':close_requests(i,by)
            x['status']='paid';x['paid']=now().isoformat(timespec='seconds');put(j,'bill',x,'bill-paid',by,ox);put(i,'rent',r,'billpaid',by,o);return {'left':left}
        if a=='paydebt':
            if act_ or not r.get('debt'):raise Bad('Долга нет')
            m=b.get('method') if b.get('method') in('cash','transfer','qr') else 'cash'
            before=ledger(r);p=addpay(r,0,r['debt'],0,m,by,kind='debt');r['debt']=0;payment_undo(r,p,before);put(i,'rent',r,'paydebt',by,o);return {}
        if a=='qr':
            if b.get('bill'):
                j,x=get('bill',b['bill'])
                if x.get('rent')!=i or x.get('status')!='open':raise Bad('Платёж уже закрыт')
                return {'qr':new_qr(i,r,0,by,bill=(j,x))}
            n=0 if (not act_ or b.get('debtonly')) else integer(b.get('n',1),1,12)
            term=(b.get('term') or None) if n else None
            return {'qr':new_qr(i,r,1 if term else n,by,debt=bool(b.get('debt')),term=term,expected=b.get('expected'),expected_quote=b.get('expected_quote'))}
        if a=='qrcancel':
            j,x=get('qr',b.get('qr'))
            if x.get('rent')!=i:raise Bad('QR относится к другой аренде')
            if x.get('status')=='open':ox=dict(x);x['status']='void';put(j,'qr',x,'qr-void',by,ox)
            return {}
        if a=='paydel':
            ps=r.get('pays') or []
            k=integer(b.get('k',-1),-1,max(0,len(ps)-1))
            if not 0<=k<len(ps):raise Bad('Платёж не найден')
            payment=ps[k]
            if payment.get('old') or (ro=='admin' and payment.get('method') not in ('cash','transfer')):raise PermissionError
            if payment.get('qr'):raise Bad('Подтверждённая оплата по СБП хранится в истории. Возврат денег оформляется отдельно.')
            if k!=len(ps)-1:raise Bad('Сначала удалите более поздние платежи этой аренды, чтобы не нарушить сроки и остаток долга.')
            undo=payment.get('undo')
            if not undo and payment.get('kind') in ('part','debt'):undo=legacy_undo(i,payment,ledger(r))
            if undo:
                if ledger(r)!=undo['after']:raise Bad('После платежа изменился долг или аренда была завершена. Автоматическая отмена небезопасна: сначала проверьте историю.')
            elif payment.get('kind') in ('part','debt'):
                raise Bad('Для старого платежа нет данных, позволяющих точно восстановить долг. Автоматическое удаление недоступно.')
            elif not act_:raise Bad('Нельзя удалить оплату периода завершённой аренды.')
            if payment.get('discount_offer'):
                ci,c=get('client',r['client']);old=json.loads(json.dumps(c))
                if c.get('discount'):raise Bad('Сначала отмените новую скидку клиента, чтобы восстановить скидку удаляемой оплаты.')
                c['discount']=payment['discount_offer'];put(ci,'client',c,'discount-restored',by,old)
            if undo:r['dl']=undo['before']['dl'];r['debt']=undo['before']['debt']
            ps.pop(k);void_bills(i,by,period_only=True);put(i,'rent',r,'paydel',by,o);return {}
        if a=='price':
            p=num(b.get('price'),1);k=paidn(r)+1;ty=b.get('type') or plan(r,k)[0]
            if ty not in BASE_TERMS:raise Bad('Неверный период')
            if b.get('once'):
                if ty!=plan(r,k)[0]:raise Bad('Разовая цена не меняет период')
                r.setdefault('once',{})[str(k)]=p
            else:
                set_plan(r,k,ty,p,t,by)
                r.get('once',{}).pop(str(k),None)
            put(i,'rent',r,'price',by,o);return {}
        if a=='defer':
            u=day(b.get('until'))
            if u<t:raise Bad('Дата уже прошла')
            r.setdefault('defer',[]).append({'from':str(t),'until':str(u),'by':by,'ts':now().isoformat(timespec='seconds')})
            put(i,'rent',r,'defer',by,o);tell(r['client'],f'Вам дана отсрочка без неустойки до {dm(u)}.');return {}
        if a=='nopen':
            on=bool(b.get('on'))
            if on!=bool(r.get('nopen')):
                pauses=r.setdefault('pen_pauses',[])
                if on:pauses.append({'from':str(t+dt.timedelta(days=1))})
                else:
                    if not pauses:pauses.append({'from':r['start']})
                    for pause in pauses:
                        if not pause.get('until'):pause['until']=str(t-dt.timedelta(days=1))
                r['nopen']=on
            put(i,'rent',r,'nopen',by,o);return {}
        if a=='waive':
            st=rstate(r,t);amt=st['pen']+st['dpen'];r['waive']=str(t);r.setdefault('waived',[]).append({'date':str(t),'amt':amt,'by':by})
            for it in r.get('dl') or []:it['pen']=0;it['pdate']=str(t)
            r['dl']=[it for it in r.get('dl') or [] if it['amt']>0.005]
            put(i,'rent',r,'waive',by,o);return {'amt':amt}
        if a=='undefer':
            yesterday=str(t-dt.timedelta(days=1))
            r['defer']=[{**x,'until':min(x['until'],yesterday)} for x in r.get('defer') or [] if D(x['from'])<t]
            put(i,'rent',r,'undefer',by,o);return {}
        if a=='contract':
            r['contract']=txt(b.get('contract'),40);put(i,'rent',r,'contract',by,o);return {}
        if a=='fix':
            nx=day(b.get('next'));cur=D(rstate(r,t)['next'])
            if nx<D(r.get('issued') or r['start']):raise Bad('Дата оплаты не может быть раньше выдачи')
            if nx==cur:return {}
            try:
                desired_start=nx+dt.timedelta(days=1)
                if not paidn(r) and str(nx)==(r.get('issued') or r['start']) and not r.get('free'):desired_start=nx
                r['shift']=r.get('shift',0)+(desired_start-pstart(r,paidn(r)+1)).days
                term_quote(r,24,on=t)
            except (ValueError,OverflowError):raise Bad('Дата оплаты выходит за допустимый диапазон')
            put(i,'rent',r,'fix',by,o,audit={'from':str(cur),'to':str(nx)});return {}
        st=rstate({**r,'waiting':r.get('waiting') or str(t)},t)
        if a=='return':
            debt=num(b.get('debt') or 0);ref=num(b.get('refund') or 0)
            r.update(status='closed',closed=str(t),debt=debt,refund={'amt':ref,'date':str(t)} if ref else None,debt0=cash(st['debt']+st['dl']),dl=[])
            r.pop('return_request',None)
            void_bills(i,by);close_requests(i,by)
            put(i,'rent',r,'return',by,o);return {}
        if a=='seize':
            if b.get('confirm')!='ИЗЪЯТЬ':raise Bad('Нужно подтверждение')
            r.update(status='seized',closed=str(t),debt=0,writeoff=cash(st['rent_due']+st['dl']),dl=[])
            r.pop('return_request',None)
            void_bills(i,by);close_requests(i,by)
            put(i,'rent',r,'seize',by,o);return {}
    if a=='unk':
        i,u=get('unk',b.get('id'));o=dict(u)
        if u.get('status')!='open':raise Bad('Уже разобрано')
        if b.get('do') in ('apply','debt'):
            ri,r=get('rent',u['rent'])
            ro_=json.loads(json.dumps(r))
            qr=u.get('qr')
            if r.get('status')=='active':
                if r.get('waiting'):raise Bad('Аренда ждёт возврата')
                if b.get('do')=='debt':
                    if u['amt']>dstate(r,t)['dl']:raise Bad('Поступление больше остатка долга. Проверьте сумму.')
                    apply_money(r,'debt',u['amt'],'qr',by,qr=qr)
                    r['pays'][-1]['ts']=u['ts']
                else:
                    term=b.get('term')
                    n=integer(b.get('n',1 if term else 0),1,24)
                    if term and (not isinstance(term,str) or term not in TERM_DAYS):raise Bad('Неверный срок оплаты')
                    if term and not paidn(r) and r.get('initial') and term!=r['initial']['term']:
                        raise Bad('Сначала оплатите первый срок аренды')
                    addpay(r,1 if term else n,u['amt'],0,'qr',by,qr,ts=u['ts'],term=term,use_discount=False)
                    void_bills(ri,by,period_only=True);close_requests(ri,by)
            else:
                if u['amt']>r.get('debt',0):raise Bad('Поступление больше остатка долга. Проверьте сумму.')
                before=ledger(r)
                payment=addpay(r,0,u['amt'],0,'qr',by,qr,ts=u['ts'],kind='debt')
                r['debt']=cash(r['debt']-u['amt'])
                payment_undo(r,payment,before)
            if u.get('fees'):r['pays'][-1]['fees']=u['fees']
            put(ri,'rent',r,'unk-apply',by,ro_);u['status']='applied';u['to']=ri
        elif b.get('do')=='done':u['status']='done';u['note']=txt(b.get('note'),200)
        else:raise Bad('Укажите, как разобрать платёж')
        put(i,'unk',u,'unk',by,o);return {}
    if a=='req':
        i,q=get('req',b.get('id'));o=dict(q)
        if q.get('status')!='pending':raise Bad('Заявка уже рассмотрена')
        ri,r=get('rent',q['rent'])
        upcoming=rstate(r,t).get('next')
        if b.get('ok') and upcoming and not r.get('waiting') and q.get('base',paidn(r))==paidn(r) and q.get('due',upcoming)==upcoming:
            ro_=json.loads(json.dumps(r));ns=D(rstate(r,t)['next'])
            u=max(ns,t)+dt.timedelta(days=q['days'])
            r.setdefault('defer',[]).append({'from':str(t),'until':str(u),'by':by,'ts':now().isoformat(timespec='seconds'),'req':i})
            put(ri,'rent',r,'defer',by,ro_);q['status']='ok';q['until']=str(u);tell(r['client'],f'Отсрочка одобрена: без неустойки до {dm(u)}.')
        else:q['status']='no';tell(r['client'],'Заявка на отсрочку отклонена.')
        q['by']=by;put(i,'req',q,'req',by,o);return {}
    if a=='cfg':
        old=doc('cfg');c=cfg();o=json.loads(json.dumps(old or {}))
        if 'renewal_rates' in b:
            c['renewal_rates']=rate_prices(b['renewal_rates'])
            c['week']=c['renewal_rates']['w'];c['month']=c['renewal_rates']['m']
        else:
            for k in ('week','month'):c[k]=num(b.get(k,c[k]),1)
        for k in ('pen','tax','acq','receipt'):c[k]=num(b.get(k,c[k]),0,100)
        for key,low,high in (('cdef',0,3),('grace',0,10),('ddays',1,60),('archdays',0,365)):
            c[key]=integer(b.get(key,c[key]),low,high)
        c['cphone']=txt(b.get('cphone',c['cphone']),30)
        for field,label in (('ctg','Telegram'),('cmax','МАКС')):
            if field in b:
                value=txt(b[field],2048)
                link=chat_url(value,telegram=field=='ctg')
                if value and not link:raise Bad(f'Укажите действующую HTTPS-ссылку для контакта в {label}')
                c[field]=link
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
        if (o.get('tochka') or {}).get('jwt'):o['tochka']['jwt']='***'
        lg=dict(c)
        if (lg.get('tochka') or {}).get('jwt'):lg['tochka']=dict(lg['tochka'],jwt='***')
        if isinstance(b.get('tochka'),dict) and b['tochka'].get('jwt'):lg['_audit']={'bank_key_changed':True}
        write_log('cfg',by,'cfg',o,lg);return {}
    if a=='admin':
        i=str(b.get('id') or nid());old=None
        if b.get('id'):
            i,old=get('admin',i);old['perms']=admin_permissions(old.get('perms') or [])
        nm=txt(b.get('name'),60);ph=txt(b.get('phone'),30)
        if not nm or not digits(ph):raise Bad('Укажите имя и телефон')
        pl=b.get('perms') if isinstance(b.get('perms'),list) else []
        d={'name':nm,'phone':ph,'perms':admin_permissions(pl),'tg':(old or {}).get('tg'),'tgu':(old or {}).get('tgu',''),'disabled':bool(b.get('disabled')),'created':(old or {}).get('created',str(t)),'m2':1}
        put(i,'admin',d,'admin',by,old);return {'id':i}
    if a=='admindel':
        i,d=get('admin',b.get('id'));db.execute('delete from docs where id=?',(i,))
        db.execute('delete from inv where client=?',('adm:'+i,))
        write_log('admindel',by,i,old=d);return {}
    raise Bad('Неизвестное действие')
def client_act(cid,b):
    a=active_of('client',cid)
    if not a:raise Bad('Активной аренды нет')
    i,r=a
    if r.get('waiting'):raise Bad('Аренда ждёт возврата')
    if b.get('a') in ('return_request','return_cancel'):
        old=json.loads(json.dumps(r))
        if b['a']=='return_cancel':
            r.pop('return_request',None);put(i,'rent',r,'return-cancel','клиент',old)
            say(f"Клиент {(doc(cid) or {}).get('name','')} отменил заявку на возврат.");return {}
        date=day(b.get('date'));t=today()
        if date<t or date>t+dt.timedelta(days=365):raise Bad('Выберите дату возврата от сегодня до года вперёд')
        r['return_request']={'date':str(date),'note':txt(b.get('note'),200),'ts':now().isoformat(timespec='seconds'),'ack':False}
        put(i,'rent',r,'return-request','клиент',old)
        say(f"Заявка на возврат: {(doc(cid) or {}).get('name','')}, {dm(date)}. Велосипед {(doc(r['bike']) or {}).get('num','')}. Посмотрите заявку в приложении.")
        return {}
    if b.get('a')=='quote':
        n=integer(b.get('n',1),1,6)
        return payment_quote(r,n,b.get('term'),bool(b.get('debt')))
    if b.get('a')=='qr' and b.get('bill'):
        j,x=get('bill',b['bill'])
        if x.get('rent')!=i or x.get('status')!='open':raise Bad('Платёж уже закрыт')
        return {'qr':new_qr(i,r,0,'клиент',bill=(j,x))}
    if b.get('a')=='qr' and b.get('debtonly'):
        if not r.get('dl'):raise Bad('Долга нет')
        return {'qr':new_qr(i,r,0,'клиент')}
    if b.get('a')=='qr':
        n=integer(b.get('n',1),1,6)
        term=b.get('term') or None
        if term:n=1
        return {'qr':new_qr(i,r,n,'клиент',debt=bool(b.get('debt')),term=term,
                            expected=b.get('expected'),expected_quote=b.get('expected_quote'))}
    if b.get('a')=='req':
        mx=cfg()['cdef']
        if not mx:raise Bad('Заявки на отсрочку отключены')
        n=integer(b.get('days',0),1,mx)
        if any(x.get('rent')==i and x.get('status')=='pending' for _,x in kind('req')):raise Bad('Заявка уже отправлена')
        put(nid(),'req',{'rent':i,'client':cid,'days':n,'base':paidn(r),'due':rstate(r)['next'],
                         'ts':now().isoformat(timespec='seconds'),'status':'pending'},'req-new','клиент')
        say(f"Заявка на отсрочку: {(doc(cid) or {}).get('name','')}, {n} дн. Одобрите или отклоните в приложении.");return {}
    raise Bad('Неизвестное действие')
# ---------- копии ----------
def dump():
    with L:
        ds=[]
        for i,k,x in db.execute('select id,kind,data from docs'):
            d=json.loads(x)
            if k=='cfg' and d.get('tochka'):d=dict(d,tochka={**d['tochka'],'jwt':'','hook':''})
            ds.append({'id':i,'kind':k,'data':d})
        log=[]
        for ts,act_,i,old,new in db.execute('select ts,act,id,old,new from log'):
            if i=='cfg':
                def safe_config_log(value):
                    try:
                        entry=json.loads(value)
                        if entry.get('tochka'):entry['tochka']={**entry['tochka'],'jwt':'***','hook':'***'}
                        return json.dumps(entry,ensure_ascii=False)
                    except (AttributeError,TypeError,ValueError):return None
                old,new=safe_config_log(old),safe_config_log(new)
            log.append([ts,act_,i,old,new])
        return json.dumps({'v':1,'app':'rent','ts':now().isoformat(),'docs':ds,'bind':[[a,c] for a,c in db.execute('select tg,client from bind')],
            'log':log},ensure_ascii=False).encode()
IMG={};DL={};TTL=int(os.environ.get('DL_TTL','120'));BS=DATA+'/last_download.json'
def prune_download_links(at):
    for token in [token for token,(expires,_) in DL.items() if expires<=at]:
        DL.pop(token,None)

def send_backup(handler,token,metadata_only=False):
    ip=('dl',handler.ip())
    current=time.time()
    with L:
        prune_download_links(current)
        failures=FAIL[ip]=[at for at in FAIL.get(ip,[]) if at>current-600]
        link=DL.get(token)
        allowed=link is not None and len(failures)<10
        if not allowed:failures.append(current)
    if not allowed:
        return handler.send(404,b'',x={'Cache-Control':'no-store'})
    _,filename=link
    headers={
        'Content-Disposition':f'attachment; filename="{filename}"',
        'Access-Control-Allow-Origin':'https://web.telegram.org',
        'Access-Control-Expose-Headers':'Content-Disposition',
        'Cache-Control':'no-store',
    }
    handler.send(200,dump(),'application/octet-stream',headers)
    if not metadata_only:bset()

def bget():
    try:
        with open(BS) as file:return json.load(file)
    except Exception:return None
def bset():
    try:
        with L:
            with open(BS+'.tmp','w') as file:json.dump({'ts':now().isoformat()},file)
            os.replace(BS+'.tmp',BS)
    except Exception as e:print('bset',e)
def backup_status(on=None):
    current=on or now()
    if current.tzinfo is None:current=current.replace(tzinfo=MSK)
    try:
        saved=bget()
        if not isinstance(saved,dict):raise ValueError
        last=dt.datetime.fromisoformat(saved['ts'])
        if last.tzinfo is None:last=last.replace(tzinfo=MSK)
        last=last.astimezone(MSK)
        if last>current:raise ValueError
        following=last+dt.timedelta(days=3)
        return {'due':current>=following,'last':last.isoformat(),'next':following.isoformat(),'interval_days':3}
    except (ValueError,TypeError,KeyError):
        return {'due':True,'last':None,'next':None,'interval_days':3}
def snap(pre):
    b=sqlite3.connect(f'{BK}/{pre}-{now():%Y-%m-%d-%H%M%S-%f}-{nid()}.db')
    try:db.backup(b)
    finally:b.close()
    for f in sorted(x for x in os.listdir(BK) if x.startswith(pre+'-'))[:-(30 if pre=='app' else 5)]:os.remove(f'{BK}/{f}')
def validate_backup(items):
    """Проверяем расчётные поля и связи до замены рабочей базы. Старые необязательные поля допустимы."""
    records={x['id']:(x['kind'],x['data']) for x in items}
    busy_bikes=set();busy_clients=set()
    def amount(value):
        if type(value) not in (int,float) or abs(num(value,0,10**12)-value)>0.0001:raise Bad('Неверная денежная сумма')
    def date(value):
        if not isinstance(value,str) or str(day(value))!=value:raise Bad('Неверная дата')
    def ref(value,kind_):
        if records.get(value,(None,))[0]!=kind_:raise Bad('Нарушена связь '+kind_)
    for i,(k,d) in records.items():
        json.dumps(d,allow_nan=False)
        if (k=='cfg')!=(i=='cfg'):raise Bad('Неверные настройки')
        for key in ('amt','cost','price','debt','debt0','writeoff'):
            if key in d:amount(d[key])
        for key in ('date','bought','gone','issued','start','closed','waiting','waive','arch','created','purged'):
            if d.get(key):date(d[key])
        if k=='cfg':
            for key in ('pen','tax','acq','receipt','week','month'):
                if key in d:
                    amount(d[key])
                    num(d[key],0,100 if key in ('pen','tax','acq','receipt') else 10**8)
            for key,hi in (('grace',10),('ddays',60),('archdays',365),('cdef',3)):
                if key in d:integer(d[key],0 if key!='ddays' else 1,hi)
            if 'renewal_rates' in d:rate_prices(d['renewal_rates'])
            if 'tochka' in d and not isinstance(d['tochka'],dict):raise Bad('Неверные настройки банка')
            if 'cats' in d and (not isinstance(d['cats'],list) or not d['cats'] or any(not isinstance(x,str) for x in d['cats'])):
                raise Bad('Неверные категории расходов')
        if k=='bike':
            if d.get('status','free') not in ('free','repair','sold','off','rent'):raise Bad('Неверный статус велосипеда')
            if d.get('sale'):
                amount(d['sale']['amt']);date(d['sale']['date'])
                if d.get('status')!='sold':raise Bad('Продажа без архива')
        if k=='client' and d.get('discount'):
            amount(d['discount']['amt'])
            if not ID.fullmatch(str(d['discount']['id'])):raise Bad('Неверная скидка')
        if k=='rent':
            if d.get('status') not in ('active','closed','seized') or d.get('type') not in BASE_TERMS:raise Bad('Неверная аренда')
            if d.get('status')=='active':
                ref(d['bike'],'bike');ref(d['client'],'client')
                date(d['start'])
                amount(d['price'])
                if d['bike'] in busy_bikes or d['client'] in busy_clients:raise Bad('Две активные аренды одного велосипеда или клиента')
                busy_bikes.add(d['bike']);busy_clients.add(d['client'])
                if records[d['bike']][1].get('status') in ('sold','off'):raise Bad('Аренда архивного велосипеда')
                if records[d['client']][1].get('arch'):raise Bad('Аренда архивного клиента')
            elif not isinstance(d.get('start') or d.get('issued'),str):raise Bad('Нет даты выдачи')
            integer(d.get('base',0),0,100000)
            if 'shift' in d:integer(d['shift'],-100000,100000)
            if d.get('rates'):rate_prices(d['rates'])
            if d.get('initial'):
                if d['initial']['term'] not in TERM_DAYS:raise Bad('Неверный первый срок')
                amount(d['initial']['price'])
            for value in (d.get('once') or {}).values():amount(value)
            for p in d.get('pays') or []:
                integer(p.get('n',0),0,400);amount(p['amt']);amount(p.get('pen',0))
                dt.datetime.fromisoformat(p['ts'])
                if p.get('due'):date(p['due'])
                if p.get('term') and (p['term'] not in TERM_DAYS or p.get('n')!=1):raise Bad('Неверный оплаченный срок')
                if p.get('method') not in ('cash','transfer','qr','discount'):raise Bad('Неверный способ оплаты')
                if p.get('fees'):
                    for fee in ('tax','acq','receipt'):amount(p['fees'][fee])
            previous=0
            for p in d.get('plan') or []:
                n=integer(p['k'],1,100000)
                if n<=previous or p.get('type',d['type']) not in BASE_TERMS:raise Bad('Неверный график')
                amount(p['price']);previous=n
            for it in d.get('dl') or []:
                amount(it['amt']);amount(it.get('pen',0));date(it['due'])
                if it.get('pdate'):date(it['pdate'])
            for it in d.get('defer') or []:
                date(it['from']);date(it['until'])
                if it['from']>it['until']:raise Bad('Неверная отсрочка')
            for it in d.get('pen_pauses') or []:
                date(it['from'])
                if it.get('until'):date(it['until'])
            if d.get('refund'):amount(d['refund']['amt']);date(d['refund']['date'])
            if d.get('return_request'):date(d['return_request']['date'])
            if d.get('status')=='active':rstate(d)
        if k=='exp':
            date(d['date']);amount(d['amt'])
            if d.get('bike'):ref(d['bike'],'bike')
        if k in ('qr','bill','req','unk'):ref(d['rent'],'rent')
        if k=='qr':
            date(d['date']);amount(d['amt']);integer(d['n'],0,24)
            if d.get('status') not in ('open','paid','void') or not d.get('tid') or not chat_url(d.get('payload')):
                raise Bad('Неверный QR')
        if k=='bill':
            amount(d['amt']);dt.datetime.fromisoformat(d['ts'])
            if d['kind'] not in ('period','debt') or d.get('status') not in ('open','paid','void'):raise Bad('Неверный счёт')
            if d.get('due'):date(d['due'])
        if k=='req':
            integer(d['days'],1,3)
            if d.get('status') not in ('ok','no','pending'):raise Bad('Неверная заявка')
        if k=='unk':amount(d['amt']);dt.datetime.fromisoformat(d['ts'])
def restore(b):
    """Заменяет все данные содержимым файла (копия или файл переноса). Журнал сохраняется. Ключ Точки остаётся текущий."""
    try:
        if not isinstance(b,dict) or b.get('v')!=1 or b.get('app')!='rent':return 400,'Это не файл копии приложения аренды.'
        docs=b['docs'];bind=b.get('bind',[]);lg=b.get('log',[])
        if not isinstance(docs,list) or not isinstance(bind,list) or not isinstance(lg,list) or len(docs)>50000:return 400,'Файл повреждён.'
        seen=set();rows=[]
        for x in docs:
            i,k,d=x['id'],x['kind'],x['data']
            if not isinstance(i,str) or not (ID.fullmatch(i) or i=='cfg') or k not in KINDS or not isinstance(d,dict) or i in seen:return 400,'Файл повреждён: неверная запись.'
            seen.add(i);rows.append((i,k,d))
        validate_backup(docs)
        clients={x['id'] for x in docs if x['kind']=='client' and not x['data'].get('arch')}
        bi=[(integer(a,1,2**52-1),str(c)) for a,c in bind if str(c) in clients]
        lg=[tuple(None if y is None else str(y) for y in r) for r in lg if isinstance(r,list) and len(r)==5][-100000:]
    except Exception:return 400,'Файл повреждён.'
    with L:
        try:
            current=doc('cfg') or {}
            bank=current.get('tochka') or {}
            prepared=[(i,k,json.dumps({**d,'tochka':bank} if k=='cfg' else d,ensure_ascii=False)) for i,k,d in rows]
            if 'cfg' not in seen and bank:prepared.append(('cfg','cfg',json.dumps({'tochka':bank})))
            snap('pre-restore')
            empty=not db.execute('select 1 from log limit 1').fetchone()
            db.execute('delete from docs');db.execute('delete from bind');db.execute('delete from inv')
            db.executemany('insert into docs values(?,?,?)',prepared);db.executemany('insert or replace into bind values(?,?)',bi)
            if empty:db.executemany('insert into log values(?,?,?,?,?)',lg)
            write_log('restore','Вы','',new={'docs':len(rows),'from':b.get('ts')})
            migrate(commit=False)
            snapshot('owner',set(PERMS))
            db.commit()
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
        s.end_headers()
        if s.command!='HEAD':s.wfile.write(b)
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
                    permissions=admin_permissions(x[1].get('perms') or []) if ro=='admin' else PERMS
                    r=snapshot(ro,set(permissions))
                    if ro=='admin':r['perms']=permissions
                elif p=='/api/log':
                    if ro!='owner':return s.send(403,{})
                    r=[list(q) for q in db.execute('select ts,act,id,old,new from log order by rowid desc limit 300')]
                elif ro=='owner':
                    reminder=backup_status()
                    r={'role':'owner','backup':{'ts':reminder['last']} if reminder['last'] else None,'backup_reminder':reminder,
                       'empty':not db.execute("select 1 from docs where kind in('rent','bike','client') limit 1").fetchone()}
                elif ro=='admin':r={'role':'admin','name':x[1].get('name',''),'perms':admin_permissions(x[1].get('perms') or [])}
                elif ro=='client':
                    c=doc(x);un=UN.get(u,'')
                    if c and c.get('tgu','')!=un:c['tgu']=un;save(x,'client',c);db.commit()
                    r=mine(x)
                else:r={'role':ro,**contact_data(cfg())}
            return s.send(200,r)
        if p.startswith('/api/qr/'):
            u=s.user()
            if u is None:return s.send(403,{})
            with L:
                ro,x=role(u);q=ID.fullmatch(p[8:]) and doc(p[8:])
                if not q or 'payload' not in q:return s.send(404,{})
                r=doc(q['rent']) or {}
                if ro=='client' and r.get('client')!=x:return s.send(403,{})
                if ro=='admin' and 'pay_qr' not in admin_permissions(x[1].get('perms') or []):return s.send(403,{})
                if ro not in('owner','admin','client'):return s.send(403,{})
                out={k:q.get(k) for k in('amt','n','pen','payload','img','status','result','date')}
                if out['status']=='open' and not qr_available(r,q,today()):out=dict(out,status='void',payload=None,img=None)
                return s.send(200,out)
        if p.startswith('/img/'):
            t=time.time()
            for a in [a for a,(e,_) in IMG.items() if e<t]:IMG.pop(a,None)
            x=IMG.get(p[5:])
            if not x:return s.send(404,b'')
            return s.send(200,x[1],'image/png',{'Content-Disposition':'attachment; filename="qr-oplata.png"','Cache-Control':'no-store'})
        if p.startswith('/dl/'):return send_backup(s,p[4:])
        f={'/':'index.html'}.get(p)
        if not f:return s.send(404,b'')
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),'static',f),'rb') as page:
            s.send(200,page.read(),'text/html; charset=utf-8',{'Cache-Control':'no-cache'})
    def do_HEAD(s):
        p=s.path.split('?')[0]
        if p.startswith('/dl/'):return send_backup(s,p[4:],metadata_only=True)
        return s.send(404,b'')
    def do_POST(s):
        p=s.path.split('?')[0]
        try:n=int(s.headers.get('Content-Length',0))
        except (TypeError,ValueError):return s.send(400,{})
        if n<0:return s.send(400,{})
        if p.startswith('/hook/tochka/'):
            # Точка сообщает о входящем платеже. Тело не проверяем, а сразу сверяем статусы открытых QR через API.
            with L:tc=(cfg().get('tochka') or {}).get('hook')
            if not tc or not hmac.compare_digest(p[13:],tc):return s.send(404,b'')
            if n>100000:return s.send(413,{})
            s.rfile.read(n);threading.Thread(target=poll,daemon=True).start();return s.send(200,{})
        u=s.user()
        if u is None:return s.send(403,{})
        if n>(40000000 if p=='/api/restore' and u==OWNER else 200000):return s.send(413,{})
        try:b=json.loads(s.rfile.read(n))
        except Exception:return s.send(400,{})
        if not isinstance(b,dict):return s.send(400,{})
        t=time.time()
        if p=='/api/qrimg':
            # картинка QR, нарисованная в приложении: храним 10 минут, чтобы Telegram мог её скачать
            import base64
            with L:ro_,admin=role(u)
            if ro_ not in('owner','admin','client'):return s.send(403,{})
            if ro_=='admin' and 'pay_qr' not in admin_permissions(admin[1].get('perms') or []):return s.send(403,{})
            try:png=base64.b64decode(str(b.get('png','')).split(',')[-1],validate=True)
            except Exception:return s.send(400,{})
            if not png.startswith(b'\x89PNG') or len(png)>400000:return s.send(400,{})
            if len(IMG)>200:IMG.clear()
            k=secrets.token_urlsafe(16);IMG[k]=(t+600,png);return s.send(200,{'url':'/img/'+k})
        if u==OWNER and p=='/api/dl':
            token=secrets.token_urlsafe(16)
            filename=f'arenda-{now():%Y-%m-%d-%H%M}.json'
            with L:
                prune_download_links(t)
                DL[token]=(t+TTL,filename)
            return s.send(200,{'url':'/dl/'+token,'filename':filename,'expires_in':TTL},x={'Cache-Control':'no-store'})
        if u==OWNER and p=='/api/restore':
            c,r=restore(b);return s.send(c,{'docs':r} if c==200 else {'err':r})
        with L:
            ro,x=role(u)
            try:
                if ro in('owner','admin') and p=='/api/act':
                    P=set(admin_permissions(x[1].get('perms') or [])) if ro=='admin' else set(PERMS)
                    by='Вы' if ro=='owner' else str(x[1].get('name',''))[:40]
                    r=act(str(b.get('a','')),b,ro,P,by);db.commit();return s.send(200,r)
                if ro=='owner' and p=='/api/mockpay' and MOCK:
                    i,q=get('qr',b.get('qr'));on_paid(i,q);db.commit();return s.send(200,{})
                if ro in('owner','admin') and p in('/api/invite','/api/unbind'):
                    c=str(b.get('client',''));adm=c.startswith('adm:')
                    a=ID.fullmatch(c[4:] if adm else c) and row(c[4:] if adm else c)
                    if adm and (ro!='owner' or not a or a[0]!='admin'):return s.send(403,{})
                    if not adm and ((ro=='admin' and 'client_access' not in admin_permissions(x[1].get('perms') or [])) or not a or a[0]!='client'):return s.send(403,{})
                    actor='Вы' if ro=='owner' else str(x[1].get('name',''))[:40]
                    audit_id=c[4:] if adm else c
                    target={'name':a[1].get('name',''),'target':'admin' if adm else 'client'}
                    if p=='/api/invite':
                        if (adm and a[1].get('disabled')) or (not adm and a[1].get('arch')):
                            raise Bad('Сначала восстановите доступ к карточке')
                        if not botname():return s.send(500,{'err':'Бот недоступен'})
                        q=bool(b.get('qr'));k=('qq'+secrets.token_hex(12)) if q else secrets.token_urlsafe(9)
                        db.execute('insert into inv values(?,?,?)',(k,c,t+(600 if q else 7*86400)))
                        write_log('invite',actor,audit_id,new={**target,'method':'qr' if q else 'link'});db.commit()
                        return s.send(200,{'tok':k,'bot':BOT[0]})
                    if adm:a[1]['tg']=None;save(c[4:],'admin',a[1])
                    else:db.execute('delete from bind where client=?',(c,))
                    db.execute('delete from inv where client=?',(c,))
                    write_log('unbind',actor,audit_id,new=target);db.commit();return s.send(200,{})
                if ro=='new' and p=='/api/claim':
                    TRY[u]=[q for q in TRY.get(u,[]) if q>t-3600]
                    if len(TRY[u])>=5:return s.send(429,{})
                    TRY[u].append(t)
                    tk=str(b.get('tok'));i=db.execute('select client from inv where tok=? and exp>?',(tk,t)).fetchone();tgt=i and i[0]
                    isq=tk.startswith('qq') and len(tk)==26;ph=digits(b.get('phone'))
                    if tgt and tgt.startswith('adm:'):
                        a=row(tgt[4:]);c=a and a[0]=='admin' and not a[1].get('disabled') and a[1]
                        if c and (isq or (ph and digits(c.get('phone'))==ph)):
                            c['tg']=u;c['tgu']=UN.get(u,'');save(tgt[4:],'admin',c);db.execute('delete from inv where tok=?',(tk,))
                            write_log('login',c.get('name',''),tgt[4:],new={'name':c.get('name',''),'target':'admin','method':'qr' if isq else 'link'});db.commit()
                            say(f"Администратор {c.get('name')} вошёл в приложение аренды.");return s.send(200,{})
                    else:
                        target=tgt and row(tgt)
                        c=target and target[0]=='client' and target[1]
                        if c and not c.get('arch') and not c.get('purged') and (isq or (ph and digits(c.get('phone'))==ph)):
                            c['tgu']=UN.get(u,'');save(tgt,'client',c)
                            db.execute('insert or replace into bind values(?,?)',(u,tgt));db.execute('delete from inv where tok=?',(tk,))
                            write_log('login','клиент',tgt,new={'name':c.get('name',''),'target':'client','method':'qr' if isq else 'link'});db.commit()
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
        rs=[(i,r,rstate(r,t)) for i,r in kind('rent') if r.get('status')=='active' and not r.get('waiting')]
        if cf['nown'] and once(f'{t}:daily'):
            late=sum(1 for _,_,s in rs if s['late']>0);due=sum(1 for _,_,s in rs if s['left']==0)
            rq=sum(1 for _,x in kind('req') if x.get('status')=='pending')
            out.append((OWNER,f'Сегодня платят: {due}. Просрочено: {late}.'+(f' Заявок на отсрочку: {rq}.' if rq else '')))
        if cf['ncli']:
            for i,r,s in rs:
                if s['left'] in(0,1) and once(f"{t}:rem:{i}:{s['next']}"):
                    amount=term_quote(r,1,on=t)['amt']
                    m=(f"Напоминание: завтра, {dm(s['next'])}, оплата аренды {rub(amount)}." if s['left'] else f"Сегодня день оплаты аренды: {rub(amount)}.")
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
def migrate(commit=True):
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
                a['perms']=admin_permissions(p);a['m2']=1;save(i,'admin',a)
        c=doc('cfg')
        if c is not None and 'grace' not in c:c['grace']=2;c['pen']=5;save('cfg','cfg',c)
        if commit:db.commit()
migrate()
if __name__=='__main__':
    threading.Thread(target=job,daemon=True).start()
    threading.Thread(target=botname,daemon=True).start()
    ThreadingHTTPServer(('',int(os.environ.get('PORT','8080'))),H).serve_forever()

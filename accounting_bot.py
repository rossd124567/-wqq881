"""群组记账纯记账机器人 - 多群独立账本、紧凑账单、撤销与清空、Z0报价，Python 3.10+, python-telegram-bot 21.x."""
import os, re, sqlite3, threading, logging, asyncio, json, urllib.request, urllib.parse, time, ast, operator
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ChatMemberHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
TZ=ZoneInfo('Asia/Shanghai')
DB=os.getenv('ACCOUNTING_DB','accounting.sqlite3')
# Railway 生产环境应将 ACCOUNTING_DB 指向持久卷路径，例如 /data/accounting.sqlite3。
LOCK=threading.RLock()
Q=Decimal('0.01')
def dec(x): return Decimal(str(x))
def money(x): return dec(x).quantize(Q,rounding=ROUND_HALF_UP)
def fmt(x): return f'{money(x):.2f}'
def connect():
    c=sqlite3.connect(DB,timeout=30)
    c.row_factory=sqlite3.Row
    return c

def init():
    with connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS groups(chat_id INTEGER PRIMARY KEY,rate TEXT NOT NULL DEFAULT "6.93",cutover TEXT NOT NULL DEFAULT "00:00",fee TEXT NOT NULL DEFAULT "0",board_id INTEGER)')
        c.execute('CREATE TABLE IF NOT EXISTS operators(chat_id INTEGER,user_id INTEGER,name TEXT,PRIMARY KEY(chat_id,user_id))')
        c.execute('CREATE TABLE IF NOT EXISTS suppliers(chat_id INTEGER,user_id INTEGER,name TEXT,PRIMARY KEY(chat_id,user_id))')
        c.execute('CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER,kind TEXT,amount TEXT,usdt TEXT,rate TEXT,actor_id INTEGER,actor_name TEXT,supplier_id INTEGER,supplier_name TEXT,created_at TEXT,voided INTEGER DEFAULT 0)')
        c.execute('CREATE TABLE IF NOT EXISTS pending(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER,amount TEXT,rate TEXT,actor_id INTEGER,actor_name TEXT,created_at TEXT,prompt_id INTEGER,status TEXT DEFAULT "open")')
        c.execute('CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER,action TEXT,entry_id INTEGER,actor_id INTEGER,actor_name TEXT,details TEXT,created_at TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS confirmations(token TEXT PRIMARY KEY,chat_id INTEGER,actor_id INTEGER,action TEXT,entry_id INTEGER,created_at REAL)')
        c.execute('CREATE TABLE IF NOT EXISTS otc_quotes(chat_id INTEGER PRIMARY KEY,quotes TEXT NOT NULL,updated_at REAL NOT NULL)')
        cols={r['name'] for r in c.execute('PRAGMA table_info(groups)')}
        for k,typ,default in [('fee','TEXT','"0"'),('board_id','INTEGER','NULL'),('otc_markup','TEXT','"0.30"'),('otc_auto','INTEGER','0'),('otc_rank','INTEGER','3'),('otc_updated','REAL','0')]:
            if k not in cols:c.execute(f'ALTER TABLE groups ADD COLUMN {k} {typ} DEFAULT {default}')
        cols={r['name'] for r in c.execute('PRAGMA table_info(entries)')}
        for k,typ in [('usdt','TEXT'),('rate','TEXT'),('supplier_id','INTEGER'),('supplier_name','TEXT')]:
            if k not in cols:c.execute(f'ALTER TABLE entries ADD COLUMN {k} {typ}')

def group(c,cid):
    c.execute('INSERT OR IGNORE INTO groups(chat_id) VALUES(?)',(cid,))
    return c.execute('SELECT * FROM groups WHERE chat_id=?',(cid,)).fetchone()
def bounds(cut,days=0):
    h,m=map(int,cut.split(':')); n=datetime.now(TZ)
    a=n.replace(hour=h,minute=m,second=0,microsecond=0)
    if n<a:a-=timedelta(days=1)
    a+=timedelta(days=days)
    return a.isoformat(),(a+timedelta(days=1)).isoformat()
def keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('昨日账单',callback_data='bill:yesterday'),InlineKeyboardButton('昨日供方人员成绩',callback_data='score:yesterday')],
        [InlineKeyboardButton('今日供方人员成绩',callback_data='score:today'),InlineKeyboardButton('使用说明',callback_data='bill:help')]
    ])
def help_text():
    return ('📒 群组记账机器人\n'
            '+0：重新发送今日账单；+300：人民币入款；+300/8：单笔指定汇率。\n'
            '回复成员消息 +300 直接记账并记录供方；直接 +300 需供方发送 1 确认后入账。\n'
            '下发50：下发50U；下发-50：冲回50U。\n'
            '撤销：撤销本群上一步入款或下发；撤销入款、撤销下发：撤销最近对应记录。\n'
            '撤销入款 订单号 / 撤销下发 订单号：指定记录。\n'
            '清空账单：清空本群当前账期，需二次确认。\n'
            'Z0 或 z0：查询币安OTC商家购买USDT报价（只显示商家与价格）。\n'
            '设置实时汇率：开启自动跟随（每10分钟）；设置汇率7.2：手动固定。\n'
            '设置日切00:00；所有群成员可操作。')

def supplier_scores(c,cid,which='today'):
    g=group(c,cid)
    a,b=bounds(g['cutover'],-1 if which=='yesterday' else 0)
    records=c.execute("""SELECT supplier_id,supplier_name,amount,usdt FROM entries
        WHERE chat_id=? AND kind='IN' AND voided=0 AND created_at>=? AND created_at<?
        AND supplier_id IS NOT NULL""",(cid,a,b)).fetchall()
    scores={}
    for r in records:
        sid=r['supplier_id']
        if sid not in scores:scores[sid]={'name':r['supplier_name'] or str(sid),'count':0,'rmb':dec(0),'usdt':dec(0)}
        item=scores[sid];item['count']+=(1 if dec(r['amount'])>0 else -1);item['rmb']+=dec(r['amount']);item['usdt']+=dec(r['usdt'] or 0)
    title='昨日供方人员成绩' if which=='yesterday' else '今日供方人员成绩'
    lines=[f'🏆 {title}',f'日切时间：{g["cutover"]}','']
    ordered=sorted(scores.values(),key=lambda x:(-x['rmb'],-x['count'],x['name']))
    if not ordered:lines.append('暂无已确认供方的入款记录')
    for i,item in enumerate(ordered,1):
        lines.extend([f'{i}. {item["name"]}',f'净入款笔数：{item["count"]}笔',f'总金额：{fmt(item["rmb"])}元',f'折合：{fmt(item["usdt"])}U',''])
    lines.extend(['────────────────────',f'供方人数：{len(ordered)}人',f'总入款笔数：{sum(x["count"] for x in ordered)}笔',f'总入款金额：{fmt(sum((x["rmb"] for x in ordered),dec(0)))}元'])
    return '\n'.join(lines)[:4000]

def bill(c,cid,which='today'):
    g=group(c,cid); a,b=bounds(g['cutover'],-1 if which=='yesterday' else 0)
    rows=c.execute('SELECT * FROM entries WHERE chat_id=? AND created_at>=? AND created_at<? AND voided=0 ORDER BY id DESC',(cid,a,b)).fetchall()
    ins=[r for r in rows if r['kind']=='IN']; outs=[r for r in rows if r['kind']=='OUT']
    rate=dec(g['rate']); total=sum((dec(r['amount']) for r in ins),dec(0))
    expected=money(total)
    # U 汇总以每笔入款的实际成交汇率为准，不能拿总人民币除以当前默认汇率。
    raw_u=sum([dec(r['usdt']) if r['usdt'] else money(dec(r['amount']) / dec(r['rate'] or g['rate'])) for r in ins], dec(0))
    exp_u=money(raw_u)
    paid=sum((dec(r['amount']) for r in outs),dec(0)); paid_u=sum((dec(r['usdt'] or '0') for r in outs),dec(0))
    lifetime=c.execute('SELECT kind,amount,usdt,rate FROM entries WHERE chat_id=? AND voided=0',(cid,)).fetchall()
    rem=money(sum((dec(r['amount']) if r['kind']=='IN' else -dec(r['amount']) for r in lifetime),dec(0)))
    rem_u=money(sum(((dec(r['usdt']) if r['usdt'] else money(dec(r['amount'])/dec(r['rate'] or g['rate']))) * (1 if r['kind']=='IN' else -1) for r in lifetime),dec(0)))
    title='昨日账单' if which=='yesterday' else '完整账单' if which=='full' else '今日账单'
    lines=[f'📒 {title}  |  日切 {g["cutover"]}',f'已入款({len(ins)}笔)']
    for r in (ins if which=='full' else ins[:3]):
        t=datetime.fromisoformat(r['created_at']).astimezone(TZ).strftime('%H:%M')
        rr=dec(r['rate'] or g['rate']); uu=dec(r['usdt']) if r['usdt'] else money(dec(r['amount'])/rr)
        lines.extend([f'{t} {fmt(r["amount"])}/{rr}={fmt(uu)}U', (f'供: {r["supplier_name"]}  ' if r['supplier_name'] else '')+f'入: {r["actor_name"]}'])
    lines.extend(['────────────────────',f'已下发({len(outs)}笔)'])
    for r in (outs if which=='full' else outs[:3]):
        t=datetime.fromisoformat(r['created_at']).astimezone(TZ).strftime('%H:%M')
        uu=dec(r['usdt'] or '0'); lines.append(f'{t} {fmt(uu)}U({fmt(r["amount"])}) ({r["actor_name"]})')
    lines.extend(['────────────────────',f'总入款金额：{fmt(total)}',f'汇率：{rate}',f'应下发：{fmt(expected)} ({fmt(exp_u)}U)',f'已下发：{fmt(paid)} ({fmt(paid_u)}U)',f'累计未下发：{fmt(rem)} ({fmt(rem_u)}U)'])
    return '\n'.join(lines)[:4000]

async def is_admin(u,ctx):
    if u.effective_chat.type=='private':return False
    member=await ctx.bot.get_chat_member(u.effective_chat.id,u.effective_user.id)
    return member.status in ('creator','administrator')
async def is_operator(u,ctx):
    if await is_admin(u,ctx):return True
    with connect() as c:
        return bool(c.execute('SELECT 1 FROM operators WHERE chat_id=? AND user_id=?',(u.effective_chat.id,u.effective_user.id)).fetchone())
async def board(u,ctx,force_new=False):
    cid=u.effective_chat.id
    with connect() as c:
        g=group(c,cid); content=bill(c,cid); mid=g['board_id']
    if mid and not force_new:
        try:
            await ctx.bot.edit_message_text(cid,mid,content,reply_markup=keyboard());return
        except Exception as e:
            if 'Message is not modified' in str(e):return
    msg=await ctx.bot.send_message(cid,content,reply_markup=keyboard())
    with connect() as c:c.execute('UPDATE groups SET board_id=? WHERE chat_id=?',(msg.message_id,cid))
async def display(u,ctx,which='today'):
    with connect() as c:content=bill(c,u.effective_chat.id,which)
    await u.message.reply_text(content,reply_markup=keyboard())
async def add_entry(cid,kind,amount,rate,user,supplier=None,when=None):
    with LOCK,connect() as c:
        group(c,cid)
        c.execute('INSERT INTO entries(chat_id,kind,amount,usdt,rate,actor_id,actor_name,supplier_id,supplier_name,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(cid,kind,str(money(amount)),str(money(dec(amount)/dec(rate))),str(rate),user['id'],user['name'],supplier['id'] if supplier else None,supplier['name'] if supplier else None,when or datetime.now(TZ).isoformat()))
def who(u):return {'id':u.id,'name':u.full_name}
def calculate_expression(text):
    """仅解析简单算术，不执行用户代码。"""
    if len(text)>100 or not re.fullmatch(r'[0-9\s.+\-*/()]+',text):return None
    if not any(ch in text for ch in '+-*/'):return None
    try:
        tree=ast.parse(text,mode='eval')
        def calc(node,depth=0):
            if depth>12:raise ValueError('表达式过于复杂')
            if isinstance(node,ast.Expression):return calc(node.body,depth+1)
            if isinstance(node,ast.Constant) and type(node.value) in (int,float):
                v=dec(node.value)
                if not v.is_finite() or abs(v)>dec('1e12'):raise ValueError('数值过大')
                return v
            if isinstance(node,ast.UnaryOp) and type(node.op) in (ast.UAdd,ast.USub):
                v=calc(node.operand,depth+1)
                return v if isinstance(node.op,ast.UAdd) else -v
            if isinstance(node,ast.BinOp) and type(node.op) in (ast.Add,ast.Sub,ast.Mult,ast.Div):
                x=calc(node.left,depth+1);y=calc(node.right,depth+1)
                if isinstance(node.op,ast.Add):v=x+y
                elif isinstance(node.op,ast.Sub):v=x-y
                elif isinstance(node.op,ast.Mult):v=x*y
                else:
                    if y==0:raise ZeroDivisionError
                    v=x/y
                if abs(v)>dec('1e12'):raise ValueError('数值过大')
                return v
            raise ValueError('不支持的表达式')
        v=calc(tree)
        result=format(v.quantize(dec('0.000001')).normalize(),'f')
        return f'🧮 计算器\n{text} = {result}'
    except (SyntaxError,ValueError,ArithmeticError,OverflowError):return '⚠️ 算式无效，请检查输入。'

async def record(update,ctx):
    msg=update.message; txt=(msg.text or msg.caption or '').strip(); cid=msg.chat_id
    # +0 只重新发送当天账单，不创建入款、不要求供方认领。
    if txt=='+0':return await display(update,ctx,'today')
    if txt=='1':return
    if re.fullmatch(r'(?i:z0)',txt):return await show_z0(update,ctx)
    m=re.fullmatch(r'([+-])(\d+(?:\.\d{1,2})?)(?:/(\d+(?:\.\d+)?))?',txt)
    outu=re.fullmatch(r'下发\s*([+-]?)(\d+(?:\.\d{1,2})?)\s*[Uu]?',txt)
    if not m and not outu:
        if re.fullmatch(r'[0-9\s.+\-*/()]+',txt) and any(op in txt for op in '+-*/'):
            result=calculate_expression(txt)
            if result:return await msg.reply_text(result)
        return await command_text(update,ctx)
    amount=dec(m.group(2) if m else outu.group(2))
    if not amount.is_finite() or amount<=0 or amount>dec('100000000000'):return await msg.reply_text('金额不正确。')
    with connect() as c:g=group(c,cid);rate=dec(g['rate'])
    # OTC采集失败时继续使用数据库保存的最后有效汇率，不阻止记账。
    if m:
        reply=msg.reply_to_message
        if not reply or not reply.from_user or reply.from_user.is_bot:
            return await msg.reply_text('麻烦回复对应的正确入款码加账')
        if m.group(3) is not None:
            rate=dec(m.group(3))
            if not rate.is_finite() or rate<=0 or rate>dec('1000000'):
                return await msg.reply_text('汇率不正确，请输入例如 +300/8。')
        supplier=who(reply.from_user)
        signed=amount if m.group(1)=='+' else -amount
        await add_entry(cid,'IN',signed,rate,who(msg.from_user),supplier)
        await board(update,ctx)
        return
    # 下发50：未下发减少50U；下发-50：未下发增加50U。
    signed_u= -amount if outu and outu.group(1)=='-' else amount
    rmb=money(signed_u*rate)
    await add_entry(cid,'OUT',rmb,rate,who(msg.from_user))
    await board(update,ctx)

async def command_text(update,ctx):
    msg=update.message;txt=(msg.text or msg.caption or '').strip(); cid=msg.chat_id
    if txt in ('实时汇率','设置实时汇率'):return await show_otc(update,ctx)
    if re.fullmatch(r'撤销(?:入款|下发)?(?:\s+\d+)?',txt):return await request_undo(update,ctx,txt)
    if txt=='清空账单':return await request_clear(update,ctx)
    if txt in ('今日账单','账单','/账单'):return await display(update,ctx)
    if txt in ('昨日账单','/昨日账单'):return await display(update,ctx,'yesterday')
    if txt in ('今日供方人员成绩','昨日供方人员成绩'):
        which='yesterday' if txt.startswith('昨日') else 'today'
        with connect() as c:content=supplier_scores(c,cid,which)
        return await msg.reply_text(content,reply_markup=keyboard())
    if txt in ('使用说明','/start','/帮助'):return await msg.reply_text(help_text())
    if txt=='管理设置':
        with connect() as c:g=group(c,cid)
        return await msg.reply_text(f'⚙️ 管理设置\n汇率：{g["rate"]}\n日切：{g["cutover"]}\n添加操作人：回复成员消息发送 添加操作人\n删除操作人：回复成员消息发送 删除操作人\n操作人名单：发送 操作人名单')
    if txt=='操作人名单':
        with connect() as c:rs=c.execute('SELECT name FROM operators WHERE chat_id=?',(cid,)).fetchall()
        return await msg.reply_text('操作人名单：\n'+('\n'.join(r['name'] for r in rs) or '暂无（群管理员默认有权限）'))
    if txt in ('添加操作人','删除操作人'):
        if not await is_admin(update,ctx):return
        target=msg.reply_to_message.from_user if msg.reply_to_message else None
        if not target or target.is_bot:return await msg.reply_text('请回复要授权或移除的成员消息发送此指令。')
        with LOCK,connect() as c:
            if txt=='添加操作人':c.execute('INSERT INTO operators(chat_id,user_id,name) VALUES(?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET name=excluded.name',(cid,target.id,target.full_name))
            else:c.execute('DELETE FROM operators WHERE chat_id=? AND user_id=?',(cid,target.id))
        return await msg.reply_text(f'✅ {txt}：{target.full_name}')
    m=re.fullmatch(r'设置(汇率|日切)\s*(.+)',txt)
    if m:
        field,value=m.groups()
        if field=='日切':
            if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value):return await msg.reply_text('格式：设置日切 00:00')
            col='cutover'
        else:
            try:v=dec(value)
            except Exception:return await msg.reply_text('请输入有效数字。')
            if not v.is_finite() or (field=='汇率' and v<=0):return await msg.reply_text('数值无效。')
            col='rate';value=str(v)
        with LOCK,connect() as c:
            group(c,cid);c.execute(f'UPDATE groups SET {col}=? WHERE chat_id=?',(value,cid))
            if field=='汇率':c.execute('UPDATE groups SET otc_auto=0 WHERE chat_id=?',(cid,))
        await msg.reply_text(f'✅ {field}已设置：{value}');await board(update,ctx);return

# Binance P2P public quote adapter (GET, no credentials).
# Public endpoints can be rate-limited or unavailable by region; never silently fall back to synthetic prices.
OTC_URL='https://www.binance.com/bapi/c2c/v1/public/c2c/agent/ad-list'
OTC_CACHE={}
def _parse_otc(payload):
    if not isinstance(payload,(dict,list)):raise ValueError('报价响应格式错误')
    root=payload.get('data',payload) if isinstance(payload,dict) else payload
    if isinstance(root,dict):
        records=next((root[k] for k in ('ads','items','list','data','records') if isinstance(root.get(k),list)),None)
    else:records=root
    if not isinstance(records,list):raise ValueError('未识别到商家广告列表')
    offers=[]
    for item in records:
        if not isinstance(item,dict):continue
        ad=item.get('adv') or item.get('advertisement') or item.get('ad') or item
        if not isinstance(ad,dict):continue
        merchant=item.get('advertiser') or item.get('merchant') or {}
        name=(merchant.get('nickName') or merchant.get('name') or merchant.get('merchantName')) if isinstance(merchant,dict) else None
        name=name or ad.get('nickName') or ad.get('merchantName')
        try:price=dec(ad.get('price',ad.get('unitPrice')))
        except Exception:continue
        if price.is_finite() and dec('1')<price<dec('100') and name:
            offers.append((str(name)[:40],price))
    if not offers:raise ValueError('没有有效的商家名称与报价')
    return sorted(offers,key=lambda x:x[1])[:5]
def fetch_otc():
    params=urllib.parse.urlencode({'fiat':'CNY','asset':'USDT','tradeType':'BUY','limit':10,'order':'asc'})
    req=urllib.request.Request(OTC_URL+'?'+params,headers={'Accept':'application/json','User-Agent':'Mozilla/5.0 XuanHeAccountingBot/8.0'})
    with urllib.request.urlopen(req,timeout=10) as response:
        if response.status!=200:raise ValueError(f'报价服务HTTP {response.status}')
        raw=response.read(300001)
        if len(raw)>300000:raise ValueError('报价响应过大')
    offers=_parse_otc(json.loads(raw))
    if len(offers)<3:raise ValueError('有效商家不足3家，保留上次汇率')
    return offers
def otc_markup(offers,markup):
    lines=['💱 Binance USDT/CNY OTC购买报价','商家卖出USDT价格（元/U）','']
    for i,(name,price) in enumerate(offers[:5],1):lines.append(f'{i}. {name}  {fmt(price)}')
    idx=min(2,len(offers)-1)
    lines+=['',f'参考：第{idx+1}家 {fmt(offers[idx][1])}',f'加价：{"+" if markup>=0 else ""}{fmt(markup)}',f'确认汇率：{fmt(offers[idx][1]+markup)}','报价仅供参考；确认后才更新默认汇率。']
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton('−0.10',callback_data='otc:adj:-0.10'),InlineKeyboardButton('+0.10',callback_data='otc:adj:0.10')],
        [InlineKeyboardButton('−0.01',callback_data='otc:adj:-0.01'),InlineKeyboardButton('+0.01',callback_data='otc:adj:0.01')],
        [InlineKeyboardButton('🔄 刷新报价',callback_data='otc:refresh'),InlineKeyboardButton('✅ 确认设置汇率',callback_data='otc:confirm')]])
    return '\n'.join(lines),kb
async def show_otc(update,ctx):
    cid=update.effective_chat.id
    try:offers=await asyncio.to_thread(fetch_otc)
    except Exception as e:
        with connect() as c:g=group(c,cid)
        last=datetime.fromtimestamp(float(g['otc_updated']),TZ).strftime('%m-%d %H:%M:%S') if g['otc_updated'] else '尚无成功记录'
        return await update.effective_message.reply_text(f'⚠️ 币安OTC报价暂时获取失败：{str(e)[:120]}\n继续使用最后有效汇率：{g["rate"]}\n最后采集成功：{last}\n自动跟随状态：{"开启" if g["otc_auto"] else "关闭"}；不影响正常记账。')
    with LOCK,connect() as c:
        g=group(c,cid);markup=dec(g['otc_markup'])
        c.execute('INSERT INTO otc_quotes(chat_id,quotes,updated_at) VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET quotes=excluded.quotes,updated_at=excluded.updated_at', (cid,json.dumps([(name,str(price)) for name,price in offers],ensure_ascii=False),time.time()))
    OTC_CACHE[cid]={'offers':offers,'at':time.monotonic(),'markup':markup}
    content,kb=otc_markup(offers,markup)
    await update.effective_message.reply_text(content,reply_markup=kb)
async def show_z0(update,ctx):
    """只显示商家名称与价格。失败时仅使用本群上次成功获取的缓存报价。"""
    cid=update.effective_chat.id
    try:
        offers=await asyncio.to_thread(fetch_otc)
        now=time.time()
        with LOCK,connect() as c:
            group(c,cid)
            c.execute('INSERT INTO otc_quotes(chat_id,quotes,updated_at) VALUES(?,?,?) '
                      'ON CONFLICT(chat_id) DO UPDATE SET quotes=excluded.quotes,updated_at=excluded.updated_at',
                      (cid,json.dumps([(name,str(price)) for name,price in offers],ensure_ascii=False),now))
        stale=False
    except Exception as exc:
        logging.warning('Z0报价获取失败 chat=%s: %s',cid,exc)
        with connect() as c:
            saved=c.execute('SELECT * FROM otc_quotes WHERE chat_id=?',(cid,)).fetchone()
        if not saved:
            return await update.effective_message.reply_text('⚠️ 商家报价获取失败，暂无上次成功采集的商家价格。')
        offers=[(name,dec(price)) for name,price in json.loads(saved['quotes'])]
        now=saved['updated_at'];stale=True
    lines=['📊 Binance OTC购买USDT商家报价']
    if stale:lines.append('⚠️ 缓存报价（非实时）  '+datetime.fromtimestamp(now,TZ).strftime('%m-%d %H:%M'))
    for idx,(name,price) in enumerate(offers[:5],1):
        lines.append(f'{idx}. {name}  {fmt(price)}')
    await update.effective_message.reply_text('\n'.join(lines))

async def request_undo(update,ctx,txt):
    cid=update.effective_chat.id
    m=re.fullmatch(r'撤销(入款|下发)?(?:\s+(\d+))?',txt)
    kind={'入款':'IN','下发':'OUT'}.get(m.group(1))
    entry_id=int(m.group(2)) if m.group(2) else None
    with connect() as c:
        g=group(c,cid)
        sql='SELECT * FROM entries WHERE chat_id=? AND voided=0'
        args=[cid]
        if kind:sql+=' AND kind=?';args.append(kind)
        if entry_id is not None:sql+=' AND id=?';args.append(entry_id)
        else:
            a,b=bounds(g['cutover']);sql+=' AND created_at>=? AND created_at<?';args.extend((a,b))
        sql+=' ORDER BY id DESC LIMIT 1'
        entry=c.execute(sql,args).fetchone()
    if not entry:
        return await update.message.reply_text('没有找到可撤销的记录。指定订单号可撤销历史记录。')
    import secrets
    token=secrets.token_hex(12)
    with LOCK,connect() as c:
        c.execute('INSERT INTO confirmations VALUES(?,?,?,?,?,?)',(token,cid,update.effective_user.id,'undo',entry['id'],time.time()))
    typ='入款' if entry['kind']=='IN' else '下发'
    details=(f'金额：{fmt(entry["amount"])}元 / {fmt(entry["usdt"] or 0)}U\n'
             f'原操作人：{entry["actor_name"]}\n订单号：{entry["id"]}')
    await update.message.reply_text(f'↩️ 确认撤销{typ}\n{details}',reply_markup=confirm_keyboard(token))

async def request_clear(update,ctx):
    cid=update.effective_chat.id
    with connect() as c:
        g=group(c,cid);a,b=bounds(g['cutover'])
        count=c.execute('SELECT COUNT(*) FROM entries WHERE chat_id=? AND voided=0 AND created_at>=? AND created_at<?',(cid,a,b)).fetchone()[0]
    import secrets
    token=secrets.token_hex(12)
    with LOCK,connect() as c:
        c.execute('INSERT INTO confirmations VALUES(?,?,?,?,?,?)',(token,cid,update.effective_user.id,'clear',None,time.time()))
    await update.message.reply_text(f'🗑️ 确认清空本群当前账单？\n当前有效记录：{count}笔\n只清空本群本账期，保留历史与汇率设置；操作将保留审计记录。',reply_markup=confirm_keyboard(token))

def confirm_keyboard(token):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('取消',callback_data=f'confirm:cancel:{token}'),
        InlineKeyboardButton('确认执行',callback_data=f'confirm:apply:{token}')]])

async def confirm_callback(update,ctx):
    q=update.callback_query
    _,mode,token=q.data.split(':',2)
    cid=q.message.chat_id
    if mode=='cancel':
        with LOCK,connect() as c:
            owner=c.execute('SELECT actor_id FROM confirmations WHERE token=? AND chat_id=?',(token,cid)).fetchone()
            if owner and owner['actor_id']!=q.from_user.id:
                return await q.answer('请由发起操作的成员取消',show_alert=True)
            c.execute('DELETE FROM confirmations WHERE token=? AND chat_id=?',(token,cid))
        await q.answer('已取消')
        return await q.edit_message_text('已取消操作。')
    if mode!='apply':return await q.answer('无效操作',show_alert=True)
    with LOCK,connect() as c:
        conf=c.execute('SELECT * FROM confirmations WHERE token=? AND chat_id=?',(token,cid)).fetchone()
        if not conf:return await q.answer('已处理或已过期',show_alert=True)
        if time.time()-conf['created_at']>300:
            c.execute('DELETE FROM confirmations WHERE token=?',(token,))
            return await q.answer('确认已过期，请重新发送指令',show_alert=True)
        if conf['actor_id']!=q.from_user.id:
            return await q.answer('请由发起操作的成员确认',show_alert=True)
        c.execute('DELETE FROM confirmations WHERE token=?',(token,))
        now=datetime.now(TZ).isoformat()
        if conf['action']=='undo':
            entry=c.execute('SELECT * FROM entries WHERE id=? AND chat_id=? AND voided=0',(conf['entry_id'],cid)).fetchone()
            if not entry:return await q.answer('记录已撤销',show_alert=True)
            c.execute('UPDATE entries SET voided=1 WHERE id=? AND chat_id=? AND voided=0',(entry['id'],cid))
            c.execute('INSERT INTO audit(chat_id,action,entry_id,actor_id,actor_name,details,created_at) VALUES(?,?,?,?,?,?,?)',
                      (cid,'UNDO',entry['id'],q.from_user.id,q.from_user.full_name,entry['kind'],now))
            result=f'✅ 已撤销{ "入款" if entry["kind"]=="IN" else "下发" }订单 #{entry["id"]}'
        else:
            g=group(c,cid);a,b=bounds(g['cutover'])
            ids=[r['id'] for r in c.execute('SELECT id FROM entries WHERE chat_id=? AND voided=0 AND created_at>=? AND created_at<?',(cid,a,b))]
            c.execute('UPDATE entries SET voided=1 WHERE chat_id=? AND voided=0 AND created_at>=? AND created_at<?',(cid,a,b))
            c.execute('UPDATE pending SET status="cancelled" WHERE chat_id=? AND status="open"',(cid,))
            c.execute('INSERT INTO audit(chat_id,action,entry_id,actor_id,actor_name,details,created_at) VALUES(?,?,?,?,?,?,?)',
                      (cid,'CLEAR',None,q.from_user.id,q.from_user.full_name,json.dumps(ids),now))
            result=f'✅ 已清空本群当前账单，共{len(ids)}笔；历史记录已保留。'
    await q.answer('操作成功')
    await q.edit_message_text(result)
    await board(update,ctx)

async def otc_callback(update,ctx):
    q=update.callback_query;cid=q.message.chat_id;data=q.data
    if data=='otc:refresh':
        await q.answer();return await show_otc(update,ctx)
    state=OTC_CACHE.get(cid)
    if not state or time.monotonic()-state['at']>60:
        return await q.answer('报价已过期，请发送 实时汇率 重新获取。',show_alert=True)
    if data.startswith('otc:adj:'):
        state['markup']=max(dec('-10'),min(dec('10'),state['markup']+dec(data.split(':')[-1])))
        content,kb=otc_markup(state['offers'],state['markup'])
        await q.answer();return await q.edit_message_text(content,reply_markup=kb)
    if data=='otc:confirm':
        idx=min(2,len(state['offers'])-1)
        rate=money(state['offers'][idx][1]+state['markup'])
        if rate<=0:return await q.answer('汇率无效',show_alert=True)
        with LOCK,connect() as c:
            group(c,cid)
            c.execute('UPDATE groups SET rate=?,otc_markup=?,otc_auto=1,otc_rank=?,otc_updated=? WHERE chat_id=?',(str(rate),str(state['markup']),idx+1,time.time(),cid))
        OTC_CACHE.pop(cid,None)
        await q.answer('汇率设置成功')
        await q.edit_message_text(f'✅ 自动跟随汇率已开启\n当前汇率：{fmt(rate)}\n参考币安第{idx+1}家商户购买报价，已加价{fmt(state["markup"])}\n每10分钟自动采集；失败时继续使用最后有效汇率，恢复后自动跟随。')
        await board(update,ctx)

async def callback(update,ctx):
    q=update.callback_query;data=q.data;cid=q.message.chat_id
    if data.startswith('otc:'):return await otc_callback(update,ctx)
    if data.startswith('confirm:'):return await confirm_callback(update,ctx)
    if data.startswith('bill:'):
        kind=data.split(':')[1]
        await q.answer()
        if kind=='help':return await q.message.reply_text(help_text())
        with connect() as c:content=bill(c,cid,'yesterday' if kind=='yesterday' else 'today')
        await q.message.reply_text(content,reply_markup=keyboard())
        return
    if data.startswith('score:'):
        kind=data.split(':')[1]
        await q.answer()
        with connect() as c:content=supplier_scores(c,cid,kind)
        await q.message.reply_text(content,reply_markup=keyboard())
        return
    if data.startswith(('select:','pick:')):
        return await q.answer('旧版供方认领功能已停用',show_alert=True)


async def private_home(update,ctx):
    await update.effective_message.reply_text(
        '🤖 群组记账 · 专业群组记账系统\n\n'
        '本机器人提供独立群组记账服务。\n'
        '请将机器人添加到群组，即可开始记账。\n\n'
        + help_text())

async def group_gate(update,ctx):
    return await record(update,ctx)

async def group_slash(update,ctx):
    return await slash(update,ctx)

async def group_callback(update,ctx):
    return await callback(update,ctx)

async def new_members(update,ctx):
    """新群加入机器人时，自动建立独立账本（不共享其他群的数据）。"""
    msg=update.effective_message
    if not msg or not msg.new_chat_members or not update.effective_chat:return
    me=await ctx.bot.get_me()
    if not any(user.id==me.id for user in msg.new_chat_members):return
    cid=update.effective_chat.id
    with LOCK,connect() as c:group(c,cid)
    logging.info('已初始化新群账本 chat_id=%s',cid)
    await msg.reply_text('🤖 群组记账机器人已加入本群。\n无需开通会员，可直接发送 +100 开始记账。\n回复成员消息发送 +100，可同时记录供方和入方。')

async def migrate_group(update,ctx):
    """基础群升级为超级群时迁移账本及配置，避免看起来像新群空账。"""
    msg=update.effective_message
    if not msg or not msg.migrate_to_chat_id:return
    old_id=update.effective_chat.id
    new_id=msg.migrate_to_chat_id
    with LOCK,connect() as c:
        # 同一个群升级为超级群，保留旧群数据并将所有关联记录迁移到新ID。
        existing=c.execute('SELECT 1 FROM groups WHERE chat_id=?',(new_id,)).fetchone()
        if existing:
            logging.warning('群迁移目标已有账本，拒绝覆盖 old=%s new=%s',old_id,new_id)
            return
        old=c.execute('SELECT 1 FROM groups WHERE chat_id=?',(old_id,)).fetchone()
        if not old:group(c,new_id);return
        c.execute('UPDATE groups SET chat_id=?,board_id=NULL WHERE chat_id=?',(new_id,old_id))
        for table in ('entries','pending','suppliers','operators','audit','confirmations','otc_quotes'):
            c.execute(f'UPDATE {table} SET chat_id=? WHERE chat_id=?',(new_id,old_id))
    OTC_CACHE.pop(old_id,None)
    logging.info('群账本迁移成功 old=%s new=%s',old_id,new_id)

async def slash(update,ctx):
    cmd=update.message.text.split()[0].split('@')[0]
    if cmd in ('/start','/help'):return await update.message.reply_text(help_text())
    if cmd=='/report':return await display(update,ctx)
    if cmd=='/details':return await display(update,ctx,'full')
    if cmd=='/undo':
        if not await is_admin(update,ctx):return
        if len(ctx.args)!=1 or not ctx.args[0].isdigit():return await update.message.reply_text('用法：/undo 编号')
        with LOCK,connect() as c:cur=c.execute('UPDATE entries SET voided=1 WHERE chat_id=? AND id=? AND voided=0',(update.effective_chat.id,int(ctx.args[0])))
        await update.message.reply_text('已撤销。' if cur.rowcount else '记录不存在。');await board(update,ctx)
    if cmd=='/clear_today':
        if not await is_admin(update,ctx):return
        if ctx.args!=['确认']:return await update.message.reply_text('确认清空今日，请发送 /clear_today 确认（保留历史审计记录）。')
        with LOCK,connect() as c:
            g=group(c,update.effective_chat.id);a,b=bounds(g['cutover'])
            c.execute('UPDATE entries SET voided=1 WHERE chat_id=? AND created_at>=? AND created_at<?',(update.effective_chat.id,a,b))
        await board(update,ctx)

async def otc_auto_refresh(app):
    # 每600秒采集；失败时保留最后有效汇率及更新时间，不阻止记账。
    while True:
        await asyncio.sleep(600)
        with connect() as c:
            active=[dict(r) for r in c.execute('SELECT chat_id,otc_markup,otc_rank FROM groups WHERE otc_auto=1')]
        if not active:continue
        try:offers=await asyncio.to_thread(fetch_otc)
        except Exception as e:
            logging.warning('OTC实时汇率更新失败，保留旧汇率: %s',e)
            continue
        if len(offers)<3:
            logging.warning('OTC有效商家不足3家，跳过更新')
            continue
        now=time.time()
        logging.info('OTC报价获取成功，有效商家=%s，更新群数=%s',len(offers),len(active))
        for g in active:
            rank=int(g['otc_rank'] or 3)
            if not 1<=rank<=len(offers):continue
            rate=money(offers[rank-1][1]+dec(g['otc_markup']))
            if rate<=0:continue
            with LOCK,connect() as c:
                c.execute('UPDATE groups SET rate=?,otc_updated=? WHERE chat_id=? AND otc_auto=1',(str(rate),now,g['chat_id']))
                c.execute('INSERT INTO otc_quotes(chat_id,quotes,updated_at) VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET quotes=excluded.quotes,updated_at=excluded.updated_at', (g['chat_id'],json.dumps([(name,str(price)) for name,price in offers],ensure_ascii=False),now))

async def midnight_refresh(app):
    while True:
        now=datetime.now(TZ)
        next_day=(now+timedelta(days=1)).replace(hour=0,minute=0,second=2,microsecond=0)
        await asyncio.sleep(max(1,(next_day-now).total_seconds()))
        with connect() as c:
            groups=c.execute('SELECT chat_id,board_id FROM groups WHERE board_id IS NOT NULL AND cutover="00:00"').fetchall()
            pages=[(g['chat_id'],g['board_id'],bill(c,g['chat_id'])) for g in groups]
        for cid,mid,content in pages:
            try:await app.bot.edit_message_text(cid,mid,content,reply_markup=keyboard())
            except Exception:logging.exception('日切页面更新失败 chat=%s',cid)

async def after_init(app):
    app.create_task(midnight_refresh(app))
    app.create_task(otc_auto_refresh(app))

def main():
    token=os.getenv('BOT_TOKEN')
    if not token:raise RuntimeError('请在 Railway 设置 BOT_TOKEN')
    init();app=Application.builder().token(token).post_init(after_init).build()
    app.add_handler(CommandHandler('start',private_home,filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler('help',private_home,filters.ChatType.PRIVATE))
    for name in ('start','help','report','details','undo','clear_today'):app.add_handler(CommandHandler(name,group_slash,filters.ChatType.GROUPS))
    app.add_handler(CallbackQueryHandler(group_callback))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, migrate_group))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS & filters.ChatType.GROUPS, new_members))
    # 文字与带说明文字的图片/文件统一交由 record 解析；不读取图片内的文字。
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND | filters.CAPTION) & filters.ChatType.GROUPS,group_gate))
    app.run_polling(drop_pending_updates=True)
if __name__=='__main__':main()

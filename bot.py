import asyncio
import logging
from html import escape
import os
import time
from contextlib import suppress
from datetime import datetime, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, RPCError

load_dotenv()
TOKEN = os.environ.get('BOT_TOKEN', '')
OWNER = int(os.environ.get('OWNER_ID', '0'))
DB_PATH = os.environ.get('DB_PATH', 'autosend.db')
API_ID = int(os.environ.get('API_ID', '0'))
API_HASH = os.environ.get('API_HASH', '')
TELETHON_SESSION = os.environ.get('TELETHON_SESSION', '')
user_client = None
db_conn = None
panel_messages = {}  # user id -> last panel message id
group_entities = {}  # cached input entities
DEFAULT_INTERVAL = 300
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 10080
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('autosend')
dp = Dispatcher()

class Form(StatesGroup):
    text = State()
    admin = State()
    interval = State()


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=c) for t,c in row] for row in rows])

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY, title TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS campaigns(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL, text TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0, next_at INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS targets(campaign_id INTEGER NOT NULL, group_id INTEGER NOT NULL, PRIMARY KEY(campaign_id,group_id));
        CREATE TABLE IF NOT EXISTS approved_groups(group_id INTEGER PRIMARY KEY, approved_by INTEGER NOT NULL, approved_at INTEGER NOT NULL);
        ''')
        # Migrate existing databases without losing campaigns or selected groups.
        columns = [row[1] for row in await (await db.execute('PRAGMA table_info(campaigns)')).fetchall()]
        if 'interval_seconds' not in columns:
            await db.execute('ALTER TABLE campaigns ADD COLUMN interval_seconds INTEGER NOT NULL DEFAULT 300')
        await db.commit()

async def sql(query, params=(), fetch='all'):
    # One shared SQLite connection avoids opening a new connection for every button.
    async with db_conn.execute(query, params) as cur:
        result = await cur.fetchall() if fetch == 'all' else await cur.fetchone() if fetch == 'one' else None
    if fetch == 'none':
        await db_conn.commit()
    return result

async def show_panel(target, text, reply_markup=None, parse_mode=None):
    """Keep one bot panel message per user, editing it instead of stacking messages."""
    if isinstance(target, CallbackQuery) or isinstance(target, CallbackProxy):
        c = target
        try:
            await c.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            panel_messages[c.from_user.id] = c.message.message_id
            return
        except TelegramBadRequest as exc:
            if 'message is not modified' in str(exc).lower():
                return
            # If editing is impossible (old/deleted message), send a new panel.
        target = c.message
        uid = c.from_user.id
    else:
        uid = target.from_user.id
    old = panel_messages.get(uid)
    if old:
        with suppress(TelegramAPIError):
            await target.bot.delete_message(target.chat.id, old)
    sent = await target.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    panel_messages[uid] = sent.message_id

async def clean_input(m):
    # User's message is no longer needed after the bot has processed it.
    with suppress(TelegramAPIError):
        await m.delete()

async def prompt(m, text):
    await show_panel(m, text)

async def authorized(user_id):
    if user_id == OWNER:
        return True
    return bool(await sql('SELECT 1 FROM admins WHERE id=?', (user_id,), 'one'))

async def guard(event):
    user = event.from_user
    if not user or not await authorized(user.id):
        if isinstance(event, CallbackQuery):
            await event.answer('Нет доступа. Обратитесь к владельцу.', show_alert=True)
        else:
            await event.answer('⛔ Доступ только для администраторов.')
        return False
    return True

async def home(message):
    rows = [ [('📣 Мои рассылки','campaigns')], [('➕ Создать рассылку','new')], [('👥 Группы','groups'),('📊 Статистика','stats')] ]
    chat = message.message.chat if isinstance(message, (CallbackQuery, CallbackProxy)) else message.chat
    if chat.type != ChatType.PRIVATE:
        return
    if message.from_user.id == OWNER:
        rows.append([('🛡 Администраторы','admins')])
    await show_panel(message, '✨ <b>AutoSend • Панель управления</b>\n\n⏱ Интервал задаётся отдельно для каждой рассылки\n🔐 Публикации только в подключённые группы\n\nВыберите действие:', reply_markup=kb(*rows), parse_mode=ParseMode.HTML)

@dp.message(CommandStart())
async def start(m: Message):
    if await guard(m):
        await clean_input(m)
        await home(m)

@dp.message(Command('id'))
async def show_id(m: Message):
    await show_panel(m, f'Ваш Telegram ID: <code>{m.from_user.id}</code>', parse_mode=ParseMode.HTML)

@dp.message(Command('panel'))
async def panel(m: Message):
    if await guard(m):
        await clean_input(m)
        await home(m)

@dp.callback_query(F.data == 'home')
async def go_home(c: CallbackQuery):
    if await guard(c):
        await c.answer()
        await home(c)

@dp.callback_query(F.data == 'new')
async def new(c: CallbackQuery, state: FSMContext):
    if not await guard(c): return
    await state.clear()
    await state.set_state(Form.text)
    await show_panel(c, '✍️ Отправьте текст новой рассылки. Можно использовать эмодзи и переносы строк.\n\n/cancel — отменить')
    await c.answer()

@dp.message(Command('cancel'))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await clean_input(m)
    await home(m)

@dp.message(Form.text, F.text)
async def save_text(m: Message, state: FSMContext):
    if not await guard(m): return
    await clean_input(m)
    if len(m.text)>4000:
        await show_panel(m, 'Максимум 4000 символов.'); return
    data=await state.get_data()
    if data.get('edit_id'):
        cid=int(data['edit_id'])
        await sql('UPDATE campaigns SET text=? WHERE id=? AND owner_id=?',(m.text,cid,m.from_user.id),'none')
        await state.clear()
        await home(m)
    else:
        await state.update_data(new_text=m.text)
        await state.set_state(Form.interval)
        await show_panel(m, '⏱ Через сколько минут повторять рассылку?\n\nВведите целое число от 1 до 10080 (например, 10, 30 или 60).\n/cancel — отменить')

@dp.message(Form.interval, F.text)
async def save_interval(m: Message, state: FSMContext):
    if not await guard(m): return
    await clean_input(m)
    value = m.text.strip()
    if not value.isdecimal() or not MIN_INTERVAL_MINUTES <= int(value) <= MAX_INTERVAL_MINUTES:
        await show_panel(m, 'Введите целое число минут от 1 до 10080. Например: 15')
        return
    minutes = int(value)
    data = await state.get_data()
    if data.get('interval_edit_id'):
        cid = int(data['interval_edit_id'])
        await sql('UPDATE campaigns SET interval_seconds=?, next_at=CASE WHEN active=1 THEN ? ELSE next_at END WHERE id=? AND owner_id=?',
                  (minutes * 60, int(time.time()) + minutes * 60, cid, m.from_user.id), 'none')
        await show_panel(m, f'✅ Интервал рассылки #{cid} изменён на {minutes} мин. Следующая отправка пересчитана.')
    else:
        text = data.get('new_text')
        if not text:
            await state.clear()
            await show_panel(m, 'Не найден текст рассылки. Начните заново: /panel')
            return
        await sql('INSERT INTO campaigns(owner_id,text,interval_seconds) VALUES(?,?,?)',
                  (m.from_user.id, text, minutes * 60), 'none')
        await show_panel(m, f'✅ Рассылка создана с интервалом {minutes} мин. Выберите группы и нажмите «Запустить».')
    await state.clear()
    await home(m)

@dp.message(Form.interval)
async def interval_nontext(m: Message):
    if await guard(m):
        await show_panel(m, 'Отправьте количество минут обычным текстом, например: 30')

@dp.callback_query(F.data == 'campaigns')
async def campaigns(c: CallbackQuery):
    if not await guard(c): return
    rows=await sql('SELECT id,active,sent FROM campaigns WHERE owner_id=? ORDER BY id DESC LIMIT 40',(c.from_user.id,))
    buttons=[[(f'{"🟢" if r["active"] else "⚪"} #{r["id"]} · отправлено {r["sent"]}',f'camp:{r["id"]}')] for r in rows]
    buttons.append([('⬅️ Назад','home')])
    await show_panel(c, '📣 <b>Мои рассылки</b>',parse_mode=ParseMode.HTML,reply_markup=kb(*buttons))
    await c.answer()

async def owned(cid,uid):
    return await sql('SELECT * FROM campaigns WHERE id=? AND owner_id=?',(cid,uid),'one')

@dp.callback_query(F.data.startswith('camp:'))
async def campaign_detail(c: CallbackQuery):
    if not await guard(c): return
    cid=int(c.data.split(':')[1]); r=await owned(cid,c.from_user.id)
    if not r: await c.answer('Нет доступа',show_alert=True); return
    targets=await sql('SELECT g.title FROM targets t JOIN groups g ON g.id=t.group_id WHERE t.campaign_id=?',(cid,))
    names=', '.join(escape(x['title']) for x in targets) or 'не выбраны'
    preview = escape(r['text'][:1500])
    await show_panel(c, f'📣 <b>Рассылка #{cid}</b>\nСтатус: {"🟢 работает" if r["active"] else "⏸ остановлена"}\nГруппы: {names[:1200]}\n⏱ Интервал: {r["interval_seconds"] // 60} мин.\nОтправлено: {r["sent"]}\n\n<b>Текст:</b>\n{preview}', parse_mode=ParseMode.HTML,
        reply_markup=kb([('👥 Выбрать группы',f'targets:{cid}')],[('✏️ Изменить текст',f'edit:{cid}')],[('⏱ Изменить интервал',f'interval:{cid}')],[('▶️ Запустить' if not r['active'] else '⏸ Остановить',f'toggle:{cid}')],[('🗑 Удалить',f'delete:{cid}')],[('⬅️ К рассылкам','campaigns')]))
    await c.answer()

@dp.callback_query(F.data.startswith('edit:'))
async def edit(c: CallbackQuery,state:FSMContext):
    if not await guard(c): return
    cid=int(c.data.split(':')[1]); r=await owned(cid,c.from_user.id)
    if not r: await c.answer('Нет доступа',show_alert=True); return
    await state.update_data(edit_id=cid)
    await state.set_state(Form.text)
    await show_panel(c, '✏️ Отправьте новый текст. /cancel для отмены.')
    await c.answer()

@dp.callback_query(F.data.startswith('interval:'))
async def edit_interval(c: CallbackQuery, state: FSMContext):
    if not await guard(c): return
    cid = int(c.data.split(':')[1])
    r = await owned(cid, c.from_user.id)
    if not r:
        await c.answer('Нет доступа', show_alert=True)
        return
    await state.clear()
    await state.update_data(interval_edit_id=cid)
    await state.set_state(Form.interval)
    await show_panel(c, f'⏱ Текущий интервал: {r["interval_seconds"] // 60} мин.\nВведите новый интервал в минутах (1–10080).\n/cancel — отменить')
    await c.answer()

PAGE_SIZE = 12

@dp.callback_query(F.data.startswith('targets:'))
async def targets(c: CallbackQuery):
    if not await guard(c): return
    parts = c.data.split(':')
    cid = int(parts[1])
    page = max(0, int(parts[2])) if len(parts) > 2 else 0
    if not await owned(cid, c.from_user.id):
        await c.answer('Нет доступа', show_alert=True)
        return
    count = await sql('SELECT COUNT(*) AS n FROM groups g JOIN approved_groups a ON a.group_id=g.id WHERE g.enabled=1', fetch='one')
    total = count['n']
    selected_count = await sql('SELECT COUNT(*) AS n FROM targets WHERE campaign_id=?', (cid,), 'one')
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages - 1)
    visible = await sql('SELECT g.* FROM groups g JOIN approved_groups a ON a.group_id=g.id WHERE g.enabled=1 ORDER BY g.title LIMIT ? OFFSET ?', (PAGE_SIZE, page * PAGE_SIZE))
    selected = {r['group_id'] for r in await sql('SELECT group_id FROM targets WHERE campaign_id=? AND group_id IN (' + ','.join('?' for _ in visible) + ')', (cid, *(g['id'] for g in visible)))} if visible else set()
    buttons = [[(('✅ ' if g['id'] in selected else '⬜ ') + g['title'][:35], f'pick:{cid}:{g["id"]}:{page}')] for g in visible]
    nav = []
    if page > 0: nav.append(('⬅️ Назад', f'targets:{cid}:{page-1}'))
    nav.append((f'📄 {page+1}/{total_pages}', f'pageinfo:{page+1}'))
    if page + 1 < total_pages: nav.append(('Вперёд ➡️', f'targets:{cid}:{page+1}'))
    buttons.append(nav)
    buttons.append([('⬅️ К рассылке', f'camp:{cid}')])
    await show_panel(c, f'👥 Выберите группы для рассылки #{cid}\nСтраница {page+1} из {total_pages} · Всего доступно: {total}\nВыбрано: {selected_count["n"]}\n\nНажмите на группу, чтобы включить или убрать её.', reply_markup=kb(*buttons))
    await c.answer()

@dp.callback_query(F.data.startswith('pick:'))
async def pick(c: CallbackQuery):
    if not await guard(c): return
    parts = c.data.split(':')
    cid, gid = int(parts[1]), int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0
    if not await owned(cid, c.from_user.id):
        await c.answer('Нет доступа', show_alert=True)
        return
    allowed = await sql('SELECT 1 FROM groups g JOIN approved_groups a ON a.group_id=g.id WHERE g.id=? AND g.enabled=1', (gid,), 'one')
    if not allowed:
        await c.answer('Группа не разрешена', show_alert=True)
        return
    exists = await sql('SELECT 1 FROM targets WHERE campaign_id=? AND group_id=?', (cid, gid), 'one')
    if exists:
        await sql('DELETE FROM targets WHERE campaign_id=? AND group_id=?', (cid, gid), 'none')
    else:
        await sql('INSERT OR IGNORE INTO targets VALUES(?,?)', (cid, gid), 'none')
    await c.answer('Группа убрана' if exists else 'Группа добавлена')
    await targets(CallbackProxy(c, f'targets:{cid}:{page}'))

@dp.callback_query(F.data.startswith('pageinfo:'))
async def pageinfo(c: CallbackQuery):
    await c.answer('Используйте стрелки для переключения страниц')

class CallbackProxy:
    def __init__(self,c,data): self.from_user=c.from_user; self.message=c.message; self.data=data; self._c=c
    async def answer(self,*args,**kwargs): pass

@dp.callback_query(F.data.startswith('toggle:'))
async def toggle(c:CallbackQuery):
    if not await guard(c): return
    cid=int(c.data.split(':')[1]); r=await owned(cid,c.from_user.id)
    if not r: await c.answer('Нет доступа',show_alert=True); return
    if not r['active']:
        cnt=await sql('SELECT COUNT(*) AS n FROM targets t JOIN groups g ON g.id=t.group_id JOIN approved_groups a ON a.group_id=g.id WHERE t.campaign_id=? AND g.enabled=1',(cid,),'one')
        if not cnt['n']: await c.answer('Сначала выберите группы',show_alert=True); return
    await sql('UPDATE campaigns SET active=?,next_at=? WHERE id=?',(0 if r['active'] else 1,int(time.time())+r['interval_seconds'],cid),'none')
    await c.answer('Остановлено' if r['active'] else f'Запущено: первая отправка через {r["interval_seconds"] // 60} мин.')
    await campaign_detail(CallbackProxy(c, f'camp:{cid}'))

@dp.callback_query(F.data.startswith('delete:'))
async def delete(c:CallbackQuery):
    if not await guard(c): return
    cid=int(c.data.split(':')[1]); r=await owned(cid,c.from_user.id)
    if not r: await c.answer('Нет доступа',show_alert=True); return
    await sql('DELETE FROM targets WHERE campaign_id=?',(cid,),'none')
    await sql('DELETE FROM campaigns WHERE id=?',(cid,),'none')
    await c.answer('Удалено')
    await campaigns(CallbackProxy(c, 'campaigns'))

async def personal_group_entity(group_id):
    entity = group_entities.get(group_id)
    if entity is not None:
        return entity
    try:
        entity = await user_client.get_input_entity(group_id)
    except (ValueError, TypeError):
        async for dialog in user_client.iter_dialogs():
            if dialog.id == group_id and dialog.is_group:
                entity = dialog.input_entity
                break
        else:
            raise ValueError('Group not found in personal account dialogs')
    group_entities[group_id] = entity
    return entity

async def refresh_personal_groups():
    """Discover groups from the signed-in user's dialogs, never the Bot API."""
    found = []
    async for dialog in user_client.iter_dialogs():
        if dialog.is_group:
            found.append((dialog.id, dialog.name or str(dialog.id)))
    group_entities.clear()
    # Only mark previously discovered groups unavailable after a successful scan.
    await db_conn.execute('UPDATE groups SET enabled=0')
    await db_conn.executemany(
        'INSERT INTO groups(id,title,enabled) VALUES(?,?,1) '
        'ON CONFLICT(id) DO UPDATE SET title=excluded.title,enabled=1', found)
    await db_conn.commit()
    return len(found)


@dp.callback_query(F.data == 'groups')
@dp.callback_query(F.data.startswith('groups:'))
async def groups(c: CallbackQuery):
    if not await guard(c): return
    parts = c.data.split(':')
    page = max(0, int(parts[1])) if len(parts) > 1 else 0
    # Scan dialogs on initial opening or explicit refresh, not every page turn.
    if c.from_user.id == OWNER and ((len(parts) > 2 and parts[2] == 'refresh') or not await sql('SELECT 1 FROM groups LIMIT 1', fetch='one')):
        try:
            await refresh_personal_groups()
        except (RPCError, OSError, ValueError) as exc:
            log.warning('Cannot refresh personal dialogs: %s', type(exc).__name__)
            await c.answer('Не удалось обновить группы Telegram. Проверьте логи.', show_alert=True)
            return
    where = 'WHERE g.enabled=1' if c.from_user.id == OWNER else 'WHERE g.enabled=1 AND a.group_id IS NOT NULL'
    count = await sql(f'SELECT COUNT(*) AS n FROM groups g LEFT JOIN approved_groups a ON a.group_id=g.id {where}', fetch='one')
    total = count['n']
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages - 1)
    visible = await sql(f"""SELECT g.*, CASE WHEN a.group_id IS NULL THEN 0 ELSE 1 END approved
                        FROM groups g LEFT JOIN approved_groups a ON a.group_id=g.id
                        {where} ORDER BY g.title LIMIT ? OFFSET ?""", (PAGE_SIZE, page * PAGE_SIZE))
    lines, buttons = [], []
    for r in visible:
        status = '✅ разрешена' if r['approved'] else '⛔ не разрешена'
        lines.append(f"{escape(r['title'])} — {status}")
        if c.from_user.id == OWNER:
            action = 'revoke' if r['approved'] else 'approve'
            label = '🚫 Запретить' if r['approved'] else '✅ Разрешить'
            buttons.append([(f"{label}: {r['title'][:25]}", f"{action}:{r['id']}:{page}")])
    nav = []
    if page > 0: nav.append(('⬅️ Назад', f'groups:{page-1}'))
    nav.append((f'📄 {page+1}/{total_pages}', f'pageinfo:{page+1}'))
    if page + 1 < total_pages: nav.append(('Вперёд ➡️', f'groups:{page+1}'))
    buttons.append(nav)
    if c.from_user.id == OWNER:
        buttons.append([('🔄 Обновить список', f'groups:{page}:refresh')])
    buttons.append([('🏠 В меню', 'home')])
    await show_panel(c, 
        f'👥 <b>Группы личного аккаунта</b>\nСтраница {page+1} из {total_pages} · Всего: {total}\n\n' +
        ('\n'.join(lines) or 'Доступных групп пока нет.') +
        '\n\nВладелец разрешает только группы, где согласована публикация сообщений.',
        parse_mode=ParseMode.HTML, reply_markup=kb(*buttons))
    await c.answer()

@dp.callback_query(F.data.startswith(('approve:', 'revoke:')))
async def group_approval(c: CallbackQuery):
    if c.from_user.id != OWNER:
        await c.answer('Только владелец может разрешать группы', show_alert=True)
        return
    parts = c.data.split(':')
    action, gid = parts[0], int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
    group = await sql('SELECT * FROM groups WHERE id=? AND enabled=1', (gid,), 'one')
    if not group:
        await c.answer('Группа недоступна в личном аккаунте', show_alert=True)
        return
    if action == 'approve':
        try:
            entity = await personal_group_entity(gid)
            permissions = await user_client.get_permissions(entity, 'me')
            if not getattr(permissions, 'is_admin', False) and getattr(permissions, 'send_messages', True) is False:
                await c.answer('У аккаунта нет права писать в группу', show_alert=True)
                return
        except (ValueError, RPCError) as exc:
            log.warning('Cannot access group %s from personal account: %s', gid, type(exc).__name__)
            await c.answer('Личный аккаунт не имеет доступа к группе.', show_alert=True)
            return
        await sql('INSERT OR REPLACE INTO approved_groups VALUES(?,?,?)', (gid, OWNER, int(time.time())), 'none')
        await c.answer('Группа разрешена', show_alert=True)
    else:
        await sql('DELETE FROM approved_groups WHERE group_id=?', (gid,), 'none')
        await sql('DELETE FROM targets WHERE group_id=?', (gid,), 'none')
        await c.answer('Разрешение отозвано', show_alert=True)
    await groups(CallbackProxy(c, f'groups:{page}'))

@dp.callback_query(F.data == 'stats')
async def stats(c:CallbackQuery):
    if not await guard(c): return
    r=await sql('SELECT COUNT(*) n,COALESCE(SUM(sent),0) sent,COALESCE(SUM(active),0) active FROM campaigns WHERE owner_id=?',(c.from_user.id,),'one')
    await show_panel(c, f'📊 <b>Статистика</b>\nРассылок: {r["n"]}\nАктивных: {r["active"]}\nУспешных отправок: {r["sent"]}',parse_mode=ParseMode.HTML)
    await c.answer()

@dp.callback_query(F.data == 'admins')
async def admins(c:CallbackQuery):
    if c.from_user.id!=OWNER: await c.answer('Только владелец',show_alert=True); return
    rows=await sql('SELECT id FROM admins ORDER BY id')
    buttons=[[(f'❌ Удалить {r["id"]}',f'unadmin:{r["id"]}')] for r in rows]
    buttons.append([('➕ Добавить админа','addadmin')])
    await show_panel(c, '🛡 <b>Администраторы</b>\nАдмины управляют только своими рассылками.\n\nПопросите пользователя написать /id.',parse_mode=ParseMode.HTML,reply_markup=kb(*buttons))
    await c.answer()

@dp.callback_query(F.data == 'addadmin')
async def addadmin(c:CallbackQuery,state:FSMContext):
    if c.from_user.id!=OWNER: await c.answer('Нет доступа',show_alert=True); return
    await state.set_state(Form.admin)
    await show_panel(c, 'Отправьте числовой Telegram ID пользователя. /cancel для отмены.')
    await c.answer()

@dp.message(Form.admin,F.text)
async def save_admin(m:Message,state:FSMContext):
    if m.from_user.id!=OWNER: return
    await clean_input(m)
    try: uid=int(m.text.strip()); assert uid>0
    except (ValueError,AssertionError): await show_panel(m, 'Введите корректный числовой ID.'); return
    await sql('INSERT OR IGNORE INTO admins VALUES(?)',(uid,),'none')
    await state.clear()
    await show_panel(m, f'✅ Администратор {uid} добавлен. Он должен открыть бота и нажать /start.')

@dp.callback_query(F.data.startswith('unadmin:'))
async def unadmin(c:CallbackQuery):
    if c.from_user.id!=OWNER: await c.answer('Нет доступа',show_alert=True); return
    uid=int(c.data.split(':')[1]); await sql('DELETE FROM admins WHERE id=?',(uid,),'none')
    await sql('UPDATE campaigns SET active=0 WHERE owner_id=?',(uid,),'none')
    await c.answer('Доступ отозван; рассылки остановлены.',show_alert=True)

async def scheduler():
    while True:
        try:
            now = int(time.time())
            due = await sql('SELECT * FROM campaigns WHERE active=1 AND next_at<=?', (now,))
            for camp in due:
                cid = camp['id']
                await sql('UPDATE campaigns SET next_at=? WHERE id=? AND active=1', (now + camp['interval_seconds'], cid), 'none')
                if not await authorized(camp['owner_id']):
                    await sql('UPDATE campaigns SET active=0 WHERE id=?', (cid,), 'none')
                    continue
                destinations = await sql("SELECT g.id FROM groups g JOIN targets t ON g.id=t.group_id JOIN approved_groups a ON a.group_id=g.id WHERE t.campaign_id=? AND g.enabled=1", (cid,))
                for g in destinations:
                    current = await sql('SELECT active FROM campaigns WHERE id=?', (cid,), 'one')
                    if not current or not current['active']:
                        break
                    try:
                        # Only groups explicitly approved by the owner can be sent to.
                        entity = await personal_group_entity(g['id'])
                        await user_client.send_message(entity, camp['text'])
                        await sql('UPDATE campaigns SET sent=sent+1 WHERE id=?', (cid,), 'none')
                    except FloodWaitError as e:
                        log.warning('Telegram flood wait for group %s: %s seconds', g['id'], e.seconds)
                        await sql('UPDATE campaigns SET active=0 WHERE id=?', (cid,), 'none')
                        break
                    except (RPCError, ValueError) as e:
                        log.warning('Personal account cannot send to group %s: %s', g['id'], type(e).__name__)
                    await asyncio.sleep(1)
        except Exception:
            log.exception('Scheduler error')
        await asyncio.sleep(5)

async def main():
    global user_client, db_conn
    if not TOKEN or not OWNER:
        raise RuntimeError('Set BOT_TOKEN and OWNER_ID environment variables')
    if not API_ID or not API_HASH or not TELETHON_SESSION:
        raise RuntimeError('Set API_ID, API_HASH and TELETHON_SESSION environment variables')
    await init_db()
    db_conn = await aiosqlite.connect(DB_PATH)
    db_conn.row_factory = aiosqlite.Row
    await db_conn.execute('PRAGMA busy_timeout=5000')
    await sql('UPDATE campaigns SET active=0', fetch='none')
    user_client = TelegramClient(StringSession(TELETHON_SESSION), API_ID, API_HASH)
    await user_client.connect()
    if not await user_client.is_user_authorized():
        await user_client.disconnect()
        raise RuntimeError('TELETHON_SESSION is invalid or expired. Re-authorize locally.')
    me = await user_client.get_me()
    log.info('Personal Telegram account connected (id=%s)', me.id)
    bot = Bot(TOKEN)
    task = asyncio.create_task(scheduler())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await user_client.disconnect()
        await bot.session.close()
        await db_conn.close()

if __name__=='__main__':
    asyncio.run(main())
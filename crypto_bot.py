import asyncio
import logging
import aiohttp
import aiosqlite
import io
import time
import re
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, BufferedInputFile
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram_stars import StarsPayments, MemoryStorage

# --- НАСТРОЙКИ ---
BOT_TOKEN = "9258912205:AAGKWfYKoHX6MsZ31aRriubl-WGziS2xNf4"
ADMIN_ID = 5765808907

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

class UserStates(StatesGroup):
    waiting_for_conv_amount = State()
    waiting_for_admin_ban_target = State()
    waiting_for_admin_ban_time = State()
    waiting_for_admin_gift_target = State()
    waiting_for_admin_gift_time = State()

COINS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "BNB": "BNBUSDT", "SOL": "SOLUSDT",
    "XRP": "XRPUSDT", "TON": "TONUSDT", "ADA": "ADAUSDT", "AVAX": "AVAXUSDT",
    "LINK": "LINKUSDT", "DOT": "DOTUSDT", "TRX": "TRXUSDT", "MATIC": "MATICUSDT",
    "NEAR": "NEARUSDT", "ATOM": "ATOMUSDT", "LTC": "LTCUSDT"
}

previous_prices = {}
current_exchange_rates = {"KZT": 450.0, "RUB": 90.0}
user_daily_requests = {}

def parse_duration(text: str):
    text = text.lower().strip()
    if "навсегда" in text or "forever" in text: return 9999999999, "навсегда"
    match = re.match(r'(\d+)\s*(сек|мин|час|день|дн|недел|месяц|год|лет|л)', text)
    if not match: return None, None
    amount, unit = int(match.group(1)), match.group(2)
    if unit in ["сек"]: return amount, f"{amount} сек."
    if unit in ["мин"]: return amount * 60, f"{amount} мин."
    if unit in ["час"]: return amount * 3600, f"{amount} ч."
    if unit in ["день", "дн"]: return amount * 86400, f"{amount} дн."
    if unit in ["недел"]: return amount * 604800, f"{amount} нед."
    if unit in ["месяц"]: return amount * 2592000, f"{amount} мес."
    if unit in ["год", "лет", "л"]: return amount * 31536000, f"{amount} г."
    return None, None

async def init_db():
    async with aiosqlite.connect("crypto_bot.db") as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, is_vip INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0, ban_until INTEGER DEFAULT 0, vip_until INTEGER DEFAULT 0
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS user_alerts (
            user_id INTEGER, coin TEXT, PRIMARY KEY (user_id, coin)
        )""")
        for coin in COINS.keys():
            await db.execute("INSERT OR IGNORE INTO user_alerts (user_id, coin) VALUES (?, ?)", (ADMIN_ID, coin))
        await db.commit()

async def get_user(user_id, username=None):
    async with aiosqlite.connect("crypto_bot.db") as db:
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            user = await cursor.fetchone()
            if not user:
                await db.execute("INSERT INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
                await db.commit()
                return {"user_id": user_id, "is_vip": 0, "is_banned": 0, "ban_until": 0, "vip_until": 0}
            return {"user_id": user[0], "username": user[1], "is_vip": user[2], "is_banned": user[3], "ban_until": user[4], "vip_until": user[5]}

async def check_vip(user_id):
    user = await get_user(user_id)
    if user["is_vip"] and user["vip_until"] < int(time.time()) and user["vip_until"] != 9999999999:
        async with aiosqlite.connect("crypto_bot.db") as db:
            await db.execute("UPDATE users SET is_vip = 0, vip_until = 0 WHERE user_id = ?", (user_id,))
            await db.commit()
        return False
    return bool(user["is_vip"]) or user_id == ADMIN_ID

async def check_ban(user_id):
    user = await get_user(user_id)
    if user["is_banned"]:
        if user["ban_until"] != 9999999999 and user["ban_until"] < int(time.time()):
            async with aiosqlite.connect("crypto_bot.db") as db:
                await db.execute("UPDATE users SET is_banned = 0, ban_until = 0 WHERE user_id = ?", (user_id,))
                await db.commit()
            return False, 0
        return True, user["ban_until"]
    return False, 0

async def get_binance_price(pair):
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={pair}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    return float(data.get("price", 0))
    except: pass
    return 0.0

async def update_exchange_rates():
    global current_exchange_rates
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    current_exchange_rates["KZT"] = data.get("rates", {}).get("KZT", 450.0)
                    current_exchange_rates["RUB"] = data.get("rates", {}).get("RUB", 90.0)
    except: pass

def format_price(price):
    if price >= 100: return f"{price:,.2f}"
    if price >= 1: return f"{price:,.3f}"
    return f"{price:,.5f}"

def get_price_string(usd_price):
    return f"{format_price(usd_price)} USD | {usd_price * current_exchange_rates['KZT']:,.0f} KZT | {usd_price * current_exchange_rates['RUB']:,.0f} RUB"

async def generate_chart(coin, pair):
    url = f"https://api.binance.com/api/v3/klines?symbol={pair}&interval=1h&limit=24"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                if response.status != 200: return None
                data = await response.json()
    except: return None
    times = [datetime.fromtimestamp(int(c[0]) / 1000) for c in data]
    closes = [float(c[4]) for c in data]
    plt.figure(figsize=(10, 5))
    plt.plot(times, closes, marker='o', linestyle='-', color='#f2a900', linewidth=2, markersize=3)
    plt.title(f'{coin} / USDT (24 часа)', fontsize=14, color='white')
    plt.grid(True, linestyle='--', alpha=0.3)
    ax = plt.gca()
    ax.set_facecolor('#1e1e2e')
    plt.gcf().set_facecolor('#1e1e2e')
    ax.tick_params(colors='white')
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    plt.xticks(rotation=45)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100, facecolor='#1e1e2e')
    plt.close()
    buf.seek(0)
    return buf

async def on_paid(payment):
    payload = payment.payload
    user_id = payment.user_id
    if payload == "vip_14d": vip_until, msg = int(time.time()) + (14 * 86400), "14 дней"
    elif payload == "vip_30d": vip_until, msg = int(time.time()) + (30 * 86400), "30 дней"
    elif payload == "vip_forever": vip_until, msg = 9999999999, "НАВСЕГДА"
    else: return
    async with aiosqlite.connect("crypto_bot.db") as db:
        await db.execute("UPDATE users SET is_vip = 1, vip_until = ? WHERE user_id = ?", (vip_until, user_id))
        await db.commit()
    await bot.send_message(user_id, f"💎 <b>Оплата получена!</b>\nПремиум активирован на {msg}.")
    await bot.send_message(ADMIN_ID, f"💰 <b>Покупка Премиума!</b>\nПользователь: {user_id}\nСумма: {payment.amount} Stars")

stars = StarsPayments(MemoryStorage(), secret="super_secret_key_1234567890", on_paid=on_paid)

def get_main_menu(is_vip=False):
    kb = [
        [InlineKeyboardButton(text="📊 Курсы", callback_data="menu_rates"), InlineKeyboardButton(text="🔥 Топ 24ч", callback_data="menu_top")],
        [InlineKeyboardButton(text="📅 Топ недели", callback_data="menu_week"), InlineKeyboardButton(text="🔔 Уведомления", callback_data="menu_alerts")],
        [InlineKeyboardButton(text="💱 Конвертер", callback_data="menu_converter"), InlineKeyboardButton(text="👤 Мой профиль", callback_data="menu_profile")]
    ]
    if not is_vip: kb.append([InlineKeyboardButton(text="💎 Купить Премиум", callback_data="buy_vip_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_rates_keyboard():
    buttons = []
    coins = list(COINS.keys())
    for i in range(0, len(coins), 3):
        row = [InlineKeyboardButton(text=f"{c}", callback_data=f"rate_{c}") for c in coins[i:i+3]]
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="menu_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="menu_back")]])

def get_admin_panel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Выдать Премиум", callback_data="admin_gift")],
        [InlineKeyboardButton(text="🚫 Забанить/Разбанить", callback_data="admin_ban_menu")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")]
    ])

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await init_db()
    await get_user(message.from_user.id, message.from_user.username)
    is_banned, until = await check_ban(message.from_user.id)
    if is_banned:
        if until == 9999999999: return await message.answer("🚫 <b>Вы забанены навсегда!</b>")
        else: return await message.answer(f"🚫 <b>Вы забанены!</b>\nДо: {datetime.fromtimestamp(until).strftime('%Y-%m-%d %H:%M')}")
    await message.answer("👋 Привет! Выбери действие:", reply_markup=get_main_menu(await check_vip(message.from_user.id)))

@dp.callback_query(F.data == "menu_back")
async def back_to_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    is_banned, _ = await check_ban(callback.from_user.id)
    if is_banned: return await callback.answer("🚫 Вы забанены!", show_alert=True)
    await callback.message.edit_text("Главное меню:", reply_markup=get_main_menu(await check_vip(callback.from_user.id)))
    await callback.answer()

@dp.callback_query(F.data == "menu_profile")
async def menu_profile(callback: types.CallbackQuery):
    is_banned, _ = await check_ban(callback.from_user.id)
    if is_banned: return await callback.answer("🚫 Вы забанены!", show_alert=True)
    user = await get_user(callback.from_user.id)
    text = f"👤 <b>Мой профиль</b>\n\n🆔 ID: <code>{callback.from_user.id}</code>\n"
    if callback.from_user.id == ADMIN_ID: text += "👑 Статус: <b>Администратор</b>\n"
    elif user["is_vip"]:
        if user["vip_until"] == 9999999999: text += "💎 Статус: <b>Премиум НАВСЕГДА</b> ♾️\n"
        else: text += f"💎 Статус: <b>Премиум</b> (осталось {max(0, (user['vip_until'] - int(time.time())) // 86400)} дн.)\n"
    else: text += "🆓 Статус: <b>Бесплатный</b> (лимит 3 запроса)\n"
    await callback.message.edit_text(text, reply_markup=get_back_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "buy_vip_menu")
async def buy_vip_menu(callback: types.CallbackQuery):
    is_banned, _ = await check_ban(callback.from_user.id)
    if is_banned: return await callback.answer("🚫 Вы забанены!", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 14 дней — 239 Stars", callback_data="pay_vip_14d")],
        [InlineKeyboardButton(text="🚀 1 месяц — 329 Stars", callback_data="pay_vip_30d")],
        [InlineKeyboardButton(text="♾️ Навсегда — 899 Stars", callback_data="pay_vip_forever")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_back")]
    ])
    await callback.message.edit_text("💎 <b>Выберите тариф Премиум:</b>", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("pay_vip_"))
async def process_vip_payment(callback: types.CallbackQuery):
    is_banned, _ = await check_ban(callback.from_user.id)
    if is_banned: return await callback.answer("🚫 Вы забанены!", show_alert=True)
    plan = callback.data.split("_")[2]
    titles = {"14d": ("Премиум на 14 дней", 200), "30d": ("Премиум на 30 дней", 329), "forever": ("Премиум Навсегда", 999)}
    if plan not in titles: return
    title, amount = titles[plan]
    await bot.send_invoice(chat_id=callback.message.chat.id, title=title, description="Доступ без лимитов.", payload=f"vip_{plan}", provider_token="", currency="XTR", prices=[LabeledPrice(label=title, amount=amount)])
    await callback.answer()

async def check_limit(user_id):
    if await check_vip(user_id) or user_id == ADMIN_ID: return True
    current_day = datetime.now().strftime("%Y-%m-%d")
    if user_id in user_daily_requests and user_daily_requests[user_id].get("date") != current_day: user_daily_requests[user_id] = {"date": current_day, "count": 0}
    elif user_id not in user_daily_requests: user_daily_requests[user_id] = {"date": current_day, "count": 0}
    if user_daily_requests[user_id]["count"] >= 3: return False
    user_daily_requests[user_id]["count"] += 1
    return True

@dp.callback_query(F.data == "menu_rates")
async def menu_rates(callback: types.CallbackQuery):
    is_banned, _ = await check_ban(callback.from_user.id)
    if is_banned: return await callback.answer("🚫 Вы забанены!", show_alert=True)
    await callback.message.edit_text("Выбери монету:", reply_markup=get_rates_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("rate_"))
async def show_rate(callback: types.CallbackQuery):
    is_banned, _ = await check_ban(callback.from_user.id)
    if is_banned: return await callback.answer("🚫 Вы забанены!", show_alert=True)
    if not await check_limit(callback.from_user.id): return await callback.answer("❌ Лимит исчерпан!", show_alert=True)
    coin, pair = callback.data.split("_")[1], COINS.get(callback.data.split("_")[1])
    await callback.message.edit_text("🔄 <i>Загружаю...</i>")
    price = await get_binance_price(pair)
    if price <= 0: return await callback.message.edit_text("❌ Ошибка цены.", reply_markup=get_rates_keyboard())
    is_vip = await check_vip(callback.from_user.id)
    caption = f"📊 <b>Курс {coin}</b>\n\n1 {coin} = <b>{get_price_string(price)}</b>"
    if is_vip:
        chart_buf = await generate_chart(coin, pair)
        if chart_buf:
            photo = BufferedInputFile(chart_buf.getvalue(), filename=f"{coin}.png")
            await callback.message.answer_photo(photo=photo, caption=caption + "\n\n📈 <i>График доступен с Премиум-статусом</i>", reply_markup=get_rates_keyboard())
            try: await callback.message.delete()
            except: pass
        else: await callback.message.edit_text(caption + "\n\n❌ Ошибка графика.", reply_markup=get_rates_keyboard())
    else:
        caption += "\n\n🔒 <i>График доступен с Премиум-статусом.</i>"
        await callback.message.edit_text(caption, reply_markup=get_rates_keyboard())
    await callback.answer()

@dp.callback_query(F.data.in_({"menu_top", "menu_week"}))
async def menu_trends(callback: types.CallbackQuery):
    is_banned, _ = await check_ban(callback.from_user.id)
    if is_banned: return await callback.answer("🚫 Вы забанены!", show_alert=True)
    if not await check_vip(callback.from_user.id): return await callback.answer("❌ Доступно с Премиум-статусом!", show_alert=True)
    if not await check_limit(callback.from_user.id): return await callback.answer("❌ Лимит исчерпан.", show_alert=True)
    is_week = callback.data == "menu_week"
    await callback.message.edit_text(f"🔄 <i>Анализирую {'неделю' if is_week else '24 часа'}...</i>")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.binance.com/api/v3/ticker/24hr", timeout=15) as response:
                data = await response.json()
    except: return await callback.message.edit_text("❌ Ошибка API.", reply_markup=get_main_menu(True))
    filtered = [item for item in data if item['symbol'].endswith('USDT') and item['symbol'].replace('USDT', '') in COINS]
    top = sorted(filtered, key=lambda x: float(x['priceChangePercent']), reverse=True)[:5]
    text = f"{'📅' if is_week else '🏆'} <b>Топ-5 монет по росту:</b>\n\n"
    for i, c in enumerate(top, 1):
        text += f"{i}. <b>{c['symbol'].replace('USDT', '')}</b>: {get_price_string(float(c['lastPrice']))}\n📈 Рост: <b>+{float(c['priceChangePercent']):.2f}%</b>\n\n"
    await callback.message.edit_text(text, reply_markup=get_main_menu(True))
    await callback.answer()

@dp.callback_query(F.data == "menu_alerts")
async def menu_alerts(callback: types.CallbackQuery):
    is_banned, _ = await check_ban(callback.from_user.id)
    if is_banned: return await callback.answer("🚫 Вы забанены!", show_alert=True)
    if not await check_vip(callback.from_user.id): return await callback.answer("❌ Доступно с Премиум-статусом!", show_alert=True)
    user_id = callback.from_user.id
    async with aiosqlite.connect("crypto_bot.db") as db:
        async with db.execute("SELECT coin FROM user_alerts WHERE user_id = ?", (user_id,)) as cursor:
            active = [row[0] for row in await cursor.fetchall()]
    buttons = []
    coins = list(COINS.keys())
    for i in range(0, len(coins), 3):
        row = []
        for c in coins[i:i+3]:
            row.append(InlineKeyboardButton(text=f"{'✅' if c in active else '❌'} {c}", callback_data=f"toggle_alert_{c}"))
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="menu_back")])
    await callback.message.edit_text("🔔 <b>Уведомления</b> (Премиум)", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(F.data.startswith("toggle_alert_"))
async def toggle_alert(callback: types.CallbackQuery):
    is_banned, _ = await check_ban(callback.from_user.id)
    if is_banned: return await callback.answer("🚫 Вы забанены!", show_alert=True)
    if not await check_vip(callback.from_user.id): return await callback.answer("❌ Доступно только с Премиум-статусом!", show_alert=True)
    coin, user_id = callback.data.split("_")[2], callback.from_user.id
    async with aiosqlite.connect("crypto_bot.db") as db:
        async with db.execute("SELECT 1 FROM user_alerts WHERE user_id = ? AND coin = ?", (user_id, coin)) as cursor:
            exists = await cursor.fetchone()
        if exists: await db.execute("DELETE FROM user_alerts WHERE user_id = ? AND coin = ?", (user_id, coin))
        else: await db.execute("INSERT INTO user_alerts (user_id, coin) VALUES (?, ?)", (user_id, coin))
        await db.commit()
    await menu_alerts(callback)

@dp.callback_query(F.data == "menu_converter")
async def menu_converter(callback: types.CallbackQuery):
    is_banned, _ = await check_ban(callback.from_user.id)
    if is_banned: return await callback.answer("🚫 Вы забанены!", show_alert=True)
    buttons = []
    coins = list(COINS.keys())
    for i in range(0, len(coins), 3):
        row = [InlineKeyboardButton(text=f"Из {c}", callback_data=f"conv_from_{c}") for c in coins[i:i+3]]
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="menu_back")])
    await callback.message.edit_text("💱 <b>Конвертер</b>\n\nЧто конвертируем?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(F.data.startswith("conv_from_"))
async def conv_choose_from(callback: types.CallbackQuery):
    is_banned, _ = await check_ban(callback.from_user.id)
    if is_banned: return await callback.answer("🚫 Вы забанены!", show_alert=True)
    coin = callback.data.split("_")[2]
    buttons = [
        [InlineKeyboardButton(text="В USD 🇺🇸", callback_data=f"conv_to_{coin}_USD"), InlineKeyboardButton(text="В KZT 🇰🇿", callback_data=f"conv_to_{coin}_KZT")],
        [InlineKeyboardButton(text="В RUB 🇷🇺", callback_data=f"conv_to_{coin}_RUB")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_converter")]
    ]
    await callback.message.edit_text(f"Выбери валюту для {coin}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(F.data.startswith("conv_to_"))
async def conv_choose_to(callback: types.CallbackQuery, state: FSMContext):
    is_banned, _ = await check_ban(callback.from_user.id)
    if is_banned: return await callback.answer("🚫 Вы забанены!", show_alert=True)
    parts = callback.data.split("_")
    await state.update_data(conv_coin=parts[2], conv_cur=parts[3])
    await state.set_state(UserStates.waiting_for_conv_amount)
    await callback.message.edit_text(f"Сколько {parts[2]} перевести в {parts[3]}? (Напиши число)", reply_markup=get_back_keyboard())
    await callback.answer()

@dp.message(UserStates.waiting_for_conv_amount)
async def conv_amount_entered(message: types.Message, state: FSMContext):
    is_banned, _ = await check_ban(message.from_user.id)
    if is_banned: return await message.answer("🚫 Вы забанены!")
    try: amount = float(message.text.replace(",", "."))
    except ValueError: return await message.answer("❌ Введи число.")
    data = await state.get_data()
    coin, target_cur = data.get("conv_coin"), data.get("conv_cur")
    await state.clear()
    usd_price = await get_binance_price(COINS[coin])
    total_usd = usd_price * amount
    if target_cur == "USD": result = f"{total_usd:,.2f} USD"
    elif target_cur == "KZT": result = f"{total_usd * current_exchange_rates['KZT']:,.0f} KZT"
    elif target_cur == "RUB": result = f"{total_usd * current_exchange_rates['RUB']:,.0f} RUB"
    await message.answer(f"💱 <b>Результат:</b>\n{amount} {coin} = <b>{result}</b>", reply_markup=get_main_menu(await check_vip(message.from_user.id)))

@dp.message(Command("admin"))
async def admin_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("🔧 <b>Админ-панель</b>", reply_markup=get_admin_panel())

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    async with aiosqlite.connect("crypto_bot.db") as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c1: total = (await c1.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_vip = 1") as c2: vips = (await c2.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1") as c3: bans = (await c3.fetchone())[0]
    await callback.message.edit_text(f"📊 <b>Статистика</b>\n\n👥 Всего: {total}\n💎 Премиум: {vips}\n🚫 Забанено: {bans}", reply_markup=get_admin_panel())
    await callback.answer()

# --- ВЫДАЧА ПРЕМИУМА (РУЧНОЙ ВВОД ВРЕМЕНИ) ---
@dp.callback_query(F.data == "admin_gift")
async def admin_gift(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(UserStates.waiting_for_admin_gift_target)
    await callback.message.edit_text("Введи <b>ID</b>, <b>@юзернейм</b> или <b>номер</b> пользователя:", reply_markup=get_back_keyboard())
    await callback.answer()

@dp.message(UserStates.waiting_for_admin_gift_target)
async def admin_gift_target_entered(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    target, found_id, found_name = message.text.strip(), None, ""
    async with aiosqlite.connect("crypto_bot.db") as db:
        if target.startswith("@"):
            target = target[1:]
            async with db.execute("SELECT user_id, username FROM users WHERE username = ?", (target,)) as cursor:
                user = await cursor.fetchone()
                if user: found_id, found_name = user[0], f"@{user[1]}"
        elif target.startswith("+") or target.isdigit():
            try:
                chat = await bot.get_chat(target)
                found_id, found_name = chat.id, chat.first_name or "Без имени"
            except:
                if target.isdigit():
                    async with db.execute("SELECT user_id, username FROM users WHERE user_id = ?", (int(target),)) as cursor:
                        user = await cursor.fetchone()
                        if user: found_id, found_name = user[0], f"@{user[1]}"
        else:
            async with db.execute("SELECT user_id, username FROM users WHERE user_id = ? OR username = ?", (target, target)) as cursor:
                user = await cursor.fetchone()
                if user: found_id, found_name = user[0], f"@{user[1]}"
    if not found_id: return await message.answer("❌ Пользователь не найден.")
    await state.update_data(target_id=found_id)
    await state.set_state(UserStates.waiting_for_admin_gift_time)
    await message.answer(f"✅ Найден: {found_name} (<code>{found_id}</code>)\n\nНапиши срок Премиума (например: <i>1 день, 2 недели, 1 месяц, навсегда</i>):")

@dp.message(UserStates.waiting_for_admin_gift_time)
async def admin_gift_time_entered(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    seconds, text_fmt = parse_duration(message.text)
    if not seconds: return await message.answer("❌ Не понял срок. Напиши понятнее (например: 1 день, 1 неделя, навсегда):")
    data = await state.get_data()
    target_id = data.get("target_id")
    vip_until = 9999999999 if seconds == 9999999999 else int(time.time()) + seconds
    async with aiosqlite.connect("crypto_bot.db") as db:
        await db.execute("UPDATE users SET is_vip = 1, vip_until = ? WHERE user_id = ?", (vip_until, target_id))
        await db.commit()
    await message.answer(f"✅ Выдан Премиум пользователю <code>{target_id}</code> на {text_fmt}!", reply_markup=get_admin_panel())
    try: await bot.send_message(target_id, f"👑 <b>Уведомление от Администратора!</b>\nВам выдан Премиум на <b>{text_fmt}</b>.")
    except: pass
    await state.clear()

# --- БАН (РУЧНОЙ ВВОД ВРЕМЕНИ) ---
@dp.callback_query(F.data == "admin_ban_menu")
async def admin_ban_menu(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(UserStates.waiting_for_admin_ban_target)
    await callback.message.edit_text("Введи <b>ID</b> или <b>@юзернейм</b> для бана/разбана:", reply_markup=get_back_keyboard())
    await callback.answer()

@dp.message(UserStates.waiting_for_admin_ban_target)
async def admin_ban_target_entered(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    target, found_id = message.text.strip(), None
    async with aiosqlite.connect("crypto_bot.db") as db:
        if target.startswith("@"):
            target = target[1:]
            async with db.execute("SELECT user_id FROM users WHERE username = ?", (target,)) as cursor:
                user = await cursor.fetchone()
                if user: found_id = user[0]
        else:
            async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (int(target),)) as cursor:
                user = await cursor.fetchone()
                if user: found_id = user[0]
    if not found_id: return await message.answer("❌ Пользователь не найден.")
    
    await state.update_data(target_id=found_id)
    await state.set_state(UserStates.waiting_for_admin_ban_time)
    await message.answer(f"✅ Найден ID: <code>{found_id}</code>\n\nНапиши срок бана (например: <i>1 секунда, 1 день, 1 неделя, навсегда</i>):")

@dp.message(UserStates.waiting_for_admin_ban_time)
async def admin_ban_time_entered(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    seconds, text_fmt = parse_duration(message.text)
    
    if seconds is None:
        # Если админ не написал число и время, значит он хочет РАЗБАНИТЬ
        if "разбан" in message.text.lower() or "снять" in message.text.lower():
            data = await state.get_data()
            target_id = data.get("target_id")
            async with aiosqlite.connect("crypto_bot.db") as db:
                await db.execute("UPDATE users SET is_banned = 0, ban_until = 0 WHERE user_id = ?", (target_id,))
                await db.commit()
            await message.answer(f"✅ Пользователь <code>{target_id}</code> разбанен!", reply_markup=get_admin_panel())
            try: await bot.send_message(target_id, "✅ <b>Ваш бан снят администратором!</b>\nДоступ к функциям восстановлен.")
            except: pass
            await state.clear()
            return
        else:
            return await message.answer("❌ Не понял. Напиши срок (например: 1 день) или слово «разбанить»:")
            
    data = await state.get_data()
    target_id = data.get("target_id")
    ban_until = 9999999999 if seconds == 9999999999 else int(time.time()) + seconds
    
    async with aiosqlite.connect("crypto_bot.db") as db:
        await db.execute("UPDATE users SET is_banned = 1, ban_until = ? WHERE user_id = ?", (ban_until, target_id))
        await db.commit()
        
    await message.answer(f"✅ Пользователь <code>{target_id}</code> забанен на {text_fmt}!", reply_markup=get_admin_panel())
    
    until_text = "навсегда" if ban_until == 9999999999 else datetime.fromtimestamp(ban_until).strftime('%Y-%m-%d %H:%M')
    try: await bot.send_message(target_id, f"🚫 <b>Вы забанены Администратором!</b>\nСрок: <b>{text_fmt}</b> (до {until_text})")
    except: pass
    await state.clear()

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🔧 <b>Админ-панель</b>", reply_markup=get_admin_panel())
    await callback.answer()

# --- БЛОКИРОВКА ЗАБАНЕННЫХ ---
@dp.message(~F.text.startswith("/"))
async def check_ban_message(message: types.Message):
    is_banned, until = await check_ban(message.from_user.id)
    if is_banned:
        until_text = "навсегда" if until == 9999999999 else datetime.fromtimestamp(until).strftime('%Y-%m-%d %H:%M')
        await message.answer(f"🚫 <b>Вы забанены!</b>\nСрок окончания: {until_text}")
        return

# --- ФОНОВАЯ ЗАДАЧА ---
async def price_watcher():
    logging.info("Наблюдатель запущен.")
    while True:
        try:
            await update_exchange_rates()
            async with aiosqlite.connect("crypto_bot.db") as db:
                async with db.execute("SELECT user_id, coin FROM user_alerts") as cursor:
                    subscriptions = await cursor.fetchall()
            tracked_coins = set([sub[1] for sub in subscriptions])
            
            for coin in tracked_coins:
                if coin == "USDT":
                    continue  # <--- ДОБАВИТЬ ЭТУ СТРОКУ
                current_price = await get_binance_price(COINS[coin])
                old_price = previous_prices.get(coin)
                if old_price and current_price > 0:
                    change_pct = ((current_price - old_price) / old_price) * 100
                    if change_pct > 1.0 or change_pct < -3.0:
                        for user_id, sub_coin in subscriptions:
                            if sub_coin == coin:
                                try:
                                    icon = "🚀" if change_pct > 0 else "📉"
                                    await bot.send_message(user_id, f"{icon} <b>{coin} изменилась!</b>\nЗа 10 мин: {change_pct:+.2f}%\nЦена: {get_price_string(current_price)}")
                                except: pass
                previous_prices[coin] = current_price
                await asyncio.sleep(0.5)
        except Exception as e:
            logging.error(f"Ошибка в наблюдателе: {e}")
        await asyncio.sleep(600)

async def main():
    await init_db()
    stars.setup(dp)
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(price_watcher())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
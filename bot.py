import os
import sqlite3
import random
import asyncio
from dataclasses import dataclass
from typing import List, Optional, Dict
from enum import Enum
from functools import lru_cache

import aiohttp
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage

# ----------------- CONFIG -----------------
TZ_NAME = "Asia/Dushanbe"
SEND_HOUR = 6
SEND_MINUTE = 0

ALQURAN_BASE = "https://api.alquran.cloud/v1"
QURANENC_TAFSIR_KEY = "uzbek_mokhtasar"
TOTAL_AYAHS = 6236

DB_PATH = "bot.db"

# ----------------- ENUMS -----------------
class Reciter(str, Enum):
    AFASY = "ar.alafasy"
    ABDUL_BASIT = "ar.abdulbasitmurattal"
    HUDHAYFI = "ar.hudhayfi"
    MINSHAWI = "ar.minshawimurattal"
    GHAMDI = "ar.shaatree"
    MATROOD = "ar.matrud"
    JUHAYNI = "ar.aljuhany"
    DOSARI = "ar.abdullahbasfar"
    SUDAYS = "ar.alsudays"
    SHURAYM = "ar.shuraym"

    @classmethod
    def get_name(cls, reciter: str) -> str:
        names = {
            cls.AFASY: "Mishari Al-Afasy",
            cls.ABDUL_BASIT: "Abdul Basit",
            cls.HUDHAYFI: "Hudhayfi",
            cls.MINSHAWI: "Minshawi",
            cls.GHAMDI: "Abu Bakr Al-Shatri",
            cls.MATROOD: "Matrud",
            cls.JUHAYNI: "Al-Juhany",
            cls.DOSARI: "Dosari",
            cls.SUDAYS: "Al-Sudays",
            cls.SHURAYM: "Shuraym",
        }
        return names.get(reciter, reciter)

# ----------------- DATACLASSES -----------------
@dataclass
class AyahBundle:
    surah: int
    ayah_in_surah: int
    arabic_text: str
    surah_name: str
    surah_english_name: str
    audio_mp3: str
    uz_tafsir: str
    juz: int = 0
    page: int = 0

@dataclass
class SurahInfo:
    number: int
    name: str
    english_name: str
    number_of_ayahs: int

# ----------------- DATABASE (OPTIMIZED) -----------------
class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
    
    def _get_connection(self):
        return sqlite3.connect(self.db_path)
    
    def _init_db(self):
        with self._get_connection() as con:
            # Create tables
            con.execute("""
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_active INTEGER DEFAULT 1,
                preferred_reciter TEXT DEFAULT 'ar.alafasy',
                joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            
            con.execute("""
            CREATE TABLE IF NOT EXISTS user_settings(
                user_id INTEGER PRIMARY KEY,
                receive_daily INTEGER DEFAULT 1,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )""")
            
            # Migrate old tables if needed
            cursor = con.execute("PRAGMA table_info(users)")
            columns = [col[1] for col in cursor.fetchall()]
            
            migrations = {
                'username': "ALTER TABLE users ADD COLUMN username TEXT",
                'first_name': "ALTER TABLE users ADD COLUMN first_name TEXT",
                'last_name': "ALTER TABLE users ADD COLUMN last_name TEXT",
                'preferred_reciter': "ALTER TABLE users ADD COLUMN preferred_reciter TEXT DEFAULT 'ar.alafasy'",
                'joined_date': "ALTER TABLE users ADD COLUMN joined_date TIMESTAMP"
            }
            
            for col, query in migrations.items():
                if col not in columns:
                    con.execute(query)
                    if col == 'joined_date':
                        con.execute("UPDATE users SET joined_date = CURRENT_TIMESTAMP WHERE joined_date IS NULL")
            
            con.commit()
    
    def save_user(self, user_id: int, **kwargs):
        with self._get_connection() as con:
            con.execute("""
            INSERT INTO users(user_id, username, first_name, last_name, is_active, preferred_reciter)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                is_active = excluded.is_active
            """, (
                user_id,
                kwargs.get('username', ''),
                kwargs.get('first_name', ''),
                kwargs.get('last_name', ''),
                kwargs.get('is_active', 1),
                kwargs.get('preferred_reciter', 'ar.alafasy')
            ))
            
            con.execute("""
            INSERT OR IGNORE INTO user_settings(user_id, receive_daily)
            VALUES(?, ?)
            """, (user_id, kwargs.get('is_active', 1)))
    
    def update_reciter(self, user_id: int, reciter: str):
        with self._get_connection() as con:
            con.execute("UPDATE users SET preferred_reciter=? WHERE user_id=?", (reciter, user_id))
    
    def get_reciter(self, user_id: int) -> str:
        with self._get_connection() as con:
            cur = con.execute("SELECT preferred_reciter FROM users WHERE user_id=?", (user_id,))
            result = cur.fetchone()
            return result[0] if result and result[0] else Reciter.AFASY
    
    def toggle_daily(self, user_id: int, enable: bool = None):
        with self._get_connection() as con:
            if enable is None:
                cur = con.execute("SELECT receive_daily FROM user_settings WHERE user_id=?", (user_id,))
                current = cur.fetchone()
                enable = not (current[0] if current else True)
            
            con.execute("""
            INSERT INTO user_settings(user_id, receive_daily)
            VALUES(?, ?)
            ON CONFLICT(user_id) DO UPDATE SET receive_daily = excluded.receive_daily
            """, (user_id, 1 if enable else 0))
            return enable
    
    def get_daily_setting(self, user_id: int) -> bool:
        with self._get_connection() as con:
            cur = con.execute("SELECT receive_daily FROM user_settings WHERE user_id=?", (user_id,))
            result = cur.fetchone()
            return bool(result[0]) if result else True
    
    def get_active_users(self) -> List[int]:
        with self._get_connection() as con:
            cur = con.execute("""
            SELECT u.user_id FROM users u
            JOIN user_settings s ON u.user_id = s.user_id
            WHERE u.is_active=1 AND s.receive_daily=1
            """)
            return [row[0] for row in cur.fetchall()]

# ----------------- API CLIENT (OPTIMIZED) -----------------
class QuranAPI:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.surahs_cache: List[SurahInfo] = []
    
    async def _get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def close(self):
        if self.session:
            await self.session.close()
    
    @lru_cache(maxsize=1)
    async def get_surahs(self) -> List[SurahInfo]:
        if self.surahs_cache:
            return self.surahs_cache
        
        session = await self._get_session()
        async with session.get(f"{ALQURAN_BASE}/surah") as resp:
            data = await resp.json()
            self.surahs_cache = [
                SurahInfo(
                    number=item['number'],
                    name=item['name'],
                    english_name=item['englishName'],
                    number_of_ayahs=item['numberOfAyahs']
                ) for item in data['data']
            ]
            return self.surahs_cache
    
    async def get_surah(self, number: int) -> Optional[SurahInfo]:
        surahs = await self.get_surahs()
        return next((s for s in surahs if s.number == number), None)
    
    async def get_ayah(self, surah: int, ayah: int, reciter: str = None) -> Optional[AyahBundle]:
        try:
            session = await self._get_session()
            reciter = reciter or Reciter.AFASY
            
            # Fetch all data concurrently
            tasks = [
                session.get(f"{ALQURAN_BASE}/ayah/{surah}:{ayah}"),
                session.get(f"{ALQURAN_BASE}/ayah/{surah}:{ayah}/{reciter}"),
                session.get(f"https://quranenc.com/api/v1/translation/aya/{QURANENC_TAFSIR_KEY}/{surah}/{ayah}")
            ]
            
            responses = await asyncio.gather(*tasks)
            data = [await r.json() for r in responses]
            
            # Parse responses
            ayah_data = data[0]['data']
            audio_data = data[1]['data']
            tafsir_data = data[2]
            
            # Extract audio URL
            audio_mp3 = next(iter(audio_data.get("audioSecondary", [])), audio_data.get("audio", ""))
            
            # Extract tafsir
            uz_tafsir = ""
            if isinstance(tafsir_data, dict):
                result = tafsir_data.get("result") or tafsir_data
                uz_tafsir = result.get("translation", "") if isinstance(result, dict) else ""
            
            return AyahBundle(
                surah=surah,
                ayah_in_surah=ayah,
                arabic_text=ayah_data["text"],
                surah_name=ayah_data["surah"]["name"],
                surah_english_name=ayah_data["surah"]["englishName"],
                audio_mp3=audio_mp3,
                uz_tafsir=uz_tafsir,
                juz=ayah_data.get("juz", 0),
                page=ayah_data.get("page", 0)
            )
        except Exception as e:
            print(f"API Error: {e}")
            return None
    
    async def get_random_ayah(self, reciter: str = None) -> Optional[AyahBundle]:
        rnd = random.randint(1, TOTAL_AYAHS)
        session = await self._get_session()
        
        try:
            async with session.get(f"{ALQURAN_BASE}/ayah/{rnd}") as resp:
                data = await resp.json()
                ayah_data = data['data']
                return await self.get_ayah(
                    ayah_data['surah']['number'],
                    ayah_data['numberInSurah'],
                    reciter
                )
        except:
            return None

# ----------------- KEYBOARDS (UZBEK) -----------------
class Keyboards:
    @staticmethod
    def main() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📖 Tasodifiy oyat", callback_data="random")],
            [InlineKeyboardButton(text="🔍 Sura tanlash", callback_data="surahs")],
            [InlineKeyboardButton(text="🎙 Qori tanlash", callback_data="reciters")],
            [InlineKeyboardButton(text="⚙️ Sozlamalar", callback_data="settings")],
        ])
    
    @staticmethod
    def back() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Ortga", callback_data="menu")]
        ])
    
    @staticmethod
    def reciters(current: str = None) -> InlineKeyboardMarkup:
        keyboard = []
        reciters = list(Reciter)
        for i in range(0, len(reciters), 2):
            row = []
            for reciter in reciters[i:i+2]:
                name = Reciter.get_name(reciter.value)
                marker = "✅ " if reciter.value == current else ""
                row.append(InlineKeyboardButton(
                    text=f"{marker}{name}", 
                    callback_data=f"reciter_{reciter.value}"
                ))
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="menu")])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
    
    @staticmethod
    def surahs(surahs: List[SurahInfo], page: int = 0) -> InlineKeyboardMarkup:
        items = 10
        start = page * items
        current = surahs[start:start + items]
        
        keyboard = [[InlineKeyboardButton(
            text=f"{s.number}. {s.name}",
            callback_data=f"surah_{s.number}"
        )] for s in current]
        
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"page_{page-1}"))
        if start + items < len(surahs):
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"page_{page+1}"))
        if nav:
            keyboard.append(nav)
        
        keyboard.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="menu")])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
    
    @staticmethod
    def ayah_nav(surah: int, current: int, total: int) -> InlineKeyboardMarkup:
        keyboard = []
        nav = []
        if current > 1:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"ayah_{surah}_{current-1}"))
        if current < total:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"ayah_{surah}_{current+1}"))
        if nav:
            keyboard.append(nav)
        
        keyboard.append([
            InlineKeyboardButton(text="🔊 Audio", callback_data=f"audio_{surah}_{current}"),
            InlineKeyboardButton(text="📝 Tafsir", callback_data=f"tafsir_{surah}_{current}")
        ])
        keyboard.append([
            InlineKeyboardButton(text="🔄 Tasodifiy", callback_data="random"),
            InlineKeyboardButton(text="🔙 Menyu", callback_data="menu")
        ])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
    
    @staticmethod
    def settings(daily: bool) -> InlineKeyboardMarkup:
        status = "✅ Yoqilgan" if daily else "❌ O'chirilgan"
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📅 Kunlik xabarlar: {status}", callback_data="toggle_daily")],
            [InlineKeyboardButton(text="🎙 Qori tanlash", callback_data="reciters")],
            [InlineKeyboardButton(text="🔙 Ortga", callback_data="menu")]
        ])

# ----------------- BOT -----------------
class QuranBot:
    def __init__(self, token: str):
        self.bot = Bot(token=token)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.db = Database(DB_PATH)
        self.api = QuranAPI()
        self._setup_handlers()
    
    def _setup_handlers(self):
        @self.dp.message(CommandStart())
        async def start_cmd(msg: Message):
            user = msg.from_user
            self.db.save_user(
                user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name
            )
            
            await msg.answer(
                f"🤲 <b>Assalomu alaykum, {user.first_name}!</b>\n\n"
                "📖 Qur'oni Karim botiga xush kelibsiz!\n\n"
                "✅ Men sizga har kuni tasodifiy oyat yuboraman\n"
                "🎯 Istalgan sura va oyatni tanlashingiz mumkin\n"
                "🎙 10 dan ortiq qorilardan tanlash imkoniyati",
                parse_mode="HTML"
            )
            await self.show_menu(msg)
        
        @self.dp.message(Command("menu"))
        async def menu_cmd(msg: Message):
            await self.show_menu(msg)
        
        @self.dp.callback_query(F.data == "menu")
        async def menu_cb(cb: CallbackQuery):
            await cb.message.edit_text("🔆 <b>Asosiy menyu</b>", reply_markup=Keyboards.main(), parse_mode="HTML")
            await cb.answer()
        
        @self.dp.callback_query(F.data == "random")
        async def random_cb(cb: CallbackQuery):
            await cb.message.edit_text("⏳ Yuklanmoqda...")
            await cb.answer()
            await self.send_ayah(cb.from_user.id, cb.message)
        
        @self.dp.callback_query(F.data == "surahs")
        async def surahs_cb(cb: CallbackQuery):
            surahs = await self.api.get_surahs()
            await cb.message.edit_text(
                "📖 <b>Sura tanlang:</b>",
                reply_markup=Keyboards.surahs(surahs),
                parse_mode="HTML"
            )
            await cb.answer()
        
        @self.dp.callback_query(F.data.startswith("page_"))
        async def page_cb(cb: CallbackQuery):
            page = int(cb.data.split("_")[1])
            surahs = await self.api.get_surahs()
            await cb.message.edit_reply_markup(reply_markup=Keyboards.surahs(surahs, page))
            await cb.answer()
        
        @self.dp.callback_query(F.data.startswith("surah_"))
        async def surah_cb(cb: CallbackQuery):
            surah_num = int(cb.data.split("_")[1])
            await cb.message.edit_text("⏳ Yuklanmoqda...")
            await self.send_ayah(cb.from_user.id, cb.message, surah_num, 1)
            await cb.answer()
        
        @self.dp.callback_query(F.data.startswith("ayah_"))
        async def ayah_cb(cb: CallbackQuery):
            _, surah, ayah = cb.data.split("_")
            await cb.message.edit_text("⏳ Yuklanmoqda...")
            await self.send_ayah(cb.from_user.id, cb.message, int(surah), int(ayah))
            await cb.answer()
        
        @self.dp.callback_query(F.data == "reciters")
        async def reciters_cb(cb: CallbackQuery):
            current = self.db.get_reciter(cb.from_user.id)
            await cb.message.edit_text(
                "🎙 <b>Qori tanlang:</b>",
                reply_markup=Keyboards.reciters(current),
                parse_mode="HTML"
            )
            await cb.answer()
        
        @self.dp.callback_query(F.data.startswith("reciter_"))
        async def reciter_cb(cb: CallbackQuery):
            reciter = cb.data.replace("reciter_", "")
            self.db.update_reciter(cb.from_user.id, reciter)
            await cb.message.edit_text(
                f"✅ <b>{Reciter.get_name(reciter)}</b> tanlandi!",
                reply_markup=Keyboards.main(),
                parse_mode="HTML"
            )
            await cb.answer()
        
        @self.dp.callback_query(F.data == "settings")
        async def settings_cb(cb: CallbackQuery):
            daily = self.db.get_daily_setting(cb.from_user.id)
            await cb.message.edit_text(
                "⚙️ <b>Sozlamalar</b>",
                reply_markup=Keyboards.settings(daily),
                parse_mode="HTML"
            )
            await cb.answer()
        
        @self.dp.callback_query(F.data == "toggle_daily")
        async def toggle_daily_cb(cb: CallbackQuery):
            new_state = self.db.toggle_daily(cb.from_user.id)
            status = "yoqildi ✅" if new_state else "o'chirildi ❌"
            await cb.answer(f"Kunlik xabarlar {status}", show_alert=False)
            await settings_cb(cb)
        
        @self.dp.callback_query(F.data.startswith("audio_"))
        async def audio_cb(cb: CallbackQuery):
            _, surah, ayah = cb.data.split("_")
            reciter = self.db.get_reciter(cb.from_user.id)
            bundle = await self.api.get_ayah(int(surah), int(ayah), reciter)
            
            if bundle and bundle.audio_mp3:
                await cb.message.answer_audio(
                    bundle.audio_mp3,
                    caption=f"🎙 {Reciter.get_name(reciter)}"
                )
            else:
                await cb.message.answer("❌ Audio topilmadi")
            await cb.answer()
        
        @self.dp.callback_query(F.data.startswith("tafsir_"))
        async def tafsir_cb(cb: CallbackQuery):
            _, surah, ayah = cb.data.split("_")
            bundle = await self.api.get_ayah(int(surah), int(ayah))
            
            if bundle and bundle.uz_tafsir:
                await cb.message.answer(
                    f"📝 <b>Tafsir:</b>\n\n{bundle.uz_tafsir}",
                    parse_mode="HTML"
                )
            else:
                await cb.message.answer("❌ Tafsir topilmadi")
            await cb.answer()
    
    async def show_menu(self, msg: Message):
        await msg.answer("🔆 <b>Asosiy menyu</b>", reply_markup=Keyboards.main(), parse_mode="HTML")
    
    async def send_ayah(self, user_id: int, edit_msg: Message = None, surah: int = None, ayah: int = None):
        try:
            reciter = self.db.get_reciter(user_id)
            
            if surah and ayah:
                bundle = await self.api.get_ayah(surah, ayah, reciter)
            else:
                bundle = await self.api.get_random_ayah(reciter)
            
            if not bundle:
                if edit_msg:
                    await edit_msg.edit_text("❌ Xatolik yuz berdi")
                return
            
            surah_info = await self.api.get_surah(bundle.surah)
            total = surah_info.number_of_ayahs if surah_info else 6236
            
            text = (f"📖 <b>{bundle.surah_name}</b> - {bundle.ayah_in_surah}-oyat\n"
                   f"📚 Juz: {bundle.juz} | Sahifa: {bundle.page}\n\n"
                   f"{bundle.arabic_text}")
            
            if edit_msg:
                await edit_msg.edit_text(text, parse_mode="HTML")
            else:
                await self.bot.send_message(user_id, text, parse_mode="HTML")
            
            if bundle.uz_tafsir:
                await self.bot.send_message(
                    user_id,
                    f"📝 <b>Tafsir:</b>\n\n{bundle.uz_tafsir}",
                    parse_mode="HTML"
                )
            
            if bundle.audio_mp3:
                await self.bot.send_audio(
                    user_id,
                    bundle.audio_mp3,
                    caption=f"🎙 {Reciter.get_name(reciter)}"
                )
            
            await self.bot.send_message(
                user_id,
                "🔍 <b>Amallar:</b>",
                reply_markup=Keyboards.ayah_nav(bundle.surah, bundle.ayah_in_surah, total),
                parse_mode="HTML"
            )
            
        except Exception as e:
            print(f"Send error: {e}")
            if edit_msg:
                await edit_msg.edit_text("⚠️ Xatolik yuz berdi")
    
    async def daily_job(self):
        users = self.db.get_active_users()
        if not users:
            return
        
        print(f"📨 Sending to {len(users)} users...")
        for uid in users:
            await self.send_ayah(uid)
            await asyncio.sleep(0.3)
    
    async def run(self):
        # Setup scheduler
        scheduler = AsyncIOScheduler(timezone=pytz.timezone(TZ_NAME))
        scheduler.add_job(
            self.daily_job,
            CronTrigger(hour=SEND_HOUR, minute=SEND_MINUTE)
        )
        scheduler.start()
        
        print(f"🤖 Bot started! Daily ayah: {SEND_HOUR:02d}:{SEND_MINUTE:02d}")
        
        try:
            await self.dp.start_polling(self.bot)
        finally:
            await self.api.close()
            await self.bot.session.close()

# ----------------- MAIN -----------------
def main():
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is required")
    
    bot = QuranBot(token)
    asyncio.run(bot.run())

if __name__ == "__main__":
    main()
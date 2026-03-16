import os
import sqlite3
import random
import asyncio
from dataclasses import dataclass
from typing import List, Optional
from enum import Enum

import aiohttp
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, BotCommandScopeDefault
from aiogram.fsm.storage.memory import MemoryStorage

# ----------------- CONFIG -----------------
TZ_NAME = "Asia/Dushanbe"
SEND_HOUR = 6
SEND_MINUTE = 0

ALQURAN_BASE = "https://api.alquran.cloud/v1"
QURANENC_TAFSIR_KEY = "uzbek_mokhtasar"
TOTAL_AYAHS = 6236
AUDIO_BASE = "https://cdn.islamic.network/quran/audio-surah"

DB_PATH = "bot.db"

# ----------------- ENUMS -----------------
class Reciter(str, Enum):
    AFASY = "ar.alafasy"
    ABDUL_BASIT = "ar.abdulbasitmurattal"
    HUDHAYFI = "ar.hudhayfi"
    MINSHAWI = "ar.minshawimurattal"
    GHAMDI = "ar.shaatree"
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
            cls.JUHAYNI: "Al-Juhany",
            cls.DOSARI: "Dosari",
            cls.SUDAYS: "Al-Sudays",
            cls.SHURAYM: "Shuraym",
        }
        return names.get(reciter, reciter)
    
    @classmethod
    def get_reciter_id(cls, reciter: str) -> int:
        """Get numeric ID for reciter for audio download"""
        ids = {
            cls.AFASY: 1,        # Mishari Al-Afasy
            cls.ABDUL_BASIT: 2,   # Abdul Basit
            cls.HUDHAYFI: 3,      # Hudhayfi
            cls.MINSHAWI: 4,      # Minshawi
            cls.GHAMDI: 5,        # Abu Bakr Al-Shatri
            cls.JUHAYNI: 6,       # Al-Juhany
            cls.DOSARI: 7,        # Dosari
            cls.SUDAYS: 8,        # Al-Sudays
            cls.SHURAYM: 9,       # Shuraym
        }
        return ids.get(reciter, 1)

# ----------------- DATACLASSES -----------------
@dataclass
class AyahBundle:
    surah: int
    ayah_in_surah: int
    arabic_text: str
    surah_name: str
    surah_english_name: str
    surah_latin_name: str
    audio_url: str
    uz_tafsir: str
    juz: int = 0
    page: int = 0

@dataclass
class SurahInfo:
    number: int
    name: str
    english_name: str
    latin_name: str
    number_of_ayahs: int

# ----------------- DATABASE -----------------
class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
    
    def _get_connection(self):
        return sqlite3.connect(self.db_path)
    
    def _init_db(self):
        with self._get_connection() as con:
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
            con.commit()
    
    def update_reciter(self, user_id: int, reciter: str):
        with self._get_connection() as con:
            con.execute("UPDATE users SET preferred_reciter=? WHERE user_id=?", (reciter, user_id))
            con.commit()
    
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
            con.commit()
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

# ----------------- API CLIENT -----------------
class QuranAPI:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.surahs_cache: List[SurahInfo] = []
        self._cache_loaded = False
    
    async def _get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None
    
    def get_latin_name(self, english_name: str) -> str:
        """Convert English name to Latin (simplified)"""
        # Basic mapping for common surahs
        latin_names = {
            "Al-Fatihah": "Al-Fatiha",
            "Al-Baqarah": "Al-Baqara",
            "Aal-i-Imraan": "Ali Imran",
            "An-Nisaa": "An-Nisa",
            "Al-Maaida": "Al-Maida",
            "Al-An'aam": "Al-An'am",
            "Al-A'raaf": "Al-A'raf",
            "Al-Anfaal": "Al-Anfal",
            "At-Tawba": "At-Tawba",
            "Yunus": "Yunus",
            "Hud": "Hud",
            "Yusuf": "Yusuf",
            "Ar-Ra'd": "Ar-Ra'd",
            "Ibrahim": "Ibrahim",
            "Al-Hijr": "Al-Hijr",
            "An-Nahl": "An-Nahl",
            "Al-Israa": "Al-Isra",
            "Al-Kahf": "Al-Kahf",
            "Maryam": "Maryam",
            "Taa-Haa": "Ta-Ha",
            "Al-Anbiyaa": "Al-Anbiya",
            "Al-Hajj": "Al-Hajj",
            "Al-Muminoon": "Al-Mu'minun",
            "An-Noor": "An-Nur",
            "Al-Furqaan": "Al-Furqan",
            "Ash-Shu'araa": "Ash-Shu'ara",
            "An-Naml": "An-Naml",
            "Al-Qasas": "Al-Qasas",
            "Al-Ankaboot": "Al-Ankabut",
            "Ar-Room": "Ar-Rum",
            "Luqman": "Luqman",
            "As-Sajda": "As-Sajda",
            "Al-Ahzaab": "Al-Ahzab",
            "Saba": "Saba",
            "Faatir": "Fatir",
            "Yaseen": "Yasin",
            "As-Saaffaat": "As-Saffat",
            "Saad": "Sad",
            "Az-Zumar": "Az-Zumar",
            "Ghafir": "Ghafir",
            "Fussilat": "Fussilat",
            "Ash-Shura": "Ash-Shura",
            "Az-Zukhruf": "Az-Zukhruf",
            "Ad-Dukhaan": "Ad-Dukhan",
            "Al-Jaathiya": "Al-Jathiya",
            "Al-Ahqaf": "Al-Ahqaf",
            "Muhammad": "Muhammad",
            "Al-Fath": "Al-Fath",
            "Al-Hujuraat": "Al-Hujurat",
            "Qaaf": "Qaf",
            "Adh-Dhaariyat": "Adh-Dhariyat",
            "At-Tur": "At-Tur",
            "An-Najm": "An-Najm",
            "Al-Qamar": "Al-Qamar",
            "Ar-Rahmaan": "Ar-Rahman",
            "Al-Waaqia": "Al-Waqia",
            "Al-Hadeed": "Al-Hadid",
            "Al-Mujaadila": "Al-Mujadila",
            "Al-Hashr": "Al-Hashr",
            "Al-Mumtahina": "Al-Mumtahina",
            "As-Saff": "As-Saff",
            "Al-Jumu'a": "Al-Jumuah",
            "Al-Munaafiqoon": "Al-Munafiqun",
            "At-Taghaabun": "At-Taghabun",
            "At-Talaaq": "At-Talaq",
            "At-Tahreem": "At-Tahrim",
            "Al-Mulk": "Al-Mulk",
            "Al-Qalam": "Al-Qalam",
            "Al-Haaqqa": "Al-Haqqa",
            "Al-Ma'aarij": "Al-Maarij",
            "Nooh": "Nuh",
            "Al-Jinn": "Al-Jinn",
            "Al-Muzzammil": "Al-Muzzammil",
            "Al-Muddaththir": "Al-Muddathir",
            "Al-Qiyaama": "Al-Qiyama",
            "Al-Insaan": "Al-Insan",
            "Al-Mursalaat": "Al-Mursalat",
            "An-Naba": "An-Naba",
            "An-Naazi'aat": "An-Naziat",
            "Abasa": "Abasa",
            "At-Takweer": "At-Takwir",
            "Al-Infitaar": "Al-Infitar",
            "Al-Mutaffifeen": "Al-Mutaffifin",
            "Al-Inshiqaaq": "Al-Inshiqaq",
            "Al-Burooj": "Al-Buruj",
            "At-Taariq": "At-Tariq",
            "Al-A'laa": "Al-Ala",
            "Al-Ghaashiya": "Al-Ghashiya",
            "Al-Fajr": "Al-Fajr",
            "Al-Balad": "Al-Balad",
            "Ash-Shams": "Ash-Shams",
            "Al-Layl": "Al-Layl",
            "Ad-Dhuhaa": "Ad-Duha",
            "Ash-Sharh": "Ash-Sharh",
            "At-Teen": "At-Tin",
            "Al-Alaq": "Al-Alaq",
            "Al-Qadr": "Al-Qadr",
            "Al-Bayyina": "Al-Bayyina",
            "Az-Zalzala": "Az-Zalzala",
            "Al-Aadiyaat": "Al-Adiyat",
            "Al-Qaari'a": "Al-Qaria",
            "At-Takaathur": "At-Takathur",
            "Al-Asr": "Al-Asr",
            "Al-Humaza": "Al-Humaza",
            "Al-Feel": "Al-Fil",
            "Quraysh": "Quraysh",
            "Al-Maa'un": "Al-Maun",
            "Al-Kawthar": "Al-Kawthar",
            "Al-Kaafiroon": "Al-Kafirun",
            "An-Nasr": "An-Nasr",
            "Al-Masad": "Al-Masad",
            "Al-Ikhlaas": "Al-Ikhlas",
            "Al-Falaq": "Al-Falaq",
            "An-Naas": "An-Nas",
        }
        return latin_names.get(english_name, english_name)
    
    async def get_surahs(self) -> List[SurahInfo]:
        """Get list of all surahs"""
        if self._cache_loaded and self.surahs_cache:
            return self.surahs_cache
        
        session = await self._get_session()
        try:
            async with session.get(f"{ALQURAN_BASE}/surah") as resp:
                data = await resp.json()
                self.surahs_cache = []
                for item in data['data']:
                    latin_name = self.get_latin_name(item['englishName'])
                    self.surahs_cache.append(
                        SurahInfo(
                            number=item['number'],
                            name=item['name'],
                            english_name=item['englishName'],
                            latin_name=latin_name,
                            number_of_ayahs=item['numberOfAyahs']
                        )
                    )
                self._cache_loaded = True
                return self.surahs_cache
        except Exception as e:
            print(f"Error fetching surahs: {e}")
            return []
    
    async def get_surah(self, number: int) -> Optional[SurahInfo]:
        surahs = await self.get_surahs()
        for s in surahs:
            if s.number == number:
                return s
        return None
    
    async def get_audio_url(self, surah: int, ayah: int, reciter: str) -> str:
        """Get audio URL for specific ayah"""
        try:
            # Try multiple audio sources
            reciter_id = Reciter.get_reciter_id(reciter)
            
            # Source 1: CDN with surah-based audio (more reliable)
            audio_url = f"{AUDIO_BASE}/{reciter_id}/{surah}.mp3"
            
            # Check if URL is accessible
            session = await self._get_session()
            async with session.head(audio_url, timeout=5) as resp:
                if resp.status == 200:
                    return audio_url
            
            # Source 2: AlQuran Cloud API
            async with session.get(f"{ALQURAN_BASE}/ayah/{surah}:{ayah}/{reciter}") as resp:
                data = await resp.json()
                audio_data = data['data']
                audio_secondary = audio_data.get("audioSecondary", [])
                if audio_secondary:
                    return audio_secondary[0]
                return audio_data.get("audio", "")
                
        except Exception as e:
            print(f"Error getting audio URL: {e}")
            return ""
    
    async def get_ayah(self, surah: int, ayah: int, reciter: str = None) -> Optional[AyahBundle]:
        try:
            session = await self._get_session()
            reciter = reciter or Reciter.AFASY
            
            # Get ayah data
            async with session.get(f"{ALQURAN_BASE}/ayah/{surah}:{ayah}") as resp:
                ayah_data = (await resp.json())['data']
            
            # Get surah info for latin name
            surah_info = await self.get_surah(surah)
            latin_name = surah_info.latin_name if surah_info else ayah_data["surah"]["englishName"]
            
            # Get audio URL
            audio_url = await self.get_audio_url(surah, ayah, reciter)
            
            # Get tafsir
            uz_tafsir = ""
            try:
                async with session.get(f"https://quranenc.com/api/v1/translation/aya/{QURANENC_TAFSIR_KEY}/{surah}/{ayah}") as resp:
                    tafsir_data = await resp.json()
                    result = tafsir_data.get("result") or tafsir_data
                    uz_tafsir = result.get("translation", "") if isinstance(result, dict) else ""
            except:
                pass
            
            return AyahBundle(
                surah=surah,
                ayah_in_surah=ayah,
                arabic_text=ayah_data["text"],
                surah_name=ayah_data["surah"]["name"],
                surah_english_name=ayah_data["surah"]["englishName"],
                surah_latin_name=latin_name,
                audio_url=audio_url,
                uz_tafsir=uz_tafsir,
                juz=ayah_data.get("juz", 0),
                page=ayah_data.get("page", 0)
            )
        except Exception as e:
            print(f"API Error for {surah}:{ayah}: {e}")
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
        except Exception as e:
            print(f"Error getting random ayah: {e}")
            return None

# ----------------- KEYBOARDS -----------------
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
    def reciters(current: str = None) -> InlineKeyboardMarkup:
        keyboard = []
        reciters = [
            (Reciter.AFASY, "Mishari Al-Afasy"),
            (Reciter.ABDUL_BASIT, "Abdul Basit"),
            (Reciter.HUDHAYFI, "Hudhayfi"),
            (Reciter.MINSHAWI, "Minshawi"),
            (Reciter.GHAMDI, "Abu Bakr Al-Shatri"),
            (Reciter.JUHAYNI, "Al-Juhany"),
            (Reciter.DOSARI, "Dosari"),
            (Reciter.SUDAYS, "Al-Sudays"),
            (Reciter.SHURAYM, "Shuraym"),
        ]
        
        for i in range(0, len(reciters), 2):
            row = []
            for j in range(2):
                if i + j < len(reciters):
                    reciter_id, reciter_name = reciters[i + j]
                    marker = "✅ " if reciter_id == current else ""
                    row.append(InlineKeyboardButton(
                        text=f"{marker}{reciter_name}", 
                        callback_data=f"reciter_{reciter_id}"
                    ))
            keyboard.append(row)
        
        keyboard.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="menu")])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
    
    @staticmethod
    def surahs(surahs: List[SurahInfo], page: int = 0) -> InlineKeyboardMarkup:
        items = 10
        start = page * items
        end = min(start + items, len(surahs))
        current = surahs[start:end]
        
        keyboard = []
        for s in current:
            # Show both Arabic and Latin names
            button_text = f"{s.number}. {s.latin_name}"
            keyboard.append([InlineKeyboardButton(
                text=button_text,
                callback_data=f"surah_{s.number}"
            )])
        
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"page_{page-1}"))
        if end < len(surahs):
            nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"page_{page+1}"))
        if nav:
            keyboard.append(nav)
        
        keyboard.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="menu")])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
    
    @staticmethod
    def ayah_nav(surah: int, current: int, total: int, surah_name: str) -> InlineKeyboardMarkup:
        keyboard = []
        nav = []
        if current > 1:
            nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"ayah_{surah}_{current-1}"))
        if current < total:
            nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"ayah_{surah}_{current+1}"))
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
    
    async def set_commands(self):
        commands = [
            BotCommand(command="start", description="🚀 Botni ishga tushirish"),
            BotCommand(command="menu", description="🔆 Asosiy menyu"),
            BotCommand(command="random", description="📖 Tasodifiy oyat"),
            BotCommand(command="surahs", description="🔍 Sura tanlash"),
            BotCommand(command="reciters", description="🎙 Qori tanlash"),
            BotCommand(command="settings", description="⚙️ Sozlamalar"),
            BotCommand(command="daily", description="📅 Kunlik xabarlar"),
        ]
        await self.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    
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
            
            welcome_text = (
                f"🤲 <b>Assalomu alaykum, {user.first_name or 'Qur\'on o\'quvchi'}!</b>\n\n"
                "📖 Qur'oni Karim botiga xush kelibsiz!\n\n"
                "✅ <b>Imkoniyatlar:</b>\n"
                "• Har kuni ertalab soat 6:00 da tasodifiy oyat\n"
                "• 9 ta mashhur qorilardan audio\n"
                "• O'zbek tilida tafsir (Al-Muxtasar)\n"
                "• Istalgan sura va oyatni o'qish\n"
                "• Sura nomlari lotin alifbosida"
            )
            
            await msg.answer(welcome_text, parse_mode="HTML")
            await self.show_menu(msg)
        
        @self.dp.message(Command("menu"))
        async def menu_cmd(msg: Message):
            await self.show_menu(msg)
        
        @self.dp.message(Command("random"))
        async def random_cmd(msg: Message):
            await msg.answer("⏳ Yuklanmoqda...")
            await self.send_ayah(msg.from_user.id)
        
        @self.dp.message(Command("surahs"))
        async def surahs_cmd(msg: Message):
            surahs = await self.api.get_surahs()
            if not surahs:
                await msg.answer("❌ Surani yuklashda xatolik. Qayta urinib ko'ring.")
                return
            await msg.answer(
                "📖 <b>Sura tanlang:</b>",
                reply_markup=Keyboards.surahs(surahs),
                parse_mode="HTML"
            )
        
        @self.dp.message(Command("reciters"))
        async def reciters_cmd(msg: Message):
            current = self.db.get_reciter(msg.from_user.id)
            await msg.answer(
                "🎙 <b>Qori tanlang:</b>",
                reply_markup=Keyboards.reciters(current),
                parse_mode="HTML"
            )
        
        @self.dp.message(Command("settings"))
        async def settings_cmd(msg: Message):
            daily = self.db.get_daily_setting(msg.from_user.id)
            await msg.answer(
                "⚙️ <b>Sozlamalar</b>",
                reply_markup=Keyboards.settings(daily),
                parse_mode="HTML"
            )
        
        @self.dp.message(Command("daily"))
        async def daily_cmd(msg: Message):
            new_state = self.db.toggle_daily(msg.from_user.id)
            status = "yoqildi ✅" if new_state else "o'chirildi ❌"
            await msg.answer(f"📅 Kunlik xabarlar {status}")
        
        # Callback handlers
        @self.dp.callback_query(F.data == "menu")
        async def menu_cb(cb: CallbackQuery):
            await cb.message.edit_text(
                "🔆 <b>Asosiy menyu</b>", 
                reply_markup=Keyboards.main(), 
                parse_mode="HTML"
            )
            await cb.answer()
        
        @self.dp.callback_query(F.data == "random")
        async def random_cb(cb: CallbackQuery):
            await cb.message.edit_text("⏳ Yuklanmoqda...")
            await cb.answer()
            await self.send_ayah(cb.from_user.id, cb.message)
        
        @self.dp.callback_query(F.data == "surahs")
        async def surahs_cb(cb: CallbackQuery):
            surahs = await self.api.get_surahs()
            if not surahs:
                await cb.message.edit_text("❌ Surani yuklashda xatolik. Qayta urinib ko'ring.")
                await cb.answer()
                return
            await cb.message.edit_text(
                "📖 <b>Sura tanlang:</b>",
                reply_markup=Keyboards.surahs(surahs),
                parse_mode="HTML"
            )
            await cb.answer()
        
        @self.dp.callback_query(F.data.startswith("page_"))
        async def page_cb(cb: CallbackQuery):
            try:
                page = int(cb.data.split("_")[1])
                surahs = await self.api.get_surahs()
                if surahs:
                    await cb.message.edit_reply_markup(
                        reply_markup=Keyboards.surahs(surahs, page)
                    )
            except Exception as e:
                print(f"Page error: {e}")
            await cb.answer()
        
        @self.dp.callback_query(F.data.startswith("surah_"))
        async def surah_cb(cb: CallbackQuery):
            try:
                surah_num = int(cb.data.split("_")[1])
                await cb.message.edit_text("⏳ Yuklanmoqda...")
                await cb.answer()
                await self.send_ayah(cb.from_user.id, cb.message, surah_num, 1)
            except Exception as e:
                print(f"Surah callback error: {e}")
                await cb.message.edit_text("❌ Xatolik yuz berdi")
                await cb.answer()
        
        @self.dp.callback_query(F.data.startswith("ayah_"))
        async def ayah_cb(cb: CallbackQuery):
            try:
                parts = cb.data.split("_")
                surah = int(parts[1])
                ayah = int(parts[2])
                await cb.message.edit_text("⏳ Yuklanmoqda...")
                await cb.answer()
                await self.send_ayah(cb.from_user.id, cb.message, surah, ayah)
            except Exception as e:
                print(f"Ayah callback error: {e}")
                await cb.message.edit_text("❌ Xatolik yuz berdi")
                await cb.answer()
        
        @self.dp.callback_query(F.data == "reciters")
        async def reciters_cb(cb: CallbackQuery):
            try:
                current = self.db.get_reciter(cb.from_user.id)
                await cb.message.edit_text(
                    "🎙 <b>Qori tanlang:</b>",
                    reply_markup=Keyboards.reciters(current),
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"Reciters callback error: {e}")
            await cb.answer()
        
        @self.dp.callback_query(F.data.startswith("reciter_"))
        async def reciter_cb(cb: CallbackQuery):
            try:
                reciter = cb.data.replace("reciter_", "")
                self.db.update_reciter(cb.from_user.id, reciter)
                
                # Test audio with new reciter
                await cb.message.edit_text(
                    f"✅ <b>{Reciter.get_name(reciter)}</b> tanlandi!\n\n"
                    "⏳ Audio tekshirilmoqda...",
                    parse_mode="HTML"
                )
                
                # Send a test ayah with new reciter
                await self.send_ayah(cb.from_user.id, cb.message)
                
            except Exception as e:
                print(f"Reciter selection error: {e}")
                await cb.message.edit_text("❌ Xatolik yuz berdi")
            await cb.answer()
        
        @self.dp.callback_query(F.data == "settings")
        async def settings_cb(cb: CallbackQuery):
            try:
                daily = self.db.get_daily_setting(cb.from_user.id)
                await cb.message.edit_text(
                    "⚙️ <b>Sozlamalar</b>",
                    reply_markup=Keyboards.settings(daily),
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"Settings error: {e}")
            await cb.answer()
        
        @self.dp.callback_query(F.data == "toggle_daily")
        async def toggle_daily_cb(cb: CallbackQuery):
            try:
                new_state = self.db.toggle_daily(cb.from_user.id)
                status = "yoqildi ✅" if new_state else "o'chirildi ❌"
                await cb.answer(f"Kunlik xabarlar {status}")
                await settings_cb(cb)
            except Exception as e:
                print(f"Toggle daily error: {e}")
                await cb.answer("❌ Xatolik yuz berdi")
        
        @self.dp.callback_query(F.data.startswith("audio_"))
        async def audio_cb(cb: CallbackQuery):
            try:
                parts = cb.data.split("_")
                surah = int(parts[1])
                ayah = int(parts[2])
                reciter = self.db.get_reciter(cb.from_user.id)
                
                await cb.answer("⏳ Audio yuklanmoqda...")
                
                bundle = await self.api.get_ayah(surah, ayah, reciter)
                
                if bundle and bundle.audio_url:
                    await cb.message.answer_audio(
                        bundle.audio_url,
                        caption=f"🎙 {Reciter.get_name(reciter)}\nSura {surah}, Oyat {ayah}",
                        title=f"{bundle.surah_latin_name} - {ayah}-oyat"
                    )
                else:
                    # Try with Afasy as fallback
                    fallback_bundle = await self.api.get_ayah(surah, ayah, Reciter.AFASY)
                    if fallback_bundle and fallback_bundle.audio_url:
                        await cb.message.answer_audio(
                            fallback_bundle.audio_url,
                            caption=f"🎙 {Reciter.get_name(Reciter.AFASY)} (standart)\nSura {surah}, Oyat {ayah}",
                            title=f"{fallback_bundle.surah_latin_name} - {ayah}-oyat"
                        )
                    else:
                        await cb.message.answer("❌ Audio topilmadi")
            except Exception as e:
                print(f"Audio error: {e}")
                await cb.message.answer("❌ Audio yuklashda xatolik")
        
        @self.dp.callback_query(F.data.startswith("tafsir_"))
        async def tafsir_cb(cb: CallbackQuery):
            try:
                parts = cb.data.split("_")
                surah = int(parts[1])
                ayah = int(parts[2])
                
                await cb.answer("⏳ Tafsir yuklanmoqda...")
                
                bundle = await self.api.get_ayah(surah, ayah)
                
                if bundle and bundle.uz_tafsir:
                    await cb.message.answer(
                        f"📝 <b>Tafsir ({bundle.surah_latin_name}, {ayah}-oyat):</b>\n\n{bundle.uz_tafsir}",
                        parse_mode="HTML"
                    )
                else:
                    await cb.message.answer("❌ Tafsir topilmadi")
            except Exception as e:
                print(f"Tafsir error: {e}")
                await cb.message.answer("❌ Tafsir yuklashda xatolik")
    
    async def show_menu(self, msg: Message):
        await msg.answer(
            "🔆 <b>Asosiy menyu</b>", 
            reply_markup=Keyboards.main(), 
            parse_mode="HTML"
        )
    
    async def send_ayah(self, user_id: int, edit_msg: Message = None, surah: int = None, ayah: int = None):
        try:
            reciter = self.db.get_reciter(user_id)
            
            if surah and ayah:
                bundle = await self.api.get_ayah(surah, ayah, reciter)
            else:
                bundle = await self.api.get_random_ayah(reciter)
            
            if not bundle:
                if edit_msg:
                    await edit_msg.edit_text("❌ Oyat topilmadi")
                else:
                    await self.bot.send_message(user_id, "❌ Oyat topilmadi")
                return
            
            surah_info = await self.api.get_surah(bundle.surah)
            total = surah_info.number_of_ayahs if surah_info else 6236
            
            # Format message with Latin name
            header = f"📖 <b>{bundle.surah_latin_name}</b> ({bundle.surah_name}) - {bundle.ayah_in_surah}-oyat"
            info_line = f"📚 Juz: {bundle.juz} | Sahifa: {bundle.page}"
            
            text = f"{header}\n{info_line}\n\n{bundle.arabic_text}"
            
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
            
            if bundle.audio_url:
                await self.bot.send_audio(
                    user_id,
                    bundle.audio_url,
                    caption=f"🎙 {Reciter.get_name(reciter)}",
                    title=f"{bundle.surah_latin_name} - {bundle.ayah_in_surah}-oyat"
                )
            
            await self.bot.send_message(
                user_id,
                "🔍 <b>Amallar:</b>",
                reply_markup=Keyboards.ayah_nav(bundle.surah, bundle.ayah_in_surah, total, bundle.surah_latin_name),
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
        
        print(f"📨 Kunlik oyat {len(users)} ta foydalanuvchiga yuborilmoqda...")
        for uid in users:
            try:
                await self.send_ayah(uid)
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"Error sending to {uid}: {e}")
    
    async def run(self):
        # Set bot commands
        await self.set_commands()
        
        # Preload surahs
        try:
            await self.api.get_surahs()
            print("✅ Surah list loaded")
        except Exception as e:
            print(f"⚠️ Could not preload surahs: {e}")
        
        # Setup scheduler
        scheduler = AsyncIOScheduler(timezone=pytz.timezone(TZ_NAME))
        scheduler.add_job(
            self.daily_job,
            CronTrigger(hour=SEND_HOUR, minute=SEND_MINUTE)
        )
        scheduler.start()
        
        print(f"🤖 Bot ishga tushdi!")
        print(f"📅 Kunlik oyat: {SEND_HOUR:02d}:{SEND_MINUTE:02d}")
        print(f"🎙 Qorilar soni: 9")
        
        try:
            await self.dp.start_polling(self.bot)
        finally:
            await self.api.close()
            await self.bot.session.close()

# ----------------- MAIN -----------------
def main():
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        # For testing - you can set your token here
        token = "YOUR_BOT_TOKEN_HERE"  # Replace with your actual token
    
    bot = QuranBot(token)
    asyncio.run(bot.run())

if __name__ == "__main__":
    main()
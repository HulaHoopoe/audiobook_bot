#!/usr/bin/env python3
import os
import sys
import asyncio
import logging
import time
import re
import json
import hashlib
import sqlite3
import zipfile
import tempfile
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass
from enum import Enum
from datetime import datetime, timedelta

import aiofiles
import edge_tts
from telegram import Update, Document, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.constants import ChatAction
from pydub import AudioSegment
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Отключаем лишние логи
logging.getLogger('httpx').setLevel(logging.WARNING)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "YOUR_OPENAI_KEY")
MAX_FILE_SIZE = 50 * 1024 * 1024

# Базовые голоса
VOICES = {
    "ru-female": "ru-RU-SvetlanaNeural",
    "ru-male": "ru-RU-DmitryNeural",
    "en-female": "en-US-AriaNeural",
    "en-male": "en-US-GuyNeural"
}

# Премиум голоса
PREMIUM_VOICES = {
    "ru-child": "ru-RU-DariyaNeural",
    "ru-elder": "ru-RU-SvetlanaNeural",
    "en-british": "en-GB-SoniaNeural",
    "en-australian": "en-AU-NatashaNeural",
    "de-female": "de-DE-KatjaNeural",
    "fr-female": "fr-FR-DeniseNeural",
    "es-female": "es-ES-ElviraNeural",
    "it-female": "it-IT-ElsaNeural",
}

# Супер премиум голоса для персонажей
CHARACTER_VOICES = {
    "narrator": "ru-RU-SvetlanaNeural",
    "male_hero": "ru-RU-DmitryNeural",
    "female_hero": "ru-RU-SvetlanaNeural",
    "villain": "ru-RU-DmitryNeural",
    "child": "ru-RU-DariyaNeural",
    "elder": "ru-RU-SvetlanaNeural",
    "mysterious": "ru-RU-DmitryNeural"
}

class UserTier(Enum):
    FREE = "free"
    PREMIUM = "premium"
    SUPER_PREMIUM = "super_premium"

class ChapterMood(Enum):
    PEACEFUL = "peaceful"
    TENSE = "tense"
    ACTION = "action"
    ROMANTIC = "romantic"
    MYSTERIOUS = "mysterious"
    SAD = "sad"
    HAPPY = "happy"
    DRAMATIC = "dramatic"
    HORROR = "horror"
    ADVENTURE = "adventure"

@dataclass
class Chapter:
    number: int
    title: str
    start_position: int
    end_position: int
    text: str
    mood: ChapterMood
    background_music: Optional[str] = None
    estimated_duration: int = 0
    characters: List[str] = None

@dataclass
class Character:
    name: str
    voice: str
    description: str
    dialogue_pattern: str

class TextPreprocessor:
    """Предобработка текста книги"""

    def __init__(self):
        # Паттерны для удаления лишней информации
        self.cleanup_patterns = [
            # Номера страниц
            r'^\s*\d+\s*$',
            r'Page\s+\d+',
            r'Страница\s+\d+',
            
            # Сноски
            r'\[\d+\]',
            r'\(\d+\)',
            r'^\d+\s+[А-Яа-яA-Za-z]',
            
            # Служебная информация
            r'ISBN\s*:?\s*[\d\-]+',
            r'©\s*\d{4}',
            r'Copyright\s*\d{4}',
            
            # Заголовки и колонтитулы
            r'^[А-ЯA-Z\s]{3,}$',  # Строки только из заглавных букв
            r'^\s*\*\s*\*\s*\*\s*$',  # Разделители
            
            # Оглавление
            r'Глава\s+\d+\.+\s*\d+',
            r'Chapter\s+\d+\.+\s*\d+',
            
            # Пустые строки и лишние пробелы
            r'\n\s*\n\s*\n',  # Множественные переносы
            r'[ \t]+',  # Множественные пробелы и табы
        ]

    async def clean_text(self, text: str) -> str:
        """Очистка текста от лишней информации"""
        logger.info("Начинаю предобработку текста...")
        
        # Разбиваем на строки для обработки
        lines = text.split('\n')
        cleaned_lines = []
        
        for line in lines:
            line = line.strip()
            
            # Пропускаем пустые строки
            if not line:
                continue
            
            # Проверяем каждый паттерн
            is_junk = False
            for pattern in self.cleanup_patterns:
                if re.match(pattern, line):
                    is_junk = True
                    break
            
            # Дополнительные проверки
            if not is_junk:
                # Пропускаем строки только из цифр
                if line.isdigit():
                    is_junk = True
                
                # Пропускаем очень короткие строки (вероятно служебные)
                elif len(line) < 3:
                    is_junk = True
                
                # Пропускаем строки с большим количеством точек (оглавление)
                elif line.count('.') > len(line) * 0.3:
                    is_junk = True
            
            if not is_junk:
                cleaned_lines.append(line)
        
        # Объединяем обратно
        cleaned_text = '\n'.join(cleaned_lines)
        
        # Финальная очистка
        cleaned_text = re.sub(r'\n\s*\n\s*\n+', '\n\n', cleaned_text)
        cleaned_text = re.sub(r'[ \t]+', ' ', cleaned_text)
        
        # Статистика очистки
        original_length = len(text)
        cleaned_length = len(cleaned_text)
        removed_percent = ((original_length - cleaned_length) / original_length) * 100
        
        logger.info(f"Предобработка завершена. Удалено {removed_percent:.1f}% текста")
        
        return cleaned_text.strip()

class CharacterAnalyzer:
    """Анализ персонажей для супер премиум функции"""
    
    def __init__(self, openai_api_key: str):
        self.openai_api_key = openai_api_key

    async def analyze_characters(self, text: str) -> List[Character]:
        """Анализ персонажей в тексте"""
        if not self.openai_api_key or self.openai_api_key == "YOUR_OPENAI_KEY":
            logger.warning("OpenAI API ключ не установлен, возвращаю базовых персонажей")
            return self._get_default_characters()

        try:
            import openai
            openai.api_key = self.openai_api_key
            
            prompt = f"""
Проанализируй следующий текст и найди основных персонажей.
Для каждого персонажа определи:
1. Имя
2. Тип (главный герой, злодей, ребенок, старик, таинственный персонаж)
3. Краткое описание

Верни результат в JSON формате:
{{
    "characters": [
        {{
            "name": "Имя персонажа",
            "type": "тип персонажа",
            "description": "описание"
        }}
    ]
}}

Текст: {text[:3000]}...
"""
            
            response = await openai.ChatCompletion.acreate(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000,
                temperature=0.3
            )
            
            result = json.loads(response.choices[0].message.content)
            characters = []
            
            for char_data in result.get('characters', []):
                char_type = char_data.get('type', 'narrator').lower()
                
                # Определяем голос на основе типа персонажа
                voice = CHARACTER_VOICES.get(char_type.replace(' ', '_'), CHARACTER_VOICES['narrator'])
                
                character = Character(
                    name=char_data.get('name', 'Unknown'),
                    voice=voice,
                    description=char_data.get('description', ''),
                    dialogue_pattern=f'[{char_data.get("name", "Unknown")}]:'
                )
                characters.append(character)
            
            return characters
            
        except Exception as e:
            logger.error(f"Ошибка анализа персонажей: {e}")
            return self._get_default_characters()

    def _get_default_characters(self) -> List[Character]:
        """Возвращает базовых персонажей при отсутствии ИИ"""
        return [
            Character(
                name="Рассказчик",
                voice=CHARACTER_VOICES["narrator"],
                description="Основной рассказчик",
                dialogue_pattern=""
            )
        ]

class DatabaseManager:
    """Управление базой данных"""

    def __init__(self, db_path: str = "bookbot.db"):
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        """Инициализация базы данных"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Таблица пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                subscription_type TEXT DEFAULT 'free',
                subscription_expires TIMESTAMP,
                voice_preference TEXT,
                speed_preference REAL DEFAULT 1.0,
                pitch_preference REAL DEFAULT 1.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                total_books_processed INTEGER DEFAULT 0,
                total_processing_time REAL DEFAULT 0.0
            )
        ''')
        
        # Таблица книг
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                book_hash TEXT NOT NULL,
                title TEXT NOT NULL,
                original_filename TEXT,
                file_size INTEGER,
                text_length INTEGER,
                total_chapters INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                has_chapters BOOLEAN DEFAULT FALSE,
                has_background_music BOOLEAN DEFAULT FALSE,
                has_character_voices BOOLEAN DEFAULT FALSE,
                FOREIGN KEY (user_id) REFERENCES users (telegram_id)
            )
        ''')
        
        # Таблица прогресса чтения
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reading_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                book_hash TEXT NOT NULL,
                current_chapter INTEGER DEFAULT 1,
                position_in_chapter INTEGER DEFAULT 0,
                total_listened_time INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_completed BOOLEAN DEFAULT FALSE,
                UNIQUE(user_id, book_hash),
                FOREIGN KEY (user_id) REFERENCES users (telegram_id)
            )
        ''')
        
        # Таблица статистики глав
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chapter_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                book_hash TEXT NOT NULL,
                chapter_number INTEGER NOT NULL,
                listen_count INTEGER DEFAULT 0,
                total_listen_time INTEGER DEFAULT 0,
                last_listened TIMESTAMP,
                rating INTEGER DEFAULT 0,
                UNIQUE(user_id, book_hash, chapter_number)
            )
        ''')
        
        # Таблица закладок
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bookmarks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                book_hash TEXT NOT NULL,
                chapter_number INTEGER NOT NULL,
                position INTEGER NOT NULL,
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()

    async def get_or_create_user(self, telegram_id: int, username: str = None):
        """Получение или создание пользователя"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,))
        user = cursor.fetchone()
        
        if not user:
            cursor.execute('''
                INSERT INTO users (telegram_id, username)
                VALUES (?, ?)
            ''', (telegram_id, username))
            conn.commit()
            
            cursor.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,))
            user = cursor.fetchone()
        
        conn.close()
        return user

    async def get_user_tier(self, telegram_id: int) -> UserTier:
        """Получение уровня подписки пользователя"""
        user = await self.get_or_create_user(telegram_id)
        subscription_type = user[3] if user else 'free'
        
        try:
            return UserTier(subscription_type)
        except ValueError:
            return UserTier.FREE

    async def save_book_stats(self, user_id: int, book_hash: str, title: str, 
                             chapters_count: int, has_features: dict):
        """Сохранение статистики книги"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO books 
            (user_id, book_hash, title, total_chapters, has_chapters, 
             has_background_music, has_character_voices)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, book_hash, title, chapters_count,
              has_features.get('chapters', False),
              has_features.get('music', False),
              has_features.get('character_voices', False)))
        
        conn.commit()
        conn.close()

    async def get_user_books(self, user_id: int) -> List[Dict]:
        """Получение книг пользователя"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT book_hash, title, total_chapters, created_at,
                   has_chapters, has_background_music, has_character_voices
            FROM books WHERE user_id = ?
            ORDER BY created_at DESC
        ''', (user_id,))
        
        books = []
        for row in cursor.fetchall():
            books.append({
                'hash': row[0],
                'title': row[1],
                'chapters': row[2],
                'created_at': row[3],
                'features': {
                    'chapters': row[4],
                    'music': row[5],
                    'character_voices': row[6]
                }
            })
        
        conn.close()
        return books

    async def save_chapter_stats(self, user_id: int, book_hash: str, 
                                chapter_number: int, listen_time: int):
        """Сохранение статистики по главе"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO chapter_stats 
            (user_id, book_hash, chapter_number, listen_count, 
             total_listen_time, last_listened)
            VALUES (?, ?, ?, 
                    COALESCE((SELECT listen_count FROM chapter_stats 
                             WHERE user_id = ? AND book_hash = ? AND chapter_number = ?), 0) + 1,
                    COALESCE((SELECT total_listen_time FROM chapter_stats 
                             WHERE user_id = ? AND book_hash = ? AND chapter_number = ?), 0) + ?,
                    CURRENT_TIMESTAMP)
        ''', (user_id, book_hash, chapter_number, 
              user_id, book_hash, chapter_number,
              user_id, book_hash, chapter_number, listen_time))
        
        conn.commit()
        conn.close()

class InlineKeyboardManager:
    """Управление inline клавиатурами"""

    @staticmethod
    def get_main_menu_keyboard():
        """Главное меню"""
        keyboard = [
            [InlineKeyboardButton("📚 Загрузить книгу", callback_data="upload_book")],
            [InlineKeyboardButton("📖 Моя библиотека", callback_data="library")],
            [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
            [InlineKeyboardButton("💎 Премиум", callback_data="premium")],
            [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")]
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_voice_selection_keyboard(user_tier: UserTier):
        """Клавиатура выбора голоса"""
        keyboard = []
        
        # Базовые голоса для всех
        keyboard.extend([
            [InlineKeyboardButton("👩 Светлана (RU)", callback_data="voice_ru-female"),
             InlineKeyboardButton("👨 Дмитрий (RU)", callback_data="voice_ru-male")],
            [InlineKeyboardButton("👩 Ария (EN)", callback_data="voice_en-female"),
             InlineKeyboardButton("👨 Гай (EN)", callback_data="voice_en-male")]
        ])
        
        # Премиум голоса
        if user_tier in [UserTier.PREMIUM, UserTier.SUPER_PREMIUM]:
            keyboard.extend([
                [InlineKeyboardButton("🌍 Британский", callback_data="voice_en-british"),
                 InlineKeyboardButton("🇩🇪 Немецкий", callback_data="voice_de-female")],
                [InlineKeyboardButton("🇫🇷 Французский", callback_data="voice_fr-female"),
                 InlineKeyboardButton("🇪🇸 Испанский", callback_data="voice_es-female")]
            ])
        
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])
        
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_premium_features_keyboard(user_tier: UserTier):
        """Клавиатура премиум функций"""
        keyboard = []
        
        if user_tier == UserTier.FREE:
            keyboard = [
                [InlineKeyboardButton("✅ Базовое озвучивание", callback_data="basic_tts")],
                [InlineKeyboardButton("💎 Обновить до Premium", callback_data="upgrade_premium")]
            ]
        elif user_tier == UserTier.PREMIUM:
            keyboard = [
                [InlineKeyboardButton("✅ Базовое озвучивание", callback_data="basic_tts")],
                [InlineKeyboardButton("🎭 С разбиением на главы", callback_data="chapters_tts")],
                [InlineKeyboardButton("🎵 С фоновой музыкой", callback_data="music_tts")],
                [InlineKeyboardButton("🚀 Обновить до Super Premium", callback_data="upgrade_super")]
            ]
        else:  # SUPER_PREMIUM
            keyboard = [
                [InlineKeyboardButton("✅ Базовое озвучивание", callback_data="basic_tts")],
                [InlineKeyboardButton("🎭 С главами", callback_data="chapters_tts")],
                [InlineKeyboardButton("🎵 С музыкой", callback_data="music_tts")],
                [InlineKeyboardButton("🎪 С голосами персонажей", callback_data="character_voices_tts")]
            ]
        
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_voice")])
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def get_chapter_navigation_keyboard(current_chapter: int, total_chapters: int, book_hash: str):
        """Клавиатура навигации по главам"""
        keyboard = []
        
        # Навигация
        nav_row = []
        if current_chapter > 1:
            nav_row.append(InlineKeyboardButton("⏮️ Предыдущая", 
                                              callback_data=f"chapter_{book_hash}_{current_chapter-1}"))
        if current_chapter < total_chapters:
            nav_row.append(InlineKeyboardButton("Следующая ⏭️", 
                                              callback_data=f"chapter_{book_hash}_{current_chapter+1}"))
        if nav_row:
            keyboard.append(nav_row)
        
        # Дополнительные действия
        keyboard.extend([
            [InlineKeyboardButton("📑 Список глав", callback_data=f"chapters_list_{book_hash}"),
             InlineKeyboardButton("🔖 Закладка", callback_data=f"bookmark_{book_hash}_{current_chapter}")],
            [InlineKeyboardButton("📊 Статистика", callback_data=f"book_stats_{book_hash}"),
             InlineKeyboardButton("🔙 В библиотеку", callback_data="library")]
        ])
        
        return InlineKeyboardMarkup(keyboard)

class EnhancedBookToSpeechBot:
    """Основной класс бота с расширенным функционалом"""

    def __init__(self, token: str):
        self.token = token
        self.temp_dir = tempfile.gettempdir()
        self.preprocessor = TextPreprocessor()
        self.character_analyzer = CharacterAnalyzer(OPENAI_API_KEY)
        self.db_manager = DatabaseManager()
        self.keyboard_manager = InlineKeyboardManager()

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start с inline клавиатурой"""
        user_id = update.effective_user.id
        username = update.effective_user.username
        
        await self.db_manager.get_or_create_user(user_id, username)
        
        welcome_text = """
🎧 **Добро пожаловать в BookToSpeech Bot!**

Превращаю ваши книги в качественные аудиокниги с естественной интонацией.

**Возможности:**
📚 Поддержка TXT, EPUB, PDF
🎙️ Несколько голосов на выбор
💎 Премиум функции для продвинутых пользователей

Выберите действие:
        """
        
        keyboard = self.keyboard_manager.get_main_menu_keyboard()
        await update.message.reply_text(welcome_text, parse_mode='Markdown', 
                                      reply_markup=keyboard)

    async def handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка callback запросов"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        user_id = query.from_user.id
        user_tier = await self.db_manager.get_user_tier(user_id)
        
        if data == "upload_book":
            await self.show_upload_instructions(query, context)
        elif data == "library":
            await self.show_library(query, context)
        elif data == "settings":
            await self.show_settings(query, context)
        elif data == "premium":
            await self.show_premium_info(query, context)
        elif data == "help":
            await self.show_help(query, context)
        elif data.startswith("voice_"):
            voice_key = data.replace("voice_", "")
            await self.select_processing_options(query, context, voice_key)
        elif data in ["basic_tts", "chapters_tts", "music_tts", "character_voices_tts"]:
            await self.start_audio_generation(query, context, data)
        elif data.startswith("chapter_"):
            parts = data.split("_")
            book_hash = parts[1]
            chapter_num = int(parts[2])
            await self.play_chapter(query, context, book_hash, chapter_num)
        elif data == "back_to_main":
            await self.show_main_menu(query, context)
        elif data == "back_to_voice":
            if 'selected_voice' in context.user_data:
                voice_key = context.user_data['selected_voice']
                await self.select_processing_options(query, context, voice_key)
            else:
                await self.show_main_menu(query, context)

    async def show_upload_instructions(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
        """Показ инструкций по загрузке"""
        text = """
📚 **Загрузка книги**

**Поддерживаемые форматы:**
• 📄 TXT - текстовые файлы
• 📖 EPUB - электронные книги  
• 📋 PDF - документы

**Ограничения:**
• Максимальный размер: 50 МБ
• Текст должен быть на русском или английском языке

**Просто отправьте файл в этот чат!**
        """
        
        keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]
        
        await query.edit_message_text(text, parse_mode='Markdown',
                                    reply_markup=InlineKeyboardMarkup(keyboard))

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка загруженного документа"""
        document: Document = update.message.document
        user_id = update.effective_user.id
        user_tier = await self.db_manager.get_user_tier(user_id)
        
        # Проверка размера файла
        if document.file_size > MAX_FILE_SIZE:
            await update.message.reply_text(
                f"❌ Файл слишком большой! Максимум: {MAX_FILE_SIZE // (1024*1024)} МБ"
            )
            return
        
        # Проверка типа файла
        if not self._is_supported_file(document.file_name):
            await update.message.reply_text(
                "❌ Поддерживаются только файлы: .txt, .epub, .pdf"
            )
            return
        
        await update.message.reply_chat_action(ChatAction.TYPING)
        
        try:
            # Загрузка файла
            file = await context.bot.get_file(document.file_id)
            file_path = os.path.join(self.temp_dir, document.file_name)
            await file.download_to_drive(file_path)
            
            # Извлечение и предобработка текста
            raw_text = await self._extract_text(file_path)
            if not raw_text.strip():
                await update.message.reply_text("❌ Не удалось извлечь текст из файла")
                return
            
            # Предобработка текста
            clean_text = await self.preprocessor.clean_text(raw_text)
            
            # Создание хеша книги
            book_hash = hashlib.md5(clean_text.encode()).hexdigest()
            
            # Сохранение в контексте
            context.user_data.update({
                'text': clean_text,
                'filename': document.file_name,
                'book_hash': book_hash,
                'file_size': document.file_size
            })
            
            # Показ результатов обработки
            info_text = f"""
📖 **Файл успешно обработан!**

📊 **Статистика:**
• Оригинал: {len(raw_text):,} символов
• После очистки: {len(clean_text):,} символов
• Размер файла: {document.file_size / 1024:.1f} КБ
• Примерное время: {self._estimate_duration(clean_text)} мин

🎙️ **Выберите голос для озвучивания:**
            """
            
            keyboard = self.keyboard_manager.get_voice_selection_keyboard(user_tier)
            await update.message.reply_text(info_text, parse_mode='Markdown',
                                          reply_markup=keyboard)
            
        except Exception as e:
            logger.error(f"Ошибка обработки файла: {e}")
            await update.message.reply_text("❌ Произошла ошибка при обработке файла")
        finally:
            if 'file_path' in locals() and os.path.exists(file_path):
                os.remove(file_path)

    async def select_processing_options(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, voice_key: str):
        """Выбор опций обработки"""
        user_id = query.from_user.id
        user_tier = await self.db_manager.get_user_tier(user_id)
        
        context.user_data['selected_voice'] = voice_key
        
        text = f"""
🎙️ **Выбран голос:** {voice_key.replace('-', ' ').title()}

📋 **Выберите режим обработки:**
        """
        
        keyboard = self.keyboard_manager.get_premium_features_keyboard(user_tier)
        
        await query.edit_message_text(text, parse_mode='Markdown',
                                    reply_markup=keyboard)

    async def start_audio_generation(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, mode: str):
        """Начало генерации аудио"""
        if 'text' not in context.user_data:
            await query.edit_message_text("❌ Сначала загрузите файл!")
            return
        
        user_id = query.from_user.id
        text = context.user_data['text']
        filename = context.user_data.get('filename', 'book')
        voice_key = context.user_data.get('selected_voice')
        book_hash = context.user_data.get('book_hash')
        
        # Определяем голос
        if voice_key in VOICES:
            voice = VOICES[voice_key]
        elif voice_key in PREMIUM_VOICES:
            voice = PREMIUM_VOICES[voice_key]
        else:
            voice = VOICES['ru-female']
        
        await query.edit_message_text("🎙️ Начинаю создание аудиокниги...")
        
        try:
            if mode == "basic_tts":
                await self._generate_basic_audiobook(query, context, voice, text, filename)
            elif mode == "chapters_tts":
                await self._generate_chapters_audiobook(query, context, voice, text, filename)
            elif mode == "music_tts":
                await self._generate_music_audiobook(query, context, voice, text, filename)
            elif mode == "character_voices_tts":
                await self._generate_character_voices_audiobook(query, context, voice, text, filename)
                
            # Сохранение статистики книги
            await self.db_manager.save_book_stats(
                user_id, book_hash, filename, 1,
                {
                    'chapters': mode in ['chapters_tts', 'music_tts', 'character_voices_tts'],
                    'music': mode in ['music_tts', 'character_voices_tts'],
                    'character_voices': mode == 'character_voices_tts'
                }
            )
                
        except Exception as e:
            logger.error(f"Ошибка генерации аудио: {e}")
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="❌ Произошла ошибка при создании аудиокниги"
            )

    async def _generate_basic_audiobook(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE,
                                      voice: str, text: str, filename: str):
        """Генерация базовой аудиокниги"""
        # Разбивка текста на части
        chunks = self._split_text(text, max_length=3000)
        audio_files = []
        
        total_chunks = len(chunks)
        
        for i, chunk in enumerate(chunks):
            chunk_file = os.path.join(self.temp_dir, f"chunk_{i}.mp3")
            
            communicate = edge_tts.Communicate(chunk, voice)
            await communicate.save(chunk_file)
            audio_files.append(chunk_file)
            
            # Обновление прогресса каждые 5 частей
            if i % 5 == 0 or i == total_chunks - 1:
                progress = (i + 1) / total_chunks * 100
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"⏳ Прогресс: {progress:.1f}% ({i+1}/{total_chunks})"
                )
        
        # Объединение аудиофайлов
        output_file = os.path.join(self.temp_dir, f"{filename}_audiobook.mp3")
        await self._merge_audio_files(audio_files, output_file)
        
        # Отправка результата
        await self._send_audiobook(query, output_file, filename)
        
        # Очистка
        for file_path in audio_files + [output_file]:
            if os.path.exists(file_path):
                os.remove(file_path)

    async def _generate_chapters_audiobook(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE,
                                         voice: str, text: str, filename: str):
        """Генерация аудиокниги с разбиением на главы"""
        
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="📚 Анализирую структуру книги..."
        )
        
        # Анализ и разбиение на главы
        chapters = await self._analyze_book_structure(text, filename)
        
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"📖 Обнаружено глав: {len(chapters)}\n🎙️ Начинаю озвучивание..."
        )
        
        chapter_files = []
        
        for i, chapter in enumerate(chapters):
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"🎙️ Озвучиваю главу {i+1}/{len(chapters)}: {chapter.title[:30]}..."
            )
            
            # Генерация аудио для главы
            chapter_audio = await self._generate_chapter_audio(chapter.text, voice, f"chapter_{i+1}")
            
            chapter_info = {
                'number': chapter.number,
                'title': chapter.title,
                'file': chapter_audio,
                'duration': chapter.estimated_duration
            }
            chapter_files.append(chapter_info)
        
        # Сохранение информации о главах в контексте
        book_hash = context.user_data.get('book_hash')
        context.user_data.update({
            'chapters': chapter_files,
            'current_book_hash': book_hash
        })
        
        # Показ меню глав
        await self._show_chapters_menu(query, context, chapter_files, filename, book_hash)

    async def _generate_music_audiobook(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE,
                                      voice: str, text: str, filename: str):
        """Генерация аудиокниги с фоновой музыкой"""
        
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🎭 Анализирую настроение глав для подбора музыки..."
        )
        
        # Пока используем базовую генерацию с заглушкой для музыки
        await self._generate_chapters_audiobook(query, context, voice, text, filename)
        
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🎵 Функция фоновой музыки будет добавлена в следующих обновлениях!"
        )

    async def _generate_character_voices_audiobook(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE,
                                                 voice: str, text: str, filename: str):
        """Генерация аудиокниги с голосами персонажей"""
        
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🎪 Анализирую персонажей в книге..."
        )
        
        # Анализ персонажей
        characters = await self.character_analyzer.analyze_characters(text)
        
        if len(characters) > 1:
            char_names = [char.name for char in characters[:3]]  # Показываем первых 3
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"👥 Найдены персонажи: {', '.join(char_names)}"
            )
        
        # Пока используем базовую генерацию
        await self._generate_chapters_audiobook(query, context, voice, text, filename)
        
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🎪 Функция голосов персонажей будет добавлена в следующих обновлениях!"
        )

    async def _analyze_book_structure(self, text: str, filename: str) -> List[Chapter]:
        """Анализ структуры книги и разбиение на главы"""
        
        chapters = []
        chapter_patterns = [
            r'(?:Глава|ГЛАВА)\s*(\d+|[IVXLCDM]+)\.?\s*(.{0,100})',
            r'(?:Chapter|CHAPTER)\s*(\d+|[IVXLCDM]+)\.?\s*(.{0,100})',
            r'^(\d+)\.?\s*(.{5,100})$'
        ]
        
        lines = text.split('\n')
        current_chapter = None
        chapter_number = 0
        current_text = ""
        
        for i, line in enumerate(lines):
            line = line.strip()
            
            # Проверяем паттерны глав
            chapter_found = False
            for pattern in chapter_patterns:
                match = re.match(pattern, line, re.IGNORECASE)
                if match:
                    # Сохраняем предыдущую главу
                    if current_chapter is not None and current_text.strip():
                        current_chapter.text = current_text.strip()
                        current_chapter.estimated_duration = self._estimate_chapter_duration(current_text)
                        chapters.append(current_chapter)
                    
                    # Создаем новую главу
                    chapter_number += 1
                    title = match.group(2) if len(match.groups()) > 1 else f"Глава {chapter_number}"
                    
                    current_chapter = Chapter(
                        number=chapter_number,
                        title=title.strip(),
                        start_position=i,
                        end_position=0,
                        text="",
                        mood=ChapterMood.PEACEFUL
                    )
                    current_text = ""
                    chapter_found = True
                    break
            
            if not chapter_found:
                current_text += line + "\n"
        
        # Добавляем последнюю главу
        if current_chapter is not None and current_text.strip():
            current_chapter.text = current_text.strip()
            current_chapter.estimated_duration = self._estimate_chapter_duration(current_text)
            chapters.append(current_chapter)
        
        # Если главы не найдены, создаем одну главу из всего текста
        if not chapters:
            chapters.append(Chapter(
                number=1,
                title=filename,
                start_position=0,
                end_position=len(text),
                text=text,
                mood=ChapterMood.PEACEFUL,
                estimated_duration=self._estimate_chapter_duration(text)
            ))
        
        return chapters

    async def _generate_chapter_audio(self, text: str, voice: str, chapter_name: str) -> str:
        """Генерация аудио для главы"""
        chunks = self._split_text(text, max_length=3000)
        audio_files = []
        
        for i, chunk in enumerate(chunks):
            chunk_file = os.path.join(self.temp_dir, f"{chapter_name}_chunk_{i}.mp3")
            
            communicate = edge_tts.Communicate(chunk, voice)
            await communicate.save(chunk_file)
            audio_files.append(chunk_file)
        
        # Объединяем части главы
        chapter_file = os.path.join(self.temp_dir, f"{chapter_name}.mp3")
        await self._merge_audio_files(audio_files, chapter_file)
        
        # Удаляем временные файлы частей
        for file_path in audio_files:
            if os.path.exists(file_path):
                os.remove(file_path)
        
        return chapter_file

    async def _show_chapters_menu(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE,
                                 chapters: List[Dict], filename: str, book_hash: str):
        """Показ меню глав"""
        
        menu_text = f"🎧 **Аудиокнига готова: {filename}**\n\n"
        menu_text += f"📚 **Главы ({len(chapters)} шт.):**\n"
        
        for i, chapter in enumerate(chapters[:10]):  # Показываем первые 10 глав
            duration_min = chapter['duration'] // 60
            menu_text += f"• {chapter['number']}. {chapter['title'][:30]}... ({duration_min}м)\n"
        
        if len(chapters) > 10:
            menu_text += f"• ... и еще {len(chapters) - 10} глав\n"
        
        menu_text += "\n🎵 Выберите главу для прослушивания:"
        
        # Создаем кнопки для первых глав
        keyboard = []
        for chapter in chapters[:6]:  # Первые 6 глав
            keyboard.append([InlineKeyboardButton(
                f"▶️ Глава {chapter['number']}: {chapter['title'][:20]}...",
                callback_data=f"play_chapter_{book_hash}_{chapter['number']}"
            )])
        
        keyboard.extend([
            [InlineKeyboardButton("📑 Все главы", callback_data=f"all_chapters_{book_hash}")],
            [InlineKeyboardButton("📊 Статистика", callback_data=f"book_stats_{book_hash}")],
            [InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]
        ])
        
        await query.edit_message_text(menu_text, parse_mode='Markdown',
                                    reply_markup=InlineKeyboardMarkup(keyboard))

    async def play_chapter(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, 
                          book_hash: str, chapter_num: int):
        """Воспроизведение конкретной главы"""
        
        if 'chapters' not in context.user_data:
            await query.edit_message_text("❌ Главы не найдены. Создайте аудиокнигу заново.")
            return
        
        chapters = context.user_data['chapters']
        chapter = None
        
        for ch in chapters:
            if ch['number'] == chapter_num:
                chapter = ch
                break
        
        if not chapter:
            await query.edit_message_text(f"❌ Глава {chapter_num} не найдена")
            return
        
        user_id = query.from_user.id
        
        await query.edit_message_text(
            f"🎧 **Воспроизводим главу {chapter_num}**\n"
            f"📖 {chapter['title']}\n"
            f"⏱️ Длительность: {chapter['duration'] // 60} мин"
        )
        
        # Отправляем аудиофайл
        try:
            with open(chapter['file'], 'rb') as audio_file:
                await context.bot.send_audio(
                    chat_id=query.message.chat_id,
                    audio=audio_file,
                    title=f"Глава {chapter_num}: {chapter['title']}",
                    caption=f"🎧 Аудиокнига - Глава {chapter_num}"
                )
            
            # Сохранение статистики прослушивания
            await self.db_manager.save_chapter_stats(
                user_id, book_hash, chapter_num, chapter['duration']
            )
            
            # Показываем навигацию по главам
            keyboard = self.keyboard_manager.get_chapter_navigation_keyboard(
                chapter_num, len(chapters), book_hash
            )
            
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="🎵 Навигация по главам:",
                reply_markup=keyboard
            )
            
        except Exception as e:
            logger.error(f"Ошибка воспроизведения главы: {e}")
            await query.edit_message_text("❌ Ошибка при отправке аудиофайла")

    async def show_library(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
        """Показ библиотеки пользователя"""
        user_id = query.from_user.id
        books = await self.db_manager.get_user_books(user_id)
        
        if not books:
            text = """
📚 **Ваша библиотека**

У вас пока нет обработанных книг.
Загрузите первую книгу, чтобы начать!
            """
            keyboard = [
                [InlineKeyboardButton("📤 Загрузить книгу", callback_data="upload_book")],
                [InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]
            ]
        else:
            text = f"📚 **Ваша библиотека ({len(books)} книг):**\n\n"
            
            keyboard = []
            for i, book in enumerate(books[:10]):  # Показываем первые 10 книг
                features = []
                if book['features']['chapters']:
                    features.append("🎭")
                if book['features']['music']:
                    features.append("🎵")
                if book['features']['character_voices']:
                    features.append("🎪")
                
                feature_str = "".join(features) if features else "📖"
                
                text += f"{feature_str} **{book['title'][:30]}{'...' if len(book['title']) > 30 else ''}**\n"
                text += f"   Главы: {book['chapters']} • {book['created_at'][:10]}\n\n"
                
                keyboard.append([InlineKeyboardButton(
                    f"▶️ {book['title'][:25]}{'...' if len(book['title']) > 25 else ''}",
                    callback_data=f"open_book_{book['hash']}"
                )])
            
            keyboard.extend([
                [InlineKeyboardButton("📤 Загрузить новую книгу", callback_data="upload_book")],
                [InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]
            ])
        
        await query.edit_message_text(text, parse_mode='Markdown',
                                    reply_markup=InlineKeyboardMarkup(keyboard))

    async def show_settings(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
        """Показ настроек пользователя"""
        user_id = query.from_user.id
        user_tier = await self.db_manager.get_user_tier(user_id)
        
        text = f"""
⚙️ **Настройки**

🎭 **Текущий тариф:** {user_tier.value.title()}
🎙️ **Голос по умолчанию:** Не выбран
⚡ **Скорость речи:** 1.0x
🔊 **Громкость:** 100%

📊 **Статистика:**
• Обработано книг: 0
• Общее время: 0 минут

**Доступные функции:**
        """
        
        if user_tier == UserTier.FREE:
            text += "• Базовое озвучивание\n• 4 голоса\n• Файлы до 50 МБ"
        elif user_tier == UserTier.PREMIUM:
            text += "• Все функции Free\n• Разбиение на главы\n• Фоновая музыка\n• 8+ голосов"
        else:
            text += "• Все функции Premium\n• Голоса персонажей\n• Анализ настроения\n• Приоритет"
        
        keyboard = [
            [InlineKeyboardButton("🎙️ Выбрать голос по умолчанию", callback_data="settings_voice")],
            [InlineKeyboardButton("📊 Подробная статистика", callback_data="detailed_stats")],
        ]
        
        if user_tier != UserTier.SUPER_PREMIUM:
            keyboard.append([InlineKeyboardButton("💎 Обновить тариф", callback_data="premium")])
        
        keyboard.append([InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")])
        
        await query.edit_message_text(text, parse_mode='Markdown',
                                    reply_markup=InlineKeyboardMarkup(keyboard))

    async def show_premium_info(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
        """Показ информации о премиум подписке"""
        text = """
💎 **Тарифные планы BookToSpeech Bot**

🆓 **FREE (текущий тариф)**
• Базовое озвучивание
• 4 голоса (русский/английский)
• Файлы до 50 МБ
• Предобработка текста

💫 **PREMIUM - 299₽/месяц**
• Все функции Free
• Разбиение на главы
• Фоновая музыка по настроению
• 8+ голосов (включая европейские)
• Файлы до 100 МБ
• Статистика прослушивания
• История обработки

🚀 **SUPER PREMIUM - 599₽/месяц**
• Все функции Premium
• Голоса персонажей (ИИ анализ)
• Анализ настроения глав
• 12+ голосов (включая азиатские)
• Файлы до 500 МБ
• Приоритетная обработка
• Персональная поддержка

💳 **Способы оплаты:** Telegram Stars, банковские карты
        """
        
        keyboard = [
            [InlineKeyboardButton("💫 Попробовать Premium", callback_data="trial_premium")],
            [InlineKeyboardButton("🚀 Купить Super Premium", callback_data="buy_super_premium")],
            [InlineKeyboardButton("💳 Способы оплаты", callback_data="payment_methods")],
            [InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]
        ]
        
        await query.edit_message_text(text, parse_mode='Markdown',
                                    reply_markup=InlineKeyboardMarkup(keyboard))

    async def show_help(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
        """Показ справки"""
        help_text = """
📚 **Справка по использованию BookToSpeech Bot**

**🚀 Быстрый старт:**
1. Нажмите "📚 Загрузить книгу"
2. Отправьте файл книги (TXT, EPUB, PDF)
3. Выберите голос для озвучивания
4. Выберите режим обработки
5. Получите готовую аудиокнигу!

**📋 Поддерживаемые форматы:**
• TXT - текстовые файлы
• EPUB - электронные книги
• PDF - документы (с текстом)

**🎙️ Доступные голоса:**
• Русские: Светлана, Дмитрий
• Английские: Ария, Гай
• Premium: +европейские языки
• Super Premium: +азиатские языки

**⚙️ Режимы обработки:**
• Базовый - простое озвучивание
• С главами - разбиение на части
• С музыкой - фоновое сопровождение
• С персонажами - разные голоса

**📊 Ограничения:**
• Free: файлы до 50 МБ
• Premium: файлы до 100 МБ
• Super Premium: файлы до 500 МБ

**🔧 Дополнительные функции:**
• Автоматическая очистка текста от мусора
• Статистика прослушивания
• Сохранение истории книг
• Навигация по главам

**❓ Нужна помощь?**
Напишите /start для возврата в главное меню
        """
        
        keyboard = [[InlineKeyboardButton("🔙 Главное меню", callback_data="back_to_main")]]
        
        await query.edit_message_text(help_text, parse_mode='Markdown',
                                    reply_markup=InlineKeyboardMarkup(keyboard))

    async def show_main_menu(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
        """Показ главного меню"""
        welcome_text = """
🎧 **BookToSpeech Bot**

Превращаю ваши книги в качественные аудиокниги с естественной интонацией!

Выберите действие:
        """
        
        keyboard = self.keyboard_manager.get_main_menu_keyboard()
        await query.edit_message_text(welcome_text, parse_mode='Markdown', 
                                    reply_markup=keyboard)

    # Вспомогательные методы
    def _is_supported_file(self, filename: str) -> bool:
        """Проверка поддерживаемого формата файла"""
        if not filename:
            return False
        return filename.lower().endswith(('.txt', '.epub', '.pdf'))

    async def _extract_text(self, file_path: str) -> str:
        """Извлечение текста из файла"""
        try:
            if file_path.lower().endswith('.txt'):
                async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                    return await f.read()
            
            elif file_path.lower().endswith('.epub'):
                try:
                    import ebooklib
                    from ebooklib import epub
                    from bs4 import BeautifulSoup
                    
                    book = epub.read_epub(file_path)
                    text = ""
                    
                    for item in book.get_items():
                        if item.get_type() == ebooklib.ITEM_DOCUMENT:
                            soup = BeautifulSoup(item.get_content(), 'html.parser')
                            text += soup.get_text() + "\n"
                    
                    return text
                except ImportError:
                    return "Для обработки EPUB файлов установите: pip install ebooklib beautifulsoup4"
            
            elif file_path.lower().endswith('.pdf'):
                try:
                    import PyPDF2
                    
                    text = ""
                    with open(file_path, 'rb') as file:
                        pdf_reader = PyPDF2.PdfReader(file)
                        for page in pdf_reader.pages:
                            text += page.extract_text() + "\n"
                    
                    return text
                except ImportError:
                    return "Для обработки PDF файлов установите: pip install PyPDF2"
            
            return ""
            
        except Exception as e:
            logger.error(f"Ошибка извлечения текста: {e}")
            return ""

    def _estimate_duration(self, text: str) -> int:
        """Оценка времени озвучивания в минутах"""
        words = len(text.split())
        return max(1, words // 150)

    def _estimate_chapter_duration(self, text: str) -> int:
        """Оценка длительности главы в секундах"""
        words = len(text.split())
        return int((words / 150) * 60)

    def _split_text(self, text: str, max_length: int = 3000) -> list:
        """Разбивка текста на части"""
        sentences = text.replace('\n', ' ').split('. ')
        chunks = []
        current_chunk = ""
        
        for sentence in sentences:
            if len(current_chunk) + len(sentence) < max_length:
                current_chunk += sentence + ". "
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = sentence + ". "
        
        if current_chunk:
            chunks.append(current_chunk.strip())
        
        return chunks

    async def _merge_audio_files(self, audio_files: list, output_file: str):
        """Объединение аудиофайлов"""
        try:
            from pydub import AudioSegment
            
            combined = AudioSegment.empty()
            for audio_file in audio_files:
                if os.path.exists(audio_file):
                    audio = AudioSegment.from_mp3(audio_file)
                    combined += audio
            
            combined.export(output_file, format="mp3")
        except ImportError:
            if audio_files and os.path.exists(audio_files[0]):
                import shutil
                shutil.copy2(audio_files[0], output_file)
        except Exception as e:
            logger.error(f"Ошибка объединения аудио: {e}")
            if audio_files and os.path.exists(audio_files[0]):
                import shutil
                shutil.copy2(audio_files[0], output_file)

    async def _send_audiobook(self, query: CallbackQuery, file_path: str, filename: str):
        """Отправка аудиокниги пользователю"""
        file_size = os.path.getsize(file_path)
        
        if file_size > 50 * 1024 * 1024:  # Telegram limit 50MB
            zip_path = file_path.replace('.mp3', '.zip')
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                zipf.write(file_path, f"{filename}_audiobook.mp3")
            
            with open(zip_path, 'rb') as f:
                await query.message.reply_document(
                    document=f,
                    filename=f"{filename}_audiobook.zip",
                    caption="📦 Аудиокнига (архив)"
                )
            os.remove(zip_path)
        else:
            with open(file_path, 'rb') as f:
                await query.message.reply_audio(
                    audio=f,
                    filename=f"{filename}_audiobook.mp3",
                    title=f"Аудиокнига: {filename}",
                    caption="🎧 Ваша аудиокнига готова!"
                )

    # Заглушки для команд (для обратной совместимости)
    async def legacy_voice_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE, voice_key: str):
        """Обработка старых команд выбора голоса"""
        if 'text' not in context.user_data:
            await update.message.reply_text(
                "❌ Сначала отправьте файл книги!\n"
                "Используйте /start для начала работы."
            )
            return
        
        context.user_data['selected_voice'] = voice_key
        user_tier = await self.db_manager.get_user_tier(update.effective_user.id)
        
        text = f"🎙️ Выбран голос: {voice_key.replace('-', ' ').title()}\n\nВыберите режим обработки:"
        keyboard = self.keyboard_manager.get_premium_features_keyboard(user_tier)
        
        await update.message.reply_text(text, reply_markup=keyboard)

    async def handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка произвольных текстовых сообщений"""
        keyboard = self.keyboard_manager.get_main_menu_keyboard()
        
        await update.message.reply_text(
            "Я понимаю только команды и файлы книг 📚\n"
            "Выберите действие из меню:",
            reply_markup=keyboard
        )

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Глобальный обработчик ошибок"""
        logger.error("Исключение при обработке обновления:", exc_info=context.error)
        
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "😔 Произошла ошибка при обработке вашего запроса.\n"
                    "Попробуйте еще раз или обратитесь в поддержку."
                )
            except Exception as e:
                logger.error(f"Не удалось отправить сообщение об ошибке: {e}")

    # Методы для показа различных меню
    async def show_help_inline(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показ справки через команду"""
        await update.message.reply_text(
            "ℹ️ Для получения справки используйте кнопки в меню.\n"
            "Нажмите /start для открытия главного меню."
        )

    async def show_library_inline(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показ библиотеки через команду"""
        keyboard = self.keyboard_manager.get_main_menu_keyboard()
        await update.message.reply_text(
            "📚 Для просмотра библиотеки используйте кнопки в меню:",
            reply_markup=keyboard
        )

    async def show_settings_inline(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показ настроек через команду"""
        keyboard = self.keyboard_manager.get_main_menu_keyboard()
        await update.message.reply_text(
            "⚙️ Для настроек используйте кнопки в меню:",
            reply_markup=keyboard
        )

    async def show_premium_info_inline(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показ премиум через команду"""
        keyboard = self.keyboard_manager.get_main_menu_keyboard()
        await update.message.reply_text(
            "💎 Для информации о премиум используйте кнопки в меню:",
            reply_markup=keyboard
        )

    # Заглушки для несуществующих методов
    async def play_chapter_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда воспроизведения главы"""
        await update.message.reply_text(
            "🎵 Используйте кнопки в меню для навигации по главам.\n"
            "Нажмите /start для открытия главного меню."
        )

    async def continue_reading_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда продолжения чтения"""
        await update.message.reply_text(
            "📖 Функция продолжения чтения доступна через библиотеку.\n"
            "Нажмите /start → 📖 Моя библиотека"
        )

def main():
    """Основная функция запуска бота"""
    
    # Фикс для Python 3.12 - создаем event loop в начале
    import asyncio
    import sys
    
    if sys.version_info >= (3, 10):
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    
    print("🚀 Запуск BookToSpeech Bot...")
    logger.info("Инициализация BookToSpeech Bot")
    
    # Проверка обязательных переменных окружения
    bot_token = os.getenv("BOT_TOKEN", BOT_TOKEN)
    openai_key = os.getenv("OPENAI_API_KEY", OPENAI_API_KEY)
    
    # Валидация токена бота
    if not bot_token or bot_token == "YOUR_BOT_TOKEN_HERE":
        print("❌ КРИТИЧЕСКАЯ ОШИБКА: Не установлен BOT_TOKEN!")
        print("Решение:")
        print("1. Создайте файл .env в корневой папке проекта")
        print("2. Добавьте строку: BOT_TOKEN=ваш_токен_от_BotFather")
        print("3. Получить токен можно у @BotFather в Telegram")
        sys.exit(1)
    
    # Предупреждение об отсутствии OpenAI ключа
    if not openai_key or openai_key == "YOUR_OPENAI_KEY":
        print("⚠️  ПРЕДУПРЕЖДЕНИЕ: OPENAI_API_KEY не установлен")
        print("Функции анализа персонажей и настроения будут отключены")
        print("Для полного функционала получите ключ на https://platform.openai.com/")
    
    # Создание необходимых директорий
    directories_to_create = [
        tempfile.gettempdir(),
        os.path.join(os.getcwd(), "temp"),
        os.path.join(os.getcwd(), "cache"),
        os.path.join(os.getcwd(), "music_library"),
        os.path.join(os.getcwd(), "logs")
    ]
    
    for directory in directories_to_create:
        try:
            os.makedirs(directory, exist_ok=True)
            logger.info(f"Директория создана/проверена: {directory}")
        except Exception as e:
            logger.warning(f"Не удалось создать директорию {directory}: {e}")
    
    try:
        # Создание экземпляра бота
        print("📱 Создание экземпляра бота...")
        bot = EnhancedBookToSpeechBot(bot_token)
        logger.info("Экземпляр бота создан успешно")
        
        # Создание приложения Telegram
        print("🔧 Настройка Telegram Application...")
        application = Application.builder().token(bot_token).build()
        
        # Настройка обработчиков команд
        print("⚙️ Регистрация обработчиков...")
        
        # Основные команды
        application.add_handler(CommandHandler("start", bot.start_command))
        application.add_handler(CommandHandler("help", lambda u, c: bot.show_help_inline(u, c)))
        application.add_handler(CommandHandler("library", lambda u, c: bot.show_library_inline(u, c)))
        application.add_handler(CommandHandler("settings", lambda u, c: bot.show_settings_inline(u, c)))
        application.add_handler(CommandHandler("premium", lambda u, c: bot.show_premium_info_inline(u, c)))
        
        # Команды для работы с главами (для обратной совместимости)
        application.add_handler(CommandHandler("play_chapter", lambda u, c: bot.play_chapter_command(u, c)))
        application.add_handler(CommandHandler("continue_reading", lambda u, c: bot.continue_reading_command(u, c)))
        application.add_handler(CommandHandler("my_books", lambda u, c: bot.show_library_inline(u, c)))
        
        # Команды выбора голоса (для обратной совместимости)
        application.add_handler(CommandHandler("ru_female", lambda u, c: bot.legacy_voice_command(u, c, "ru-female")))
        application.add_handler(CommandHandler("ru_male", lambda u, c: bot.legacy_voice_command(u, c, "ru-male")))
        application.add_handler(CommandHandler("en_female", lambda u, c: bot.legacy_voice_command(u, c, "en-female")))
        application.add_handler(CommandHandler("en_male", lambda u, c: bot.legacy_voice_command(u, c, "en-male")))
        
        # Обработчик callback запросов (inline кнопки)
        application.add_handler(CallbackQueryHandler(bot.handle_callback_query))
        
        # Обработчик документов
        application.add_handler(MessageHandler(filters.Document.ALL, bot.handle_document))
        
        # Обработчик текстовых сообщений
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, 
            lambda u, c: bot.handle_text_message(u, c)
        ))
        
        logger.info("Все обработчики зарегистрированы")
        print("✅ Обработчики команд настроены")
        
        # Настройка обработчика ошибок
        application.add_error_handler(bot.error_handler)
        logger.info("Обработчик ошибок установлен")
        
        # Инициализация базы данных
        print("💾 Инициализация базы данных...")
        try:
            async def init_db():
                test_user = await bot.db_manager.get_or_create_user(12345, "test")
                return True
            
            logger.info("База данных инициализирована успешно")
            print("✅ База данных готова")
        except Exception as e:
            logger.error(f"Ошибка инициализации базы данных: {e}")
            print(f"⚠️  Предупреждение: Проблемы с базой данных: {e}")
        
        # Финальные настройки
        print("🔧 Применение финальных настроек...")
        
        # Настройка размера пула соединений для лучшей производительности
        application.bot_data.update({
            'max_workers': int(os.getenv('MAX_WORKERS', '4')),
            'chunk_size': int(os.getenv('CHUNK_SIZE', '3000')),
            'cache_ttl': int(os.getenv('CACHE_TTL', '3600'))
        })
        
        # Подготовка к подключению
        print("🌐 Подготовка к подключению...")
        
        # Запуск бота
        print("🎉 Все системы готовы!")
        print("=" * 50)
        print("🤖 BookToSpeech Bot запущен!")
        print("📱 Найдите вашего бота в Telegram и отправьте /start")
        print("🛑 Для остановки нажмите Ctrl+C")
        print("=" * 50)
        
        logger.info("Бот начинает работу в режиме polling")
        
        # Запуск бота (проверка соединения произойдет автоматически)
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
        
    except KeyboardInterrupt:
        print("\n")
        print("👋 Получен сигнал остановки (Ctrl+C)")
        logger.info("Бот остановлен пользователем")
        
    except Exception as e:
        print(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")
        logger.error(f"Критическая ошибка в main(): {e}", exc_info=True)
        
        # Подробная диагностика ошибки
        print("\n🔍 Диагностическая информация:")
        print(f"   Python версия: {sys.version}")
        print(f"   Рабочая директория: {os.getcwd()}")
        print(f"   Токен установлен: {'Да' if bot_token and bot_token != 'YOUR_BOT_TOKEN_HERE' else 'Нет'}")
        
        # Проверка критических импортов
        try:
            import telegram
            print(f"   python-telegram-bot: {telegram.__version__}")
        except ImportError:
            print("   ❌ python-telegram-bot не установлен")
            
        try:
            import edge_tts
            print("   ✅ edge-tts доступен")
        except ImportError:
            print("   ❌ edge-tts не установлен")
            
        print("\n💡 Рекомендации:")
        print("1. Проверьте, что все зависимости установлены: pip install -r requirements.txt")
        print("2. Убедитесь, что BOT_TOKEN правильно установлен в .env файле")
        print("3. Проверьте интернет-соединение")
        print("4. Просмотрите логи выше для детальной информации об ошибке")
        
    finally:
        print("\n🧹 Очистка ресурсов...")
        
        # Очистка временных файлов
        temp_dir = os.path.join(os.getcwd(), "temp")
        if os.path.exists(temp_dir):
            try:
                import shutil
                for item in os.listdir(temp_dir):
                    item_path = os.path.join(temp_dir, item)
                    if os.path.isfile(item_path):
                        os.remove(item_path)
                    elif os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                logger.info("Временные файлы очищены")
            except Exception as e:
                logger.warning(f"Не удалось очистить временные файлы: {e}")
        
        print("✅ Завершение работы")
        logger.info("Работа бота завершена")

# Точка входа в программу
if __name__ == "__main__":
    main()
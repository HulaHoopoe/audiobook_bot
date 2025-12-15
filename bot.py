#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import asyncio
import logging
import re
import json
import sqlite3
import tempfile
import zipfile
import hashlib
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict

import aiofiles
import edge_tts

from telegram import (
    Update,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv

# ================== КОНФИГУРАЦИЯ ==================

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
MAX_FILE_SIZE = 15 * 1024 * 1024  # 15MB по сценарию

VOICES = {
    "male": "ru-RU-DmitryNeural",   
    "female": "ru-RU-SvetlanaNeural" 
}

# ================== МОДЕЛИ ДАННЫХ ==================

@dataclass
class Chapter:
    number: int
    title: str
    text: str
    duration_seconds: int = 0

# ================== ЛОГИКА ОБРАБОТКИ ТЕКСТА ==================

class TextPreprocessor:
    def __init__(self):
        self.cleanup_patterns = [
            r'^\s*\d+\s*$',               
            r'Page\s+\d+',
            r'Страница\s+\d+',
            r'\[\d+\]', r'\(\d+\)',       
            r'ISBN\s*:?[\d\-]+',
            r'©\s*\d{4}',
            r'^\s*\*\s*\*\s*\*\s*$',      
        ]

    async def clean_text(self, text: str) -> str:
        lines = text.split("\n")
        cleaned_lines = []
        for line in lines:
            line = line.strip()
            if not line: continue
            
            is_junk = False
            if len(line) < 2 and not line.isdigit(): 
                 is_junk = True
            
            if not is_junk:
                cleaned_lines.append(line)

        text = "\n".join(cleaned_lines)
        text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
        return text.strip()

    def validate_tts_text(self, text: str) -> str:
        """Валидация и очистка текста для edge_tts"""
        if not text or len(text.strip()) < 10:
            return None  # Возвращаем None для пустого текста
        
        # Ограничиваем длину
        if len(text) > 15000:
            text = text[:15000]
        
        # Удаляем неподдерживаемые символы
        text = re.sub(r'[^\w\s\.\,\!\?\;\:\-\(\)\[\]\'\"]', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        
        # Минимум 20 символов
        if len(text) < 20:
            return None
            
        # Добавляем точку в конце если нет знака препинания
        if not re.search(r'[.!?]$', text):
            text += "."
            
        return text

    def analyze_chapters(self, text: str) -> List[Chapter]:
        chapters = []
        lines = text.split('\n')
        
        chapter_patterns = [
            r'^(?:Глава|ГЛАВА|Часть|ЧАСТЬ|Chapter|CHAPTER)\s*(\d+|[IVXLCDM]+)\.?\s*(.*)$',
            r'^(\d+)\.\s+(.+)$',
            r'^([IVXLCDM]+)\.\s+(.+)$'
        ]
        
        current_chapter_text = []
        current_title = "Вступление"
        chapter_num = 0
        
        for line in lines:
            line = line.strip()
            if not line: continue
            
            is_new_chapter = False
            new_title = ""
            
            if len(line) < 100:
                for pattern in chapter_patterns:
                    match = re.match(pattern, line, re.IGNORECASE)
                    if match:
                        is_new_chapter = True
                        part1 = match.group(1)
                        part2 = match.group(2) if len(match.groups()) > 1 else ""
                        new_title = f"Глава {part1}. {part2}".strip().strip('.')
                        break
            
            if is_new_chapter:
                if current_chapter_text and len("".join(current_chapter_text)) > 50:
                    full_text = "\n".join(current_chapter_text)
                    chapters.append(Chapter(
                        number=chapter_num,
                        title=current_title,
                        text=full_text,
                        duration_seconds=len(full_text.split()) // 2
                    ))
                
                chapter_num += 1
                current_title = new_title if new_title else f"Глава {chapter_num}"
                current_chapter_text = [line]
            else:
                current_chapter_text.append(line)
                
        # Последняя глава
        if current_chapter_text and len("".join(current_chapter_text)) > 50:
            full_text = "\n".join(current_chapter_text)
            chapters.append(Chapter(
                number=chapter_num,
                title=current_title,
                text=full_text,
                duration_seconds=len(full_text.split()) // 2
            ))
            
        # Искусственное разбиение если глав мало
        if len(chapters) < 2:
            words = text.split()
            chunk_size = max(100, len(words) // 5)  # Минимум 100 слов
            for i in range(0, len(words), chunk_size):
                chunk_words = words[i:i+chunk_size]
                if len(chunk_words) > 50:
                    chapters.append(Chapter(
                        number=(i//chunk_size) + 1,
                        title=f"Часть {(i//chunk_size) + 1}",
                        text=" ".join(chunk_words),
                        duration_seconds=len(chunk_words) // 2
                    ))
                    
        return chapters[:20]

# ================== БАЗА ДАННЫХ ==================

class DatabaseManager:
    def __init__(self, db_path: str = "bookvoice.db"):
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                voice_preference TEXT DEFAULT 'male',
                last_book_filename TEXT,
                last_book_hash TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                book_title TEXT,
                chapter_title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        conn.close()

    def get_user(self, telegram_id: int):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "telegram_id": row[0],
                "username": row[1],
                "voice": row[2],
                "last_book_filename": row[3],
                "last_book_hash": row[4]
            }
        return None

    def create_or_update_user(self, telegram_id: int, username: str, **kwargs):
        user = self.get_user(telegram_id)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if not user:
            cursor.execute("INSERT INTO users (telegram_id, username) VALUES (?, ?)", 
                          (telegram_id, username))
        
        if kwargs:
            set_clause = ", ".join([f"{k} = ?" for k in kwargs.keys()])
            values = list(kwargs.values()) + [telegram_id]
            cursor.execute(f"UPDATE users SET {set_clause} WHERE telegram_id = ?", values)
            
        conn.commit()
        conn.close()

    def add_history(self, user_id: int, book_title: str, chapter_title: str):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO history (user_id, book_title, chapter_title) VALUES (?, ?, ?)",
                      (user_id, book_title, chapter_title))
        conn.commit()
        conn.close()

    def get_history(self, user_id: int, limit: int = 10):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT created_at, book_title, chapter_title 
            FROM history 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT ?
        """, (user_id, limit))
        rows = cursor.fetchall()
        conn.close()
        return rows

# ================== КЛАВИАТУРЫ ==================

class KeyboardManager:
    @staticmethod
    def get_voice_selection() -> ReplyKeyboardMarkup:
        keyboard = [
            [KeyboardButton("Мужской"), KeyboardButton("Женский")]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

    @staticmethod
    def get_main_menu(last_book_filename: str = None) -> ReplyKeyboardMarkup:
        keyboard = []
        if last_book_filename:
            keyboard.append([KeyboardButton("🎧 Озвучить главу из последней книги")])
        keyboard.append([KeyboardButton("📚 Загрузить новую книгу")])
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    @staticmethod
    def get_chapters_inline(chapters_data: List[Dict], page: int = 0) -> InlineKeyboardMarkup:
        """Принимает List[Dict], а не List[Chapter]"""
        keyboard = []
        per_page = 4
        start = page * per_page
        end = min(start + per_page, len(chapters_data))
        
        for i in range(start, end):
            ch_data = chapters_data[i]
            ch_num = ch_data.get('number', i+1)
            ch_title = ch_data['title'][:25]
            duration = max(1, ch_data.get('duration_seconds', 60) // 60)
            btn_text = f"{ch_num}. {ch_title} ({duration}м)"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"play_{i}")])
            
        # Навигация
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"page_{page-1}"))
        if end < len(chapters_data):
            nav.append(InlineKeyboardButton("➡️", callback_data=f"page_{page+1}"))
        if nav:
            keyboard.append(nav)
            
        return InlineKeyboardMarkup(keyboard)

# ================== БОТ ==================

class BookVoiceBot:
    def __init__(self, token: str):
        self.temp_dir = tempfile.gettempdir()
        self.preprocessor = TextPreprocessor()
        self.db = DatabaseManager()
        self.kb = KeyboardManager()

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        username = update.effective_user.username
        user = self.db.get_user(user_id)

        if not user:
            self.db.create_or_update_user(user_id, username)
            await update.message.reply_text(
                "Привет! 👋 Я BookVoice — превращаю книги в аудио.\n"
                "Загружай txt, epub или fb2 до 15 МБ, и я сделаю из них удобный MP3-файл.\n\n"
                "Выбери тип голоса:",
                reply_markup=self.kb.get_voice_selection(),
                parse_mode="Markdown"
            )
            context.user_data['state'] = 'WAITING_VOICE'
            return

        voice_label = "Мужской RU" if user['voice'] == 'male' else "Женский RU"
        
        text = (
            f"Привет! 👋 Рад видеть вас снова в BookVoice.\n"
            f"💡 Ваш текущий голос: {voice_label}\n"
        )
        
        if user['last_book_filename']:
            text += f"📚 Последняя загруженная книга: {user['last_book_filename']}"

        await update.message.reply_text(
            text,
            reply_markup=self.kb.get_main_menu(user['last_book_filename']),
            parse_mode="Markdown"
        )
        context.user_data['state'] = 'MAIN_MENU'

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        user_id = update.effective_user.id
        state = context.user_data.get('state')

        if text in ["Мужской", "Женский"]:
            voice_code = "male" if text == "Мужской" else "female"
            self.db.create_or_update_user(user_id, update.effective_user.username, voice_preference=voice_code)
            
            voice_label = "Мужской RU" if text == "Мужской" else "Женский RU"
            
            if state == 'CHANGING_VOICE':
                await update.message.reply_text(
                    f"Выбран голос: {voice_label}.\nТеперь любые последующие книги будут озвучиваться этим голосом.",
                    reply_markup=self.kb.get_main_menu(context.user_data.get('last_filename'))
                )
                context.user_data['state'] = 'MAIN_MENU'
                return

            await update.message.reply_text(
                f"Выбран голос: {voice_label}.\nТеперь отправь файл книги — txt, epub или fb2 до 15 МБ."
            )
            context.user_data['state'] = 'WAITING_FILE'
            return

        if text == "📚 Загрузить новую книгу":
            await update.message.reply_text(
                "Теперь отправь файл книги — txt, epub или fb2 до 15 МБ."
            )
            context.user_data['state'] = 'WAITING_FILE'
            return

        if text == "🎧 Озвучить главу из последней книги":
            chapters_data = context.user_data.get('chapters', [])
            if not chapters_data:
                await update.message.reply_text("⚠️ Данные книги устарели. Пожалуйста, загрузите книгу заново.")
                return
            
            filename = context.user_data.get('book_title', 'Книга')
            
            await update.message.reply_text(
                f"Найдено {len(chapters_data)} глав в книге {filename}\n"
                "Выберите главу для озвучивания:",
                reply_markup=self.kb.get_chapters_inline(chapters_data),
                parse_mode="Markdown"
            )
            return

        await update.message.reply_text(
            "Доступные команды:\n\n"
            "• /start — Озвучить книгу\n"
            "• /history — Просмотреть список ранее озвученных книг\n"
            "• /change_voice — Сменить текущий голос"
        )

    async def change_voice_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Выберите голос:",
            reply_markup=self.kb.get_voice_selection()
        )
        context.user_data['state'] = 'CHANGING_VOICE'

    async def history_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        history = self.db.get_history(update.effective_user.id)
        if not history:
            await update.message.reply_text("История пуста.")
            return
            
        text = "Ваши озвучки:\n\n"
        for row in history:
            date_str = datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S').strftime('%d.%m.%Y %H:%M')
            text += f"• {date_str} - *{row[1]}.* {row[2]}\n"
            
        await update.message.reply_text(text, parse_mode="Markdown")

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        doc = update.message.document
        
        ext = os.path.splitext(doc.file_name)[1].lower()
        if ext not in ['.txt', '.epub', '.fb2']:
            await update.message.reply_text(
                "⚠️ Я пока принимаю только txt, epub и fb2.\n"
                "Попробуйте конвертировать файл в поддерживаемый формат."
            )
            return

        if doc.file_size > MAX_FILE_SIZE:
            await update.message.reply_text(
                "⚠️ Файл слишком большой — максимум 15 МБ.\n"
                "Сожмите файл или отправьте книгу частями."
            )
            return

        msg = await update.message.reply_text(
            f"📚 *{doc.file_name}* получен.\n⏳ Извлекаю текст...", 
            parse_mode="Markdown"
        )
        
        try:
            file_path = os.path.join(self.temp_dir, doc.file_name)
            remote_file = await context.bot.get_file(doc.file_id)
            await remote_file.download_to_drive(file_path)
            
            raw_text = await self._extract_text(file_path, ext)
            if not raw_text:
                await msg.edit_text("❌ Не удалось прочитать текст из файла.")
                if os.path.exists(file_path):
                    os.remove(file_path)
                return
            
            clean_text = await self.preprocessor.clean_text(raw_text)
            chapters = self.preprocessor.analyze_chapters(clean_text)
            
            context.user_data['chapters'] = [asdict(c) for c in chapters]
            context.user_data['book_title'] = doc.file_name
            
            self.db.create_or_update_user(
                update.effective_user.id, 
                update.effective_user.username,
                last_book_filename=doc.file_name
            )
            
            if os.path.exists(file_path):
                os.remove(file_path)

            await msg.edit_text(
                f"Текст успешно извлечён!\nНайдено **{len(chapters)} глав**.\n\n"
                f"🎯 **Выберите одну главу для озвучивания:**",
                parse_mode="Markdown",
                reply_markup=self.kb.get_chapters_inline(context.user_data['chapters'])
            )
            
        except Exception as e:
            logger.error(f"Error processing file: {e}")
            await msg.edit_text("❌ Произошла ошибка при обработке файла.")

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        data = query.data
        chapters_data = context.user_data.get('chapters', [])

        if not chapters_data:
            await query.edit_message_text("⚠️ Нет данных о книге. Загрузите файл заново.")
            return

        if data.startswith("page_"):
            page = int(data.split("_")[1])
            await query.edit_message_reply_markup(
                reply_markup=self.kb.get_chapters_inline(chapters_data, page)
            )
            return

        if data.startswith("play_"):
            idx = int(data.split("_")[1])
            if idx >= len(chapters_data):
                await query.answer("Глава не найдена")
                return

            ch_data = chapters_data[idx]
            chapter_title = ch_data['title']
            chapter_text = ch_data['text']

            user = self.db.get_user(query.from_user.id)
            voice_code = user['voice'] if user else 'male'
            voice = VOICES.get(voice_code, VOICES['male'])

            # Валидация текста
            tts_text = self.preprocessor.validate_tts_text(chapter_text)
            if not tts_text:
                await query.edit_message_text("❌ Глава слишком короткая для озвучивания.")
                return

            await query.edit_message_text(
                f"🎧 Начинаю озвучивать **{chapter_title[:50]}**...\n"
                f"⏳ Подключение к серверу...",
                parse_mode="Markdown"
            )

            audio_path = os.path.join(self.temp_dir, f"chapter_{idx}.mp3")

            try:
                communicate = edge_tts.Communicate(tts_text, voice)
                
                # === НАЧАЛО БЛОКА С ПРОГРЕСС БАРОМ ===
                file_size = 0
                last_update_time = 0
                update_interval = 2.0  # Интервал обновления в секундах
                
                # Визуальные фазы прогресс-бара
                bars = [
                    "⬜⬜⬜⬜⬜", "⬛⬜⬜⬜⬜", "⬛⬛⬜⬜⬜", 
                    "⬛⬛⬛⬜⬜", "⬛⬛⬛⬛⬜", "⬛⬛⬛⬛⬛"
                ]
                bar_step = 0

                with open(audio_path, "wb") as f:
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":
                            data_chunk = chunk["data"]
                            f.write(data_chunk)
                            file_size += len(data_chunk)
                            
                            current_time = asyncio.get_running_loop().time()
                            if current_time - last_update_time > update_interval:
                                last_update_time = current_time
                                size_mb = file_size / (1024 * 1024)
                                bar_visual = bars[bar_step % len(bars)]
                                bar_step += 1
                                
                                try:
                                    await query.edit_message_text(
                                        f"🎧 Озвучиваю: **{chapter_title[:50]}**\n"
                                        f"{bar_visual} ({size_mb:.2f} MB)\n"
                                        f"⏳ Генерация аудио...",
                                        parse_mode="Markdown"
                                    )
                                except Exception:
                                    pass # Игнорируем ошибки сети при редактировании
                # === КОНЕЦ БЛОКА С ПРОГРЕСС БАРОМ ===

                book_title = context.user_data.get('book_title', 'Книга')
                with open(audio_path, 'rb') as audio:
                    await query.message.reply_audio(
                        audio=audio,
                        title=chapter_title[:90],
                        performer="BookVoice",
                        caption=f"Готово! 🙌 Вот ваша озвучка:\n\n*{book_title}*\n{chapter_title}",
                        parse_mode="Markdown",
                        reply_markup=self.kb.get_main_menu(book_title)
                    )

                self.db.add_history(query.from_user.id, book_title, chapter_title)

            except edge_tts.exceptions.NoAudioReceived:
                await query.edit_message_text(
                    "❌ Ошибка озвучивания. Попробуйте:\n"
                    "• Другую главу\n"
                    "• Сменить голос (/change_voice)"
                )
            except Exception as e:
                logger.error(f"TTS Error: {e}")
                await query.edit_message_text("❌ Ошибка генерации аудио.")
            finally:
                if os.path.exists(audio_path):
                    os.remove(audio_path)


    async def _extract_text(self, path: str, ext: str) -> str:
        try:
            if ext == '.txt':
                async with aiofiles.open(path, 'r', encoding='utf-8') as f:
                    return await f.read()
            elif ext == '.epub':
                import ebooklib
                from ebooklib import epub
                from bs4 import BeautifulSoup
                book = epub.read_epub(path)
                texts = []
                for item in book.get_items():
                    if item.get_type() == ebooklib.ITEM_DOCUMENT:
                        soup = BeautifulSoup(item.get_content(), 'html.parser')
                        texts.append(soup.get_text())
                return "\n\n".join(texts)
            elif ext == '.fb2':
                import xml.etree.ElementTree as ET
                tree = ET.parse(path)
                root = tree.getroot()
                ns = {'fb2': 'http://www.gribuser.ru/xml/fictionbook/2.0'}
                texts = []
                for p in root.findall('.//fb2:p', ns):
                    if p.text:
                        texts.append(p.text)
                return "\n".join(texts)
        except Exception as e:
            logger.error(f"Extract error: {e}")
        return ""

def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Не задан BOT_TOKEN")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    bot = BookVoiceBot(BOT_TOKEN)

    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("change_voice", bot.change_voice_command))
    app.add_handler(CommandHandler("history", bot.history_command))
    
    app.add_handler(MessageHandler(filters.Document.ALL, bot.handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_text))
    app.add_handler(CallbackQueryHandler(bot.handle_callback))

    print("BookVoice Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()

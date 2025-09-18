import os
import asyncio
import logging
from typing import Optional
import aiofiles
import edge_tts
from telegram import Update, Document
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ChatAction
import tempfile
import zipfile


# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Токен бота (получить у @BotFather)
BOT_TOKEN = "7987974646:AAEgXBUrbk0_lNILR_nV6mDdWI53DjEsR4A"

# Максимальный размер файла (50 МБ в байтах)
MAX_FILE_SIZE = 50 * 1024 * 1024

# Поддерживаемые голоса с интонацией
VOICES = {
    "ru-female": "ru-RU-SvetlanaNeural",
    "ru-male": "ru-RU-DmitryNeural", 
    "en-female": "en-US-AriaNeural",
    "en-male": "en-US-GuyNeural"
}


class BookToSpeechBot:
    def __init__(self, token: str):
        self.token = token
        self.temp_dir = tempfile.gettempdir()
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        welcome_text = """
🎧 **Добро пожаловать в BookToSpeech Bot!**

Я могу озвучить вашу книгу с естественной интонацией.

📖 **Как пользоваться:**
1. Отправьте мне текстовый файл книги (.txt, .epub, .pdf)
2. Выберите голос для озвучки
3. Получите аудиоверсию книги

🎙️ **Доступные голоса:**
• Русский женский (Светлана)
• Русский мужской (Дмитрий)
• Английский женский (Ария)
• Английский мужской (Гай)

**Команды:**
/start - начать работу
/help - справка
/voices - список голосов

Отправьте файл книги для начала!
        """
        await update.message.reply_text(welcome_text, parse_mode='Markdown')
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /help"""
        help_text = """
📚 **Справка по использованию бота**

**Поддерживаемые форматы файлов:**
• .txt - обычные текстовые файлы
• .epub - электронные книги
• .pdf - PDF документы

**Ограничения:**
• Максимальный размер файла: 50 МБ
• Время обработки зависит от размера текста

**Качество озвучки:**
• Естественная интонация
• Автоматическая расстановка пауз
• Правильное произношение

Просто отправьте файл и следуйте инструкциям!
        """
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    async def voices_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показать доступные голоса"""
        voices_text = """
🎙️ **Доступные голоса:**

🇷🇺 **Русские голоса:**
• Светлана (женский) - естественный, выразительный
• Дмитрий (мужской) - четкий, приятный

🇺🇸 **Английские голоса:**
• Ария (женский) - мелодичный, эмоциональный
• Гай (мужской) - глубокий, профессиональный

Все голоса поддерживают естественную интонацию и эмоциональную окраску речи.
        """
        await update.message.reply_text(voices_text, parse_mode='Markdown')
    
    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка загруженного документа"""
        document: Document = update.message.document
        
        # Проверка размера файла
        if document.file_size > MAX_FILE_SIZE:
            await update.message.reply_text(
                f"❌ Файл слишком большой! Максимальный размер: {MAX_FILE_SIZE // (1024*1024)} МБ"
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
            # Скачивание файла
            file = await context.bot.get_file(document.file_id)
            file_path = os.path.join(self.temp_dir, document.file_name)
            await file.download_to_drive(file_path)
            
            # Извлечение текста
            text = await self._extract_text(file_path)
            
            if not text.strip():
                await update.message.reply_text("❌ Не удалось извлечь текст из файла")
                return
            
            # Сохранение текста в контексте пользователя
            context.user_data['text'] = text
            context.user_data['filename'] = document.file_name
            
            # Предложение выбора голоса
            keyboard_text = """
📖 Файл успешно обработан!
📊 Извлечено символов: {}

Выберите голос для озвучки:
• /ru_female - Светлана (русский женский)
• /ru_male - Дмитрий (русский мужской)
• /en_female - Ария (английский женский)
• /en_male - Гай (английский мужской)
            """.format(len(text))
            
            await update.message.reply_text(keyboard_text)
            
        except Exception as e:
            logger.error(f"Ошибка обработки файла: {e}")
            await update.message.reply_text(
                "❌ Произошла ошибка при обработке файла. Попробуйте еще раз."
            )
        finally:
            # Очистка временного файла
            if os.path.exists(file_path):
                os.remove(file_path)
    
    async def generate_audio(self, update: Update, context: ContextTypes.DEFAULT_TYPE, voice_key: str):
        """Генерация аудио с выбранным голосом"""
        if 'text' not in context.user_data:
            await update.message.reply_text(
                "❌ Сначала отправьте файл книги для обработки!"
            )
            return
        
        text = context.user_data['text']
        filename = context.user_data.get('filename', 'book')
        voice = VOICES[voice_key]
        
        await update.message.reply_text("🎙️ Начинаю озвучку... Это может занять некоторое время.")
        await update.message.reply_chat_action(ChatAction.RECORD_VOICE)
        
        try:
            # Разбивка текста на части (Edge TTS имеет ограничения)
            chunks = self._split_text(text, max_length=3000)
            audio_files = []
            
            for i, chunk in enumerate(chunks):
                chunk_file = os.path.join(self.temp_dir, f"chunk_{i}.mp3")
                
                # Создание аудио с интонацией
                communicate = edge_tts.Communicate(chunk, voice)
                await communicate.save(chunk_file)
                audio_files.append(chunk_file)
                
                # Обновление прогресса
                progress = (i + 1) / len(chunks) * 100
                if i % 10 == 0:  # Обновляем каждые 10 частей
                    await update.message.reply_text(f"⏳ Прогресс: {progress:.1f}%")
            
            # Объединение аудиофайлов
            output_file = os.path.join(self.temp_dir, f"{filename}_audiobook.mp3")
            await self._merge_audio_files(audio_files, output_file)
            
            # Отправка аудио
            await self._send_audio_file(update, output_file, filename)
            
            # Очистка временных файлов
            for file_path in audio_files + [output_file]:
                if os.path.exists(file_path):
                    os.remove(file_path)
            
            await update.message.reply_text("✅ Аудиокнига готова!")
            
        except Exception as e:
            logger.error(f"Ошибка генерации аудио: {e}")
            await update.message.reply_text(
                "❌ Произошла ошибка при генерации аудио. Попробуйте еще раз."
            )
    
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
                # Для EPUB потребуется библиотека ebooklib
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
                # Для PDF потребуется библиотека PyPDF2
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
        """Объединение аудиофайлов (простая конкатенация)"""
        try:
            from pydub import AudioSegment
            
            combined = AudioSegment.empty()
            for audio_file in audio_files:
                audio = AudioSegment.from_mp3(audio_file)
                combined += audio
            
            combined.export(output_file, format="mp3")
        except ImportError:
            # Если pydub недоступен, просто используем первый файл
            if audio_files:
                os.rename(audio_files[0], output_file)
    
    async def _send_audio_file(self, update: Update, file_path: str, original_filename: str):
        """Отправка аудиофайла пользователю"""
        file_size = os.path.getsize(file_path)
        
        if file_size > 50 * 1024 * 1024:  # Telegram limit 50MB
            # Создание архива для больших файлов
            zip_path = file_path.replace('.mp3', '.zip')
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                zipf.write(file_path, f"{original_filename}_audiobook.mp3")
            
            with open(zip_path, 'rb') as f:
                await update.message.reply_document(
                    document=f,
                    filename=f"{original_filename}_audiobook.zip",
                    caption="📦 Аудиокнига (архив)"
                )
            os.remove(zip_path)
        else:
            with open(file_path, 'rb') as f:
                await update.message.reply_audio(
                    audio=f,
                    filename=f"{original_filename}_audiobook.mp3",
                    title=f"Аудиокнига: {original_filename}"
                    # title=f"Без названия"
                )

def main():
    """Запуск бота"""
    bot = BookToSpeechBot(BOT_TOKEN)
    
    # Создание приложения
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", bot.start_command))
    application.add_handler(CommandHandler("help", bot.help_command))
    application.add_handler(CommandHandler("voices", bot.voices_command))
    
    # Обработчики голосов
    application.add_handler(CommandHandler("ru_female", 
        lambda u, c: bot.generate_audio(u, c, "ru-female")))
    application.add_handler(CommandHandler("ru_male", 
        lambda u, c: bot.generate_audio(u, c, "ru-male")))
    application.add_handler(CommandHandler("en_female", 
        lambda u, c: bot.generate_audio(u, c, "en-female")))
    application.add_handler(CommandHandler("en_male", 
        lambda u, c: bot.generate_audio(u, c, "en-male")))
    
    # Обработчик документов
    application.add_handler(MessageHandler(filters.Document.ALL, bot.handle_document))
    
    # Запуск бота
    print("🤖 Бот запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
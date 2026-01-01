import os
import asyncio
import logging
import re
import glob
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.client.default import DefaultBotProperties
import yt_dlp

# --- ТВОЙ ТОКЕН ---
TOKEN = "8250742177:AAGOPppYA5PALhoNwZsfoa_uLdQcE3m3Ktc"
# ------------------

# Настройка логирования
logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# Хранилище данных
user_data = {}
progress_storage = {}
pending_files = {} # Для файлов, которые ждут нарезки

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def clean_filename(title):
    # Убираем плохие символы, чтобы не было ошибок файловой системы
    clean = re.sub(r'[^\w\s\-\.]', '', str(title))
    return clean.strip()[:50]

def get_ffmpeg_location():
    # Умный поиск FFmpeg
    if os.path.exists("ffmpeg.exe"):
        return os.getcwd() # Windows (локально)
    return None # Linux/Docker (системный путь)

# --- КЛАВИАТУРЫ ---

def get_quality_keyboard(url):
    buttons = [
        [
            InlineKeyboardButton(text="💎 1080p / Max", callback_data="quality_1080"),
            InlineKeyboardButton(text="💿 720p", callback_data="quality_720")
        ],
        [
            InlineKeyboardButton(text="📼 480p", callback_data="quality_480"),
            InlineKeyboardButton(text="🎵 Аудио (MP3)", callback_data="quality_audio")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_split_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="✂️ Разрезать и отправить", callback_data="split_yes"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="split_cancel")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- ПРОГРЕСС БАР И ХУКИ ---

def make_progress_hook(chat_id):
    def hook(d):
        if d['status'] == 'downloading':
            try:
                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                percent = 0
                if total:
                    percent = (downloaded / total) * 100
                mb = downloaded / 1024 / 1024
                progress_storage[chat_id] = {"percent": percent, "mb": mb, "status": "downloading"}
            except: pass
        elif d['status'] == 'finished':
            progress_storage[chat_id] = {"status": "finished", "percent": 100}
    return hook

async def progress_tracker_task(chat_id, message_id):
    last_text = ""
    while True:
        await asyncio.sleep(1.5)
        data = progress_storage.get(chat_id)
        if not data: continue
        
        if data.get("status") == "finished":
            # Финальное обновление перед удалением трекера
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="⚙️ <b>Скачано! Обработка...</b>")
            except: pass
            break

        percent = data.get("percent", 0)
        mb = data.get("mb", 0)
        
        text = "⏳ <b>Скачивание...</b>\n"
        if percent > 0:
            filled = int(10 * percent // 100)
            bar = '█' * filled + '░' * (10 - filled)
            text += f"[{bar}] {percent:.1f}%"
        elif mb > 0:
            text += f"📥 Скачано: {mb:.1f} MB"
        else:
            text += "🚀 Подключение..."

        if text != last_text:
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
                last_text = text
            except TelegramRetryAfter:
                pass # Просто пропускаем цикл
            except TelegramBadRequest:
                pass # Сообщение не изменилось или удалено
            except Exception:
                break

# --- ЛОГИКА НАРЕЗКИ (CUTTER) ---

async def split_and_send(chat_id, file_path, status_msg):
    try:
        await status_msg.edit_text("🔪 <b>Нарезаю видео на куски по 3 минуты...</b>")
        
        base_name = os.path.splitext(file_path)[0]
        output_pattern = f"{base_name}_part%03d.mp4"
        
        # Команда FFmpeg (работает и на Windows, и на Linux)
        cmd = f'ffmpeg -i "{file_path}" -c copy -map 0 -segment_time 180 -f segment -reset_timestamps 1 "{output_pattern}"'
        
        process = await asyncio.create_subprocess_shell(cmd)
        await process.communicate()

        # Собираем куски
        search_pattern = f"{base_name}_part*.mp4"
        parts = sorted(glob.glob(search_pattern))

        if not parts:
            await status_msg.edit_text("⚠️ Ошибка: не удалось нарезать файл.")
            return

        await status_msg.edit_text(f"📦 Получилось частей: {len(parts)}. Отправляю по очереди...")

        for i, part in enumerate(parts):
            part_size = os.path.getsize(part) / (1024 * 1024)
            caption = f"📹 <b>Часть {i+1} из {len(parts)}</b>"
            
            if part_size > 49.5:
                caption += "\n⚠️ (Кусок всё ещё >50МБ, может не пройти)"

            try:
                input_part = FSInputFile(part)
                await bot.send_video(chat_id, input_part, caption=caption)
                await asyncio.sleep(1) # Пауза чтобы не спамить
            except Exception as e:
                await bot.send_message(chat_id, f"⚠️ Не удалось отправить часть {i+1}: {e}")
            finally:
                if os.path.exists(part): os.remove(part)

        await status_msg.delete()
        
    except Exception as e:
        logging.error(e)
        await status_msg.edit_text(f"⚠️ Ошибка при нарезке: {e}")
    
    finally:
        # Удаляем оригинал и чистим память
        if os.path.exists(file_path): os.remove(file_path)
        pending_files.pop(chat_id, None)

# --- ОБРАБОТЧИКИ (HANDLERS) ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("👋 <b>Привет!</b>\nКидай ссылку на TikTok, YouTube или SoundCloud.")

@dp.message(F.text)
async def process_link(message: types.Message):
    url = message.text.strip()
    if not ("http" in url): return
    user_data[message.from_user.id] = url
    await message.answer("🔎 Ссылка принята!", reply_markup=get_quality_keyboard(url))

# --- ОБРАБОТКА НАРЕЗКИ ---
@dp.callback_query(F.data == "split_yes")
async def process_split_yes(callback: CallbackQuery):
    file_path = pending_files.get(callback.message.chat.id)
    if not file_path or not os.path.exists(file_path):
        await callback.message.edit_text("❌ Файл уже удален. Попробуй скачать заново.")
        return
    await split_and_send(callback.message.chat.id, file_path, callback.message)

@dp.callback_query(F.data == "split_cancel")
async def process_split_cancel(callback: CallbackQuery):
    file_path = pending_files.get(callback.message.chat.id)
    if file_path and os.path.exists(file_path):
        os.remove(file_path)
    pending_files.pop(callback.message.chat.id, None)
    await callback.message.edit_text("❌ Отменено. Файл удален.")

# --- ОСНОВНАЯ ЛОГИКА СКАЧИВАНИЯ ---
@dp.callback_query(F.data.startswith("quality_"))
async def process_quality(callback: CallbackQuery):
    quality = callback.data.split("_")[1]
    user_id = callback.from_user.id
    url = user_data.get(user_id)

    if not url:
        await callback.message.edit_text("❌ Ссылка устарела.")
        return

    # Подготовка
    progress_storage[user_id] = {}
    temp_name_tmpl = f'downloads/{user_id}_temp_%(id)s.%(ext)s'
    ffmpeg_loc = get_ffmpeg_location() # <--- УНИВЕРСАЛЬНЫЙ ПУТЬ
    
    opts = {
        'outtmpl': temp_name_tmpl,
        'noplaylist': True,
        'progress_hooks': [make_progress_hook(user_id)],
        'ffmpeg_location': ffmpeg_loc, 
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://www.tiktok.com/'
        }
    }

    if quality == 'audio':
        opts['format'] = 'bestaudio/best'
        opts['postprocessors'] = [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '192'}]
    else:
        opts['merge_output_format'] = 'mp4'
        if quality == '1080': opts['format'] = 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best'
        elif quality == '720': opts['format'] = 'bestvideo[height<=720]+bestaudio/best[height<=720]/best'
        elif quality == '480': opts['format'] = 'bestvideo[height<=480]+bestaudio/best[height<=480]/best'

    status_msg = await callback.message.edit_text("⏳ <b>Инициализация...</b>")
    tracker_task = asyncio.create_task(progress_tracker_task(callback.message.chat.id, status_msg.message_id))

    downloaded_file = None
    final_path = None

    try:
        loop = asyncio.get_event_loop()
        
        # Получаем название и качаем
        video_title = "video"
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
                video_title = info.get('title', 'video')
            except: pass
            
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))
            downloaded_file = ydl.prepare_filename(info)

        # Переименование в красивый вид
        base, _ = os.path.splitext(downloaded_file)
        target_ext = ".mp3" if quality == 'audio' else ".mp4"
        final_name = f"{clean_filename(video_title)}{target_ext}"
        
        # Поиск реального файла
        possible_files = [base + target_ext, downloaded_file]
        real_file = None
        for p in possible_files:
            if os.path.exists(p):
                real_file = p
                break
        
        if not real_file: raise Exception("Файл не найден после скачивания")

        final_path = os.path.join('downloads', final_name)
        if os.path.exists(final_path): os.remove(final_path)
        os.rename(real_file, final_path)

        # ПРОВЕРКА РАЗМЕРА
        file_size_mb = os.path.getsize(final_path) / (1024 * 1024)
        
        if file_size_mb > 49.5:
            # Если большой - предлагаем резать
            pending_files[user_id] = final_path
            await status_msg.edit_text(
                f"⚠️ <b>Файл весит {file_size_mb:.1f} МБ!</b>\n"
                f"Телеграм принимает до 50 МБ.\n\n"
                f"✂️ Разрезать на части по 3 минуты?",
                reply_markup=get_split_keyboard()
            )
            # Не удаляем файл, он ждет решения!
        else:
            # Если маленький - отправляем
            await status_msg.edit_text("📤 <b>Загрузка в Telegram...</b>")
            input_file = FSInputFile(final_path)
            
            if quality == 'audio':
                await callback.message.answer_audio(input_file, caption=f"🎧 {final_name}")
            else:
                await callback.message.answer_video(input_file, caption=f"📹 {final_name}")
            
            await status_msg.delete()
            if os.path.exists(final_path): os.remove(final_path)

    except Exception as e:
        if "Entity Too Large" in str(e):
             await status_msg.edit_text("⚠️ Ошибка размера файла (Entity Too Large).")
        else:
             logging.error(e)
             await status_msg.edit_text(f"⚠️ Ошибка: {e}")
             # Если ошибка, удаляем файл, чтобы не занимал место
             if final_path and os.path.exists(final_path): os.remove(final_path)
            
    finally:
        tracker_task.cancel()
        # Чистка временных файлов
        if downloaded_file and os.path.exists(downloaded_file):
            try: os.remove(downloaded_file)
            except: pass
        # final_path чистим только если он НЕ в списке ожидания нарезки
        if final_path and os.path.exists(final_path) and user_id not in pending_files:
            try: os.remove(final_path)
            except: pass

async def main():
    if not os.path.exists('downloads'): os.makedirs('downloads')
    print("✅ БОТ ГОТОВ! (Universal Version v12.0)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

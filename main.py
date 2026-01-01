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

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

user_data = {}
progress_storage = {}
pending_files = {}

# --- ФУНКЦИИ ---

def clean_filename(title):
    clean = re.sub(r'[^\w\s\-\.]', '', str(title))
    return clean.strip()[:50]

def get_ffmpeg_location():
    if os.path.exists("ffmpeg.exe"):
        return os.getcwd()
    return None

def get_cookies_location():
    # Проверяем, есть ли файл cookies.txt рядом
    if os.path.exists("cookies.txt"):
        return "cookies.txt"
    return None

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

# --- ПРОГРЕСС ---

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
            except TelegramRetryAfter: pass
            except TelegramBadRequest: pass
            except Exception: break

# --- НАРЕЗКА ---

async def split_and_send(chat_id, file_path, status_msg):
    try:
        await status_msg.edit_text("🔪 <b>Нарезаю видео...</b>")
        base_name = os.path.splitext(file_path)[0]
        output_pattern = f"{base_name}_part%03d.mp4"
        cmd = f'ffmpeg -i "{file_path}" -c copy -map 0 -segment_time 180 -f segment -reset_timestamps 1 "{output_pattern}"'
        process = await asyncio.create_subprocess_shell(cmd)
        await process.communicate()

        parts = sorted(glob.glob(f"{base_name}_part*.mp4"))
        if not parts:
            await status_msg.edit_text("⚠️ Ошибка нарезки.")
            return

        await status_msg.edit_text(f"📦 Частей: {len(parts)}. Отправляю...")

        for i, part in enumerate(parts):
            caption = f"📹 <b>Часть {i+1}/{len(parts)}</b>"
            try:
                input_part = FSInputFile(part)
                await bot.send_video(chat_id, input_part, caption=caption)
                await asyncio.sleep(1)
            except Exception as e:
                await bot.send_message(chat_id, f"⚠️ Ошибка отправки: {e}")
            finally:
                if os.path.exists(part): os.remove(part)

        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"⚠️ Ошибка: {e}")
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        pending_files.pop(chat_id, None)

# --- HANDLERS ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("👋 <b>Бот работает!</b> (v13.0 + Cookies)")

@dp.message(F.text)
async def process_link(message: types.Message):
    url = message.text.strip()
    if not ("http" in url): return
    user_data[message.from_user.id] = url
    await message.answer("🔎 Ссылка принята!", reply_markup=get_quality_keyboard(url))

@dp.callback_query(F.data == "split_yes")
async def process_split_yes(callback: CallbackQuery):
    file_path = pending_files.get(callback.message.chat.id)
    if not file_path or not os.path.exists(file_path):
        await callback.message.edit_text("❌ Файл потерян.")
        return
    await split_and_send(callback.message.chat.id, file_path, callback.message)

@dp.callback_query(F.data == "split_cancel")
async def process_split_cancel(callback: CallbackQuery):
    file_path = pending_files.get(callback.message.chat.id)
    if file_path and os.path.exists(file_path):
        os.remove(file_path)
    pending_files.pop(callback.message.chat.id, None)
    await callback.message.edit_text("❌ Отменено.")

@dp.callback_query(F.data.startswith("quality_"))
async def process_quality(callback: CallbackQuery):
    quality = callback.data.split("_")[1]
    user_id = callback.from_user.id
    url = user_data.get(user_id)

    if not url:
        await callback.message.edit_text("❌ Ссылка устарела.")
        return

    progress_storage[user_id] = {}
    temp_name_tmpl = f'downloads/{user_id}_temp_%(id)s.%(ext)s'
    
    # ПРОВЕРЯЕМ КУКИ
    cookies = get_cookies_location()

    opts = {
        'outtmpl': temp_name_tmpl,
        'noplaylist': True,
        'progress_hooks': [make_progress_hook(user_id)],
        'ffmpeg_location': get_ffmpeg_location(),
        'cookiefile': cookies, # <--- САМОЕ ВАЖНОЕ: ПОДКЛЮЧАЕМ КУКИ
        'http_headers': {'User-Agent': 'Mozilla/5.0'}
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
        
        video_title = "video"
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
                video_title = info.get('title', 'video')
            except: pass
            
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))
            downloaded_file = ydl.prepare_filename(info)

        base, _ = os.path.splitext(downloaded_file)
        target_ext = ".mp3" if quality == 'audio' else ".mp4"
        final_name = f"{clean_filename(video_title)}{target_ext}"
        
        possible_files = [base + target_ext, downloaded_file]
        real_file = None
        for p in possible_files:
            if os.path.exists(p):
                real_file = p
                break
        
        if not real_file: raise Exception("Файл не найден")

        final_path = os.path.join('downloads', final_name)
        if os.path.exists(final_path): os.remove(final_path)
        os.rename(real_file, final_path)

        file_size_mb = os.path.getsize(final_path) / (1024 * 1024)
        
        if file_size_mb > 49.5:
            pending_files[user_id] = final_path
            await status_msg.edit_text(
                f"⚠️ <b>Размер: {file_size_mb:.1f} МБ</b>\n✂️ Разрезать на части?",
                reply_markup=get_split_keyboard()
            )
        else:
            await status_msg.edit_text("📤 <b>Загрузка...</b>")
            input_file = FSInputFile(final_path)
            if quality == 'audio':
                await callback.message.answer_audio(input_file, caption=f"🎧 {final_name}")
            else:
                await callback.message.answer_video(input_file, caption=f"📹 {final_name}")
            await status_msg.delete()
            if os.path.exists(final_path): os.remove(final_path)

    except Exception as e:
        if "Sign in" in str(e):
             await status_msg.edit_text("⚠️ <b>Ошибка доступа YouTube:</b>\nНужно обновить файл cookies.txt на сервере.")
        elif "Entity Too Large" in str(e):
             await status_msg.edit_text("⚠️ Ошибка: Слишком большой файл.")
        else:
             logging.error(e)
             await status_msg.edit_text(f"⚠️ Ошибка: {e}")
             if final_path and os.path.exists(final_path): os.remove(final_path)
    finally:
        tracker_task.cancel()
        if downloaded_file and os.path.exists(downloaded_file):
            try: os.remove(downloaded_file)
            except: pass
        if final_path and os.path.exists(final_path) and user_id not in pending_files:
            try: os.remove(final_path)
            except: pass

async def main():
    if not os.path.exists('downloads'): os.makedirs('downloads')
    print("✅ БОТ ГОТОВ! (v13.0 + Cookies)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

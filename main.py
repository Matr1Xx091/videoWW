import os
import asyncio
import logging
import re
import glob
import shutil
import aiohttp
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

# --- HELPER FUNCTIONS ---

def clean_filename(title):
    clean = re.sub(r'[^\w\s\-\.]', '', str(title))
    return clean.strip()[:50]

def get_ffmpeg_location():
    if os.path.exists("ffmpeg.exe"): return os.getcwd()
    return None

def get_cookies_location():
    if os.path.exists("cookies.txt"): return "cookies.txt"
    return None

def is_aria2_installed():
    if os.path.exists("aria2c.exe"): return "aria2c.exe"
    if shutil.which("aria2c"): return "aria2c"
    return None

# --- UPLOAD TO CLOUD (Catbox / Litterbox) ---
# Дает прямую ссылку без рекламы

async def upload_to_catbox(file_path):
    url = "https://litterbox.catbox.moe/resources/internals/api.php"
    async with aiohttp.ClientSession() as session:
        data = aiohttp.FormData()
        data.add_field('reqtype', 'fileupload')
        data.add_field('time', '24h') # Файл живет 24 часа (макс размер 1ГБ)
        data.add_field('fileToUpload', open(file_path, 'rb'))
        
        try:
            async with session.post(url, data=data) as resp:
                if resp.status == 200:
                    link = await resp.text()
                    return link.strip() # Возвращает ссылку типа https://litter.catbox.moe/xyz.mp4
        except Exception as e:
            print(f"Upload Error: {e}")
            return None
    return None

# --- KEYBOARDS ---

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

def get_error_keyboard():
    buttons = [
        [InlineKeyboardButton(text="🔗 Прямая ссылка (Direct Link)", callback_data="link_yes")],
        [InlineKeyboardButton(text="✂️ Разрезать на части", callback_data="split_yes")],
        [InlineKeyboardButton(text="📉 Сжать до 50 МБ", callback_data="compress_yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="split_cancel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- PROGRESS ---

def make_progress_hook(chat_id):
    def hook(d):
        if d['status'] == 'downloading':
            try:
                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                percent = 0
                if total: percent = (downloaded / total) * 100
                mb = downloaded / 1024 / 1024
                progress_storage[chat_id] = {"percent": percent, "mb": mb, "status": "downloading"}
            except: pass
        elif d['status'] == 'finished':
            progress_storage[chat_id] = {"status": "finished", "percent": 100}
    return hook

async def progress_tracker_task(chat_id, message_id):
    last_text = ""
    while True:
        await asyncio.sleep(2)
        data = progress_storage.get(chat_id)
        if not data: continue
        
        if data.get("status") == "finished":
            try: await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="⚙️ <b>Скачано! Обработка...</b>")
            except: pass
            break

        percent = data.get("percent", 0)
        mb = data.get("mb", 0)
        text = "🚀 <b>Скачивание...</b>\n"
        if percent > 0:
            filled = int(10 * percent // 100)
            bar = '█' * filled + '░' * (10 - filled)
            text += f"[{bar}] {percent:.1f}%"
        elif mb > 0:
            text += f"📥 Скачано: {mb:.1f} MB"
        else:
            text += "⏳ Подключение..."

        if text != last_text:
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
                last_text = text
            except: break

# --- ACTION HANDLERS ---

async def handle_cloud_upload(chat_id, file_path, status_msg):
    try:
        await status_msg.edit_text("☁️ <b>Загружаю на быстрый сервер...</b>\n(Прямая ссылка, без рекламы)")
        
        link = await upload_to_catbox(file_path)
        
        if link and link.startswith("http"):
            await status_msg.edit_text(
                f"✅ <b>Готово!</b>\n\n"
                f"🔗 <b>Нажми, чтобы скачать:</b>\n{link}\n\n"
                f"<i>(Ссылка активна 24 часа)</i>",
                disable_web_page_preview=True
            )
        else:
            await status_msg.edit_text("❌ Ошибка сервера загрузки. Попробуй нарезать.")
            
    except Exception as e:
        await status_msg.edit_text(f"⚠️ Ошибка: {e}")
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        pending_files.pop(chat_id, None)

async def split_and_send(chat_id, file_path, status_msg):
    try:
        await status_msg.edit_text("🔪 <b>Нарезаю видео...</b>")
        base_name = os.path.splitext(file_path)[0]
        output_pattern = f"{base_name}_part%03d.mp4"
        cmd = f'ffmpeg -i "{file_path}" -c copy -map 0 -segment_time 170 -f segment -reset_timestamps 1 "{output_pattern}"'
        proc = await asyncio.create_subprocess_shell(cmd)
        await proc.communicate()

        parts = sorted(glob.glob(f"{base_name}_part*.mp4"))
        if not parts:
            await status_msg.edit_text("⚠️ Ошибка нарезки.")
            return

        await status_msg.edit_text(f"📦 Частей: {len(parts)}. Отправляю...")
        for i, part in enumerate(parts):
            try:
                await bot.send_video(chat_id, FSInputFile(part), caption=f"📹 <b>Часть {i+1}/{len(parts)}</b>")
                await asyncio.sleep(1)
            except Exception as e:
                await bot.send_message(chat_id, f"⚠️ Ошибка: {e}")
            finally:
                if os.path.exists(part): os.remove(part)
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"⚠️ Ошибка: {e}")
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        pending_files.pop(chat_id, None)

async def compress_and_send(chat_id, file_path, status_msg):
    compressed_path = None
    try:
        await status_msg.edit_text("📉 <b>Сжимаю видео до 50 МБ...</b>")
        probe_cmd = f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{file_path}"'
        proc = await asyncio.create_subprocess_shell(probe_cmd, stdout=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        try: duration = float(stdout.decode().strip())
        except: duration = 600

        target_total_bits = 48 * 8 * 1024 * 1024
        target_bitrate = int(target_total_bits / duration)
        if target_bitrate < 100000: target_bitrate = 100000

        base_name = os.path.splitext(file_path)[0]
        compressed_path = f"{base_name}_compressed.mp4"

        cmd = f'ffmpeg -i "{file_path}" -c:v libx264 -b:v {target_bitrate} -maxrate {target_bitrate} -bufsize {target_bitrate*2} -preset veryfast -c:a aac -b:a 128k "{compressed_path}"'
        
        process = await asyncio.create_subprocess_shell(cmd)
        await process.communicate()

        if os.path.exists(compressed_path):
            file_size = os.path.getsize(compressed_path) / (1024*1024)
            if file_size > 49.9:
                await status_msg.edit_text(f"⚠️ Сжатый файл {file_size:.1f} МБ. Отправляю ссылкой...")
                await handle_cloud_upload(chat_id, compressed_path, status_msg)
            else:
                await status_msg.edit_text("📤 <b>Отправка...</b>")
                await bot.send_video(chat_id, FSInputFile(compressed_path), caption="📉 <b>Сжатая версия</b>")
                await status_msg.delete()
        else:
            await status_msg.edit_text("⚠️ Ошибка сжатия.")

    except Exception as e:
        await status_msg.edit_text(f"⚠️ Ошибка: {e}")
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        if compressed_path and os.path.exists(compressed_path): os.remove(compressed_path)
        pending_files.pop(chat_id, None)

# --- BOT HANDLERS ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("👋 <b>Бот готов!</b> (v18.0 Direct Link)")

@dp.message(F.text)
async def process_link(message: types.Message):
    url = message.text.strip()
    if not ("http" in url): return
    user_data[message.from_user.id] = url
    await message.answer("🔎 Ссылка принята!", reply_markup=get_quality_keyboard(url))

@dp.callback_query(F.data == "link_yes")
async def process_link_yes(callback: CallbackQuery):
    path = pending_files.get(callback.message.chat.id)
    if not path or not os.path.exists(path):
        await callback.message.edit_text("❌ Файл потерян.")
        return
    await handle_cloud_upload(callback.message.chat.id, path, callback.message)

@dp.callback_query(F.data == "split_yes")
async def process_split_yes(callback: CallbackQuery):
    path = pending_files.get(callback.message.chat.id)
    if not path or not os.path.exists(path):
        await callback.message.edit_text("❌ Файл потерян.")
        return
    await split_and_send(callback.message.chat.id, path, callback.message)

@dp.callback_query(F.data == "compress_yes")
async def process_compress_yes(callback: CallbackQuery):
    path = pending_files.get(callback.message.chat.id)
    if not path or not os.path.exists(path):
        await callback.message.edit_text("❌ Файл потерян.")
        return
    await compress_and_send(callback.message.chat.id, path, callback.message)

@dp.callback_query(F.data == "split_cancel")
async def process_cancel(callback: CallbackQuery):
    path = pending_files.get(callback.message.chat.id)
    if path and os.path.exists(path): os.remove(path)
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
    temp_tmpl = f'downloads/{user_id}_temp_%(id)s.%(ext)s'
    
    opts = {
        'outtmpl': temp_tmpl,
        'noplaylist': True,
        'progress_hooks': [make_progress_hook(user_id)],
        'ffmpeg_location': get_ffmpeg_location(),
        'cookiefile': get_cookies_location(),
        'http_headers': {'User-Agent': 'Mozilla/5.0'}
    }

    aria2 = is_aria2_installed()
    if aria2:
        opts['external_downloader'] = aria2
        opts['external_downloader_args'] = ['-x', '16', '-s', '16', '-k', '1M']

    if quality == 'audio':
        opts['format'] = 'bestaudio/best'
        opts['postprocessors'] = [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '192'}]
    else:
        opts['merge_output_format'] = 'mp4'
        if quality == '1080': opts['format'] = 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best'
        elif quality == '720': opts['format'] = 'bestvideo[height<=720]+bestaudio/best[height<=720]/best'
        elif quality == '480': opts['format'] = 'bestvideo[height<=480]+bestaudio/best[height<=480]/best'

    status_msg = await callback.message.edit_text("⏳ <b>Инициализация...</b>")
    tracker = asyncio.create_task(progress_tracker_task(callback.message.chat.id, status_msg.message_id))

    d_file, f_path = None, None

    try:
        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL(opts) as ydl:
            try: info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
            except: info = {"title": "video"}
            video_title = info.get('title', 'video')
            
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))
            d_file = ydl.prepare_filename(info)

        base, _ = os.path.splitext(d_file)
        ext = ".mp3" if quality == 'audio' else ".mp4"
        f_name = f"{clean_filename(video_title)}{ext}"
        
        for p in [base + ext, d_file]:
            if os.path.exists(p):
                f_path = os.path.join('downloads', f_name)
                if os.path.exists(f_path): os.remove(f_path)
                os.rename(p, f_path)
                break
        
        if not f_path: raise Exception("Файл не найден")

        size_mb = os.path.getsize(f_path) / (1024*1024)
        
        if size_mb > 49.5:
            pending_files[user_id] = f_path
            await status_msg.edit_text(
                f"⚠️ <b>Файл весит {size_mb:.1f} МБ</b>\nЧто делаем?",
                reply_markup=get_error_keyboard()
            )
        else:
            await status_msg.edit_text("📤 <b>Отправка...</b>")
            if quality == 'audio': await callback.message.answer_audio(FSInputFile(f_path), caption=f"🎧 {f_name}")
            else: await callback.message.answer_video(FSInputFile(f_path), caption=f"📹 {f_name}")
            await status_msg.delete()
            os.remove(f_path)

    except Exception as e:
        if "Sign in" in str(e): await status_msg.edit_text("⚠️ Ошибка Cookies.")
        else:
            logging.error(e)
            await status_msg.edit_text(f"⚠️ Ошибка: {e}")
            if f_path and os.path.exists(f_path): os.remove(f_path)
    finally:
        tracker.cancel()
        if d_file and os.path.exists(d_file): 
            try: os.remove(d_file)
            except: pass
        if f_path and os.path.exists(f_path) and user_id not in pending_files:
            try: os.remove(f_path)
            except: pass

async def main():
    if not os.path.exists('downloads'): os.makedirs('downloads')
    print("✅ БОТ ЗАПУЩЕН! (v18.0 Direct Link)")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

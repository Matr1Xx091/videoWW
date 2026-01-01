import os
import asyncio
import logging
import re
import glob
import shutil
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.client.default import DefaultBotProperties
import yt_dlp

TOKEN = "8250742177:AAGOPppYA5PALhoNwZsfoa_uLdQcE3m3Ktc"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

user_data = {}
progress_storage = {}
pending_files = {}

def clean_filename(title):
    clean = re.sub(r'[^\w\s\-\.]', '', str(title))
    return clean.strip()[:50]

def get_ffmpeg_location():
    if os.path.exists("ffmpeg.exe"): return os.getcwd()
    return None

def is_aria2_installed():
    if os.path.exists("aria2c.exe"): return "aria2c.exe"
    if shutil.which("aria2c"): return "aria2c"
    return None

# --- WEB SERVER (Для Render) ---
async def health_check(request): return web.Response(text="Alive")

async def start_web_server():
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# --- CLOUD UPLOAD ---
async def upload_to_catbox(file_path):
    url = "https://litterbox.catbox.moe/resources/internals/api.php"
    async with aiohttp.ClientSession() as session:
        data = aiohttp.FormData()
        data.add_field('reqtype', 'fileupload')
        data.add_field('time', '24h') 
        data.add_field('fileToUpload', open(file_path, 'rb'))
        try:
            async with session.post(url, data=data) as resp:
                if resp.status == 200:
                    return (await resp.text()).strip()
        except: pass
    return None

# --- KEYBOARDS ---
def get_quality_keyboard(url):
    buttons = [
        [InlineKeyboardButton(text="📹 Видео (Best)", callback_data="quality_1080"),
         InlineKeyboardButton(text="🎵 Аудио (MP3)", callback_data="quality_audio")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_error_keyboard():
    buttons = [
        [InlineKeyboardButton(text="🔗 Прямая ссылка", callback_data="link_yes")],
        [InlineKeyboardButton(text="✂️ Разрезать", callback_data="split_yes")],
        [InlineKeyboardButton(text="📉 Сжать (<50МБ)", callback_data="compress_yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="split_cancel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- PROGRESS ---
def make_progress_hook(chat_id):
    def hook(d):
        if d['status'] == 'downloading':
            try:
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                downloaded = d.get('downloaded_bytes', 0)
                if total:
                    pct = (downloaded / total) * 100
                    progress_storage[chat_id] = {"percent": pct, "status": "downloading"}
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
            try: await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="⚙️ <b>Обработка...</b>")
            except: pass
            break
        pct = data.get("percent", 0)
        text = f"🚀 <b>Загрузка:</b> {int(pct)}%"
        if text != last_text:
            try: 
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
                last_text = text
            except: break

# --- ACTIONS ---
async def handle_cloud_upload(chat_id, file_path, status_msg):
    try:
        await status_msg.edit_text("☁️ <b>Загрузка на сервер...</b>")
        link = await upload_to_catbox(file_path)
        if link and link.startswith("http"):
            await status_msg.edit_text(f"✅ <b>Готово!</b>\n🔗 <a href='{link}'>Скачать видео</a>", disable_web_page_preview=True)
        else: await status_msg.edit_text("❌ Ошибка сервера.")
    except Exception as e: await status_msg.edit_text(f"⚠️ {e}")
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        pending_files.pop(chat_id, None)

async def split_and_send(chat_id, file_path, status_msg):
    try:
        await status_msg.edit_text("🔪 <b>Нарезка...</b>")
        base = os.path.splitext(file_path)[0]
        cmd = f'ffmpeg -i "{file_path}" -c copy -map 0 -segment_time 170 -f segment "{base}_part%03d.mp4"'
        proc = await asyncio.create_subprocess_shell(cmd)
        await proc.communicate()
        parts = sorted(glob.glob(f"{base}_part*.mp4"))
        await status_msg.edit_text(f"📦 Частей: {len(parts)}. Отправляю...")
        for p in parts:
            try: await bot.send_video(chat_id, FSInputFile(p))
            except: pass
            finally: os.remove(p)
        await status_msg.delete()
    except: await status_msg.edit_text("⚠️ Ошибка нарезки")
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        pending_files.pop(chat_id, None)

async def compress_and_send(chat_id, file_path, status_msg):
    try:
        await status_msg.edit_text("📉 <b>Сжатие...</b>")
        base = os.path.splitext(file_path)[0]
        comp_path = f"{base}_comp.mp4"
        # Жесткое сжатие
        cmd = f'ffmpeg -i "{file_path}" -vcodec libx264 -crf 28 -preset ultrafast "{comp_path}"'
        proc = await asyncio.create_subprocess_shell(cmd)
        await proc.communicate()
        
        if os.path.exists(comp_path) and os.path.getsize(comp_path) < 52400000:
            await status_msg.edit_text("📤 <b>Отправка...</b>")
            await bot.send_video(chat_id, FSInputFile(comp_path))
            await status_msg.delete()
        else:
            await status_msg.edit_text("⚠️ Файл все равно большой. Используй ссылку.")
        if os.path.exists(comp_path): os.remove(comp_path)
    except Exception as e: await status_msg.edit_text(f"⚠️ {e}")
    finally:
        if os.path.exists(file_path): os.remove(file_path)
        pending_files.pop(chat_id, None)

# --- HANDLERS ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("👋 <b>Бот v20 (Anti-Ban)</b>\nПришли ссылку!")

@dp.message(F.text)
async def process_link(message: types.Message):
    user_data[message.from_user.id] = message.text.strip()
    await message.answer("🔎 Формат?", reply_markup=get_quality_keyboard(""))

@dp.callback_query(F.data.in_({"link_yes", "split_yes", "compress_yes", "split_cancel"}))
async def process_action(call: CallbackQuery):
    path = pending_files.get(call.message.chat.id)
    if not path or not os.path.exists(path):
        return await call.message.edit_text("❌ Файл потерян.")
    
    if call.data == "link_yes": await handle_cloud_upload(call.message.chat.id, path, call.message)
    elif call.data == "split_yes": await split_and_send(call.message.chat.id, path, call.message)
    elif call.data == "compress_yes": await compress_and_send(call.message.chat.id, path, call.message)
    elif call.data == "split_cancel":
        os.remove(path)
        await call.message.edit_text("❌ Отменено.")

@dp.callback_query(F.data.startswith("quality_"))
async def process_quality(call: CallbackQuery):
    url = user_data.get(call.from_user.id)
    if not url: return await call.message.edit_text("❌ Ссылка старая.")
    
    quality = call.data.split("_")[1]
    progress_storage[call.from_user.id] = {}
    temp_tmpl = f'downloads/{call.from_user.id}_temp_%(id)s.%(ext)s'
    
    # --- НОВЫЕ НАСТРОЙКИ (ANTI-BAN) ---
    opts = {
        'outtmpl': temp_tmpl,
        'noplaylist': True,
        'progress_hooks': [make_progress_hook(call.from_user.id)],
        'ffmpeg_location': get_ffmpeg_location(),
        'http_headers': {'User-Agent': 'Mozilla/5.0'},
        # ВАЖНО: Притворяемся Андроидом, чтобы обойти 429
        'extractor_args': {'youtube': {'player_client': ['android', 'ios']}},
    }
    
    if is_aria2_installed():
        opts['external_downloader'] = 'aria2c'
        opts['external_downloader_args'] = ['-x', '8', '-k', '1M']

    if quality == 'audio':
        opts['format'] = 'bestaudio/best'
        opts['postprocessors'] = [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3'}]
    else:
        # Качаем лучшее что есть, но не больше 1080p
        opts['format'] = 'bestvideo[height<=1080]+bestaudio/best'

    msg = await call.message.edit_text("⏳ <b>Старт...</b>")
    asyncio.create_task(progress_tracker_task(call.message.chat.id, msg.message_id))

    try:
        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))
            d_file = ydl.prepare_filename(info)

        base, ext = os.path.splitext(d_file)
        final_ext = ".mp3" if quality == 'audio' else ".mp4"
        
        # Ищем файл
        real_path = None
        for p in [base + final_ext, d_file]:
            if os.path.exists(p):
                real_path = p
                break
        
        if not real_path: raise Exception("Файл не скачался.")
        
        # Переименование
        final_name = f"{clean_filename(info.get('title', 'video'))}{final_ext}"
        final_path = os.path.join('downloads', final_name)
        if os.path.exists(final_path): os.remove(final_path)
        os.rename(real_path, final_path)

        # Проверка размера
        size_mb = os.path.getsize(final_path) / (1024*1024)
        if size_mb > 49.5:
            pending_files[call.message.chat.id] = final_path
            await msg.edit_text(f"⚠️ <b>{size_mb:.1f} МБ</b> (Лимит 50)\nВыбери действие:", reply_markup=get_error_keyboard())
        else:
            await msg.edit_text("📤 <b>Отправка...</b>")
            if quality == 'audio': await call.message.answer_audio(FSInputFile(final_path), caption=final_name)
            else: await call.message.answer_video(FSInputFile(final_path), caption=final_name)
            await msg.delete()
            os.remove(final_path)

    except Exception as e:
        err = str(e)
        if "429" in err: await msg.edit_text("⛔️ <b>Бан от YouTube (429)</b>\nСервер перегружен. Попробуй позже или скачай с TikTok.")
        elif "Sign in" in err: await msg.edit_text("🔒 <b>Ошибка доступа.</b>\nМы удалили куки, но YouTube все равно ругается. Попробуй другое видео.")
        else: await msg.edit_text(f"⚠️ Ошибка: {err}")
        # Чистка
        if 'd_file' in locals() and d_file and os.path.exists(d_file): os.remove(d_file)

async def main():
    if not os.path.exists('downloads'): os.makedirs('downloads')
    asyncio.create_task(start_web_server())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

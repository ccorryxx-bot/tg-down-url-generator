import os
import asyncio
import aiohttp
import requests
import subprocess
import time
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# Configs
API_ID = int(os.environ.get('API_ID'))
API_HASH = os.environ.get('API_HASH')
STRING_SESSION = os.environ.get('STRING_SESSION')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
MEDIA_LINK = os.environ.get('MEDIA_LINK')
TARGET_CHAT_ID = int(os.environ.get('TARGET_CHAT_ID'))

# Supabase Configs
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')
SUPABASE_BUCKET = os.environ.get('SUPABASE_BUCKET')

def get_video_metadata(file_path):
    try:
        cmd = [
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration,size',
            '-of', 'default=noprint_wrappers=1:nokey=1', file_path
        ]
        output = subprocess.check_output(cmd).decode().split('\n')
        duration = float(output[0])
        size = int(output[1])
        return duration, size
    except:
        return 0, 0

def generate_thumbnail(file_path, thumb_path):
    try:
        subprocess.call([
            'ffmpeg', '-i', file_path, '-ss', '00:00:01', '-vframes', '1', thumb_path
        ])
        return True
    except:
        return False

async def upload_to_supabase(file_path, file_name):
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{file_name}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/octet-stream"
    }
    async with aiohttp.ClientSession() as session:
        with open(file_path, 'rb') as f:
            async with session.post(url, headers=headers, data=f) as resp:
                if resp.status in [200, 201]:
                    return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{file_name}"
                else:
                    return None

def update_progress_msg(chat_id, message_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    requests.post(url, json=payload)

async def progress_callback(current, total, chat_id, message_id, last_update_time):
    now = time.time()
    if now - last_update_time[0] > 5: # Update every 5 seconds to avoid flood
        percentage = (current / total) * 100
        text = f"⏳ Downloading: {percentage:.1f}% ({current/(1024*1024):.1f}MB / {total/(1024*1024):.1f}MB)"
        update_progress_msg(chat_id, message_id, text)
        last_update_time[0] = now

async def main():
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    bot = TelegramClient('bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)
    await client.connect()
    
    try:
        parts = MEDIA_LINK.split('/')
        msg_id = int(parts[-1])
        chat_id = parts[-2]
        if chat_id.isdigit():
            chat_id = int(f"-100{chat_id}")
            
        status_msg = await bot.send_message(TARGET_CHAT_ID, "⏳ Starting download...")
        last_update = [time.time()]
        
        msg = await client.get_messages(chat_id, ids=msg_id)
        if not msg or not msg.media:
            await bot.edit_message(status_msg, "❌ Media not found.")
            return
            
        file_path = await client.download_media(
            msg, 
            progress_callback=lambda c, t: progress_callback(c, t, TARGET_CHAT_ID, status_msg.id, last_update)
        )
        file_name = os.path.basename(file_path)
        
        await bot.edit_message(status_msg, "✅ Download complete. Processing metadata...")
        
        # Metadata & Thumbnail
        duration, size = get_video_metadata(file_path)
        thumb_path = "thumb.jpg"
        has_thumb = generate_thumbnail(file_path, thumb_path)
        
        metadata_text = f"📄 *File:* `{file_name}`\n📦 *Size:* {size/(1024*1024):.1f} MB\n⏱️ *Duration:* {int(duration//60)}m {int(duration%60)}s"
        
        # 1. Send to Telegram
        if has_thumb:
            await bot.send_file(TARGET_CHAT_ID, file_path, caption=metadata_text, thumb=thumb_path, parse_mode='markdown')
        else:
            await bot.send_file(TARGET_CHAT_ID, file_path, caption=metadata_text, parse_mode='markdown')
            
        # 2. Upload to Supabase
        await bot.edit_message(status_msg, "☁️ Uploading to Supabase for Direct Link...")
        public_url = await upload_to_supabase(file_path, file_name)
        
        if public_url:
            final_text = f"🔗 *Direct Download Link:*\n`{public_url}`\n\n{metadata_text}"
            # Use requests for inline button
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": TARGET_CHAT_ID,
                "text": final_text,
                "parse_mode": "Markdown",
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "🗑️ Delete from Cloud", "callback_data": f"delete:{file_name}"}
                    ]]
                }
            }
            requests.post(url, json=payload)
            await bot.delete_messages(TARGET_CHAT_ID, [status_msg.id])
        else:
            await bot.edit_message(status_msg, f"✅ Done! (Direct link failed/limit reached)\n\n{metadata_text}")
            
    except Exception as e:
        await bot.send_message(TARGET_CHAT_ID, f"❌ Error: {str(e)}")
    finally:
        await client.disconnect()
        await bot.disconnect()

if __name__ == "__main__":
    asyncio.run(main())

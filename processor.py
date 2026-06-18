import os
import asyncio
import aiohttp
import requests
import subprocess
import time
import json
import sys
import re
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# Configs
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
STRING_SESSION = os.environ.get('STRING_SESSION', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
MEDIA_LINK = os.environ.get('MEDIA_LINK', '')
TARGET_CHAT_ID = int(os.environ.get('TARGET_CHAT_ID', 0))
QUALITY = os.environ.get('QUALITY', '720')

# Supabase Configs
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')
SUPABASE_BUCKET = os.environ.get('SUPABASE_BUCKET', '')

async def report_progress(percent, status="Downloading"):
    file_name = f"progress_{TARGET_CHAT_ID}.json"
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{file_name}"
    data = json.dumps({"percent": percent, "status": status, "time": time.time()})
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "x-upsert": "true"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data) as resp:
                pass
    except: pass

def send_tg_msg(text):
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
        "chat_id": TARGET_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    })

async def download_telegram_media(client, link, file_path):
    try:
        # Parse link to get message ID and chat
        # Format: https://t.me/c/123456789/123 or https://t.me/channel/123
        parts = link.split('/')
        msg_id = int(parts[-1])
        chat_id = parts[-2]
        if chat_id.isdigit(): chat_id = int(f"-100{chat_id}")
        
        entity = await client.get_entity(chat_id)
        message = await client.get_messages(entity, ids=msg_id)
        
        if not message or not message.media:
            return False, "Message has no media."
        
        async def progress_callback(current, total):
            p = round((current / total) * 100, 1)
            await report_progress(p, "Downloading from Telegram")

        await client.download_media(message, file_path, progress_callback=progress_callback)
        return True, None
    except Exception as e:
        return False, str(e)

async def main():
    await report_progress(0, "Starting")
    file_path = f"video_{TARGET_CHAT_ID}.mp4"
    
    is_tg_link = "t.me/" in MEDIA_LINK
    
    if is_tg_link:
        if not STRING_SESSION:
            send_tg_msg("❌ Telegram link များကို download ဆွဲရန် STRING_SESSION လိုအပ်ပါသည်။")
            return
        
        client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
        await client.connect()
        success, err = await download_telegram_media(client, MEDIA_LINK, file_path)
        await client.disconnect()
        
        if not success:
            send_tg_msg(f"❌ Telegram Download Error: {err}")
            return
    else:
        # Download using yt-dlp
        format_str = f"bestvideo[height<={QUALITY}]+bestaudio/best[height<={QUALITY}]/best"
        cmd = ['yt-dlp', '-f', format_str, '--merge-output-format', 'mp4', '-o', file_path, MEDIA_LINK]
        
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        for line in process.stdout:
            if '%' in line:
                match = re.search(r'(\d+\.\d+)%', line)
                if match:
                    await report_progress(float(match.group(1)), "Downloading")
        process.wait()
        
        if process.returncode != 0:
            send_tg_msg(f"❌ yt-dlp Error: Download failed for {MEDIA_LINK}")
            return

    if os.path.exists(file_path):
        await report_progress(100, "Uploading to Storage")
        file_name = f"{int(time.time())}_{file_path}"
        upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{file_name}"
        
        with open(file_path, 'rb') as f:
            resp = requests.post(
                upload_url,
                headers={"Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "video/mp4"},
                data=f
            )
        
        if resp.ok:
            direct_link = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{file_name}"
            msg = f"✅ *Download ပြီးပါပြီ!*\n\n🔗 [Direct Download Link]({direct_link})"
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                "chat_id": TARGET_CHAT_ID,
                "text": msg,
                "parse_mode": "Markdown",
                "reply_markup": {
                    "inline_keyboard": [[{"text": "🗑️ Delete File", "callback_data": f"delete:{file_name}"}]]
                }
            })
            await report_progress(100, "Completed")
        else:
            send_tg_msg(f"❌ Supabase Upload Error: {resp.status_code} - {resp.text}")
    else:
        send_tg_msg("❌ Error: Downloaded file not found.")

if __name__ == "__main__":
    asyncio.run(main())

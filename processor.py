import os
import asyncio
import aiohttp
import requests
import subprocess
import time
import json
import sys
from telethon import TelegramClient
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
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=data) as resp:
            pass

def progress_hook(d):
    if d['status'] == 'downloading':
        p = d.get('_percent_str', '0%').replace('%','')
        try:
            percent = float(p)
            asyncio.run(report_progress(percent, "Downloading"))
        except: pass
    elif d['status'] == 'finished':
        asyncio.run(report_progress(100, "Uploading"))

async def main():
    await report_progress(0, "Starting")
    
    # Download using yt-dlp with quality
    file_path = f"video_{TARGET_CHAT_ID}.mp4"
    format_str = f"bestvideo[height<={QUALITY}]+bestaudio/best[height<={QUALITY}]/best"
    
    cmd = [
        'yt-dlp',
        '-f', format_str,
        '--merge-output-format', 'mp4',
        '-o', file_path,
        MEDIA_LINK
    ]
    
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    for line in process.stdout:
        if '%' in line:
            # Extract percentage from yt-dlp output
            parts = line.split()
            for part in parts:
                if '%' in part:
                    try:
                        p = float(part.replace('%', ''))
                        await report_progress(p, "Downloading")
                    except: pass
    
    process.wait()
    
    if os.path.exists(file_path):
        await report_progress(100, "Uploading to Storage")
        # Upload to Supabase
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
            # Send to Telegram
            msg = f"✅ *Download ပြီးပါပြီ!* (Quality: {QUALITY}p)\n\n🔗 [Direct Download Link]({direct_link})"
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
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                "chat_id": TARGET_CHAT_ID,
                "text": f"❌ Upload Error: {resp.status_code}"
            })
    else:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
            "chat_id": TARGET_CHAT_ID,
            "text": "❌ Download မအောင်မြင်ပါ။ Link ကို ပြန်စစ်ပေးပါ။"
        })

if __name__ == "__main__":
    asyncio.run(main())

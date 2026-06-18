import os
import asyncio
import re
import time
import subprocess
import requests
import boto3
from botocore.client import Config
from telethon import TelegramClient
from telethon.sessions import StringSession

# Environment Variables
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
STRING_SESSION = os.environ.get("STRING_SESSION", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TARGET_CHAT_ID = int(os.environ.get("TARGET_CHAT_ID", 0))
MEDIA_LINK = os.environ.get("MEDIA_LINK", "")
QUALITY = os.environ.get("QUALITY", "720")

# Backblaze B2 Settings
B2_KEY_ID = os.environ.get("B2_KEY_ID", "")
B2_APPLICATION_KEY = os.environ.get("B2_APPLICATION_KEY", "")
B2_BUCKET_NAME = os.environ.get("B2_BUCKET_NAME", "")
B2_ENDPOINT = os.environ.get("B2_ENDPOINT", "s3.us-east-005.backblazeb2.com")

# S3 Client for Backblaze B2
s3 = boto3.client(
    's3',
    endpoint_url=f'https://{B2_ENDPOINT}',
    aws_access_key_id=B2_KEY_ID,
    aws_secret_access_key=B2_APPLICATION_KEY,
    config=Config(signature_version='s3v4')
)

def send_tg_msg(text):
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
        "chat_id": TARGET_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    })

async def download_telegram_media(client, link, file_path):
    try:
        # Match t.me links
        match = re.search(r't\.me/(?:c/)?([^/]+)/(\d+)', link)
        if not match: return False, "Invalid Telegram link format."
        
        chat_identifier = match.group(1)
        msg_id = int(match.group(2))
        
        # Handle private vs public chat identifiers
        if chat_identifier.isdigit():
            chat_id = int(f"-100{chat_identifier}")
        else:
            chat_id = chat_identifier
            
        entity = await client.get_entity(chat_id)
        message = await client.get_messages(entity, ids=msg_id)
        
        if not message or not message.media: 
            return False, "No media found in the message."
        
        await client.download_media(message, file_path)
        return True, None
    except Exception as e:
        return False, str(e)

async def main():
    file_path = f"video_{int(time.time())}.mp4"
    is_tg_link = "t.me/" in MEDIA_LINK
    
    send_tg_msg("⏳ *Downloading...* Please wait.")

    if is_tg_link:
        if not STRING_SESSION:
            send_tg_msg("❌ STRING_SESSION missing. Cannot download from Telegram.")
            return
        client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
        await client.connect()
        success, err = await download_telegram_media(client, MEDIA_LINK, file_path)
        await client.disconnect()
        if not success:
            send_tg_msg(f"❌ TG Download Error: {err}")
            return
    else:
        # For non-Telegram links (YouTube, etc.)
        format_str = f"bestvideo[height<={QUALITY}]+bestaudio/best[height<={QUALITY}]/best"
        cmd = ['yt-dlp', '-f', format_str, '--merge-output-format', 'mp4', '-o', file_path, MEDIA_LINK]
        process = subprocess.run(cmd)
        if process.returncode != 0:
            send_tg_msg("❌ yt-dlp Download Failed.")
            return

    if os.path.exists(file_path):
        send_tg_msg("🚀 *Uploading to Backblaze B2...*")
        file_name = os.path.basename(file_path)
        
        try:
            # Upload to B2
            s3.upload_file(file_path, B2_BUCKET_NAME, file_name)
            
            # Generate Presigned URL (Valid for 24 hours)
            download_url = s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': B2_BUCKET_NAME, 'Key': file_name},
                ExpiresIn=86400
            )
            
            msg = f"✅ *Download Complete!*\n\n📄 File: `{file_name}`\n🔗 [Direct Download Link]({download_url})\n\n_Note: This link is valid for 24 hours._"
            send_tg_msg(msg)
            
            # Cleanup local file
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            send_tg_msg(f"❌ B2 Upload Error: {str(e)}")
    else:
        send_tg_msg("❌ Error: File not found after download.")

if __name__ == "__main__":
    asyncio.run(main())

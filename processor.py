import os
import asyncio
import re
import time
import subprocess
import requests
import boto3
from botocore.client import Config
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaWebPage

# Environment Variables
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
STRING_SESSION = os.environ.get("STRING_SESSION", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TARGET_CHAT_ID = int(os.environ.get("TARGET_CHAT_ID", 0))
MEDIA_LINK = os.environ.get("MEDIA_LINK", "")
QUALITY = os.environ.get("QUALITY", "720")
WF_NAME = os.environ.get("WF_NAME", "Unknown WF")

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

class ProgressReporter:
    def __init__(self, wf_name, bot_token, chat_id):
        self.wf_name = wf_name
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.last_update_time = 0
        self.message_id = None

    def send_msg(self, text):
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        resp = requests.post(url, json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"})
        data = resp.json()
        if data.get("ok"):
            self.message_id = data["result"]["message_id"]

    def update_progress(self, current, total, action="Downloading"):
        now = time.time()
        if now - self.last_update_time < 5:  # Update every 5 seconds to avoid TG rate limits
            return
        
        self.last_update_time = now
        percent = (current / total) * 100
        text = f"⏳ *[{self.wf_name}] {action}...*\n\nProgress: `{percent:.2f}%`\nDone: `{current/(1024*1024):.2f} MB` / `{total/(1024*1024):.2f} MB`"
        
        if self.message_id:
            url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"
            requests.post(url, json={
                "chat_id": self.chat_id,
                "message_id": self.message_id,
                "text": text,
                "parse_mode": "Markdown"
            })
        else:
            self.send_msg(text)

async def download_telegram_media(client, link, file_path, reporter):
    try:
        # Regex for both public and private links
        # t.me/channel/123 or t.me/c/12345678/123
        match = re.search(r't\.me/(?:c/)?([^/]+)/(\d+)', link)
        if not match: return False, "Invalid Telegram link format."
        
        chat_identifier = match.group(1)
        msg_id = int(match.group(2))
        
        if chat_identifier.isdigit():
            # Private channel IDs need -100 prefix
            chat_id = int(f"-100{chat_identifier}")
        else:
            chat_id = chat_identifier
            
        entity = await client.get_entity(chat_id)
        messages = await client.get_messages(entity, ids=msg_id)
        
        if not messages or not messages.media:
            return False, "No media found in the message."
        
        message = messages # get_messages with ids returns a single object if one id
        
        # Start download with progress callback
        await client.download_media(
            message, 
            file_path,
            progress_callback=lambda c, t: reporter.update_progress(c, t, "Downloading")
        )
        return True, None
    except Exception as e:
        return False, str(e)

async def main():
    reporter = ProgressReporter(WF_NAME, BOT_TOKEN, TARGET_CHAT_ID)
    file_path = f"video_{int(time.time())}.mp4"
    is_tg_link = "t.me/" in MEDIA_LINK
    
    if is_tg_link:
        if not STRING_SESSION:
            reporter.send_msg(f"❌ *[{WF_NAME}]* STRING_SESSION missing.")
            return
        
        client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
        await client.connect()
        
        if not await client.is_user_authorized():
            reporter.send_msg(f"❌ *[{WF_NAME}]* Session is invalid or expired.")
            return
            
        success, err = await download_telegram_media(client, MEDIA_LINK, file_path, reporter)
        await client.disconnect()
        
        if not success:
            reporter.send_msg(f"❌ *[{WF_NAME}]* TG Download Error: {err}")
            return
    else:
        reporter.send_msg(f"⏳ *[{WF_NAME}] Starting yt-dlp...*")
        format_str = f"bestvideo[height<={QUALITY}]+bestaudio/best[height<={QUALITY}]/best"
        cmd = ['yt-dlp', '-f', format_str, '--merge-output-format', 'mp4', '-o', file_path, MEDIA_LINK]
        process = subprocess.run(cmd)
        if process.returncode != 0:
            reporter.send_msg(f"❌ *[{WF_NAME}]* yt-dlp Download Failed.")
            return

    if os.path.exists(file_path):
        file_size = os.path.getsize(file_path)
        reporter.send_msg(f"🚀 *[{WF_NAME}] Uploading to B2...* ({file_size/(1024*1024):.2f} MB)")
        file_name = os.path.basename(file_path)
        
        try:
            # Upload with boto3
            s3.upload_file(file_path, B2_BUCKET_NAME, file_name)
            
            download_url = s3.generate_presigned_url(
                'get_object', 
                Params={'Bucket': B2_BUCKET_NAME, 'Key': file_name}, 
                ExpiresIn=86400
            )
            
            final_msg = (
                f"✅ *[{WF_NAME}] Download Complete!*\n\n"
                f"📄 File: `{file_name}`\n"
                f"⚖️ Size: `{file_size/(1024*1024):.2f} MB`\n"
                f"🔗 [Direct Download Link]({download_url})\n\n"
                f"_Note: Link valid for 24 hours._"
            )
            reporter.send_msg(final_msg)
            os.remove(file_path)
        except Exception as e:
            reporter.send_msg(f"❌ *[{WF_NAME}]* B2 Upload Error: {str(e)}")
    else:
        reporter.send_msg(f"❌ *[{WF_NAME}]* Error: File not found.")

if __name__ == "__main__":
    asyncio.run(main())

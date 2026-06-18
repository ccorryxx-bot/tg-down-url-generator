import os
import asyncio
import re
import time
import subprocess
import requests
import base64
import hashlib
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
WF_NAME = os.environ.get("WF_NAME", "Unknown WF")

# Backblaze B2 Settings
B2_KEY_ID = os.environ.get("B2_KEY_ID", "").strip()
B2_APPLICATION_KEY = os.environ.get("B2_APPLICATION_KEY", "").strip()
B2_BUCKET_NAME = os.environ.get("B2_BUCKET_NAME", "").strip()

class B2NativeClient:
    def __init__(self, key_id, app_key):
        self.key_id = key_id
        self.app_key = app_key
        self.auth_token = None
        self.api_url = None
        self.download_url = None
        self.account_id = None

    def authorize(self):
        auth_string = f"{self.key_id}:{self.app_key}"
        encoded_auth = base64.b64encode(auth_string.encode()).decode()
        headers = {"Authorization": f"Basic {encoded_auth}"}
        resp = requests.get("https://api.backblazeb2.com/b2api/v2/b2_authorize_account", headers=headers)
        data = resp.json()
        if resp.status_code != 200:
            raise Exception(f"B2 Auth Failed: {data.get('message', 'Unknown error')}")
        self.auth_token = data['authorizationToken']
        self.api_url = data['apiUrl']
        self.download_url = data['downloadUrl']
        self.account_id = data['accountId']
        return data

    def get_upload_url(self, bucket_name):
        # First get bucket ID
        headers = {"Authorization": self.auth_token}
        resp = requests.post(f"{self.api_url}/b2api/v2/b2_list_buckets", headers=headers, json={"accountId": self.account_id})
        buckets = resp.json().get('buckets', [])
        bucket_id = next((b['bucketId'] for b in buckets if b['bucketName'] == bucket_name), None)
        if not bucket_id:
            raise Exception(f"Bucket '{bucket_name}' not found.")
        
        resp = requests.post(f"{self.api_url}/b2api/v2/b2_get_upload_url", headers=headers, json={"bucketId": bucket_id})
        return resp.json()

    def upload_file(self, file_path, bucket_name):
        self.authorize()
        upload_data = self.get_upload_url(bucket_name)
        upload_url = upload_data['uploadUrl']
        upload_auth_token = upload_data['authorizationToken']
        
        file_name = os.path.basename(file_path)
        with open(file_path, 'rb') as f:
            file_data = f.read()
            sha1_hash = hashlib.sha1(file_data).hexdigest()
            
            headers = {
                "Authorization": upload_auth_token,
                "X-Bz-File-Name": file_name,
                "Content-Type": "video/mp4",
                "X-Bz-Content-Sha1": sha1_hash
            }
            resp = requests.post(upload_url, headers=headers, data=file_data)
            return resp.json()

    def get_download_link(self, bucket_name, file_name):
        # For private buckets, we need a download authorization token
        headers = {"Authorization": self.auth_token}
        # Get bucket ID first
        resp = requests.post(f"{self.api_url}/b2api/v2/b2_list_buckets", headers=headers, json={"accountId": self.account_id})
        bucket_id = next((b['bucketId'] for b in resp.json()['buckets'] if b['bucketName'] == bucket_name), None)
        
        resp = requests.post(f"{self.api_url}/b2api/v2/b2_get_download_authorization", headers=headers, json={
            "bucketId": bucket_id,
            "fileNamePrefix": file_name,
            "validDurationInSeconds": 86400
        })
        token = resp.json()['authorizationToken']
        return f"{self.download_url}/file/{bucket_name}/{file_name}?Authorization={token}"

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
        if now - self.last_update_time < 5:
            return
        self.last_update_time = now
        percent = (current / total) * 100
        text = f"⏳ *[{self.wf_name}] {action}...*\n\nProgress: `{percent:.2f}%`"
        if self.message_id:
            url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"
            requests.post(url, json={"chat_id": self.chat_id, "message_id": self.message_id, "text": text, "parse_mode": "Markdown"})
        else:
            self.send_msg(text)

async def main():
    reporter = ProgressReporter(WF_NAME, BOT_TOKEN, TARGET_CHAT_ID)
    file_path = f"video_{int(time.time())}.mp4"
    is_tg_link = "t.me/" in MEDIA_LINK
    
    if is_tg_link:
        client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
        await client.connect()
        match = re.search(r't\.me/(?:c/)?([^/]+)/(\d+)', MEDIA_LINK)
        chat_identifier, msg_id = match.group(1), int(match.group(2))
        chat_id = int(f"-100{chat_identifier}") if chat_identifier.isdigit() else chat_identifier
        entity = await client.get_entity(chat_id)
        msg = await client.get_messages(entity, ids=msg_id)
        await client.download_media(msg, file_path, progress_callback=lambda c, t: reporter.update_progress(c, t))
        await client.disconnect()
    else:
        reporter.send_msg(f"⏳ *[{WF_NAME}] Downloading with yt-dlp...*")
        subprocess.run(['yt-dlp', '-f', 'best', '-o', file_path, MEDIA_LINK])

    if os.path.exists(file_path):
        file_size = os.path.getsize(file_path)
        reporter.send_msg(f"🚀 *[{WF_NAME}] Uploading to B2 (Native API)...*")
        try:
            b2 = B2NativeClient(B2_KEY_ID, B2_APPLICATION_KEY)
            b2.upload_file(file_path, B2_BUCKET_NAME)
            file_name = os.path.basename(file_path)
            download_url = b2.get_download_link(B2_BUCKET_NAME, file_name)
            
            final_msg = f"✅ *[{WF_NAME}] Download Complete!*\n\n📄 File: `{file_name}`\n⚖️ Size: `{file_size/(1024*1024):.2f} MB`\n🔗 [Direct Download Link]({download_url})\n\n_Note: Link valid for 24 hours._"
            reporter.send_msg(final_msg)
            os.remove(file_path)
        except Exception as e:
            reporter.send_msg(f"❌ *[{WF_NAME}]* B2 Upload Error: {str(e)}")
    else:
        reporter.send_msg(f"❌ *[{WF_NAME}]* Error: File not found.")

if __name__ == "__main__":
    asyncio.run(main())

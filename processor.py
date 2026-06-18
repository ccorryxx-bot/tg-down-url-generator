import os
import asyncio
import re
import time
import json
import hashlib
import requests
import subprocess
from telethon import TelegramClient
from telethon.sessions import StringSession

# Environment Variables
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
STRING_SESSION = os.environ.get("STRING_SESSION_1", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
B2_KEY_ID = os.environ.get("B2_KEY_ID", "")
B2_APPLICATION_KEY = os.environ.get("B2_APPLICATION_KEY", "")
B2_BUCKET_NAME = os.environ.get("B2_BUCKET_NAME", "")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID", "")

class B2NativeClient:
    def __init__(self, key_id, application_key):
        self.key_id = key_id
        self.application_key = application_key
        self.auth_token = None
        self.api_url = None
        self.download_url = None

    def authorize(self):
        resp = requests.get("https://api.backblazeb2.com/b2api/v2/b2_authorize_account", auth=(self.key_id, self.application_key))
        data = resp.json()
        self.auth_token = data['authorizationToken']
        self.api_url = data['apiUrl']
        self.download_url = data['downloadUrl']
        self.account_id = data['accountId']

    def upload_file(self, file_path, bucket_name):
        self.authorize()
        headers = {"Authorization": self.auth_token}
        resp = requests.post(f"{self.api_url}/b2api/v2/b2_list_buckets", headers=headers, json={"accountId": self.account_id})
        bucket_id = next((b['bucketId'] for b in resp.json()['buckets'] if b['bucketName'] == bucket_name), None)
        resp = requests.post(f"{self.api_url}/b2api/v2/b2_get_upload_url", headers=headers, json={"bucketId": bucket_id})
        upload_data = resp.json()
        file_name = os.path.basename(file_path)
        with open(file_path, 'rb') as f:
            file_data = f.read()
            sha1_hash = hashlib.sha1(file_data).hexdigest()
            headers = {"Authorization": upload_data['authorizationToken'], "X-Bz-File-Name": file_name, "Content-Type": "video/mp4", "X-Bz-Content-Sha1": sha1_hash}
            requests.post(upload_data['uploadUrl'], headers=headers, data=file_data)

    def get_download_link(self, bucket_name, file_name):
        headers = {"Authorization": self.auth_token}
        resp = requests.post(f"{self.api_url}/b2api/v2/b2_list_buckets", headers=headers, json={"accountId": self.account_id})
        bucket_id = next((b['bucketId'] for b in resp.json()['buckets'] if b['bucketName'] == bucket_name), None)
        resp = requests.post(f"{self.api_url}/b2api/v2/b2_get_download_authorization", headers=headers, json={"bucketId": bucket_id, "fileNamePrefix": file_name, "validDurationInSeconds": 86400})
        token = resp.json()['authorizationToken']
        return f"{self.download_url}/file/{bucket_name}/{file_name}?Authorization={token}"

class ProgressReporter:
    def __init__(self, chat_id, msg_id, filename):
        self.chat_id, self.msg_id, self.filename = chat_id, msg_id, filename
        self.last_update, self.start_time = 0, time.time()

    def get_bar(self, percent):
        done = int(percent / 10)
        return "█" * done + "░" * (10 - done)

    def update(self, current, total, action="Downloading"):
        now = time.time()
        if now - self.last_update < 4 and current < total: return
        self.last_update = now
        percent = (current / total) * 100 if total > 0 else 0
        bar = self.get_bar(percent)
        speed = current / (now - self.start_time) if (now - self.start_time) > 0 else 0
        eta = time.strftime("%M:%S", time.gmtime((total - current) / speed)) if speed > 0 else "00:00"
        text = f"⏳ *{action}...*\n📄 File: `{self.filename}`\n📊 Progress: `[{bar}] {percent:.1f}%`\n⏱️ ETA: `{eta}`"
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText", json={"chat_id": self.chat_id, "message_id": self.msg_id, "text": text, "parse_mode": "Markdown"})

async def get_kv_tasks():
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/keys"
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    resp = requests.get(url, headers=headers).json()
    tasks = []
    for k in resp.get("result", []):
        if k["name"].startswith("task:"):
            val = requests.get(f"{url}/{k['name']}", headers=headers).json()
            tasks.append({"key": k["name"], "data": val})
            requests.delete(f"{url}/{k['name']}", headers=headers)
    return tasks

async def process_task(client, b2, task):
    data = task["data"]
    chat_id, media_link, msg_id = data["chatId"], data["mediaLink"], data["statusMessageId"]
    file_path = f"video_{int(time.time())}_{hashlib.md5(media_link.encode()).hexdigest()[:5]}.mp4"
    reporter = ProgressReporter(chat_id, msg_id, "Extracting Metadata...")
    try:
        if "t.me/" in media_link:
            match = re.search(r't\.me/(?:c/)?([^/]+)/(\d+)', media_link)
            chat_identifier, m_id = match.group(1), int(match.group(2))
            target_id = int(f"-100{chat_identifier}") if chat_identifier.isdigit() else chat_identifier
            entity = await client.get_entity(target_id)
            msg = await client.get_messages(entity, ids=m_id)
            reporter.filename = msg.file.name if msg.file and msg.file.name else "telegram_video.mp4"
            await client.download_media(msg, file_path, progress_callback=lambda c, t: reporter.update(c, t))
        else:
            info = json.loads(subprocess.check_output(['yt-dlp', '--dump-json', media_link]).decode())
            reporter.filename = info.get('title', 'video')[:30] + ".mp4"
            subprocess.run(['yt-dlp', '-f', 'best', '-o', file_path, media_link])
        if os.path.exists(file_path):
            reporter.update(100, 100, action="Uploading to B2")
            b2.upload_file(file_path, B2_BUCKET_NAME)
            link = b2.get_download_link(B2_BUCKET_NAME, os.path.basename(file_path))
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": f"✅ *Download Complete!*\n\n📄 File: `{reporter.filename}`\n🔗 [Direct Link]({link})", "parse_mode": "Markdown"})
            os.remove(file_path)
    except Exception as e:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": f"❌ Error: {str(e)}"})

async def main():
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.connect()
    b2 = B2NativeClient(B2_KEY_ID, B2_APPLICATION_KEY)
    empty_polls = 0
    while empty_polls < 10:
        tasks = await get_kv_tasks()
        if tasks:
            empty_polls = 0
            await asyncio.gather(*(process_task(client, b2, t) for t in tasks))
        else:
            empty_polls += 1
            await asyncio.sleep(30)
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())

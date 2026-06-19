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
CF_AUTH_EMAIL = os.environ.get("CF_AUTH_EMAIL", "")
CF_AUTH_KEY   = os.environ.get("CF_AUTH_KEY", "")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID", "")

CHUNK_SIZE = 100 * 1024 * 1024  # 100 MB per B2 chunk


def send_telegram(method, payload, retries=3):
    for attempt in range(retries):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                json=payload,
                timeout=15
            )
            if r.status_code == 200:
                return r.json()
            print(f"[WARN] Telegram {method} attempt {attempt+1} returned {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[WARN] Telegram {method} attempt {attempt+1} exception: {e}")
        time.sleep(2)
    return None


def cf_kv_headers():
    return {
        "X-Auth-Email": CF_AUTH_EMAIL,
        "X-Auth-Key": CF_AUTH_KEY,
        "Content-Type": "application/json"
    }


def cf_kv_put(key, value):
    kv_base = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}"
    headers = {
        "X-Auth-Email": CF_AUTH_EMAIL,
        "X-Auth-Key": CF_AUTH_KEY,
    }
    resp = requests.put(f"{kv_base}/values/{key}", headers=headers, data=value, timeout=30)
    if resp.status_code not in (200, 204):
        print(f"[WARN] cf_kv_put '{key}' returned {resp.status_code}: {resp.text[:100]}")


class B2NativeClient:
    def __init__(self, key_id, application_key):
        self.key_id = key_id
        self.application_key = application_key
        self.auth_token = None
        self.api_url = None
        self.download_url = None
        self.account_id = None
        self._bucket_id_cache = {}

    def authorize(self):
        resp = requests.get(
            "https://api.backblazeb2.com/b2api/v2/b2_authorize_account",
            auth=(self.key_id, self.application_key),
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        self.auth_token = data['authorizationToken']
        self.api_url = data['apiUrl']
        self.download_url = data['downloadUrl']
        self.account_id = data['accountId']

    def _get_bucket_id(self, bucket_name):
        if bucket_name in self._bucket_id_cache:
            return self._bucket_id_cache[bucket_name]
        headers = {"Authorization": self.auth_token}
        resp = requests.post(
            f"{self.api_url}/b2api/v2/b2_list_buckets",
            headers=headers,
            json={"accountId": self.account_id},
            timeout=30
        )
        resp.raise_for_status()
        bucket_id = next((b['bucketId'] for b in resp.json()['buckets'] if b['bucketName'] == bucket_name), None)
        if not bucket_id:
            raise Exception(f"Bucket '{bucket_name}' not found")
        self._bucket_id_cache[bucket_name] = bucket_id
        return bucket_id

    def list_files(self, bucket_name):
        bucket_id = self._get_bucket_id(bucket_name)
        headers = {"Authorization": self.auth_token}
        all_files = []
        next_file_name = None
        while True:
            body = {"bucketId": bucket_id, "maxFileCount": 1000}
            if next_file_name:
                body["startFileName"] = next_file_name
            resp = requests.post(
                f"{self.api_url}/b2api/v2/b2_list_file_names",
                headers=headers,
                json=body,
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            all_files.extend(data["files"])
            next_file_name = data.get("nextFileName")
            if not next_file_name:
                break
        return all_files

    def delete_file(self, file_id, file_name):
        headers = {"Authorization": self.auth_token}
        resp = requests.post(
            f"{self.api_url}/b2api/v2/b2_delete_file_version",
            headers=headers,
            json={"fileId": file_id, "fileName": file_name},
            timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    def upload_file(self, file_path, bucket_name):
        self.authorize()
        file_size = os.path.getsize(file_path)
        file_name = os.path.basename(file_path)
        bucket_id = self._get_bucket_id(bucket_name)
        headers = {"Authorization": self.auth_token}

        if file_size <= CHUNK_SIZE:
            resp = requests.post(
                f"{self.api_url}/b2api/v2/b2_get_upload_url",
                headers=headers,
                json={"bucketId": bucket_id},
                timeout=30
            )
            resp.raise_for_status()
            upload_data = resp.json()
            with open(file_path, 'rb') as f:
                file_data = f.read()
            sha1_hash = hashlib.sha1(file_data).hexdigest()
            up_headers = {
                "Authorization": upload_data['authorizationToken'],
                "X-Bz-File-Name": file_name,
                "Content-Type": "video/mp4",
                "X-Bz-Content-Sha1": sha1_hash,
                "Content-Length": str(file_size)
            }
            up_resp = requests.post(upload_data['uploadUrl'], headers=up_headers, data=file_data, timeout=300)
            up_resp.raise_for_status()
        else:
            print(f"[INFO] Large file ({file_size/1024/1024:.1f} MB) — using multipart upload")
            start_resp = requests.post(
                f"{self.api_url}/b2api/v2/b2_start_large_file",
                headers=headers,
                json={"bucketId": bucket_id, "fileName": file_name, "contentType": "video/mp4"},
                timeout=30
            )
            start_resp.raise_for_status()
            file_id = start_resp.json()['fileId']
            part_sha1s = []
            part_number = 1
            with open(file_path, 'rb') as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    part_url_resp = requests.post(
                        f"{self.api_url}/b2api/v2/b2_get_upload_part_url",
                        headers=headers,
                        json={"fileId": file_id},
                        timeout=30
                    )
                    part_url_resp.raise_for_status()
                    part_data = part_url_resp.json()
                    sha1 = hashlib.sha1(chunk).hexdigest()
                    part_headers = {
                        "Authorization": part_data['authorizationToken'],
                        "X-Bz-Part-Number": str(part_number),
                        "Content-Length": str(len(chunk)),
                        "X-Bz-Content-Sha1": sha1
                    }
                    for attempt in range(3):
                        pr = requests.post(part_data['uploadUrl'], headers=part_headers, data=chunk, timeout=600)
                        if pr.status_code == 200:
                            break
                        print(f"[WARN] Part {part_number} attempt {attempt+1} failed: {pr.status_code}")
                        time.sleep(5)
                    pr.raise_for_status()
                    part_sha1s.append(sha1)
                    print(f"[INFO] Uploaded part {part_number} ({len(chunk)/1024/1024:.1f} MB)")
                    part_number += 1
            finish_resp = requests.post(
                f"{self.api_url}/b2api/v2/b2_finish_large_file",
                headers=headers,
                json={"fileId": file_id, "partSha1Array": part_sha1s},
                timeout=60
            )
            finish_resp.raise_for_status()

        return file_name

    def get_download_link(self, bucket_name, file_name):
        headers = {"Authorization": self.auth_token}
        bucket_id = self._get_bucket_id(bucket_name)
        resp = requests.post(
            f"{self.api_url}/b2api/v2/b2_get_download_authorization",
            headers=headers,
            json={"bucketId": bucket_id, "fileNamePrefix": file_name, "validDurationInSeconds": 86400},
            timeout=30
        )
        resp.raise_for_status()
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
        if now - self.last_update < 4 and current < total:
            return
        self.last_update = now
        percent = (current / total) * 100 if total > 0 else 0
        bar = self.get_bar(percent)
        speed = current / (now - self.start_time) if (now - self.start_time) > 0 else 0
        eta = time.strftime("%M:%S", time.gmtime((total - current) / speed)) if speed > 0 else "00:00"
        text = (
            f"⏳ *{action}...*\n"
            f"📄 File: `{self.filename}`\n"
            f"📊 Progress: `[{bar}] {percent:.1f}%`\n"
            f"⏱️ ETA: `{eta}`"
        )
        send_telegram("editMessageText", {
            "chat_id": self.chat_id,
            "message_id": self.msg_id,
            "text": text,
            "parse_mode": "Markdown"
        })


async def get_kv_tasks():
    kv_base = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}"
    headers = cf_kv_headers()

    # Fetch both task: and cmd: prefixed keys
    all_tasks = []
    for prefix in ["task:", "cmd:"]:
        list_resp = requests.get(f"{kv_base}/keys?prefix={prefix}", headers=headers, timeout=30)
        if list_resp.status_code == 401:
            raise Exception("Cloudflare KV 401 Unauthorized. Check CF_AUTH_EMAIL and CF_AUTH_KEY.")
        list_resp.raise_for_status()
        keys = list_resp.json().get("result", [])
        for k in keys:
            key_name = k["name"]
            val_resp = requests.get(f"{kv_base}/values/{key_name}", headers=headers, timeout=30)
            if val_resp.status_code != 200:
                print(f"[WARN] Could not read KV key '{key_name}': {val_resp.status_code}")
                continue
            try:
                val = val_resp.json()
            except Exception as e:
                print(f"[WARN] Failed to parse KV value for '{key_name}': {e}")
                continue
            all_tasks.append({"key": key_name, "data": val})
            del_resp = requests.delete(f"{kv_base}/values/{key_name}", headers=headers, timeout=30)
            if del_resp.status_code not in (200, 204):
                print(f"[WARN] Failed to delete KV key '{key_name}': {del_resp.status_code}")
    return all_tasks


async def process_storage_command(b2, cmd_data):
    chat_id = cmd_data.get("chatId")
    msg_id = cmd_data.get("msgId")
    cmd_type = cmd_data.get("type")

    def reply(text, keyboard=None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        if msg_id:
            payload["message_id"] = msg_id
            send_telegram("editMessageText", payload)
        else:
            send_telegram("sendMessage", payload)

    if cmd_type == "list_storage":
        print(f"[INFO] Listing B2 storage for chat_id={chat_id}")
        try:
            b2.authorize()
            files = b2.list_files(B2_BUCKET_NAME)

            if not files:
                reply("📊 *B2 သိုလှောင်မှုအခြေအနေ*\n\n✅ Bucket ဗလာဖြစ်နေသည် — ဖိုင်မရှိပါ။")
                return

            total_size = sum(f.get("contentLength", 0) for f in files)
            total_mb = total_size / 1024 / 1024
            total_gb = total_mb / 1024

            # Store filelist in KV for delete lookups
            filelist = [
                {"fileId": f["fileId"], "fileName": f["fileName"], "fileSize": f.get("contentLength", 0)}
                for f in files
            ]
            cf_kv_put(f"filelist:{chat_id}", json.dumps(filelist))

            header = (
                f"📊 *B2 သိုလှောင်မှုအခြေအနေ*\n\n"
                f"📁 ဖိုင်အရေအတွက်: `{len(files)}`\n"
                f"💾 သုံးထားသော နေရာ: `{total_gb:.3f} GB ({total_mb:.1f} MB)`\n"
                f"🔢 ကန်သတ်ချက်: `10 GB`\n"
                f"─────────────────────\n\n"
            )

            file_lines = []
            display_files = files[:20]
            for i, f in enumerate(display_files):
                size_mb = f.get("contentLength", 0) / 1024 / 1024
                name = f["fileName"]
                short_name = name[:30] + "…" if len(name) > 30 else name
                upload_ts = f.get("uploadTimestamp", 0) / 1000
                age_h = (time.time() - upload_ts) / 3600 if upload_ts else 0
                file_lines.append(f"`{i+1}.` 📄 `{short_name}`\n    💾 `{size_mb:.1f} MB`  ⏱ `{age_h:.1f}h ago`")

            if len(files) > 20:
                file_lines.append(f"\n_...နောက်ထပ် {len(files) - 20} ဖိုင် ရှိသေးသည်_")

            text = header + "\n".join(file_lines)

            # Build delete keyboard
            keyboard = []
            for i, f in enumerate(display_files):
                short = f["fileName"][:22] + "…" if len(f["fileName"]) > 22 else f["fileName"]
                size_mb = f.get("contentLength", 0) / 1024 / 1024
                keyboard.append([{
                    "text": f"🗑️ {short} ({size_mb:.0f}MB)",
                    "callback_data": f"del|{i}|{chat_id}"
                }])
            keyboard.append([{"text": "🔄 Refresh", "callback_data": "storage_refresh"}])

            reply(text, keyboard)
            print(f"[INFO] Storage list sent: {len(files)} files, {total_gb:.3f} GB")

        except Exception as e:
            print(f"[ERROR] list_storage failed: {e}")
            reply(f"❌ *B2 ဖိုင်စာရင်း ရယူမရပါ:*\n`{str(e)}`")

    elif cmd_type == "delete":
        file_id = cmd_data.get("fileId")
        file_name = cmd_data.get("fileName")
        print(f"[INFO] Deleting B2 file: {file_name} ({file_id})")
        try:
            b2.authorize()
            b2.delete_file(file_id, file_name)
            short = file_name[:40] + "…" if len(file_name) > 40 else file_name
            reply(
                f"✅ *ဖျက်သိမ်းပြီးပါပြီ!*\n\n"
                f"📄 `{short}`\n\n"
                f"_B2 Bucket မှ ဖျက်သိမ်းလိုက်ပါသည်။_\n\n"
                f"_ဖိုင်စာရင်း ကြည့်ရန် 📊 ခလုတ်ကို နှိပ်ပါ။_"
            )
            print(f"[INFO] Deleted: {file_name}")
        except Exception as e:
            print(f"[ERROR] delete failed: {e}")
            reply(f"❌ *ဖျက်မရပါ:*\n`{str(e)}`")


async def process_task(client, b2, task):
    data = task["data"]
    chat_id = data["chatId"]
    media_link = data["mediaLink"]
    msg_id = data["statusMessageId"]
    file_prefix = f"video_{int(time.time())}_{hashlib.md5(media_link.encode()).hexdigest()[:5]}"
    file_path = None

    reporter = ProgressReporter(chat_id, msg_id, "Extracting Metadata...")
    try:
        if "t.me/" in media_link:
            match = re.search(r't\.me/(?:c/)?([^/]+)/(\d+)', media_link)
            if not match:
                raise Exception("Invalid Telegram link format")
            chat_identifier, m_id = match.group(1), int(match.group(2))
            target_id = int(f"-100{chat_identifier}") if chat_identifier.isdigit() else chat_identifier
            entity = await client.get_entity(target_id)
            msg = await client.get_messages(entity, ids=m_id)
            if not msg or not msg.file:
                raise Exception("No downloadable file found in this Telegram message")
            reporter.filename = msg.file.name if msg.file.name else (file_prefix + ".mp4")
            actual_path = await client.download_media(
                msg, file_prefix,
                progress_callback=lambda c, t: reporter.update(c, t)
            )
            file_path = actual_path
            print(f"[INFO] Telethon saved file to: {file_path}")
        else:
            info_raw = subprocess.check_output(['yt-dlp', '--dump-json', media_link], timeout=60).decode()
            info = json.loads(info_raw)
            reporter.filename = (info.get('title', 'video')[:30] + ".mp4")
            out_path = file_prefix + ".mp4"
            result = subprocess.run(['yt-dlp', '-f', 'best', '-o', out_path, media_link], timeout=7200)
            if result.returncode != 0:
                raise Exception("yt-dlp failed to download the video")
            file_path = out_path

        if not file_path or not os.path.exists(file_path):
            raise Exception(f"Download failed: output file not found (expected: {file_path})")
        if os.path.getsize(file_path) == 0:
            raise Exception("Download failed: output file is empty")

        file_size_mb = os.path.getsize(file_path) / 1024 / 1024
        print(f"[INFO] Download complete: {file_path} ({file_size_mb:.1f} MB)")

        reporter.update(100, 100, action="Uploading to B2")
        uploaded_name = b2.upload_file(file_path, B2_BUCKET_NAME)
        link = b2.get_download_link(B2_BUCKET_NAME, uploaded_name)

        send_telegram("editMessageText", {
            "chat_id": chat_id,
            "message_id": msg_id,
            "text": (
                f"✅ *Download Complete!*\n\n"
                f"📄 File: `{reporter.filename}`\n"
                f"💾 Size: `{file_size_mb:.1f} MB`\n"
                f"🔗 [Direct Link]({link})\n\n"
                f"⏰ Link expires in 24 hours"
            ),
            "parse_mode": "Markdown"
        })
        print(f"[INFO] Task complete for chat_id={chat_id}")

    except Exception as e:
        print(f"[ERROR] process_task failed for chat_id={chat_id}: {e}")
        send_telegram("sendMessage", {
            "chat_id": chat_id,
            "text": f"❌ *Download Failed*\n\n`{str(e)}`",
            "parse_mode": "Markdown"
        })
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            print(f"[INFO] Cleaned up: {file_path}")


async def main():
    missing = []
    if not STRING_SESSION:     missing.append("STRING_SESSION_1")
    if not CF_AUTH_EMAIL:      missing.append("CF_AUTH_EMAIL")
    if not CF_AUTH_KEY:        missing.append("CF_AUTH_KEY")
    if not CF_ACCOUNT_ID:      missing.append("CF_ACCOUNT_ID")
    if not CF_KV_NAMESPACE_ID: missing.append("CF_KV_NAMESPACE_ID")
    if missing:
        raise Exception(f"Missing GitHub Secrets: {', '.join(missing)}")

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.connect()
    b2 = B2NativeClient(B2_KEY_ID, B2_APPLICATION_KEY)
    empty_polls = 0
    print("[INFO] Processor started. Polling KV for tasks...")
    while empty_polls < 10:
        tasks = await get_kv_tasks()
        if tasks:
            empty_polls = 0
            print(f"[INFO] Found {len(tasks)} task(s). Processing...")
            for task in tasks:
                if task["key"].startswith("cmd:"):
                    await process_storage_command(b2, task["data"])
                else:
                    await process_task(client, b2, task)
        else:
            empty_polls += 1
            print(f"[INFO] No tasks. Poll {empty_polls}/10. Sleeping 30s...")
            await asyncio.sleep(30)
    print("[INFO] No tasks after 10 polls. Exiting.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

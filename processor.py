import os
import asyncio
import re
import time
import json
import hashlib
import requests
import subprocess
import boto3
from botocore.config import Config
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
STRING_SESSION = os.environ.get("STRING_SESSION_1", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
E2_ACCESS_KEY_ID     = os.environ.get("E2_ACCESS_KEY_ID", "")
E2_SECRET_ACCESS_KEY = os.environ.get("E2_SECRET_ACCESS_KEY", "")
E2_ENDPOINT          = os.environ.get("E2_ENDPOINT", "")
E2_BUCKET_NAME       = os.environ.get("E2_BUCKET_NAME", "")
E2_REGION            = os.environ.get("E2_REGION", "us-west-2")
CF_AUTH_EMAIL      = os.environ.get("CF_AUTH_EMAIL", "")
CF_AUTH_KEY        = os.environ.get("CF_AUTH_KEY", "")
CF_ACCOUNT_ID      = os.environ.get("CF_ACCOUNT_ID", "")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID", "")


def get_s3():
    return boto3.client(
        "s3",
        endpoint_url=E2_ENDPOINT,
        aws_access_key_id=E2_ACCESS_KEY_ID,
        aws_secret_access_key=E2_SECRET_ACCESS_KEY,
        region_name=E2_REGION,
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def send_telegram(method, payload, retries=3):
    for attempt in range(retries):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                json=payload, timeout=15
            )
            if r.status_code == 200:
                return r.json()
            print(f"[WARN] Telegram {method} attempt {attempt+1} → {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[WARN] Telegram {method} attempt {attempt+1}: {e}")
        time.sleep(2)
    return None


def cf_kv_headers():
    return {"X-Auth-Email": CF_AUTH_EMAIL, "X-Auth-Key": CF_AUTH_KEY, "Content-Type": "application/json"}


def cf_kv_put(key, value):
    kv_base = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}"
    headers = {"X-Auth-Email": CF_AUTH_EMAIL, "X-Auth-Key": CF_AUTH_KEY}
    resp = requests.put(f"{kv_base}/values/{key}", headers=headers, data=value, timeout=30)
    if resp.status_code not in (200, 204):
        print(f"[WARN] cf_kv_put '{key}' → {resp.status_code}: {resp.text[:100]}")


async def get_kv_tasks():
    kv_base = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}"
    headers = cf_kv_headers()
    all_tasks = []
    for prefix in ["task:", "cmd:"]:
        list_resp = requests.get(f"{kv_base}/keys?prefix={prefix}", headers=headers, timeout=30)
        if list_resp.status_code == 401:
            raise Exception("CF KV 401 — check CF_AUTH_EMAIL / CF_AUTH_KEY")
        list_resp.raise_for_status()
        for k in list_resp.json().get("result", []):
            key_name = k["name"]
            val_resp = requests.get(f"{kv_base}/values/{key_name}", headers=headers, timeout=30)
            if val_resp.status_code != 200:
                print(f"[WARN] KV read '{key_name}': {val_resp.status_code}")
                continue
            try:
                val = val_resp.json()
            except Exception as e:
                print(f"[WARN] KV parse '{key_name}': {e}")
                continue
            all_tasks.append({"key": key_name, "data": val})
            del_resp = requests.delete(f"{kv_base}/values/{key_name}", headers=headers, timeout=30)
            if del_resp.status_code not in (200, 204):
                print(f"[WARN] KV delete '{key_name}': {del_resp.status_code}")
    return all_tasks


# ─── Storage Commands ──────────────────────────────────────────────────────────

async def process_storage_command(cmd_data):
    chat_id  = cmd_data.get("chatId")
    msg_id   = cmd_data.get("msgId")
    cmd_type = cmd_data.get("type")
    s3 = get_s3()

    def reply(text, keyboard=None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        if msg_id:
            payload["message_id"] = msg_id
            send_telegram("editMessageText", payload)
        else:
            send_telegram("sendMessage", payload)

    # ── list_storage ──────────────────────────────────────────────────────────
    if cmd_type == "list_storage":
        print(f"[INFO] Listing e2 storage for chat_id={chat_id}")
        try:
            paginator = s3.get_paginator("list_objects_v2")
            files = []
            for page in paginator.paginate(Bucket=E2_BUCKET_NAME):
                files.extend(page.get("Contents", []))

            if not files:
                reply("📊 *IDrive e2 သိုလှောင်မှုအခြေအနေ*\n\n✅ Bucket ဗလာဖြစ်နေသည် — ဖိုင်မရှိပါ။")
                return

            total_bytes = sum(f.get("Size", 0) for f in files)
            total_mb = total_bytes / 1024 / 1024
            total_gb = total_mb / 1024

            filelist = [{"key": f["Key"], "size": f.get("Size", 0)} for f in files]
            cf_kv_put(f"filelist:{chat_id}", json.dumps(filelist))

            header = (
                f"📊 *IDrive e2 သိုလှောင်မှုအခြေအနေ*\n\n"
                f"📁 ဖိုင်အရေအတွက်: `{len(files)}`\n"
                f"💾 သုံးထားသောနေရာ: `{total_gb:.3f} GB ({total_mb:.1f} MB)`\n"
                f"─────────────────────\n\n"
            )

            file_lines = []
            for i, f in enumerate(files[:15]):
                size_mb = f.get("Size", 0) / 1024 / 1024
                key = f["Key"]
                short_key = key[:30] + "…" if len(key) > 30 else key
                modified = f.get("LastModified")
                age_h = (time.time() - modified.timestamp()) / 3600 if modified else 0
                file_lines.append(f"`{i+1}.` 📄 `{short_key}`\n    💾 `{size_mb:.0f} MB`  ⏱ `{age_h:.1f}h ago`")

            if len(files) > 15:
                file_lines.append(f"\n_...နောက်ထပ် {len(files) - 15} ဖိုင် ရှိသေးသည်_")

            text = header + "\n".join(file_lines)

            # Per-file delete buttons (max 15)
            keyboard = []
            for i, f in enumerate(files[:15]):
                size_mb = f.get("Size", 0) / 1024 / 1024
                short = f["Key"][:22] + "…" if len(f["Key"]) > 22 else f["Key"]
                keyboard.append([{"text": f"🗑️ {short} ({size_mb:.0f}MB)", "callback_data": f"del|{i}|{chat_id}"}])

            # Delete All + Refresh buttons at bottom
            keyboard.append([
                {"text": f"💣 ဖိုင်အားလုံး ဖျက်မည် ({len(files)} ဖိုင်)", "callback_data": f"delete_all_confirm|{chat_id}|{len(files)}"}
            ])
            keyboard.append([{"text": "🔄 Refresh", "callback_data": "storage_refresh"}])

            reply(text, keyboard)
            print(f"[INFO] Storage list sent: {len(files)} files, {total_gb:.3f} GB")

        except Exception as e:
            print(f"[ERROR] list_storage: {e}")
            reply(f"❌ *ဖိုင်စာရင်း ရယူမရပါ:*\n`{str(e)}`")

    # ── delete (single file) ──────────────────────────────────────────────────
    elif cmd_type == "delete":
        file_key = cmd_data.get("fileKey")
        print(f"[INFO] Deleting e2 object: {file_key}")
        try:
            s3.delete_object(Bucket=E2_BUCKET_NAME, Key=file_key)
            short = file_key[:40] + "…" if len(file_key) > 40 else file_key
            reply(
                f"✅ *ဖျက်သိမ်းပြီးပါပြီ!*\n\n"
                f"📄 `{short}`\n\n"
                f"_📊 ခလုတ်နှိပ်ပြီး ဖိုင်စာရင်း ပြန်ကြည့်ပါ_"
            )
            print(f"[INFO] Deleted: {file_key}")
        except Exception as e:
            print(f"[ERROR] delete: {e}")
            reply(f"❌ *ဖျက်မရပါ:*\n`{str(e)}`")

    # ── delete_all ────────────────────────────────────────────────────────────
    elif cmd_type == "delete_all":
        print(f"[INFO] Delete ALL objects in bucket for chat_id={chat_id}")
        try:
            # List all objects with pagination
            paginator = s3.get_paginator("list_objects_v2")
            all_keys = []
            for page in paginator.paginate(Bucket=E2_BUCKET_NAME):
                for obj in page.get("Contents", []):
                    all_keys.append({"Key": obj["Key"]})

            if not all_keys:
                reply("✅ *Bucket ဗလာဖြစ်နေပြီ* — ဖျက်စရာ ဖိုင်မရှိပါ။")
                return

            total = len(all_keys)
            # Update status
            reply(f"💣 *ဖိုင် {total} ခု ဖျက်နေသည်...*\n\n_S3 batch delete — ခဏစောင့်ပါ..._")

            # Batch delete (1000 files per request)
            deleted = 0
            errors = []
            for i in range(0, len(all_keys), 1000):
                batch = all_keys[i:i+1000]
                resp = s3.delete_objects(
                    Bucket=E2_BUCKET_NAME,
                    Delete={"Objects": batch, "Quiet": True}
                )
                deleted += len(batch) - len(resp.get("Errors", []))
                errors.extend(resp.get("Errors", []))

            if errors:
                err_names = ", ".join(e.get("Key","?")[:20] for e in errors[:3])
                reply(
                    f"⚠️ *Partial Delete*\n\n"
                    f"✅ ဖျက်ပြီး: `{deleted}` ဖိုင်\n"
                    f"❌ မဖျက်ရ: `{len(errors)}` ဖိုင်\n"
                    f"Error: `{err_names}`"
                )
            else:
                reply(
                    f"💣 *ဖျက်သိမ်းပြီးပါပြီ!*\n\n"
                    f"✅ ဖိုင် `{deleted}` ခု အားလုံး ဖျက်သိမ်းလိုက်ပါသည်\n\n"
                    f"_Bucket ယခု ဗလာဖြစ်နေပါပြီ_"
                )
            print(f"[INFO] delete_all complete: {deleted} deleted, {len(errors)} errors")

        except Exception as e:
            print(f"[ERROR] delete_all: {e}")
            reply(f"❌ *Delete All မရပါ:*\n`{str(e)}`")


# ─── Progress Reporter ─────────────────────────────────────────────────────────

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
        send_telegram("editMessageText", {
            "chat_id": self.chat_id, "message_id": self.msg_id, "parse_mode": "Markdown",
            "text": (
                f"⏳ *{action}...*\n"
                f"📄 File: `{self.filename}`\n"
                f"📊 Progress: `[{bar}] {percent:.1f}%`\n"
                f"⏱️ ETA: `{eta}`"
            )
        })


# ─── Download Task ─────────────────────────────────────────────────────────────

async def process_task(client, task):
    data = task["data"]
    chat_id    = data["chatId"]
    media_link = data["mediaLink"]
    msg_id     = data["statusMessageId"]
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
                raise Exception("No downloadable file in this Telegram message")
            reporter.filename = msg.file.name if msg.file.name else (file_prefix + ".mp4")
            actual_path = await client.download_media(
                msg, file_prefix,
                progress_callback=lambda c, t: reporter.update(c, t)
            )
            file_path = actual_path
            print(f"[INFO] Telethon saved to: {file_path}")
        else:
            info_raw = subprocess.check_output(["yt-dlp", "--dump-json", media_link], timeout=60).decode()
            info = json.loads(info_raw)
            reporter.filename = info.get("title", "video")[:30] + ".mp4"
            out_path = file_prefix + ".mp4"
            result = subprocess.run(["yt-dlp", "-f", "best", "-o", out_path, media_link], timeout=7200)
            if result.returncode != 0:
                raise Exception("yt-dlp failed to download")
            file_path = out_path

        if not file_path or not os.path.exists(file_path):
            raise Exception(f"Output file not found: {file_path}")
        if os.path.getsize(file_path) == 0:
            raise Exception("Output file is empty")

        file_size_mb = os.path.getsize(file_path) / 1024 / 1024
        object_key   = os.path.basename(file_path)
        print(f"[INFO] Download OK: {file_path} ({file_size_mb:.1f} MB) — uploading to e2...")

        reporter.update(100, 100, action="Uploading to IDrive e2")
        s3 = get_s3()
        # IDrive e2: use put_object for files <4.9GB (avoids multipart permission issues)
        # Only files >4.9GB fall back to multipart upload
        file_size = os.path.getsize(file_path)
        if file_size < 4_900 * 1024 * 1024:
            with open(file_path, 'rb') as f:
                s3.put_object(Bucket=E2_BUCKET_NAME, Key=object_key, Body=f)
        else:
            from boto3.s3.transfer import TransferConfig
            tc = TransferConfig(multipart_threshold=5*1024*1024*1024, multipart_chunksize=100*1024*1024)
            s3.upload_file(file_path, E2_BUCKET_NAME, object_key, Config=tc)

        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": E2_BUCKET_NAME, "Key": object_key},
            ExpiresIn=86400
        )

        send_telegram("editMessageText", {
            "chat_id": chat_id, "message_id": msg_id, "parse_mode": "Markdown",
            "text": (
                f"✅ *Download Complete!*\n\n"
                f"📄 File: `{reporter.filename}`\n"
                f"💾 Size: `{file_size_mb:.1f} MB`\n"
                f"🔗 [Direct Link]({url})\n\n"
                f"⏰ Link expires in 24 hours"
            )
        })
        print(f"[INFO] Task complete for chat_id={chat_id}")

    except Exception as e:
        print(f"[ERROR] process_task for chat_id={chat_id}: {e}")
        send_telegram("sendMessage", {
            "chat_id": chat_id,
            "text": f"❌ *Download Failed*\n\n`{str(e)}`",
            "parse_mode": "Markdown"
        })
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            print(f"[INFO] Cleaned up: {file_path}")


# ─── Main ──────────────────────────────────────────────────────────────────────

async def main():
    missing = []
    if not STRING_SESSION:       missing.append("STRING_SESSION_1")
    if not E2_ACCESS_KEY_ID:     missing.append("E2_ACCESS_KEY_ID")
    if not E2_SECRET_ACCESS_KEY: missing.append("E2_SECRET_ACCESS_KEY")
    if not E2_ENDPOINT:          missing.append("E2_ENDPOINT")
    if not E2_BUCKET_NAME:       missing.append("E2_BUCKET_NAME")
    if not CF_AUTH_EMAIL:        missing.append("CF_AUTH_EMAIL")
    if not CF_AUTH_KEY:          missing.append("CF_AUTH_KEY")
    if not CF_ACCOUNT_ID:        missing.append("CF_ACCOUNT_ID")
    if not CF_KV_NAMESPACE_ID:   missing.append("CF_KV_NAMESPACE_ID")
    if missing:
        raise Exception(f"Missing GitHub Secrets: {', '.join(missing)}")

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.connect()
    empty_polls = 0
    print("[INFO] Processor started (IDrive e2). Polling KV...")
    while empty_polls < 10:
        tasks = await get_kv_tasks()
        if tasks:
            empty_polls = 0
            print(f"[INFO] {len(tasks)} task(s) found.")
            for task in tasks:
                if task["key"].startswith("cmd:"):
                    await process_storage_command(task["data"])
                else:
                    await process_task(client, task)
        else:
            empty_polls += 1
            print(f"[INFO] No tasks — poll {empty_polls}/10 — sleeping 30s...")
            await asyncio.sleep(30)
    print("[INFO] Exiting after 10 empty polls.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

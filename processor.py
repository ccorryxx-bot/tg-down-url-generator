import os
import asyncio
import aiohttp
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
                    # Generate Public URL
                    return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{file_name}"
                else:
                    text = await resp.text()
                    raise Exception(f"Supabase Upload Failed: {text}")

async def main():
    # User Client for Downloading Restricted Content
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    # Bot Client for Sending Messages
    bot = TelegramClient('bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)
    await client.connect()
    
    try:
        # 1. Parse Link and Download
        parts = MEDIA_LINK.split('/')
        msg_id = int(parts[-1])
        chat_id = parts[-2]
        if chat_id.isdigit():
            chat_id = int(f"-100{chat_id}")
            
        await bot.send_message(TARGET_CHAT_ID, "⏳ Downloading media...")
        msg = await client.get_messages(chat_id, ids=msg_id)
        
        if not msg or not msg.media:
            await bot.send_message(TARGET_CHAT_ID, "❌ Media not found or link invalid.")
            return
            
        file_path = await client.download_media(msg)
        await bot.send_message(TARGET_CHAT_ID, "✅ Downloaded. Uploading to Supabase and Telegram...")
        
        # 2. Upload to Telegram (as file)
        await bot.send_file(TARGET_CHAT_ID, file_path, caption="Here is your media!")
        
        # 3. Upload to Supabase for Direct Link
        if SUPABASE_URL and SUPABASE_KEY:
            file_name = os.path.basename(file_path)
            public_url = await upload_to_supabase(file_path, file_name)
            await bot.send_message(TARGET_CHAT_ID, f"🔗 Direct Download Link:\n`{public_url}`")
            
    except Exception as e:
        await bot.send_message(TARGET_CHAT_ID, f"❌ Error: {str(e)}")
    finally:
        await client.disconnect()
        await bot.disconnect()

if __name__ == "__main__":
    asyncio.run(main())

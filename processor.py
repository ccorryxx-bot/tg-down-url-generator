import os
import asyncio
import boto3
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaWebPage

# Configs
API_ID = int(os.environ.get('API_ID'))
API_HASH = os.environ.get('API_HASH')
STRING_SESSION = os.environ.get('STRING_SESSION')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
MEDIA_LINK = os.environ.get('MEDIA_LINK')
TARGET_CHAT_ID = int(os.environ.get('TARGET_CHAT_ID'))

# R2 Configs
R2_ACCESS_KEY = os.environ.get('R2_ACCESS_KEY_ID')
R2_SECRET_KEY = os.environ.get('R2_SECRET_ACCESS_KEY')
R2_BUCKET = os.environ.get('R2_BUCKET_NAME')
R2_ENDPOINT = os.environ.get('R2_ENDPOINT_URL')
PUBLIC_PREFIX = os.environ.get('PUBLIC_URL_PREFIX')

async def main():
    # User Client for Downloading Restricted Content
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    # Bot Client for Sending Messages
    bot = TelegramClient('bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)
    
    await client.connect()
    
    try:
        # 1. Parse Link and Download
        # Example: https://t.me/c/123456789/123 or https://t.me/channel/123
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
        await bot.send_message(TARGET_CHAT_ID, "✅ Downloaded. Uploading to R2 and Telegram...")

        # 2. Upload to Telegram (as file)
        await bot.send_file(TARGET_CHAT_ID, file_path, caption="Here is your media!")

        # 3. Upload to R2 for Direct Link
        if R2_BUCKET:
            s3 = boto3.client('s3',
                endpoint_url=R2_ENDPOINT,
                aws_access_key_id=R2_ACCESS_KEY,
                aws_secret_access_key=R2_SECRET_KEY
            )
            file_name = os.path.basename(file_path)
            s3.upload_file(file_path, R2_BUCKET, file_name)
            
            direct_link = f"{PUBLIC_PREFIX}/{file_name}"
            await bot.send_message(TARGET_CHAT_ID, f"🔗 Direct Download Link:\n`{direct_link}`")

    except Exception as e:
        await bot.send_message(TARGET_CHAT_ID, f"❌ Error: {str(e)}")
    finally:
        await client.disconnect()
        await bot.disconnect()

if __name__ == "__main__":
    asyncio.run(main())

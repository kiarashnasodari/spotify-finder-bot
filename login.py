"""
یک‌بار اجرا کن تا SESSION_STRING اکانت جستجوگر ساخته بشه.
شماره، کد تأیید و (اگه داری) رمز دو مرحله‌ای رو می‌پرسه.
"""
import os
from urllib.parse import urlparse

from dotenv import load_dotenv
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

load_dotenv()
api_id = int(os.getenv("API_ID") or input("API_ID: "))
api_hash = os.getenv("API_HASH") or input("API_HASH: ")

proxy = None
if os.getenv("PROXY"):
    u = urlparse(os.getenv("PROXY"))
    proxy = {"proxy_type": u.scheme, "addr": u.hostname, "port": u.port,
             "username": u.username, "password": u.password, "rdns": True}

with TelegramClient(StringSession(), api_id, api_hash, proxy=proxy) as client:
    print("\n✅ این رو کامل کپی کن و تو .env جلوی SESSION_STRING بذار:\n")
    print(client.session.save())

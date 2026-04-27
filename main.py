import asyncio
import logging
from aiogram import Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message
import aiohttp
from dotenv import load_dotenv
import os
import uvicorn
from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from crud import (
    async_session, engine, Base, get_chats, get_chat, get_messages, create_chat,
    create_message, update_chat_waiting, update_chat_ai, update_chat_mark, get_stats,
    get_chats_with_last_messages, get_chat_messages, get_chat_by_uuid,
    add_chat_tag, remove_chat_tag,
    create_sticker, get_all_stickers, get_sticker_by_id, get_sticker_by_file_unique_id, delete_sticker
)
import requests
from pydantic import BaseModel
from shared import get_bot
import json
from contextlib import asynccontextmanager
from typing import List, Dict, Any, Optional
from datetime import datetime
from sqlalchemy import select, insert
from crud import Message
import crud
from aiogram import F
from minio import Minio
import io
import tempfile
import threading
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from aiogram.types import FSInputFile
import auth
import notifications

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)


VK_TOKEN = os.getenv("VK_TOKEN")        # токен сообщества
VK_GROUP_ID = int(os.getenv("VK_GROUP_ID"))  # ID вашего сообщества

# Синхронные объекты vk_api
vk_session = vk_api.VkApi(token=VK_TOKEN)
vk          = vk_session.get_api()
longpoll    = VkBotLongPoll(vk_session, VK_GROUP_ID)

# Асинхронная очередь для передачи событий из потока
queue: asyncio.Queue = asyncio.Queue()

def start_poller(loop: asyncio.AbstractEventLoop):
    """Запускаем блокирующий longpoll.listen() в фоновом потоке
       и шлём события в asyncio.Queue."""
    logging.info("▶️ Запускаю VK-poller thread")
    def _poller():
        try:
            logging.info("🟢 VK bot started polling")
            for event in longpoll.listen():
                logging.info(f"🟢 VK event received: {event.type}")
                # передаём событие в цикл
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as e:
            logging.error(f"❌ VK poller error: {e}")
            # Перезапускаем поллер при ошибке
            loop.call_soon_threadsafe(lambda: start_poller(loop))
    thread = threading.Thread(target=_poller, daemon=True)
    thread.start()

async def handle_events():
    """Асинхронно обрабатываем события из очереди."""
    logging.info("👂 Начинаю асинхронно обрабатывать VK-события")
    while True:
        event = await queue.get()
        logging.debug("⚪ Взял из очереди событие: %s", event)

        if event.type != VkBotEventType.MESSAGE_NEW:
            continue

        msg = event.object['message']
        peer_id = msg['peer_id']
        user_id = msg['from_id']
        text = msg.get('text', "")
        attachments = msg.get("attachments", [])

        # Получаем информацию о пользователе
        try:
            user_info = await asyncio.to_thread(
                vk.users.get,
                user_ids=[user_id],
                fields=['first_name', 'last_name']
            )
            if user_info:
                user = user_info[0]
                user_name = f"{user['first_name']} {user['last_name']}"
            else:
                user_name = str(user_id)
        except Exception as e:
            logging.error(f"Error getting VK user info: {e}")
            user_name = str(user_id)

        # Определяем, ответ ли это от паблика
        is_from_group = (user_id == -VK_GROUP_ID)

        # --- 1) Работа с чатом в БД, WebSocket-апдейты ---
        async with async_session() as session:
            # Получаем или создаём чат
            chat = await get_chat_by_uuid(session, str(peer_id))
            if not chat:
                chat = await create_chat(
                    session,
                    str(peer_id),
                    name=user_name,
                    messager="vk"
                )
                new_chat_message = {
                    "type": "chat_created",
                    "chat": {
                        "id": chat.id,
                        "uuid": chat.uuid,
                        "name": chat.name,
                        "messager": chat.messager,
                        "waiting": chat.waiting,
                        "ai": chat.ai,
                        "tags": chat.tags,
                        "last_message_content": None,
                        "last_message_timestamp": None
                    }
                }
                await updates_manager.broadcast(json.dumps(new_chat_message))

            # --- 2) Текстовое сообщение ---
            if text:
                if is_from_group:
                    # Сообщение от админа/паблика — сохраняем как answer
                    db_msg = Message(
                        chat_id=chat.id,
                        message=text,
                        message_type="answer",
                        ai=True,
                        created_at=datetime.now()
                    )
                    session.add(db_msg)
                    await session.commit()
                    await session.refresh(db_msg)

                    await messages_manager.broadcast(json.dumps({
                        "type": "message",
                        "chatId": str(db_msg.chat_id),
                        "content": db_msg.message,
                        "message_type": db_msg.message_type,
                        "ai": db_msg.ai,
                        "timestamp": db_msg.created_at.isoformat(),
                        "id": db_msg.id
                    }))

                    # Сбрасываем waiting, т.к. менеджер ответил
                    await update_chat_waiting(db=session, chat_id=chat.id, waiting=False)
                    await updates_manager.broadcast(json.dumps({
                        "type": "chat_update",
                        "chat_id": chat.id,
                        "waiting": False
                    }))
                else:
                    # Сообщение от пользователя
                    db_msg = Message(
                        chat_id=chat.id,
                        message=text,
                        message_type="question",
                        ai=False,
                        created_at=datetime.now()
                    )
                    session.add(db_msg)
                    await session.commit()
                    await session.refresh(db_msg)

                    # Шлём фронту через WS
                    message_for_frontend = {
                        "type": "message",
                        "chatId": str(db_msg.chat_id),
                        "content": db_msg.message,
                        "message_type": db_msg.message_type,
                        "ai": db_msg.ai,
                        "timestamp": db_msg.created_at.isoformat(),
                        "id": db_msg.id
                    }
                    await messages_manager.broadcast(json.dumps(message_for_frontend))

                    # Если AI выключен — обновляем waiting и выходим
                    if not chat.ai:
                        await update_chat_waiting(db=session, chat_id=chat.id, waiting=True)
                        await updates_manager.broadcast(json.dumps({
                            "type": "chat_update",
                            "chat_id": chat.id,
                            "waiting": True
                        }))
                        
                        # Отправляем уведомление админам
                        notification_manager = notifications.get_notification_manager()
                        if notification_manager:
                            await notification_manager.send_waiting_notification(
                                chat_id=peer_id,
                                chat_name=user_name,
                                messager="vk"
                            )
                    else:
                        # --- 3) Отправляем вопрос AI-сервису и ждём ответ ---
                        async with aiohttp.ClientSession() as http_sess:
                            try:
                                resp = await http_sess.post(
                                    API_URL,
                                    json={"question": text, "chat_id": chat.id}
                                )
                                data = await resp.json()
                            except Exception as e:
                                logging.error(f"AI request error: {e}")
                                continue

                        # Если пришёл ответ
                        if data.get("answer"):
                            answer = data["answer"]
                            # Отправляем ответ обратно в VK
                            await asyncio.to_thread(
                                vk.messages.send,
                                peer_id=peer_id,
                                message=answer,
                                random_id=0
                            )
                            # Сохраняем ответ в БД
                            db_ans = Message(
                                chat_id=chat.id,
                                message=answer,
                                message_type="answer",
                                ai=True,
                                created_at=datetime.now()
                            )
                            session.add(db_ans)
                            await session.commit()
                            await session.refresh(db_ans)
                            # Шлём фронту
                            await messages_manager.broadcast(json.dumps({
                                "type": "message",
                                "chatId": str(db_ans.chat_id),
                                "content": db_ans.message,
                                "message_type": db_ans.message_type,
                                "ai": db_ans.ai,
                                "timestamp": db_ans.created_at.isoformat(),
                                "id": db_ans.id
                            }))

                        # Если сервис переключил на менеджера
                        if data.get("manager") == "true":
                            await update_chat_waiting(db=session, chat_id=chat.id, waiting=True)
                            await update_chat_ai(db=session, chat_id=chat.id, ai=False)
                            await updates_manager.broadcast(json.dumps({
                                "type": "chat_update",
                                "chat_id": chat.id,
                                "waiting": True,
                                "ai": False
                            }))
                            
                            # Отправляем уведомление админам
                            notification_manager = notifications.get_notification_manager()
                            if notification_manager:
                                await notification_manager.send_waiting_notification(
                                    chat_id=peer_id,
                                    chat_name=user_name,
                                    messager="vk"
                                )

            # --- 4) Обработка фото-вложений ---
            for att in attachments:
                if att["type"] != "photo":
                    continue
                
                # Получаем прямые URL фотографии
                photo = att["photo"]
                logging.info(f"VK photo data: {json.dumps(photo, indent=2)}")
                
                # Пробуем получить URL в порядке убывания размера
                url = None
                for size in ['photo_1280', 'photo_807', 'photo_604', 'photo_130', 'photo_75']:
                    if size in photo:
                        url = photo[size]
                        logging.info(f"Found photo URL for size {size}: {url}")
                        break
                
                if not url:
                    # Если не нашли прямые URL, пробуем получить из sizes
                    if "sizes" in photo:
                        sizes = photo["sizes"]
                        max_size = max(sizes, key=lambda s: s["height"])
                        url = max_size["url"]
                        logging.info(f"Using URL from sizes: {url}")
                    else:
                        logging.error("No suitable photo URL found")
                        continue
                
                logging.info(f"Selected VK photo URL: {url}")
                
                # Скачиваем картинку
                try:
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                        'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
                        'Accept-Language': 'en-US,en;q=0.9',
                        'Referer': 'https://vk.com/'
                    }
                    async with aiohttp.ClientSession() as http_sess:
                        resp = await http_sess.get(url, headers=headers)
                        if resp.status != 200:
                            logging.error(f"Failed to download VK photo: {resp.status}")
                            continue
                        content = await resp.read()
                        if not content:
                            logging.error("Empty photo content received")
                            continue
                        logging.info(f"Successfully downloaded VK photo, size: {len(content)} bytes")
                except Exception as e:
                    logging.error(f"VK photo download error: {e}")
                    continue

                # Загружаем в Minio
                file_ext = os.path.splitext(url.split('?')[0])[1] or ".jpg"
                file_name = f"{peer_id}-{int(datetime.now().timestamp())}{file_ext}"
                logging.info(f"Attempting to upload to MinIO: {file_name}")
                
                try:
                    # Создаем BytesIO объект с правильным размером
                    file_data = io.BytesIO(content)
                    file_data.seek(0, 2)  # Перемещаемся в конец файла
                    file_size = file_data.tell()  # Получаем размер
                    file_data.seek(0)  # Возвращаемся в начало
                    
                    logging.info(f"Uploading to MinIO: {file_name}, size: {file_size} bytes")
                    
                    await asyncio.to_thread(
                        minio_client.put_object,
                        BUCKET_NAME,
                        file_name,
                        file_data,
                        file_size,
                        content_type="image/jpeg"
                    )
                    img_url = f"{MINIO_PUBLIC_URL}/{BUCKET_NAME}/{file_name}"
                    logging.info(f"Successfully uploaded to MinIO: {img_url}")
                except Exception as e:
                    logging.error(f"MinIO upload error: {e}")
                    continue

                # Сохраняем как сообщение
                try:
                    if is_from_group:
                        db_img = Message(
                            chat_id=chat.id,
                            message=img_url,
                            message_type="answer",
                            ai=True,
                            created_at=datetime.now(),
                            is_image=True
                        )
                    else:
                        db_img = Message(
                            chat_id=chat.id,
                            message=img_url,
                            message_type="question",
                            ai=False,
                            created_at=datetime.now(),
                            is_image=True
                        )
                    session.add(db_img)
                    await session.commit()
                    await session.refresh(db_img)
                    logging.info(f"Successfully saved message to database with image URL: {img_url}")
                except Exception as e:
                    logging.error(f"Database error while saving image message: {e}")
                    continue
                # Шлём на фронт
                await messages_manager.broadcast(json.dumps({
                    "type": "message",
                    "chatId": str(db_img.chat_id),
                    "content": db_img.message,
                    "message_type": db_img.message_type,
                    "ai": db_img.ai,
                    "timestamp": db_img.created_at.isoformat(),
                    "id": db_img.id,
                    "is_image": True
                }))
                # Обновление waiting
                if is_from_group:
                    await update_chat_waiting(db=session, chat_id=chat.id, waiting=False)
                    await updates_manager.broadcast(json.dumps({
                        "type": "chat_update",
                        "chat_id": chat.id,
                        "waiting": False
                    }))
                else:
                    await update_chat_waiting(db=session, chat_id=chat.id, waiting=True)
                    await updates_manager.broadcast(json.dumps({
                        "type": "chat_update",
                        "chat_id": chat.id,
                        "waiting": True
                    }))
                    
                    # Отправляем уведомление админам
                    notification_manager = notifications.get_notification_manager()
                    if notification_manager:
                        await notification_manager.send_waiting_notification(
                            chat_id=peer_id,
                            chat_name=user_name,
                            messager="vk"
                        )

async def start_vk_bot():
    loop = asyncio.get_running_loop()
    start_poller(loop)
    print("Async VK-бот запущен. Ожидаем сообщений…")
    await handle_events()


# Initialize bot and dispatcher
bot = get_bot()
dp = Dispatcher()

# API endpoint for sending questions
API_URL = os.getenv("API_URL", "http://pavel")
APP_HOST = os.getenv("APP_HOST", "localhost")
STICKER_SOURCE_TG_ID = os.getenv("STICKER_SOURCE_TG_ID")
MINIO_PUBLIC_URL = os.getenv("MINIO_PUBLIC_URL", f"http://{APP_HOST}:9000")
MINIO_LOGIN = os.getenv("MINIO_LOGIN")
MINIO_PWD = os.getenv("MINIO_PWD")
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
ENABLE_BOTS = os.getenv("ENABLE_BOTS", "true").lower() == "true"

BUCKET_NAME = "psih-photo"
minio_client = Minio(
    endpoint=MINIO_ENDPOINT,
    access_key=MINIO_LOGIN,
    secret_key=MINIO_PWD,
    secure=False  # True для HTTPS
)

# Create database tables
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Инициализируем БД
    await init_db()
    
    # Инициализируем менеджер уведомлений
    notifications.init_notification_manager(bot)
    
    tg_task = None
    vk_task = None

    if ENABLE_BOTS:
        # Запускаем aiogram-бота
        tg_task = asyncio.create_task(dp.start_polling(bot))
        
        # Запускаем VK бота
        vk_task = asyncio.create_task(start_vk_bot())
    else:
        print("[INFO] Боты отключены (ENABLE_BOTS=false)")
    
    yield
    
    if tg_task:
        # Завершаем aiogram-бота
        tg_task.cancel()
        try:
            await tg_task
        except asyncio.CancelledError:
            pass

    if vk_task:
        # Завершаем VK-бота
        vk_task.cancel()
        try:
            await vk_task
        except asyncio.CancelledError:
            pass

app = FastAPI(lifespan=lifespan)

# Увеличиваем лимит размера файла до 10MB
# Parse ALLOWED_ORIGINS from env (used by docker-compose) or fallback to defaults
_allowed_origins_raw = os.getenv("ALLOWED_ORIGINS", "")
if _allowed_origins_raw:
    ALLOWED_ORIGINS = [o.strip() for o in _allowed_origins_raw.split(",") if o.strip()]
else:
    ALLOWED_ORIGINS = ["http://82.202.143.118:8080", "http://localhost:8080"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dependency
async def get_db():
    async with async_session() as session:
        yield session

# WebSocket connection managers
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception as e:
                logging.error(f"Error broadcasting message: {e}")
                disconnected.append(connection)
        for connection in disconnected:
            self.disconnect(connection)

# Create separate managers for messages and updates
messages_manager = ConnectionManager()
updates_manager = ConnectionManager()

# WebSocket endpoints
@app.websocket("/ws/messages")
async def messages_websocket(websocket: WebSocket):
    await messages_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                message_data = json.loads(data)
                # If message is from frontend (manager), send it to the bot
                if "chatId" in message_data and "content" in message_data:
                    try:
                        chat_id = int(message_data["chatId"])
                        await bot.send_message(chat_id=chat_id, text=message_data["content"])
                        # Create message in database
                        chat = await get_chat(async_session(), chat_id)
                        if chat:
                            await create_message(
                                async_session(),
                                chat.id,
                                message_data["content"],
                                "text",
                                False
                            )
                            # Send update to all clients
                            update_message = {
                                "type": "update",
                                "chatId": str(chat_id),
                                "content": message_data["content"],
                                "message_type": "text",
                                "ai": False,
                                "timestamp": datetime.now().isoformat()
                            }
                            await updates_manager.broadcast(json.dumps(update_message))
                    except (ValueError, TypeError) as e:
                        logging.error(f"Invalid chat_id format: {e}")
                # If message is from bot, broadcast it to all frontend clients
                else:
                    await messages_manager.broadcast(data)
            except json.JSONDecodeError as e:
                logging.error(f"Error parsing message: {e}")
    except WebSocketDisconnect:
        messages_manager.disconnect(websocket)

@app.websocket("/ws/updates")
async def updates_websocket(websocket: WebSocket):
    await updates_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                update_data = json.loads(data)
                # Broadcast the update to all connected clients
                await updates_manager.broadcast(data)
            except json.JSONDecodeError as e:
                logging.error(f"Error parsing update: {e}")
    except WebSocketDisconnect:
        updates_manager.disconnect(websocket)

# Endpoints
@app.get("/api/chats")
async def read_chats(db: AsyncSession = Depends(get_db), _: bool = Depends(auth.require_auth)):
    chats_data = await get_chats_with_last_messages(db)
    print("Backend /api/chats response data:", chats_data)
    return chats_data

@app.get("/api/chats/{chat_id}")
async def read_chat(chat_id: int, db: AsyncSession = Depends(get_db), _: bool = Depends(auth.require_auth)):
    chat = await get_chat(db, chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat

@app.get("/api/chats/{chat_id}/messages")
async def read_messages(
    chat_id: int,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth.require_auth)
):
    return await get_chat_messages(db, chat_id)

# Schemas
class ChatCreate(BaseModel):
    uuid: str
    ai: bool = False

@app.post("/api/chats")
async def create_chat_endpoint(chat: ChatCreate, db: AsyncSession = Depends(get_db), _: bool = Depends(auth.require_auth)):
    return await create_chat(db, chat.uuid, chat.ai)

class MessageCreate(BaseModel):
    chat_id: int
    message: str
    message_type: str
    ai: bool = False

@app.post("/api/messages")
async def create_message_endpoint(msg: MessageCreate, db: AsyncSession = Depends(get_db), _: bool = Depends(auth.require_auth)):
    # 1. Создаем сообщение в БД
    db_msg = await create_message(
        db=db,
        chat_id=msg.chat_id,
        message=msg.message,
        message_type=msg.message_type,
        ai=msg.ai
    )

    # 2. Получаем информацию о чате
    chat = await get_chat(db, msg.chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    # 3. Отправляем сообщение в соответствующий мессенджер
    try:
        if chat.messager == "telegram":
            # Отправка в Telegram
            await bot.send_message(
                chat_id=chat.uuid,
                text=msg.message
            )
        elif chat.messager == "vk":
            # Отправка в VK
            await asyncio.to_thread(
                vk.messages.send,
                peer_id=int(chat.uuid),
                message=msg.message,
                random_id=0
            )
    except Exception as e:
        logging.error(f"Error sending message to {chat.messager}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to send message to {chat.messager}")

    # 4. Отправляем сообщение через WebSocket
    message_for_frontend = {
        "type": "message",
        "chatId": str(db_msg.chat_id),
        "content": db_msg.message,
        "message_type": db_msg.message_type,
        "ai": db_msg.ai,
        "timestamp": db_msg.created_at.isoformat(),
        "id": db_msg.id
    }
    await messages_manager.broadcast(json.dumps(message_for_frontend))

    return db_msg

class WaitingUpdate(BaseModel):
    waiting: bool

@app.put("/api/chats/{chat_id}/waiting")
async def update_waiting(chat_id: int, data: WaitingUpdate, db: AsyncSession = Depends(get_db), _: bool = Depends(auth.require_auth)):
    chat = await get_chat(db, chat_id)
    old_waiting = chat.waiting
    chat = await update_chat_waiting(db, chat_id, data.waiting)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    
    if chat.waiting != old_waiting:
        stats = await get_stats(db)
        await updates_manager.broadcast(json.dumps({
                    "type": "stats_update",
                    "total": stats["total"], 
                    "pending": stats["pending"], 
                    "ai": stats["ai"]
                }))
    return {"success": True, "chat": chat}

class AIUpdate(BaseModel):
    ai: bool

@app.put("/api/chats/{chat_id}/ai")
async def update_ai(chat_id: int, data: AIUpdate, db: AsyncSession = Depends(get_db), _: bool = Depends(auth.require_auth)):
    chat = await update_chat_ai(db, chat_id, data.ai)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    # Отправляем обновление по WebSocket
    update_message = {
        "type": "chat_ai_updated",
        "chatId": str(chat_id),
        "ai": chat.ai
    }
    await updates_manager.broadcast(json.dumps(update_message))
    return chat

class MarkUpdate(BaseModel):
    mark: str | None

@app.put("/api/chats/{chat_id}/mark")
async def update_mark(chat_id: int, data: MarkUpdate, db: AsyncSession = Depends(get_db), _: bool = Depends(auth.require_auth)):
    chat = await update_chat_mark(db, chat_id, data.mark)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"success": True, "chat": chat}

@app.get("/api/stats")
async def stats(db: AsyncSession = Depends(get_db), _: bool = Depends(auth.require_auth)):
    return await get_stats(db)


# ─── Sticker endpoints ───────────────────────────────────────────

@app.get("/api/stickers")
async def list_stickers(db: AsyncSession = Depends(get_db), _: bool = Depends(auth.require_auth)):
    """Возвращает список всех сохранённых стикеров."""
    stickers = await get_all_stickers(db)
    return [
        {
            "id": s.id,
            "file_id": s.file_id,
            "file_unique_id": s.file_unique_id,
            "emoji": s.emoji,
            "set_name": s.set_name,
            "custom_tag": s.custom_tag,
            "is_animated": s.is_animated,
            "is_video": s.is_video,
            "file_url": s.file_url,
            "created_at": s.created_at.isoformat() if s.created_at else None
        }
        for s in stickers
    ]


class StickerMessageCreate(BaseModel):
    chat_id: int
    sticker_id: int


class StickerTagUpdate(BaseModel):
    tag: str


@app.post("/api/messages/sticker")
async def send_sticker_message(
    data: StickerMessageCreate,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth.require_auth)
):
    """Отправляет сохранённый стикер в указанный чат."""
    chat = await get_chat(db, data.chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    sticker = await get_sticker_by_id(db, data.sticker_id)
    if not sticker:
        raise HTTPException(status_code=404, detail="Sticker not found")

    # Отправка в мессенджер
    try:
        if chat.messager == "telegram":
            await bot.send_sticker(chat_id=chat.uuid, sticker=sticker.file_id)
        else:
            raise HTTPException(status_code=400, detail="Stickers are supported for Telegram only")
    except Exception as e:
        logging.error(f"Error sending sticker: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to send sticker: {e}")

    # Сохраняем в БД как сообщение
    db_msg = Message(
        chat_id=chat.id,
        message=sticker.emoji or "[sticker]",
        message_type="answer",
        ai=False,
        created_at=datetime.now(),
        is_sticker=True
    )
    db.add(db_msg)
    await db.commit()
    await db.refresh(db_msg)

    # WebSocket broadcast
    message_for_frontend = {
        "type": "message",
        "chatId": str(db_msg.chat_id),
        "content": db_msg.message,
        "message_type": db_msg.message_type,
        "ai": db_msg.ai,
        "timestamp": db_msg.created_at.isoformat(),
        "id": db_msg.id,
        "is_sticker": True,
        "sticker_file_id": sticker.file_id
    }
    await messages_manager.broadcast(json.dumps(message_for_frontend))

    return {"success": True, "message_id": db_msg.id}


@app.put("/api/stickers/{sticker_id}")
async def update_sticker_tag(
    sticker_id: int,
    tag_data: StickerTagUpdate,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth.require_auth)
):
    """Обновляет custom_tag (псевдоним/категорию) стикера."""
    sticker = await get_sticker_by_id(db, sticker_id)
    if not sticker:
        raise HTTPException(status_code=404, detail="Sticker not found")
    sticker.custom_tag = tag_data.tag
    await db.commit()
    await db.refresh(sticker)
    return {
        "id": sticker.id,
        "custom_tag": sticker.custom_tag,
        "emoji": sticker.emoji,
        "file_unique_id": sticker.file_unique_id
    }


@app.delete("/api/stickers/{sticker_id}")
async def remove_sticker(
    sticker_id: int,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth.require_auth)
):
    """Удаляет стикер из библиотеки бота."""
    ok = await delete_sticker(db, sticker_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Sticker not found")
    return {"success": True}


class TagCreate(BaseModel):
    tag: str

@app.post("/api/chats/{chat_id}/tags")
async def add_chat_tag_endpoint(chat_id: int, tag_data: TagCreate, db: AsyncSession = Depends(get_db), _: bool = Depends(auth.require_auth)):
    result = await crud.add_chat_tag(db, chat_id, tag_data.tag)
    if result.get("success"):
        # Broadcast updated tags via WebSocket
        update_message = {
            "type": "chat_tags_updated",
            "chatId": chat_id,
            "tags": result["tags"]
        }
        await updates_manager.broadcast(json.dumps(update_message))
    return result

@app.delete("/api/chats/{chat_id}/tags/{tag}")
async def remove_chat_tag_endpoint(chat_id: int, tag: str, db: AsyncSession = Depends(get_db), _: bool = Depends(auth.require_auth)):
    result = await crud.remove_chat_tag(db, chat_id, tag)
    if result.get("success"):
        # Broadcast updated tags via WebSocket
        update_message = {
            "type": "chat_tags_updated",
            "chatId": chat_id,
            "tags": result["tags"]
        }
        await updates_manager.broadcast(json.dumps(update_message))
    return result

@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: int, db: AsyncSession = Depends(get_db), _: bool = Depends(auth.require_auth)):
    chat = await get_chat(db, chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    # Удаляем все сообщения этого чата
    messages = await db.execute(select(Message).where(Message.chat_id == chat_id))
    for msg in messages.scalars().all():
        await db.delete(msg)
    await db.delete(chat)
    await db.commit()
    # Отправляем уведомление по WebSocket всем фронтендам
    update_message = {
        "type": "chat_deleted",
        "chatId": str(chat_id)
    }
    await updates_manager.broadcast(json.dumps(update_message))
    return {"success": True}

@app.post("/api/messages/image")
async def upload_image(
    image: UploadFile = File(...),
    chat_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(auth.require_auth)
):
    # 1. Получаем информацию о чате
    chat = await get_chat(db, chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    # 2. Читаем содержимое файла
    content = await image.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    # 3. Генерируем имя файла
    file_ext = os.path.splitext(image.filename)[1] or ".jpg"
    file_name = f"{chat.uuid}-{int(datetime.now().timestamp())}{file_ext}"

    # 4. Загружаем в MinIO
    try:
        await asyncio.to_thread(
            minio_client.put_object,
            BUCKET_NAME,
            file_name,
            io.BytesIO(content),
            len(content),
            content_type="image/jpeg"
        )
        img_url = f"{MINIO_PUBLIC_URL}/{BUCKET_NAME}/{file_name}"
    except Exception as e:
        logging.error(f"MinIO upload error: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload image")

    # 5. Сохраняем сообщение в БД
    db_img = Message(
        chat_id=chat_id,
        message=img_url,
        message_type="answer",
        ai=False,
        created_at=datetime.now(),
        is_image=True
    )
    db.add(db_img)
    await db.commit()
    await db.refresh(db_img)

    # 6. Отправляем фотографию в соответствующий мессенджер
    try:
        if chat.messager == "telegram":
            # Создаем временный файл для отправки в Telegram
            with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as temp_file:
                temp_file.write(content)
                temp_file.flush()
                # Отправка в Telegram
                await bot.send_photo(
                    chat_id=chat.uuid,
                    photo=FSInputFile(temp_file.name)
                )
            # Удаляем временный файл
            os.unlink(temp_file.name)
        elif chat.messager == "vk":
            # Загружаем фото на сервер VK
            upload_url = await asyncio.to_thread(
                vk.photos.getMessagesUploadServer
            )
            upload_url = upload_url['upload_url']

            # Отправляем фото на сервер VK
            async with aiohttp.ClientSession() as http_sess:
                form = aiohttp.FormData()
                form.add_field('photo', content, filename='photo.jpg')
                async with http_sess.post(upload_url, data=form) as resp:
                    if resp.status != 200:
                        raise Exception(f"Failed to upload photo to VK: {resp.status}")
                    upload_result = await resp.json()

            # Сохраняем фото на сервере VK
            photo_data = await asyncio.to_thread(
                vk.photos.saveMessagesPhoto,
                photo=upload_result['photo'],
                server=upload_result['server'],
                hash=upload_result['hash']
            )

            # Отправляем сообщение с фото в VK
            await asyncio.to_thread(
                vk.messages.send,
                peer_id=int(chat.uuid),
                attachment=f"photo{photo_data[0]['owner_id']}_{photo_data[0]['id']}",
                random_id=0
            )
    except Exception as e:
        logging.error(f"Error sending photo to {chat.messager}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to send photo to {chat.messager}")

    # 7. Отправляем сообщение через WebSocket
    message_for_frontend = {
        "type": "message",
        "chatId": str(db_img.chat_id),
        "content": db_img.message,
        "message_type": db_img.message_type,
        "ai": db_img.ai,
        "timestamp": db_img.created_at.isoformat(),
        "id": db_img.id,
        "is_image": True
    }
    await messages_manager.broadcast(json.dumps(message_for_frontend))

    return db_img

@app.get("/api/ai/context")
async def get_ai_context(_: bool = Depends(auth.require_auth)):
    async with aiohttp.ClientSession() as http_session:
        try:
            async with http_session.get(
                API_URL,
            ) as response:
                data = await response.json()
                return data
        except Exception as e:
            HTTPException(status_code=500, detail=f"Ошибка при отправке на API {e}")

class PutAIContext(BaseModel):
    system_message: str
    faqs: str

@app.put("/api/ai/context")
async def put_ai_context(new_ai_context: PutAIContext, _: bool = Depends(auth.require_auth)):
    async with aiohttp.ClientSession() as http_session:
        try:
            async with http_session.post(
                API_URL,
                json={
                    "system_message": new_ai_context.system_message,
                    "faqs": new_ai_context.faqs
                }
            ) as response:
                data = await response.json()
                return data
        except Exception as e:
            HTTPException(status_code=500, detail=f"Ошибка при отправке на API {e}")

@app.post("/api/auth/refresh-token")
async def refresh_token_endpoint(request: Request):
    """
    Эндпоинт для обновления JWT токена
    """
    token = await auth.get_token_from_header(request)
    if not token:
        raise HTTPException(status_code=401, detail="Authorization header missing or invalid")
    
    result = await auth.refresh_token(token)
    if result is None:
        raise HTTPException(status_code=401, detail="Failed to refresh token")
    
    return result


@dp.message(Command("start"))
async def cmd_start(message: Message):
    async with async_session() as session:
        chat = await get_chat_by_uuid(session, str(message.chat.id))
        is_new_chat = False
        if not chat:
            chat = await create_chat(session, str(message.chat.id), name=message.chat.first_name, messager="telegram")
            is_new_chat = True
            new_chat_message = {
                "type": "chat_created",
                "chat": {
                    "id": chat.id,
                    "uuid": chat.uuid,
                    "name": chat.name,
                    "messager": chat.messager,
                    "waiting": chat.waiting,
                    "ai": chat.ai,
                    "tags": chat.tags,
                    "last_message_content": None,
                    "last_message_timestamp": None
                }
            }
            await updates_manager.broadcast(json.dumps(new_chat_message))
        # Сохраняем команду в БД
        new_message = Message(
            chat_id=chat.id,
            message=message.text,
            message_type="question",
            ai=False,
            created_at=datetime.now()
        )
        session.add(new_message)
        await session.commit()
        await session.refresh(new_message)

        # Проверяем, является ли пользователь админом
        notification_manager = notifications.get_notification_manager()
        if notification_manager:
            user_username = message.from_user.username
            if user_username and user_username in notification_manager.admin_usernames:
                # Сохраняем chat_id админа
                await notification_manager.save_admin_chat_id(user_username, message.chat.id)
                
                # Отправляем приветствие с клавиатурой управления уведомлениями
                await message.answer(
                    "👋 Добро пожаловать в панель администратора!\n\n"
                    "Здесь вы можете управлять уведомлениями о новых сообщениях.",
                    reply_markup=notification_manager.get_notification_keyboard(user_username)
                )
                return
        
        # Обычное приветствие для обычных пользователей
        await message.answer(
            "Добро пожаловать в Psihclothes!\n"
            "Можете задать любой вопрос"
        )

@dp.callback_query(lambda c: c.data.startswith("notifications_"))
async def handle_notification_toggle(callback: types.CallbackQuery):
    """Обработчик переключения уведомлений"""
    notification_manager = notifications.get_notification_manager()
    if not notification_manager:
        await callback.answer("Ошибка: менеджер уведомлений не инициализирован")
        return
    
    # Извлекаем username из callback_data
    parts = callback.data.split("_")
    if len(parts) >= 3:
        action = parts[1]  # on или off
        username = "_".join(parts[2:])  # username может содержать подчеркивания
        
        if username in notification_manager.admin_usernames:
            await notification_manager.toggle_notifications(username, callback.message.chat.id)
            await callback.answer()
        else:
            await callback.answer("Ошибка: пользователь не найден в списке админов")
    else:
        await callback.answer("Ошибка: неверный формат callback данных")

@dp.message(Command("notifications"))
async def cmd_notifications(message: Message):
    """Команда для вызова панели управления уведомлениями"""
    async with async_session() as session:
        chat = await get_chat_by_uuid(session, str(message.chat.id))
        is_new_chat = False
        if not chat:
            chat = await create_chat(session, str(message.chat.id), name=message.chat.first_name, messager="telegram")
            is_new_chat = True
            new_chat_message = {
                "type": "chat_created",
                "chat": {
                    "id": chat.id,
                    "uuid": chat.uuid,
                    "name": chat.name,
                    "messager": chat.messager,
                    "waiting": chat.waiting,
                    "ai": chat.ai,
                    "tags": chat.tags,
                    "last_message_content": None,
                    "last_message_timestamp": None
                }
            }
            await updates_manager.broadcast(json.dumps(new_chat_message))
        # Сохраняем команду в БД
        new_message = Message(
            chat_id=chat.id,
            message=message.text,
            message_type="question",
            ai=False,
            created_at=datetime.now()
        )
        session.add(new_message)
        await session.commit()
        await session.refresh(new_message)

        notification_manager = notifications.get_notification_manager()
        if not notification_manager:
            await message.answer("Ошибка: менеджер уведомлений не инициализирован")
            return
        
        user_username = message.from_user.username
        if not user_username or user_username not in notification_manager.admin_usernames:
            await message.answer("⛔ У вас нет доступа к панели администратора.")
            return
        
        # Сохраняем chat_id админа
        await notification_manager.save_admin_chat_id(user_username, message.chat.id)
        
        # Отправляем панель управления уведомлениями
        await message.answer(
            "🔔 Панель управления уведомлениями\n\n"
            "Здесь вы можете управлять уведомлениями о новых сообщениях.",
            reply_markup=notification_manager.get_notification_keyboard(user_username)
        )

@dp.message(F.text)
async def handle_message(message: Message):
    async with async_session() as session:
        chat = await get_chat_by_uuid(session, str(message.chat.id))
        if not chat:
            chat = await create_chat(session, str(message.chat.id), name=message.chat.first_name, messager="telegram")
            # Send WebSocket update about new chat creation
            new_chat_message = {
                "type": "chat_created",
                "chat": {
                    "id": chat.id,
                    "uuid": chat.uuid,
                    "name": chat.name,
                    "messager": chat.messager,
                    "waiting": chat.waiting,
                    "ai": chat.ai,
                    "tags": chat.tags,
                    "last_message_content": None, # New chat has no last message yet
                    "last_message_timestamp": None # New chat has no last message yet
                }
            }
            await updates_manager.broadcast(json.dumps(new_chat_message))

        # Создаем сообщение в базе данных
        new_message = Message(
            chat_id=chat.id,
            message=message.text,
            message_type="question",
            ai=False,
            created_at=datetime.now()
        )
        session.add(new_message)
        await session.commit()
        await session.refresh(new_message)

        # Format message for frontend
        message_for_frontend = {
            "type": "message",
            "chatId": str(new_message.chat_id),
            "content": new_message.message,
            "message_type": new_message.message_type,
            "ai": new_message.ai,
            "timestamp": new_message.created_at.isoformat(),
            "id": new_message.id
        }
        # Отправляем на фронтенд по WebSocket
        await messages_manager.broadcast(json.dumps(message_for_frontend))

        if not chat.ai:
            await update_chat_waiting(db=session, chat_id=chat.id, waiting=True)
            # Send WebSocket update about chat status change
            update_message = {
                "type": "chat_update",
                "chat_id": chat.id,
                "waiting": True
            }
            await updates_manager.broadcast(json.dumps(update_message))
            
            # Отправляем уведомление админам
            notification_manager = notifications.get_notification_manager()
            if notification_manager:
                await notification_manager.send_waiting_notification(
                    chat_id=message.chat.id,
                    chat_name=message.chat.first_name or str(message.chat.id),
                    messager="telegram"
                )
            return
        
        async with aiohttp.ClientSession() as http_session:
            try:
                async with http_session.post(
                    API_URL,
                    json={
                        "question": message.text,
                        "chat_id": chat.id
                    }
                ) as response:
                    if response.status != 200:
                        await message.answer("Извините, произошла ошибка при обработке запроса")
                    data = await response.json()
                    if not data:
                        await message.answer("Извините, произошла ошибка при обработке запроса")
                    if "answer" in data:
                        answer = data["answer"]
                        await message.answer(answer)
                        # Create message in database
                        new_answer = Message(
                            chat_id=chat.id,
                            message=answer,
                            message_type="answer",
                            ai=True,
                            created_at=datetime.now()
                        )
                        session.add(new_answer)
                        await session.commit()
                        await session.refresh(new_answer)
                        # Format message for frontend
                        message_for_frontend = {
                            "type": "message",
                            "chatId": chat.id,
                            "content": answer,
                            "message_type": "answer",
                            "ai": True,
                            "timestamp": new_answer.created_at.isoformat(),
                            "id": new_answer.id
                        }
                        # Отправляем на фронтенд по WebSocket
                        await messages_manager.broadcast(json.dumps(message_for_frontend))
                    else:
                        await message.answer("Извините, не удалось получить ответ. Попробуйте позже.")
                    if "manager" in data and data["manager"] == "true":
                        await update_chat_waiting(db=session, chat_id=chat.id, waiting=True)
                        await update_chat_ai(db=session, chat_id=chat.id, ai=False)
                        # Send WebSocket update about chat status change
                        update_message = {
                            "type": "chat_update",
                            "chat_id": chat.id,
                            "waiting": True,
                            "ai": False
                        }
                        await updates_manager.broadcast(json.dumps(update_message))
                        
                        # Отправляем уведомление админам
                        notification_manager = notifications.get_notification_manager()
                        if notification_manager:
                            await notification_manager.send_waiting_notification(
                                chat_id=message.chat.id,
                                chat_name=message.chat.first_name or str(message.chat.id),
                                messager="telegram"
                            )


            except Exception as e:
                logging.error(f"Error processing message: {e}")
                await message.answer("Извините, произошла ошибка при обработке запроса")


@dp.message(F.photo)
async def handle_photos(message: types.Message):
    # Берем фото с самым высоким разрешением
    photo = message.photo[-1]
    
    # Скачиваем фото
    file = await bot.get_file(photo.file_id)
    file_data = await bot.download_file(file.file_path)
    
    # Генерируем уникальное имя файла
    file_extension = os.path.splitext(file.file_path)[1]
    file_name = f"{message.from_user.id}-{photo.file_id}{file_extension}"
    
    # Асинхронно загружаем в Minio
    success = minio_client.put_object(
                bucket_name=BUCKET_NAME,
                object_name=file_name,
                data=file_data,
                length=photo.file_size,
                content_type="image/jpeg"
            )
    
    if success:
        async with async_session() as session:
            # Создаем сообщение в базе данных
            chat = await get_chat_by_uuid(session, str(message.chat.id))
            is_new_chat = False
            if not chat:
                chat = await create_chat(session, str(message.chat.id), name=message.chat.first_name, messager="telegram")
                is_new_chat = True
                new_chat_message = {
                    "type": "chat_created",
                    "chat": {
                        "id": chat.id,
                        "uuid": chat.uuid,
                        "name": chat.name,
                        "messager": chat.messager,
                        "waiting": chat.waiting,
                        "ai": chat.ai,
                        "tags": chat.tags,
                        "last_message_content": None,
                        "last_message_timestamp": None
                    }
                }
                await updates_manager.broadcast(json.dumps(new_chat_message))

            new_message = Message(
                chat_id=chat.id,
                message=f"{MINIO_PUBLIC_URL}/{BUCKET_NAME}/{file_name}",
                message_type="question",
                ai=False,
                created_at=datetime.now(),
                is_image=True
            )
            session.add(new_message)
            await session.commit()
            await session.refresh(new_message)
            # Format message for frontend
            message_for_frontend = {
                "type": "message",
                "chatId": str(new_message.chat_id),
                "content": new_message.message,
                "message_type": new_message.message_type,
                "ai": new_message.ai,
                "timestamp": new_message.created_at.isoformat(),
                "id": new_message.id,
                "is_image": new_message.is_image
            }
            # Отправляем на фронтенд по WebSocket
            await messages_manager.broadcast(json.dumps(message_for_frontend))

            # Если есть подпись к фото — сохраняем отдельным сообщением
            if message.caption:
                caption_message = Message(
                    chat_id=chat.id,
                    message=message.caption,
                    message_type="question",
                    ai=False,
                    created_at=datetime.now()
                )
                session.add(caption_message)
                await session.commit()
                await session.refresh(caption_message)
                await messages_manager.broadcast(json.dumps({
                    "type": "message",
                    "chatId": str(caption_message.chat_id),
                    "content": caption_message.message,
                    "message_type": caption_message.message_type,
                    "ai": caption_message.ai,
                    "timestamp": caption_message.created_at.isoformat(),
                    "id": caption_message.id
                }))

            update_message = {
                "type": "chat_update",
                "chat_id": chat.id,
                "waiting": True
            }
            await updates_manager.broadcast(json.dumps(update_message))
            
            # Отправляем уведомление админам
            notification_manager = notifications.get_notification_manager()
            if notification_manager:
                await notification_manager.send_waiting_notification(
                    chat_id=message.chat.id,
                    chat_name=message.chat.first_name or str(message.chat.id),
                    messager="telegram"
                )
    else:
        await message.reply("Произошла ошибка при загрузке фото")


@dp.message(F.sticker)
async def handle_sticker(message: types.Message):
    """
    Приём стикеров от доверенного источника (STICKER_SOURCE_TG_ID).
    Сохраняет file_id в БД для последующей отправки в другие чаты.
    """
    if not STICKER_SOURCE_TG_ID:
        await message.answer("❌ Приём стикеров отключён: не задан STICKER_SOURCE_TG_ID в конфиге.")
        return

    try:
        source_tg_id = int(STICKER_SOURCE_TG_ID)
    except ValueError:
        await message.answer("❌ Ошибка конфигурации: STICKER_SOURCE_TG_ID должен быть числом.")
        return

    if message.from_user.id != source_tg_id:
        await message.answer("⛔ У вас нет прав на добавление стикеров.")
        return

    sticker = message.sticker
    async with async_session() as session:
        existing = await get_sticker_by_file_unique_id(session, sticker.file_unique_id)
        if existing:
            await message.answer(f"⚠️ Этот стикер уже сохранён (ID: {existing.id}, эмодзи: {existing.emoji})")
            return

        # Download sticker file and upload to MinIO for thumbnail preview
        file_url = None
        try:
            tg_file = await bot.get_file(sticker.file_id)
            file_data = await bot.download_file(tg_file.file_path)
            ext = ".webm" if sticker.is_video else ".tgs" if sticker.is_animated else ".webp"
            file_name = f"stickers/{sticker.file_unique_id}{ext}"
            file_bytes = file_data.read() if hasattr(file_data, "read") else file_data
            file_size = len(file_bytes)
            file_stream = io.BytesIO(file_bytes)
            content_type = "video/webm" if sticker.is_video else "application/x-tgsticker" if sticker.is_animated else "image/webp"
            await asyncio.to_thread(
                minio_client.put_object,
                BUCKET_NAME,
                file_name,
                file_stream,
                file_size,
                content_type=content_type
            )
            file_url = f"{MINIO_PUBLIC_URL}/{BUCKET_NAME}/{file_name}"
        except Exception as e:
            logging.warning(f"Не удалось загрузить превью стикера в MinIO: {e}")

        new_sticker = await create_sticker(
            session,
            file_id=sticker.file_id,
            file_unique_id=sticker.file_unique_id,
            emoji=sticker.emoji or "",
            set_name=sticker.set_name,
            is_animated=sticker.is_animated,
            is_video=sticker.is_video,
            file_url=file_url
        )
        await message.answer(
            f"✅ Стикер сохранён!\n"
            f"ID в базе: {new_sticker.id}\n"
            f"Эмодзи: {new_sticker.emoji}\n"
            f"Set: {new_sticker.set_name or '—'}"
        )


@dp.message()
async def handle_unsupported(message: types.Message):
    """Catch-all для неподдерживаемых типов сообщений (голосовые, видео, документы и т.д.)"""
    async with async_session() as session:
        chat = await get_chat_by_uuid(session, str(message.chat.id))
        is_new_chat = False
        if not chat:
            chat = await create_chat(session, str(message.chat.id), name=message.chat.first_name, messager="telegram")
            is_new_chat = True
            new_chat_message = {
                "type": "chat_created",
                "chat": {
                    "id": chat.id,
                    "uuid": chat.uuid,
                    "name": chat.name,
                    "messager": chat.messager,
                    "waiting": chat.waiting,
                    "ai": chat.ai,
                    "tags": chat.tags,
                    "last_message_content": None,
                    "last_message_timestamp": None
                }
            }
            await updates_manager.broadcast(json.dumps(new_chat_message))
        
        content_type = message.content_type if message.content_type else "unknown"
        new_message = Message(
            chat_id=chat.id,
            message=f"[unsupported:{content_type}]",
            message_type="question",
            ai=False,
            created_at=datetime.now()
        )
        session.add(new_message)
        await session.commit()
        await session.refresh(new_message)
        
        await messages_manager.broadcast(json.dumps({
            "type": "message",
            "chatId": str(new_message.chat_id),
            "content": new_message.message,
            "message_type": new_message.message_type,
            "ai": new_message.ai,
            "timestamp": new_message.created_at.isoformat(),
            "id": new_message.id
        }))
        
        await update_chat_waiting(db=session, chat_id=chat.id, waiting=True)
        await updates_manager.broadcast(json.dumps({
            "type": "chat_update",
            "chat_id": chat.id,
            "waiting": True
        }))
        
        notification_manager = notifications.get_notification_manager()
        if notification_manager:
            await notification_manager.send_waiting_notification(
                chat_id=message.chat.id,
                chat_name=message.chat.first_name or str(message.chat.id),
                messager="telegram"
            )
    
    await message.answer("Извините, этот тип сообщений пока не поддерживается. Пожалуйста, напишите текстом.")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3001)

import os
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, selectinload
from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, func, select, desc, ARRAY
from sqlalchemy.exc import SQLAlchemyError
from typing import List, Optional, Dict, Any

# Load environment variables
load_dotenv()

# Database configuration
DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")
DB_PORT = os.getenv("DB_PORT")

DATABASE_URL = f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# SQLAlchemy engine and session
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()

# Models
class Chat(Base):
    __tablename__ = "chats"
    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, unique=True, nullable=False)
    ai = Column(Boolean, default=False)
    waiting = Column(Boolean, default=False)
    tags = Column(ARRAY(String), default=[])
    name = Column(String(30), default="Не известно")
    messager = Column(String(16), nullable=False, default="telegram")
    messages = relationship("Message", back_populates="chat")

class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(Integer, ForeignKey("chats.id"), nullable=False)
    message = Column(String, nullable=False)
    message_type = Column(String, nullable=False)
    ai = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    is_image = Column(Boolean, default=False)
    is_sticker = Column(Boolean, default=False)
    chat = relationship("Chat", back_populates="messages")


class Sticker(Base):
    __tablename__ = "stickers"
    id = Column(Integer, primary_key=True, index=True)
    file_id = Column(String, nullable=False)
    file_unique_id = Column(String, unique=True, nullable=False)
    emoji = Column(String, default="")
    set_name = Column(String, nullable=True)
    custom_tag = Column(String, nullable=True)
    is_animated = Column(Boolean, default=False)
    is_video = Column(Boolean, default=False)
    file_url = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# CRUD operations
async def get_chats(db: AsyncSession):
    result = await db.execute(select(Chat).order_by(Chat.id.desc()))
    return result.scalars().all()

async def get_chat(db: AsyncSession, chat_id: int):
    result = await db.execute(select(Chat).filter(Chat.id == chat_id))
    return result.scalar_one_or_none()

async def get_chat_by_uuid(db: AsyncSession, uuid: str):
    result = await db.execute(select(Chat).filter(Chat.uuid == uuid))
    return result.scalar_one_or_none()

async def get_messages(db: AsyncSession, chat_id: int):
    result = await db.execute(
        select(Message)
        .filter(Message.chat_id == chat_id)
        .order_by(Message.created_at.asc())
    )
    return result.scalars().all()

async def create_chat(db: AsyncSession, uuid: str, ai: bool = True, name: str = "Не известно", tags: List[str] = None, messager: str = "telegram"):
    new_chat = Chat(
        uuid=uuid,
        ai=ai,
        name=name,
        tags=tags or [],
        messager=messager
    )
    db.add(new_chat)
    try:
        await db.commit()
        await db.refresh(new_chat)
        return new_chat
    except SQLAlchemyError:
        await db.rollback()
        raise

async def create_message(db: AsyncSession, chat_id: int, message: str, message_type: str, ai: bool = False):
    new_message = Message(chat_id=chat_id, message=message, message_type=message_type, ai=ai)
    db.add(new_message)
    try:
        await db.commit()
        await db.refresh(new_message)
        return new_message
    except SQLAlchemyError:
        await db.rollback()
        raise

async def update_chat_waiting(db: AsyncSession, chat_id: int, waiting: bool):
    chat = await get_chat(db, chat_id)
    if chat:
        chat.waiting = waiting
        await db.commit()
        await db.refresh(chat)
    return chat

async def update_chat_ai(db: AsyncSession, chat_id: int, ai: bool):
    chat = await get_chat(db, chat_id)
    if chat:
        chat.ai = ai
        await db.commit()
        await db.refresh(chat)
    return chat

async def get_stats(db: AsyncSession):
    total = await db.scalar(select(func.count(Chat.id)))
    pending = await db.scalar(select(func.count(Chat.id)).filter(Chat.waiting == True))
    ai_count = await db.scalar(select(func.count(Chat.id)).filter(Chat.ai == True))
    return {"total": total, "pending": pending, "ai": ai_count}

async def get_chats_with_last_messages(db: AsyncSession, limit: int = 10000) -> List[Dict[str, Any]]:
    """Get all chats with their last message"""
    # First get all chats
    query = select(Chat).order_by(desc(Chat.id))
    if limit:
        query = query.limit(limit)
    result = await db.execute(query)
    chats = result.scalars().all()
    
    chats_with_messages = []
    for chat in chats:
        # Get only the last message for each chat
        last_message_query = (
            select(Message)
            .where(Message.chat_id == chat.id)
            .order_by(desc(Message.id))
            .limit(1)
        )
        last_message_result = await db.execute(last_message_query)
        last_message = last_message_result.scalar_one_or_none()
        
        chat_dict = {
            "id": chat.id,
            "uuid": chat.uuid,
            "ai": chat.ai,
            "waiting": chat.waiting,
            "name": chat.name,
            "tags": chat.tags,
            "messager": chat.messager,
            "last_message": None
        }
        
        if last_message:
            chat_dict["last_message"] = {
                "id": last_message.id,
                "content": last_message.message,
                "message_type": last_message.message_type,
                "ai": last_message.ai,
                "timestamp": last_message.created_at.isoformat() if last_message.created_at else None
            }
        
        chats_with_messages.append(chat_dict)
    
    return chats_with_messages

async def get_chat_messages(db: AsyncSession, chat_id: int) -> List[Dict[str, Any]]:
    """Get all messages for a specific chat"""
    # Remove pagination: page and limit
    query = (
        select(Message)
        .where(Message.chat_id == chat_id)
        .order_by(desc(Message.created_at)) # Keep ordering
        # Removed: .limit(limit)
        # Removed: .offset(offset)
    )
    
    result = await db.execute(query);
    messages = result.scalars().all();
    
    # Prepare messages in the format expected by the frontend
    return [
        {
            "id": msg.id,
            "content": msg.message,
            "message_type": msg.message_type,
            "ai": msg.ai,
            "timestamp": msg.created_at.isoformat() if msg.created_at else None,
            "chatId": str(chat_id),
            "is_image": msg.is_image,
            "is_sticker": msg.is_sticker
        }
        for msg in messages
    ]

async def add_chat_tag(db: AsyncSession, chat_id: int, tag: str) -> dict:
    chat = await get_chat(db, chat_id)
    if not chat:
        return {"message": "error"}
    
    try:
        # Инициализируем tags как пустой список, если None
        if chat.tags is None:
            chat.tags = []
        
        # Добавляем тег, если его еще нет
        if tag not in chat.tags:
            chat.tags = chat.tags + [tag]  # Создаем новый список для ARRAY
        
        await db.commit()
        await db.refresh(chat)
        return {"success": True, "tags": chat.tags}
    except Exception as e:
        await db.rollback()
        return {"message": "error"}

async def remove_chat_tag(db: AsyncSession, chat_id: int, tag: str) -> dict:
    chat = await get_chat(db, chat_id)
    if not chat:
        return {"message": "error"}
    
    try:
        # Инициализируем tags как пустой список, если None
        if chat.tags is None:
            chat.tags = []
        
        # Удаляем тег, если он есть
        if tag in chat.tags:
            chat.tags = [t for t in chat.tags if t != tag]  # Создаем новый список без тега
        
        await db.commit()
        await db.refresh(chat)
        return {"success": True, "tags": chat.tags}
    except Exception as e:
        await db.rollback()
        return {"message": "error"}


# ─── Sticker CRUD ────────────────────────────────────────────────

async def create_sticker(
    db: AsyncSession,
    file_id: str,
    file_unique_id: str,
    emoji: str = "",
    set_name: Optional[str] = None,
    custom_tag: Optional[str] = None,
    is_animated: bool = False,
    is_video: bool = False,
    file_url: Optional[str] = None
) -> Sticker:
    new_sticker = Sticker(
        file_id=file_id,
        file_unique_id=file_unique_id,
        emoji=emoji,
        set_name=set_name,
        custom_tag=custom_tag,
        is_animated=is_animated,
        is_video=is_video,
        file_url=file_url
    )
    db.add(new_sticker)
    try:
        await db.commit()
        await db.refresh(new_sticker)
        return new_sticker
    except SQLAlchemyError:
        await db.rollback()
        raise


async def get_all_stickers(db: AsyncSession):
    result = await db.execute(select(Sticker).order_by(Sticker.id.desc()))
    return result.scalars().all()


async def get_sticker_by_id(db: AsyncSession, sticker_id: int) -> Optional[Sticker]:
    result = await db.execute(select(Sticker).filter(Sticker.id == sticker_id))
    return result.scalar_one_or_none()


async def get_sticker_by_file_unique_id(db: AsyncSession, file_unique_id: str) -> Optional[Sticker]:
    result = await db.execute(select(Sticker).filter(Sticker.file_unique_id == file_unique_id))
    return result.scalar_one_or_none()


async def delete_sticker(db: AsyncSession, sticker_id: int) -> bool:
    sticker = await get_sticker_by_id(db, sticker_id)
    if not sticker:
        return False
    try:
        await db.delete(sticker)
        await db.commit()
        return True
    except SQLAlchemyError:
        await db.rollback()
        return False
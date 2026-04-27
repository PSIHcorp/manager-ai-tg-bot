import os
import aiohttp
import logging
from fastapi import HTTPException, Depends, Request
from typing import Optional

DISABLE_AUTH = os.getenv("DISABLE_AUTH", "false").lower() == "true"

# Конфигурация сервиса аутентификации
AUTH_SERVICE_BASE_URL = "http://87.242.85.68:8000"
CHECK_PERMISSIONS_URL = f"{AUTH_SERVICE_BASE_URL}/api/jwt/check-permissions"
REFRESH_TOKEN_URL = f"{AUTH_SERVICE_BASE_URL}/api/jwt/refresh/"

async def check_permissions(token: str, permission: str = "message") -> bool:
    """
    Проверяет разрешения пользователя через сервис аутентификации
    
    Args:
        token: JWT токен
        permission: Требуемое разрешение (по умолчанию "message")
    
    Returns:
        bool: True если пользователь авторизован и имеет разрешение
    """
    headers = {
        "accept": "*/*",
        "Authorization": f"Bearer {token}"
    }
    
    params = {"permission": permission}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(CHECK_PERMISSIONS_URL, headers=headers, params=params) as response:
                if response.status == 204:
                    return True
                else:
                    logging.warning(f"Permission check failed: {response.status}")
                    return False
    except Exception as e:
        logging.error(f"Error checking permissions: {e}")
        return False

async def refresh_token(token: str) -> Optional[dict]:
    """
    Обновляет JWT токен через сервис аутентификации
    
    Args:
        token: Текущий JWT токен
    
    Returns:
        dict: Ответ от сервера аутентификации или None в случае ошибки
    """
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {token}"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(REFRESH_TOKEN_URL, headers=headers, data="") as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logging.warning(f"Token refresh failed: {response.status}")
                    return None
    except Exception as e:
        logging.error(f"Error refreshing token: {e}")
        return None

async def get_token_from_header(request: Request) -> Optional[str]:
    """
    Извлекает JWT токен из заголовка Authorization
    
    Args:
        request: FastAPI Request объект
    
    Returns:
        str: JWT токен или None если не найден
    """
    authorization = request.headers.get("Authorization")
    if not authorization:
        return None
    
    if not authorization.startswith("Bearer "):
        return None
    
    return authorization.replace("Bearer ", "")

async def verify_token(request: Request) -> bool:
    """
    Проверяет JWT токен и разрешения пользователя
    
    Args:
        request: FastAPI Request объект
    
    Returns:
        bool: True если токен валиден и пользователь имеет разрешения
    """
    token = await get_token_from_header(request)
    if not token:
        raise HTTPException(status_code=401, detail="Authorization header missing or invalid")
    
    is_authorized = await check_permissions(token)
    if not is_authorized:
        raise HTTPException(status_code=401, detail="Invalid token or insufficient permissions")
    
    return True

# Dependency для использования в эндпоинтах
async def require_auth(request: Request) -> bool:
    """
    Dependency для проверки аутентификации в эндпоинтах
    """
    if DISABLE_AUTH:
        return True
    return await verify_token(request) 
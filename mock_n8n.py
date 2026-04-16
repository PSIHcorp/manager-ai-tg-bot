"""
Простая заглушка для n8n webhook.
Бот шлёт сюда вопросы пользователей и запрашивает/обновляет AI-контекст.
Запуск: python mock_n8n.py
"""
from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()

ai_context = {
    "system_message": "Ты — помощник интернет-магазина. Отвечай вежливо и по делу.",
    "faqs": "1. Доставка 2-3 дня.\n2. Оплата картой или наличными."
}

@app.get("/webhook/76a8bfb0-a105-41a0-8553-e64a9d25ad79")
async def get_context():
    return ai_context

@app.post("/webhook/76a8bfb0-a105-41a0-8553-e64a9d25ad79")
async def webhook(request: Request):
    data = await request.json()

    if "question" in data:
        return {
            "answer": f"[MOCK n8n] Получил вопрос: '{data['question']}'. Это тестовый ответ от заглушки."
        }

    if "system_message" in data:
        ai_context["system_message"] = data.get("system_message", ai_context["system_message"])
        ai_context["faqs"] = data.get("faqs", ai_context["faqs"])
        return {"status": "ok"}

    return {"status": "unknown"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5678)

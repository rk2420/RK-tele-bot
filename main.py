# ===================== IMPORTS =====================
import os
import re
import json
import base64
import pytz
import requests
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    ContextTypes,
    filters
)

from paddleocr import PaddleOCR
import gspread
from google.oauth2.service_account import Credentials


# ===================== LOAD ENV =====================
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")


# ===================== RECREATE credentials.json =====================
if not os.path.exists("credentials.json"):
    encoded = os.getenv("GOOGLE_CREDENTIALS_BASE64")
    if not encoded:
        raise RuntimeError("GOOGLE_CREDENTIALS_BASE64 not found")

    with open("credentials.json", "wb") as f:
        f.write(base64.b64decode(encoded))


# ===================== GOOGLE SHEETS =====================
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
client = gspread.authorize(creds)
sheet = client.open_by_key(SHEET_ID).sheet1

# Add header if empty
if sheet.row_count == 0 or sheet.cell(1, 1).value is None:
    sheet.append_row([
        "Timestamp (IST)",
        "Telegram_ID",
        "Name",
        "Designation",
        "Company",
        "Phone",
        "Email",
        "Website",
        "Address",
        "Industry",
        "Services"
    ])


# ===================== OCR (PaddleOCR - CPU ONLY) =====================
ocr = PaddleOCR(
    use_angle_cls=True,
    lang="en",
    use_gpu=False
)

def run_ocr(image_path: str) -> str:
    result = ocr.ocr(image_path)
    texts = []

    for block in result:
        for line in block:
            texts.append(line[1][0])

    return " ".join(texts)


# ===================== REGEX EXTRACTION =====================
def regex_extract(text: str) -> dict:
    phone = re.findall(r'\+?\d[\d\s\-]{8,}', text)
    email = re.findall(r'[\w\.-]+@[\w\.-]+', text)
    website = re.findall(r'(https?://\S+|www\.\S+)', text)

    return {
        "Phone": phone[0] if phone else "Not Found",
        "Email": email[0] if email else "Not Found",
        "Website": website[0] if website else "Not Found"
    }


# ===================== AI EXTRACTION (GROQ) =====================
def ai_extract(text: str) -> dict:
    prompt = """
Extract visiting card details.
Return ONLY valid JSON with keys:
Name, Designation, Company, Address, Industry, Services.
If missing, use "Not Found".
"""

    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama3-70b-8192",
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text}
                ]
            },
            timeout=15
        )

        return json.loads(r.json()["choices"][0]["message"]["content"])

    except Exception:
        return {
            "Name": "Not Found",
            "Designation": "Not Found",
            "Company": "Not Found",
            "Address": "Not Found",
            "Industry": "Not Found",
            "Services": "Not Found"
        }


# ===================== USER CONTEXT =====================
user_context = {}


# ===================== SAVE TO GOOGLE SHEET =====================
def save_to_sheet(chat_id: int, data: dict):
    ist = pytz.timezone("Asia/Kolkata")
    timestamp = datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S")

    sheet.append_row([
        timestamp,
        chat_id,
        data["Name"],
        data["Designation"],
        data["Company"],
        data["Phone"],
        data["Email"],
        data["Website"],
        data["Address"],
        data["Industry"],
        data["Services"]
    ])


# ===================== FOLLOW-UP Q&A =====================
def answer_followup(company_data: dict, question: str) -> str:
    context = f"""
Company: {company_data['Company']}
Industry: {company_data['Industry']}
Services: {company_data['Services']}
"""

    prompt = f"""
You are a business analyst.
Answer using public knowledge and reasoning.
If exact data is unavailable, provide realistic estimates
and clearly state assumptions.

Context:
{context}

Question:
{question}
"""

    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama3-70b-8192",
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=15
        )

        return r.json()["choices"][0]["message"]["content"]

    except Exception:
        return "Unable to fetch information right now."


# ===================== TELEGRAM HANDLERS =====================
async def image_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Image received & analyzing…")

    photo = update.message.photo[-1]
    file = await photo.get_file()
    path = f"/tmp/{photo.file_id}.jpg"
    await file.download_to_drive(path)

    text = run_ocr(path)

    regex_data = regex_extract(text)
    ai_data = ai_extract(text)

    final_data = {
        "Name": ai_data.get("Name", "Not Found"),
        "Designation": ai_data.get("Designation", "Not Found"),
        "Company": ai_data.get("Company", "Not Found"),
        "Phone": regex_data["Phone"],
        "Email": regex_data["Email"],
        "Website": regex_data["Website"],
        "Address": ai_data.get("Address", "Not Found"),
        "Industry": ai_data.get("Industry", "Not Found"),
        "Services": ai_data.get("Services", "Not Found")
    }

    user_context[update.effective_chat.id] = final_data
    save_to_sheet(update.effective_chat.id, final_data)

    reply = "\n".join([f"*{k}*: {v}" for k, v in final_data.items()])
    await update.message.reply_markdown(reply)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if chat_id not in user_context:
        await update.message.reply_text("Please send a visiting card image first.")
        return

    answer = answer_followup(user_context[chat_id], update.message.text)
    await update.message.reply_text(answer)


# ===================== MAIN =====================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.PHOTO, image_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("Bot running 24×7")
    app.run_polling()


if __name__ == "__main__":
    main()

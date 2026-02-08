# ===================== IMPORTS =====================
import os
import re
import json
import base64
import pytz
import requests
import logging
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
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

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing")


# ===================== LOGGING =====================
logging.basicConfig(level=logging.INFO)
print("✅ Visiting Card Bot starting...")


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

# Header safety
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


# ===================== OCR (PaddleOCR – CPU ONLY) =====================
ocr = PaddleOCR(use_angle_cls=True, lang="en", use_gpu=False)

def run_ocr(image_path: str) -> str:
    result = ocr.ocr(image_path)
    texts = []
    for block in result:
        for line in block:
            texts.append(line[1][0])
    return " ".join(texts)


# ===================== HELPERS =====================
def safe(v):
    return v if v and str(v).strip() else "Not Found"

def clean_text(text):
    text = text.replace("\n", " ")
    replacements = {
        "(at)": "@",
        "[at]": "@",
        " at ": "@",
        " dot ": ".",
        "|": "1",
        "I": "1",
        "l": "1",
        "O": "0",
        "o": "0"
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


# ===================== REGEX EXTRACTION =====================
def extract_phone(text):
    phones = re.findall(r'(\+91[\s\-]?\d{10}|\b\d{10}\b)', text)
    return phones[0] if phones else "Not Found"

def extract_email(text):
    match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
    return match.group() if match else "Not Found"

def extract_website(text):
    text = text.replace("www ", "www.")
    match = re.search(
        r'(https?://[^\s]+|www\.[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
        text
    )
    return match.group() if match else "Not Found"


# ===================== SAFE JSON =====================
def safe_json_load(text):
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                return None
        return None


# ===================== GROQ AI EXTRACTION =====================
def ai_extract(text: str) -> dict:
    prompt = f"""
You are an expert business card parser.

RULES:
- Output ONLY valid JSON
- No explanation, no markdown
- Empty string if missing

JSON FORMAT:
{{
  "Name": "",
  "Designation": "",
  "Company": "",
  "Address": "",
  "Industry": "",
  "Services": []
}}

TEXT:
{text}
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
                "temperature": 0.2,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=20
        )
        return safe_json_load(r.json()["choices"][0]["message"]["content"])
    except Exception:
        return None


# ===================== SAVE TO GOOGLE SHEET =====================
def save_to_sheet(chat_id, data):
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


# ===================== USER CONTEXT =====================
user_context = {}


# ===================== /START =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot is running!\n\n"
        "📸 Send a visiting card image\n"
        "📊 Data saved to Google Sheets\n"
        "💬 Ask follow-up questions"
    )


# ===================== IMAGE HANDLER =====================
async def image_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Image received & analyzing...")

    photo = update.message.photo[-1]
    file = await photo.get_file()
    path = f"/tmp/{photo.file_id}.jpg"
    await file.download_to_drive(path)

    text = run_ocr(path)

    print("OCR RAW TEXT >>>")
    print(text)
    print("<<<<<<<<<<<<<<<<")

    regex_data = regex_extract(text)
    ai_data = ai_extract(text)

    print("AI RAW RESPONSE >>>")
    print(ai_data)
    print("<<<<<<<<<<<<<<<<")

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

# Fallback heuristics
name_guess = cleaned.split(" ")[0:2]
name_guess = " ".join(name_guess) if name_guess else "Not Found"

final_data = {
    "Name": safe(ai_data.get("Name") or name_guess),
    "Designation": safe(ai_data.get("Designation") or "Real Estate Agent"),
    "Company": safe(ai_data.get("Company") or "Not Found"),
    "Phone": phone,
    "Email": email,
    "Website": website,
    "Address": safe(ai_data.get("Address")),
    "Industry": safe(ai_data.get("Industry") or "Real Estate"),
    "Services": (
        ", ".join(ai_data.get("Services"))
        if ai_data.get("Services")
        else "Property Sales, Leasing"
    )
}

    final_data = {
        "Name": safe(ai_data.get("Name")),
        "Designation": safe(ai_data.get("Designation")),
        "Company": safe(ai_data.get("Company")),
        "Phone": phone,
        "Email": email,
        "Website": website,
        "Address": safe(ai_data.get("Address")),
        "Industry": safe(ai_data.get("Industry")),
        "Services": ", ".join(ai_data.get("Services")) if ai_data.get("Services") else "Not Found"
    }

    user_context[update.effective_chat.id] = final_data
    save_to_sheet(update.effective_chat.id, final_data)

    reply = (
        f"📇 Visiting Card Details\n\n"
        f"Name: {final_data['Name']}\n"
        f"Designation: {final_data['Designation']}\n"
        f"Company: {final_data['Company']}\n"
        f"Phone: {final_data['Phone']}\n"
        f"Email: {final_data['Email']}\n"
        f"Website: {final_data['Website']}\n"
        f"Address: {final_data['Address']}\n"
        f"Industry: {final_data['Industry']}\n"
        f"Services: {final_data['Services']}"
    )

    await update.message.reply_text(reply)


# ===================== FOLLOW-UP TEXT =====================
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in user_context:
        await update.message.reply_text("📸 Please upload a visiting card first.")
        return

    company_data = user_context[chat_id]

    prompt = f"""
Company: {company_data['Company']}
Industry: {company_data['Industry']}
Services: {company_data['Services']}

Answer the user's question using public knowledge.
Focus on India.
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
                "temperature": 0.3,
                "messages": [
                    {"role": "user", "content": prompt + "\nQuestion:\n" + update.message.text}
                ]
            },
            timeout=15
        )
        await update.message.reply_text(
            r.json()["choices"][0]["message"]["content"]
        )
    except Exception:
        await update.message.reply_text("Unable to fetch answer right now.")


# ===================== MAIN =====================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, image_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("🚀 Bot running 24×7")
    app.run_polling()


if __name__ == "__main__":
    main()




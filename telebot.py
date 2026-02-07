import os
import re
import json
import pytz
import requests
import easyocr
from PIL import Image
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import gspread
from google.oauth2.service_account import Credentials

# ================= LOAD CONFIG =================
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# ================= GOOGLE SHEETS =================
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
client = gspread.authorize(creds)
sheet = client.open_by_key(SHEET_ID).sheet1

# Add header if empty
if sheet.row_count == 0 or sheet.cell(1, 1).value is None:
    sheet.append_row([
        "Timestamp (IST)", "Telegram_ID",
        "Name", "Designation", "Company",
        "Phone", "Email", "Website",
        "Address", "Industry", "Services"
    ])

# ================= OCR =================
reader = easyocr.Reader(['en'], gpu=False)

def preprocess(path):
    img = Image.open(path)
    img = img.resize((img.width * 2, img.height * 2))
    img.save(path)

def run_ocr(path):
    preprocess(path)
    result = reader.readtext(path, detail=0)
    return " ".join(result)

# ================= REGEX =================
def regex_extract(text):
    phone = re.findall(r'\+?\d[\d\s\-]{8,}', text)
    email = re.findall(r'[\w\.-]+@[\w\.-]+', text)
    website = re.findall(r'(https?://\S+|www\.\S+)', text)

    return {
        "Phone": phone[0] if phone else "Not Found",
        "Email": email[0] if email else "Not Found",
        "Website": website[0] if website else "Not Found"
    }

# ================= AI EXTRACTION =================
def ai_extract(text):
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
    except:
        return {
            "Name": "Not Found",
            "Designation": "Not Found",
            "Company": "Not Found",
            "Address": "Not Found",
            "Industry": "Not Found",
            "Services": "Not Found"
        }

# ================= CONTEXT MEMORY =================
user_context = {}

# ================= SAVE TO SHEET =================
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

# ================= FOLLOW-UP AI =================
def answer_followup(company_data, question):
    context = f"""
Company: {company_data['Company']}
Industry: {company_data['Industry']}
Services: {company_data['Services']}
"""

    prompt = f"""
You are a business analyst.
Use public knowledge and reasoning.
If exact data is unavailable, give realistic estimates
and clearly mention assumptions.

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
    except:
        return "Unable to fetch information right now."

# ================= TELEGRAM HANDLERS =================
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

# ================= MAIN =================
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, image_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("Bot running 24×7")
    app.run_polling()

if __name__ == "__main__":
    main()

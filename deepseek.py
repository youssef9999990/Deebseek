import asyncio
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
from typing import Dict, Optional
from contextlib import asynccontextmanager
import time
import http.server
import socketserver
import threading
import os

# --- إعدادات التوكنات والمفاتيح ---

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") 
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY") 
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")   # اسم القناة المطلوب الاشتراك فيها
DEVELOPER_ID = os.getenv("DEVELOPER_ID")   # أي دي المطور

# --- إعدادات الأداء ---
REQUEST_TIMEOUT = 60
MAX_RETRIES = 3
DELAY_BETWEEN_RETRIES = 2
MAX_MESSAGE_LENGTH = 4000
USER_COOLDOWN = 60  # 120 ثانية (دقيقتين) بين كل طلب

# --- إدارة حالة المستخدمين ---
active_users: Dict[int, bool] = {}
user_tasks: Dict[int, asyncio.Task] = {}
last_request_time: Dict[int, float] = {}  # لتتبع وقت آخر طلب لكل مستخدم
users_data: Dict[int, dict] = {}  # لتخزين بيانات المستخدمين

@asynccontextmanager
async def get_http_client():
    """إنشاء عميل HTTP مع إعدادات متقدمة"""
    limits = httpx.Limits(max_keepalive_connections=50, max_connections=200)
    timeout = httpx.Timeout(REQUEST_TIMEOUT, connect=10.0)
    
    async with httpx.AsyncClient(
        http2=True,
        limits=limits,
        timeout=timeout,
        headers={
            "Accept": "application/json",
            "User-Agent": "DeepSeekTelegramBot/2.0"
        }
    ) as client:
        yield client

async def check_subscription(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """التحقق من اشتراك المستخدم في القناة"""
    try:
        chat_member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return chat_member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        print(f"Error checking subscription: {e}")
        return False

async def send_subscription_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إرسال رسالة الاشتراك الإجباري"""
    user = update.effective_user
    keyboard = [
        [{"text": "قناة البوت 🌐", "url": f"https://t.me/{CHANNEL_USERNAME[1:]}"}],
        [{"text": "تحقق", "callback_data": "verify"}]
    ]
    reply_markup = {"inline_keyboard": keyboard}
    
    message = (
        f"*• عـذراً .. عـزيـزي {user.first_name} 🤷🏻‍♀*\n"
        f"*• لـ إستخـدام البـوت 👨🏻‍💻*\n"
        f"*• عليك الإشتـراك في قناة البـوت الرسمية 🌐*\n"
        f"*• You must subscribe to the bot channel.*"
    )
    
    await update.message.reply_text(
        message,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def notify_developer(context: ContextTypes.DEFAULT_TYPE, user: dict):
    """إعلام المطور بوجود عضو جديد"""
    total_users = len(users_data)
    message = (
        f"☑️| انضم عضو جديد\n"
        "━━━━━━━━━━━━━\n"
        f"👤 الاسم: <code>{user['first_name']}</code>\n"
        f"🔗 المعرف: @{user.get('username', 'N/A')}\n"
        f"🆔 الآي دي: <code>{user['id']}</code>\n"
        "━━━━━━━━━━━━━\n"
        f"📊 إجمالي الأعضاء: {total_users}"
    )
    
    await context.bot.send_message(
        chat_id=DEVELOPER_ID,
        text=message,
        parse_mode=ParseMode.HTML
    )

async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة زر التحقق"""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'verify':
        if await check_subscription(query.from_user.id, context):
            await query.edit_message_text("تم التحقق ✔️")
            await start_command(update, context)
        else:
            await query.answer("❌ لم تشترك في القناة بعد.", show_alert=True)

async def get_ai_response(client: httpx.AsyncClient, user_message: str) -> Optional[str]:
    """الحصول على رد من OpenRouter API"""
    if not user_message.strip():
        return None
    
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://t.me/YourBotName",
        "X-Title": "DeepSeek Telegram Bot",
    }
    payload = {
        "model": "deepseek/deepseek-chat-v3-0324:free",
        "messages": [{"role": "user", "content": user_message}],
        "temperature": 0.7,
    }

    for attempt in range(MAX_RETRIES):
        try:
            response = await client.post(url, headers=headers, json=payload)
            
            if response.status_code == 200:
                data = response.json()
                return data["choices"][0]["message"]["content"]
            else:
                error_data = response.json()
                error_msg = error_data.get("error", {}).get("message", "Unknown error")
                if attempt == MAX_RETRIES - 1:
                    return f"❌ خطأ من OpenRouter (كود {response.status_code}): {error_msg}"
                await asyncio.sleep(DELAY_BETWEEN_RETRIES)
                
        except httpx.ReadTimeout:
            if attempt == MAX_RETRIES - 1:
                return "⌛ انتهى وقت الانتظار. الخادم يستغرق وقتًا أطول من المعتاد."
            await asyncio.sleep(DELAY_BETWEEN_RETRIES)
            
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                return f"⚠️ خطأ غير متوقع: {str(e)}"
            await asyncio.sleep(DELAY_BETWEEN_RETRIES)
    
    return "❌ فشلت جميع محاولات الاتصال بالخادم."

async def handle_user_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة طلب المستخدم مع إدارة الحالة"""
    user_id = update.message.from_user.id
    chat_id = update.message.chat_id
    user_message = update.message.text
    
    # التحقق من وجود نص في الرسالة
    if not user_message or not user_message.strip():
        await update.message.reply_text(
            "*⚠️ يبدو أن رسالتك فارغة. يرجى إرسال نص للرد عليه.*",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # التحقق من اشتراك المستخدم في القناة
    if not await check_subscription(user_id, context):
        await send_subscription_prompt(update, context)
        return
    
    # التحقق من الوقت بين الطلبات (ماعدا المطور)
    if user_id != DEVELOPER_ID:
        current_time = time.time()
        last_time = last_request_time.get(user_id, 0)
        
        if current_time - last_time < USER_COOLDOWN:
            remaining_time = int(USER_COOLDOWN - (current_time - last_time))
            await update.message.reply_text(
                f"*⏳ يرجى الانتظار {remaining_time} ثانية قبل إرسال طلب جديد.*",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        last_request_time[user_id] = current_time
    
    # التحقق مما إذا كان المستخدم لديه طلب قيد المعالجة
    if active_users.get(user_id, False):
        await update.message.reply_text(
            "*⏳ يرجى الانتظار حتى انتهاء طلبك السابق قبل إرسال طلب جديد.*",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # تسجيل بيانات المستخدم إذا كان جديداً
    if user_id not in users_data:
        user = update.message.from_user
        users_data[user_id] = {
            "id": user.id,
            "first_name": user.first_name,
            "username": user.username
        }
        await notify_developer(context, users_data[user_id])
    
    # وضع علامة على المستخدم كنشط
    active_users[user_id] = True
    wait_msg = None
    
    try:
        wait_msg = await update.message.reply_text(
            "*⏳ D𝑒𝑒𝑝S𝑒𝑒𝑘 يعمل على طلبك. يرجى الانتظار لحظة...*",
            parse_mode=ParseMode.MARKDOWN
        )
        
        async with get_http_client() as client:
            bot_response = await get_ai_response(client, user_message)
            
            if not bot_response:
                await update.message.reply_text(
                    "*⚠️ لم أستطع الحصول على رد. يرجى المحاولة مرة أخرى.*",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            
            # إضافة النص بخط عريض
            formatted_response = f"*{bot_response.replace('**', '*').replace('__', '_')}*"
            
            try:
                if wait_msg:
                    await context.bot.delete_message(chat_id=chat_id, message_id=wait_msg.message_id)
            except:
                pass
            
            # إرسال الرد مع تقسيم الرسائل الطويلة
            if len(formatted_response) > MAX_MESSAGE_LENGTH:
                parts = [formatted_response[i:i+MAX_MESSAGE_LENGTH] for i in range(0, len(formatted_response), MAX_MESSAGE_LENGTH)]
                for part in parts:
                    await update.message.reply_text(part, parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(formatted_response, parse_mode=ParseMode.MARKDOWN)
                
    except asyncio.CancelledError:
        if wait_msg:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=wait_msg.message_id,
                    text="*❌ تم إلغاء طلبك.*",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass
    except Exception as e:
        error_msg = f"*❌ حدث خطأ أثناء معالجة طلبك: {str(e)}*"
        try:
            if wait_msg:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=wait_msg.message_id,
                    text=error_msg,
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await update.message.reply_text(error_msg, parse_mode=ParseMode.MARKDOWN)
        except:
            await update.message.reply_text(error_msg, parse_mode=ParseMode.MARKDOWN)
    finally:
        active_users[user_id] = False
        if user_id in user_tasks:
            del user_tasks[user_id]

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إدارة طلبات المستخدمين بشكل غير متزامن"""
    user_id = update.message.from_user.id
    
    if user_id in active_users and active_users[user_id]:
        return
    
    task = asyncio.create_task(handle_user_request(update, context))
    user_tasks[user_id] = task
    
    try:
        await task
    except Exception as e:
        await update.message.reply_text(
            f"*⚠️ حدث خطأ غير متوقع: {str(e)}*",
            parse_mode=ParseMode.MARKDOWN
        )
    finally:
        if user_id in user_tasks:
            del user_tasks[user_id]

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رسالة الترحيب"""
    user_id = update.message.from_user.id
    
    # التحقق من اشتراك المستخدم في القناة
    if not await check_subscription(user_id, context):
        await send_subscription_prompt(update, context)
        return
    
    welcome_msg = """
*🚀 مرحبًا! أنا بوت D𝑒𝑒𝑝S𝑒𝑒𝑘 المدعوم بـ OpenRouter*

*✍️ يمكنك سؤالي عن أي شيء، وسأحاول مساعدتك بأفضل طريقة.*
    """
    await update.message.reply_text(welcome_msg, parse_mode=ParseMode.MARKDOWN)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إلغاء الطلب الحالي للمستخدم"""
    user_id = update.message.from_user.id
    
    if user_id in user_tasks:
        user_tasks[user_id].cancel()
        try:
            await user_tasks[user_id]
        except asyncio.CancelledError:
            await update.message.reply_text(
                "*تم إلغاء طلبك الحالي.*",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            await update.message.reply_text(
                "*حدث خطأ أثناء محاولة إلغاء طلبك.*",
                parse_mode=ParseMode.MARKDOWN
            )
        finally:
            active_users[user_id] = False
            if user_id in user_tasks:
                del user_tasks[user_id]
    else:
        await update.message.reply_text(
            "*ليس لديك أي طلب قيد المعالجة.*",
            parse_mode=ParseMode.MARKDOWN
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج الأخطاء العام"""
    if update and update.message:
        await update.message.reply_text(
            "*⚠️ حدث خطأ غير متوقع أثناء معالجة طلبك. يرجى المحاولة مرة أخرى لاحقًا.*",
            parse_mode=ParseMode.MARKDOWN
        )

def main():
    # إعداد البوت مع تحسينات الأداء
    app = Application.builder() \
        .token(TELEGRAM_BOT_TOKEN) \
        .concurrent_updates(True) \
        .pool_timeout(60) \
        .get_updates_read_timeout(60) \
        .build()
    
    # إضافة المعالجات
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(verify_callback, pattern="^verify$"))
    
    # إضافة معالج الأخطاء
    app.add_error_handler(error_handler)
    
    # تشغيل البوت مع إعدادات متقدمة
    print("🤖 البوت يعمل الآن بشكل محسن وسريع مع HTTP/2... استخدم Ctrl+C لإيقافه.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
        # تم إزالة الوسيطات غير المدعومة
    )
    
def run_server():
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", 8000), handler) as httpd:
        print("Serving on port 8000")
        httpd.serve_forever()

# تشغيل الخادم في خيط جديد
server_thread = threading.Thread(target=run_server)
server_thread.start()	        
        

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 إيقاف البوت...")
    except Exception as e:
        print(f"❌ خطأ غير متوقع: {str(e)}")
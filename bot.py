"""
Telegram Bot + Mini App Backend (Supabase storage version)
----------------------------------------------------------------------
Flow:
  /start (optionally /start <referrer_id>)
    -> Channel join check
    -> Device verification (UX step only, not a real device check)
    -> "Open App" button launches the Mini App (mini_app.html)

Mini App shows: auto-fetched user name/id, wallet balance, spins,
referral link, and a withdraw REQUEST form (demo only — no real payout,
requests are just saved to Supabase for manual review).

Data is stored in Supabase (tables: users, transactions, withdrawals)
instead of a local file — works even across restarts/redeploys and lets
multiple devices see the same real data.

IMPORTANT — read before using for real money:
  The wallet/spin credits here are DEMO ONLY. No real money moves.
  To pay real rupees you must integrate a licensed payment/payout API
  (e.g. Razorpay Payouts, Cashfree Payouts) and follow RBI compliance
  requirements for cashback/referral programs.

Requirements:
  pip install python-telegram-bot --upgrade
  pip install flask
  pip install supabase

Hosting note:
  Telegram Mini Apps REQUIRE a public HTTPS url. Running this only on
  localhost will not work inside Telegram. For testing, use a tunnel
  tool (e.g. ngrok/cloudflared) and put that https url into WEBAPP_URL
  below. For permanent hosting, deploy on a VPS with a domain + HTTPS.
"""

import logging
import os
import threading
import os

from flask import Flask, jsonify, request, send_file
from supabase import create_client, Client
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ------------------- CONFIG (fill these in) -------------------
BOT_TOKEN =os.getenv("BOT_TOKEN")
BOT_USERNAME = "Rohitloots_bot"

CHANNEL_ID = "@rohitlootsss"
CHANNEL_INVITE_LINK = "https://t.me/rohitlootsss"

WEBAPP_URL = "https://finalist-coil-entire.ngrok-free.dev"   # MUST be https

SPINS_PER_REFERRAL = 1        # how many spins the referrer gets per successful referral
SPIN_WIN_AMOUNT = 5.0         # ₹ credited to wallet per spin (demo — fixed amount)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")   # regenerate this key, see note above
# ----------------------------------------------------------------

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

data_lock = threading.Lock()  # kept for the Flask thread start, storage itself is now in Supabase


# ============== SUPABASE STORAGE ==============
def get_or_create_user(telegram_id, name: str, referred_by=None):
    telegram_id = int(telegram_id)
    existing = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()

    if existing.data:
        if name and existing.data[0].get("name") != name:
            supabase.table("users").update({"name": name}).eq("telegram_id", telegram_id).execute()
        return

    valid_ref = None
    if referred_by and int(referred_by) != telegram_id:
        valid_ref = int(referred_by)

    supabase.table("users").insert({
        "telegram_id": telegram_id,
        "name": name,
        "wallet": 0,
        "spins_available": 0,
        "spins_earned_total": 0,
        "referred_by": valid_ref,
    }).execute()

    if valid_ref:
        ref_res = supabase.table("users").select("*").eq("telegram_id", valid_ref).execute()
        if ref_res.data:
            ref_user = ref_res.data[0]
            supabase.table("users").update({
                "spins_available": ref_user.get("spins_available", 0) + SPINS_PER_REFERRAL,
                "spins_earned_total": ref_user.get("spins_earned_total", 0) + SPINS_PER_REFERRAL,
            }).eq("telegram_id", valid_ref).execute()


def get_user_stats(telegram_id):
    telegram_id = int(telegram_id)
    res = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    user = res.data[0] if res.data else {
        "wallet": 0, "spins_available": 0, "spins_earned_total": 0, "name": "",
    }

    ref_res = supabase.table("users").select("telegram_id", count="exact").eq("referred_by", telegram_id).execute()
    referral_count = ref_res.count or 0

    tx_res = (
        supabase.table("transactions")
        .select("*")
        .eq("telegram_id", telegram_id)
        .order("created_at", desc=True)
        .limit(20)
        .execute()
    )
    transactions = tx_res.data or []

    return user, referral_count, transactions


def do_spin(telegram_id):
    telegram_id = int(telegram_id)
    res = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    if not res.data or res.data[0].get("spins_available", 0) <= 0:
        return None

    user = res.data[0]
    new_wallet = float(user.get("wallet", 0)) + SPIN_WIN_AMOUNT
    new_spins = user.get("spins_available", 0) - 1

    supabase.table("users").update({
        "wallet": new_wallet,
        "spins_available": new_spins,
    }).eq("telegram_id", telegram_id).execute()

    supabase.table("transactions").insert({
        "telegram_id": telegram_id,
        "title": f"Lucky Spin Win (₹{SPIN_WIN_AMOUNT:.2f})",
        "amount": SPIN_WIN_AMOUNT,
        "type": "credit",
    }).execute()

    return {
        "won_amount": SPIN_WIN_AMOUNT,
        "wallet": new_wallet,
        "spins_available": new_spins,
        "spins_earned_total": user.get("spins_earned_total", 0),
    }


def add_withdrawal(telegram_id, amount, upi_id):
    supabase.table("withdrawals").insert({
        "telegram_id": int(telegram_id),
        "amount": amount,
        "upi_id": upi_id,
        "status": "pending",
    }).execute()


# ============== TELEGRAM BOT ==============
async def is_user_member(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning(f"Membership check failed for {user_id}: {e}")
        return False


def join_channel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=CHANNEL_INVITE_LINK)],
        [InlineKeyboardButton("🔄 Check Again", callback_data="check_membership")],
    ])


def verify_device_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 Verify Device", callback_data="verify_device")]
    ])


def open_app_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Open App", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    referred_by = None
    if context.args:
        try:
            referred_by = int(context.args[0])
        except ValueError:
            referred_by = None

    get_or_create_user(user.id, user.full_name, referred_by)

    joined = await is_user_member(context.bot, user.id)
    if joined:
        await update.message.reply_text(
            "🔐 <b>Device Verification</b>\n\nPlease verify to continue.",
            parse_mode=ParseMode.HTML,
            reply_markup=verify_device_keyboard(),
        )
    else:
        await update.message.reply_text(
            "📢 <b>Please Join Our Channel First</b>\n\n"
            "Channel join karne ke baad '🔄 Check Again' dabao.",
            parse_mode=ParseMode.HTML,
            reply_markup=join_channel_keyboard(),
        )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if query.data == "check_membership":
        joined = await is_user_member(context.bot, user_id)
        if joined:
            await query.edit_message_text(
                "🔐 <b>Device Verification</b>\n\nPlease verify to continue.",
                parse_mode=ParseMode.HTML,
                reply_markup=verify_device_keyboard(),
            )
        else:
            await query.answer(text="❌ Aapne abhi tak channel join nahi kiya!", show_alert=True)

    elif query.data == "verify_device":
        await query.edit_message_text(
            "✅ <b>Device Verified</b>\n\nAb app open karo aur earning shuru karo 👇",
            parse_mode=ParseMode.HTML,
            reply_markup=open_app_keyboard(),
        )


# ============== FLASK (serves Mini App + API) ==============
flask_app = Flask(__name__)
MINI_APP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mini_app.html")


@flask_app.route("/")
def serve_mini_app():
    return send_file(MINI_APP_PATH)


@flask_app.route("/api/user/<int:telegram_id>")
def api_user(telegram_id):
    user, referral_count, transactions = get_user_stats(telegram_id)
    return jsonify({
        "telegram_id": telegram_id,
        "name": user.get("name", ""),
        "wallet": user.get("wallet", 0.0),
        "spins_available": user.get("spins_available", 0),
        "spins_earned_total": user.get("spins_earned_total", 0),
        "referral_count": referral_count,
        "referral_link": f"https://t.me/{BOT_USERNAME}?start={telegram_id}",
        "bot_username": BOT_USERNAME,
        "transactions": transactions,
    })


@flask_app.route("/api/spin", methods=["POST"])
def api_spin():
    body = request.get_json(force=True)
    telegram_id = body.get("telegram_id")
    if not telegram_id:
        return jsonify({"success": False, "message": "Missing telegram_id"}), 400

    result = do_spin(telegram_id)
    if result is None:
        return jsonify({"success": False, "message": "No spins left. Refer a friend to get +1 spin!"}), 400

    return jsonify({"success": True, **result})


@flask_app.route("/api/withdraw", methods=["POST"])
def api_withdraw():
    body = request.get_json(force=True)
    telegram_id = body.get("telegram_id")
    amount = body.get("amount")
    upi_id = body.get("upi_id")

    if not telegram_id or not amount or not upi_id:
        return jsonify({"success": False, "message": "Missing fields"}), 400

    add_withdrawal(telegram_id, amount, upi_id)
    logger.info(f"Withdraw request (DEMO, not processed): user={telegram_id} amount={amount} upi={upi_id}")
    return jsonify({"success": True, "message": "Request received (demo, manual review)"})


def run_flask():
    flask_app.run(host="0.0.0.0", port=5000)


# ============== MAIN ==============
def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask server started on port 5000")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Bot started polling...")
    app.run_polling()


if __name__ == "__main__":
    main()

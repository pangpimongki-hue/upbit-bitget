import base64
import requests
import time
import random
import re
import uuid
import math
import threading
from datetime import datetime
from dateutil import parser
from bitget.v2.mix.order_api import OrderApi
from bitget.v2.mix.account_api import AccountApi
from bitget.v2.mix.market_api import MarketApi
from bitget.exceptions import BitgetAPIException
from dateutil import parser
from datetime import datetime, timedelta, timezone

active_monitoring = True
BALANCE_THRESHOLD = 50  # 超过这个余额才重新启动监听

# 想测试第几条公告（1 表示最新，2 表示第二条，以此类推）
TARGET_INDEX = 1  # 正式运行时设为 1

# === Bitget API 配置 ===
API_KEY = "待填写"
API_SECRET = "待填写"
API_PASSPHRASE = "待填写"

# === Telegram 配置 ===
TELEGRAM_BOT_TOKEN = "待填写"
TELEGRAM_CHAT_ID = "待填写"

# === 交易配置 ===
PRODUCT_TYPE = 'USDT-FUTURES'
MARGIN_COIN = 'USDT'
MARGIN_MODE = 'crossed'
LEVERAGE = 20  # 仅用于下单计算，不再设置杠杆

# 快代理统一隧道地址和端口
proxy_host = "待填写"
proxy_port = 待填写

# 快代理用户名密码
username = "待填写"
password = "待填写"

# 可用的通道编号（1 ~ 10）
channels = list(range(1, 11))

# 目标 URL
url = "https://api-manager.upbit.com/api/v1/announcements"

# 关键词
KEYWORDS = ["신규 거래지원", "디지털 자산 추가", "Market Support for"]


# 处理逻辑
# === 初始化 API 实例 ===
order_api = OrderApi(API_KEY, API_SECRET, API_PASSPHRASE)
account_api = AccountApi(API_KEY, API_SECRET, API_PASSPHRASE)
market_api = MarketApi(API_KEY, API_SECRET, API_PASSPHRASE)

# === 缓存结构 ===
contracts_cache = None
contracts_cache_time = 0
contracts_cache_ttl = 50000 # 50000s缓存

balance_cache = None
balance_cache_time = 0
balance_cache_ttl = 100  # 100秒缓存

def send_telegram_message_async(message):#telegram推送
    def send():
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
            response = requests.post(url, data=payload, timeout=10)
            if response.status_code != 200:
                print(f"Telegram 警报发送失败: {response.text}")
        except Exception as e:
            print(f"发送 Telegram 警报出错: {e}")
    threading.Thread(target=send, daemon=True).start()
def send_pushplus_message_async(content):#微信推送
    def send():
        try:
            url = "https://www.pushplus.plus/send"
            payload = {
                "token": "待填写",
                "title": "Bitget下单通知",
                "content": content,
                "template": "html"
            }
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code != 200:
                print(f"PushPlus 推送失败: {response.text}")
        except Exception as e:
            print(f"PushPlus 发送出错: {e}")
    threading.Thread(target=send, daemon=True).start()


def get_contracts():
    global contracts_cache, contracts_cache_time, symbol_dict
    now = time.time()
    if contracts_cache and now - contracts_cache_time < contracts_cache_ttl:
        return contracts_cache
    try:
        res = market_api.contracts({'productType': PRODUCT_TYPE})
        contracts_cache = res.get('data', [])
        contracts_cache_time = now
        symbol_dict = {contract['symbol']: contract for contract in contracts_cache}
        return contracts_cache
    except BitgetAPIException as e:
        print(f"获取合约列表失败: {e.message}")
        return []


def get_symbol_from_cache(coin):
    symbol = coin.upper() + MARGIN_COIN
    return symbol if symbol in symbol_dict else None

def get_balance(margin_coin):
    global balance_cache, balance_cache_time
    now = time.time()
    if balance_cache and now - balance_cache_time < balance_cache_ttl:
        return balance_cache
    try:
        accounts = account_api.accounts({'productType': PRODUCT_TYPE})#这里不是要传2个参数吗，难道全部的话，币种留空即可（）不是symbol不是必须
        for account in accounts['data']:
            if account['marginCoin'] == margin_coin:
                balance_cache = float(account['available'])
                balance_cache_time = now
                return balance_cache
    except BitgetAPIException as e:
        print(f"获取账户余额失败: {e.message}")
    balance_cache = 0.0
    balance_cache_time = now
    return 0.0

def get_latest_price(symbol):  #最新价有用吗，还是要指数价格indexPrice
    try:
        url = f"https://api.bitget.com/api/v2/mix/market/ticker?productType=USDT-FUTURES&symbol={symbol}"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        return float(resp.json()['data'][0]['lastPr'])#改指数价格的话就是 indexPrice
    except Exception as e:
        print(f"获取现价失败: {e}")
        return 0.0
def place_order(symbol, size):
    try:
        order = {
                "symbol": symbol,
                "productType": PRODUCT_TYPE,
                "size": str(size),
                "marginCoin":MARGIN_COIN,
                "side": "buy",
                "tradeSide":"open",#不知道为什么变成双向持仓，所以要加上这个，可以取消变单向https://www.bitget.com/zh-CN/api-doc/contract/account/Change-Hold-Mode
                "orderType": "market",
                "marginMode": MARGIN_MODE,
                "clientOid": str(uuid.uuid4())
        }
        response = order_api.placeOrder(order)
        print(f"下单成功: {response}")
        if response.get("code") != "00000":
            msg = response.get("msg", "未知错误")
            print(f"⚠️ 下单返回错误: {msg}")
            send_telegram_message_async(f"❌ 下单失败: {msg}")
            return

        order_id = response["data"]["orderId"]
        send_telegram_message_async(f"📈 已买入 <b>{symbol}</b>\n下单张数: <b>{size}</b>")

    except BitgetAPIException as e:
        print(f"下单失败: {e.message}")
        send_telegram_message_async(f"❌ 下单失败: {e.message}")  
     

def calculate_order_size(balance, leverage, index_price, min_trade_num, size_multiplier, volume_place):#和https://www.bitget.com/zh-CN/api-doc/contract/account/Est-Open-Count这个方法有何不同#
    if index_price == 0:
        return 0
    max_size = balance / index_price
    leveraged_size = max_size * leverage
    adjusted_size = math.floor(leveraged_size / size_multiplier) * size_multiplier
    if adjusted_size < min_trade_num:
        adjusted_size = min_trade_num
    return round(adjusted_size, volume_place)       
def process_coin(coin, detection_time=None):
    start_time = time.time()
    symbol = get_symbol_from_cache(coin)
    if not symbol:
        send_telegram_message_async(f"⚠️ Bitget 不支持 {coin}/USDT 合约")
        return

    contract = symbol_dict.get(symbol)
    if not contract:
        send_telegram_message_async(f"⚠️ 获取 {symbol} 合约参数失败")
        return
    balance = get_balance(MARGIN_COIN)  
    if balance <= 0:
        send_telegram_message_async("⚠️ 账户 USDT 余额不足，无法下单")
        return

    price = get_latest_price(symbol)
    if price <= 0:
        send_telegram_message_async("⚠️ 获取价格失败，无法下单")
        return

    min_trade_num = float(contract['minTradeNum'])
    size_multiplier = float(contract['sizeMultiplier'])
    volume_place = int(contract['volumePlace'])
    safe_balance = balance * 0.95
    size = calculate_order_size(safe_balance, LEVERAGE, price, min_trade_num, size_multiplier, volume_place)
    if size < min_trade_num:
        send_telegram_message_async(f"⚠️ 计算出的下单张数 {size} 小于最小交易数 {min_trade_num}，跳过下单")
        return

    place_order(symbol, size)
    global active_monitoring, balance_cache_time
    active_monitoring = False
    balance_cache_time = 0  # 强制下次调用 get_balance 重新请求接口
    print("🎯 成功下单，暂停公告监听，等待余额恢复")

    end_time = time.time()

    # 计算并打印从检测公告到下单完成的耗时
    if detection_time:
        elapsed_ms = (end_time - detection_time) * 1000
        print(f"⌛ 从检测公告到下单完成耗时: {elapsed_ms:.2f} ms")
        send_telegram_message_async(f"⌛ 从检测公告到下单完成耗时: {elapsed_ms:.0f} ms")
        send_pushplus_message_async(f"⌛ 从检测公告到下单完成耗时: {elapsed_ms:.0f} ms")

    # 额外打印本函数处理耗时（可选）
    func_elapsed_ms = (end_time - start_time) * 1000
    print(f"处理币种 {coin} 下单耗时: {func_elapsed_ms:.2f} ms")


def get_proxy_headers(channel_id):
    auth_str = f"{username}:{password}:{channel_id}"
    auth_encoded = base64.b64encode(auth_str.encode()).decode()
    return {
        "Proxy-Authorization": f"Basic {auth_encoded}",
        "Connection": "close",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }

def make_request(channel_id):
    proxies = {
        "http": f"http://{proxy_host}:{proxy_port}",
        "https": f"http://{proxy_host}:{proxy_port}"
    }

    headers = get_proxy_headers(channel_id)
    params = {
        "category": "trade",
        "page": 1,
        "per_page": 5,
        "os": "web"
    }

    try:
        response = requests.get(url, headers=headers, proxies=proxies, params=params, timeout=5)
        if response.status_code == 200:
            notices = response.json()['data']['notices']
            if len(notices) < TARGET_INDEX:
                print(f"⚠️ 公告不足 {TARGET_INDEX} 条，当前仅有 {len(notices)} 条")
                return

            notice = notices[TARGET_INDEX - 1]
            title = notice['title']
            created_at = parser.isoparse(notice['listed_at'])  # 解析公告时间
            now = datetime.now(timezone.utc)  # 当前 UTC 时间

            print(f"✅ 通道 {channel_id} 第 {TARGET_INDEX} 条公告: {title} | 发布时间: {created_at}")

            if (now - created_at) > timedelta(minutes=5):
                print("⏰ 公告发布时间超过 5 分钟，仅提示不处理")
                return

            if any(keyword in title for keyword in KEYWORDS):
                match = re.search(r"\(([^)]+)\)", title)
                if match:
                    coin = match.group(1).strip()
                    detection_time = time.time()  # 记录检测到公告的时间
                    print(f"🎯 命中关键词，提取币种: {coin}")
                    process_coin(coin, detection_time)  # 传入检测时间
                else:
                    print("⚠️ 找到关键词但未提取到括号中的币种")
            else:
                print("🔍 该公告不包含关键词")
        else:
            print(f"⚠️ 通道 {channel_id} 状态异常: {response.status_code}")
    except Exception as e:
        print(f"❌ 通道 {channel_id} 请求失败: {e}")

if __name__ == "__main__":
    print("启动时预加载合约列表...")
    get_contracts()

    print("启动时预加载账户余额...")
    get_balance(MARGIN_COIN)

    while True:
        if active_monitoring:
            channel = random.choice(channels)
            make_request(channel)
            time.sleep(0.5)
        else:
            # 检查余额是否恢复大于阈值
            balance = get_balance(MARGIN_COIN)
            print(f"⏳ 当前余额为 {balance:.2f}，等待大于 {BALANCE_THRESHOLD} 后恢复监听...")
            if balance > BALANCE_THRESHOLD:
                active_monitoring = True
                print("✅ 余额充足，恢复公告监听！")
                send_telegram_message_async("✅ 余额已恢复，继续监听公告。")
            time.sleep(1)  # 检查频率不需要太高

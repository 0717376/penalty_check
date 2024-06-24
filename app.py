import os
import logging
import requests
import json
import socks
import socket
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TimedOut, NetworkError

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Get the bot token from environment variable
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
if not TOKEN:
    raise ValueError("No token provided. Set the TELEGRAM_BOT_TOKEN environment variable.")

# Constants for proxy timeout
PROXY_TIMEOUT = 5  # Increased timeout for proxy testing
MAX_RETRIES = 5    # Increased number of retries

def get_proxy_list():
    url = "https://proxylist.geonode.com/api/proxy-list?country=GE&protocols=socks4&limit=500&page=1&sort_by=lastChecked&sort_type=desc"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        return [(proxy['ip'], proxy['port']) for proxy in data['data']]
    except Exception as e:
        logger.error(f"Failed to fetch proxy list: {e}")
        return []

def set_socks_proxy(proxy_host, proxy_port):
    socks.set_default_proxy(socks.SOCKS4, proxy_host, int(proxy_port))
    socket.socket = socks.socksocket

def test_proxy(proxy):
    try:
        set_socks_proxy(proxy[0], proxy[1])
        response = requests.get('https://police.ge/protocol/index.php?lang=en', timeout=PROXY_TIMEOUT)
        return response.status_code == 200
    except Exception as e:
        logger.warning(f"Proxy {proxy[0]}:{proxy[1]} failed: {str(e)}")
        return False

def get_working_proxy():
    proxy_list = get_proxy_list()
    for _ in range(MAX_RETRIES):
        for proxy in proxy_list:
            if test_proxy(proxy):
                logger.info(f"Working proxy found: {proxy[0]}:{proxy[1]}")
                return proxy
        logger.warning("No working proxy found, retrying from the beginning of the list...")
    logger.error("No working proxy found after all retries")
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Welcome! Please enter your vehicle registration number to check for fines.')

async def check_fines(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    vehicle_number = update.message.text.strip().upper()
    logger.info(f"Checking fines for vehicle number: {vehicle_number}")
    
    proxy = get_working_proxy()
    if not proxy:
        await update.message.reply_text("Sorry, couldn't find a working proxy. Please try again later.")
        return

    set_socks_proxy(proxy[0], proxy[1])

    try:
        # First request to get the CSRF token and cookies
        session = requests.Session()
        response = session.get('https://police.ge/protocol/index.php?lang=en', 
                               headers={
                                   'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.60 Safari/537.36'
                               },
                               timeout=30)
        response.raise_for_status()
        
        # Extract CSRF token from the response
        csrf_token = None
        for line in response.text.split('\n'):
            if 'csrf_token' in line:
                csrf_token = line.split('value="')[1].split('"')[0]
                break
        
        if not csrf_token:
            logger.error("Failed to extract CSRF token")
            await update.message.reply_text("Sorry, couldn't retrieve the necessary information. Please try again later.")
            return
        
        logger.info(f"CSRF token obtained: {csrf_token}")
        logger.info(f"Cookies: {session.cookies.get_dict()}")
        
        # Second request to check for fines
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.60 Safari/537.36',
            'Origin': 'https://police.ge',
            'Referer': 'https://police.ge/protocol/index.php?lang=en',
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
        }
        data = {
            'firstResult': '0',
            'protocolAuto': vehicle_number,
            'csrf_token': csrf_token
        }
        response = session.post('https://police.ge/protocol/index.php?url=protocols/searchByAuto', 
                                headers=headers, 
                                data=data,
                                timeout=30)
        response.raise_for_status()
        
        logger.info(f"Response status code: {response.status_code}")
        logger.info(f"Response headers: {response.headers}")
        logger.info(f"Response content: {response.text}")
        
        result = response.json()
        if result['success']:
            fines_count = result['data']['count']
            if fines_count > 0:
                fines = result['data']['results']
                message = f"Found {fines_count} fine(s) for vehicle {vehicle_number}:\n\n"
                for fine in fines:
                    message += f"Date: {fine['violationDate']}\n"
                    message += f"Amount: {fine['protocolAmount']} GEL\n"
                    message += f"Due date: {fine['lastDate']}\n\n"
            else:
                message = f"No fines found for vehicle {vehicle_number}."
        else:
            message = f"The server reported an error: {result.get('message', 'Unknown error')}"
    
    except requests.RequestException as e:
        logger.error(f"Request error: {e}")
        message = f"Sorry, there was an error checking for fines: {str(e)}"
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error: {e}")
        logger.error(f"Response content: {response.text}")
        message = "Sorry, couldn't process the response from the server. The response wasn't in the expected format."
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        message = f"An unexpected error occurred: {str(e)}"
    
    try:
        await update.message.reply_text(message)
    except (TimedOut, NetworkError) as e:
        logger.error(f"Error sending message to user: {e}")
        await update.message.reply_text("Sorry, there was an error sending the response. Please try again later.")

def main() -> None:
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_fines))

    application.run_polling()

if __name__ == '__main__':
    main()
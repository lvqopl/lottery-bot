import os
import logging

from giveaway_bot.bot import GiveawayBot
from giveaway_bot.storage import DB
from giveaway_bot.telegram_api import TelegramClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
DB_PATH = os.path.join(DATA_DIR, 'giveaway.db')


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def setup_logging():
    ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        handlers=[logging.FileHandler(os.path.join(LOG_DIR, 'bot.log'), encoding='utf-8'), logging.StreamHandler()],
    )


def load_token():
    token = os.environ.get('BOT_TOKEN', '').strip()
    if not token:
        raise RuntimeError('请先设置环境变量 BOT_TOKEN。')
    return token


def main():
    setup_logging()
    db = DB(DB_PATH)
    api = TelegramClient(load_token())
    bot = GiveawayBot(db, api)
    bot.run()


if __name__ == '__main__':
    main()

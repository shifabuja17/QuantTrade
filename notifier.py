import aiohttp
import logging
from config import NotificationConfig

logger = logging.getLogger(__name__)

class TelegramNotifier:
    """Modul asinkron untuk mengirim notifikasi ke Telegram."""
    
    def __init__(self, config: NotificationConfig):
        self.config = config
        self.api_url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"

    async def send_message(self, text: str) -> None:
        """Mengirim pesan teks format HTML ke Telegram."""
        if not self.config.enabled or not self.config.telegram_bot_token or not self.config.telegram_chat_id:
            return

        payload = {
            "chat_id": self.config.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, json=payload) as response:
                    if response.status != 200:
                        error_msg = await response.text()
                        logger.error(f"Gagal mengirim notifikasi Telegram: {error_msg}")
        except Exception as e:
            logger.error(f"Error asinkron pada TelegramNotifier: {e}")
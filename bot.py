#!/usr/bin/env python3
"""
Telegram бот для отслеживания промокодов и предметов в нескольких профилях на case-battle.xxx
"""

import asyncio
import json
import logging
import os
import sys
import signal
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any, List, Set

import aiohttp
from aiohttp import ClientTimeout, ClientError
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# =====================================================
# ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# =====================================================

load_dotenv()

class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не установлен! Создайте файл .env")
    
    URL: str = os.getenv("URL", "https://case-battle.xxx")
    # Несколько профилей через запятую
    USER_URLS: List[str] = [
        url.strip() for url in os.getenv("USER_URLS", "").split(",") if url.strip()
    ]
    if not USER_URLS:
        USER_URLS = ["https://case-battle.xxx/users/xxx"]
    
    CHECK_INTERVAL: int = int(os.getenv("CHECK_INTERVAL", "10"))
    DROP_CHECK_INTERVAL: int = int(os.getenv("DROP_CHECK_INTERVAL", "5"))
    COOKIE_FILE: Path = Path(os.getenv("COOKIE_FILE", "cookies.txt"))
    STATE_FILE: Path = Path(os.getenv("STATE_FILE", "state.json"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "15"))
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"
    
    ALLOWED_USERS: List[int] = []
    if os.getenv("ALLOWED_USERS"):
        ALLOWED_USERS = [int(x.strip()) for x in os.getenv("ALLOWED_USERS", "").split(",") if x]

# =====================================================
# НАСТРОЙКА ЛОГГИРОВАНИЯ
# =====================================================

def setup_logging():
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, Config.LOG_LEVEL))
    logger.handlers.clear()
    
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(log_format, date_format))
    logger.addHandler(console)
    
    try:
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            log_dir / "bot.log",
            maxBytes=10_485_760,
            backupCount=5,
            encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(log_format, date_format))
        logger.addHandler(file_handler)
        
        error_handler = RotatingFileHandler(
            log_dir / "errors.log",
            maxBytes=5_242_880,
            backupCount=3,
            encoding="utf-8"
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(logging.Formatter(log_format, date_format))
        logger.addHandler(error_handler)
        
    except Exception as e:
        logging.warning(f"Не удалось создать файловый логгер: {e}")

setup_logging()
logger = logging.getLogger(__name__)

# =====================================================
# МОДЕЛИ ДАННЫХ
# =====================================================

@dataclass
class ProfileItem:
    name: str
    rarity: str
    price: float
    image: str
    source: str
    source_name: str
    status: str
    timestamp: datetime
    item_id: str = ""
    profile_url: str = ""
    
    def __post_init__(self):
        if not self.item_id:
            self.item_id = f"{self.name}_{self.price:.2f}"
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "rarity": self.rarity,
            "price": self.price,
            "image": self.image,
            "source": self.source,
            "source_name": self.source_name,
            "status": self.status,
            "timestamp": self.timestamp.isoformat(),
            "item_id": self.item_id,
            "profile_url": self.profile_url
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "ProfileItem":
        return cls(
            name=data["name"],
            rarity=data["rarity"],
            price=data["price"],
            image=data["image"],
            source=data["source"],
            source_name=data["source_name"],
            status=data.get("status", "active"),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            item_id=data.get("item_id", ""),
            profile_url=data.get("profile_url", "")
        )
    
    def get_rarity_emoji(self) -> str:
        emojis = {
            "consumer": "⬜",
            "industrial": "🟦",
            "mil-spec": "🟩",
            "restricted": "🟪",
            "classified": "🟫",
            "covert": "🟥",
            "contraband": "🟧"
        }
        return emojis.get(self.rarity.lower(), "⬜")
    
    def get_rarity_name(self) -> str:
        names = {
            "consumer": "Базовое",
            "industrial": "Промышленное",
            "mil-spec": "Армейское",
            "restricted": "Запрещённое",
            "classified": "Секретное",
            "covert": "Тайное",
            "contraband": "Контрабандное"
        }
        return names.get(self.rarity.lower(), "Неизвестно")
    
    def format_message(self) -> str:
        emoji = self.get_rarity_emoji()
        rarity_name = self.get_rarity_name()
        return (
            f"{emoji} **{self.name}**\n"
            f"📊 {rarity_name}\n"
            f"💰 {self.price:.2f} ₽\n"
            f"📦 {self.source_name}\n"
            f"🕐 {self.timestamp.strftime('%H:%M:%S')}"
        )

@dataclass
class ProfileState:
    """Состояние для одного профиля"""
    last_items_ids: Set[str] = None
    total_items_count: int = 0
    last_item: Optional[ProfileItem] = None
    first_run: bool = True
    profile_url: str = ""
    
    def __post_init__(self):
        if self.last_items_ids is None:
            self.last_items_ids = set()
    
    def to_dict(self) -> dict:
        return {
            "last_items_ids": list(self.last_items_ids),
            "total_items_count": self.total_items_count,
            "last_item": self.last_item.to_dict() if self.last_item else None,
            "first_run": self.first_run,
            "profile_url": self.profile_url
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "ProfileState":
        state = cls()
        state.last_items_ids = set(data.get("last_items_ids", []))
        state.total_items_count = data.get("total_items_count", 0)
        state.first_run = data.get("first_run", True)
        state.profile_url = data.get("profile_url", "")
        if data.get("last_item"):
            state.last_item = ProfileItem.from_dict(data["last_item"])
        return state

@dataclass
class BotState:
    running: bool = False
    chat_id: Optional[int] = None
    promo_message_id: Optional[int] = None
    current_promo: Optional[str] = None
    promo_timer: str = "--:--:--"
    profiles: Dict[str, ProfileState] = None
    
    def __post_init__(self):
        if self.profiles is None:
            self.profiles = {}
    
    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "chat_id": self.chat_id,
            "promo_message_id": self.promo_message_id,
            "current_promo": self.current_promo,
            "promo_timer": self.promo_timer,
            "profiles": {url: state.to_dict() for url, state in self.profiles.items()}
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "BotState":
        state = cls()
        state.running = data.get("running", False)
        state.chat_id = data.get("chat_id")
        state.promo_message_id = data.get("promo_message_id")
        state.current_promo = data.get("current_promo")
        state.promo_timer = data.get("promo_timer", "--:--:--")
        
        profiles_data = data.get("profiles", {})
        for url, profile_data in profiles_data.items():
            state.profiles[url] = ProfileState.from_dict(profile_data)
        
        return state

# =====================================================
# МЕНЕДЖЕР СОСТОЯНИЯ
# =====================================================

class StateManager:
    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.state = BotState()
        self._load()
    
    def _load(self):
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.state = BotState.from_dict(data)
                    logger.info("✅ Состояние загружено из %s", self.state_file)
            except Exception as e:
                logger.error("❌ Ошибка загрузки состояния: %s", e)
    
    def save(self):
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.state.to_dict(), f, ensure_ascii=False, indent=2)
            logger.debug("💾 Состояние сохранено")
        except Exception as e:
            logger.error("❌ Ошибка сохранения состояния: %s", e)
    
    def update(self, **kwargs):
        for key, value in kwargs.items():
            if hasattr(self.state, key):
                setattr(self.state, key, value)
        self.save()
    
    def get_profile_state(self, url: str) -> ProfileState:
        """Получить состояние для профиля"""
        if url not in self.state.profiles:
            self.state.profiles[url] = ProfileState(profile_url=url)
        return self.state.profiles[url]
    
    def update_profile(self, url: str, **kwargs):
        """Обновить состояние профиля"""
        if url not in self.state.profiles:
            self.state.profiles[url] = ProfileState(profile_url=url)
        for key, value in kwargs.items():
            if hasattr(self.state.profiles[url], key):
                setattr(self.state.profiles[url], key, value)
        self.save()

# =====================================================
# HTTP КЛИЕНТ
# =====================================================

class HttpClient:
    def __init__(self):
        self.timeout = ClientTimeout(total=Config.REQUEST_TIMEOUT)
        self.session: Optional[aiohttp.ClientSession] = None
        self._cookies_loaded = False
        
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
    
    async def __aenter__(self):
        await self.init_session()
        return self
    
    async def __aexit__(self, *args):
        await self.close()
    
    async def init_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers=self.headers,
                connector=aiohttp.TCPConnector(
                    limit=10,
                    ttl_dns_cache=300,
                    enable_cleanup_closed=True
                )
            )
            await self.load_cookies()
    
    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None
    
    async def load_cookies(self):
        if not Config.COOKIE_FILE.exists():
            logger.warning("⚠️ Файл cookies не найден: %s", Config.COOKIE_FILE)
            self._cookies_loaded = False
            return
        
        try:
            cookies = {}
            with open(Config.COOKIE_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 7:
                        name = parts[5]
                        value = parts[6]
                        cookies[name] = value
            
            if cookies:
                self.session.cookie_jar.update_cookies(cookies)
                self._cookies_loaded = True
                logger.info("🍪 Загружено %d cookies", len(cookies))
            else:
                logger.warning("⚠️ Cookies не найдены в файле")
                self._cookies_loaded = False
                
        except Exception as e:
            logger.error("❌ Ошибка загрузки cookies: %s", e)
            self._cookies_loaded = False
    
    async def get_html(self, url: str) -> Optional[str]:
        if self.session is None:
            await self.init_session()
        
        for attempt in range(Config.MAX_RETRIES):
            try:
                async with self.session.get(url, ssl=False) as response:
                    if response.status == 200:
                        html = await response.text()
                        logger.debug("📄 HTML получен (%d байт)", len(html))
                        return html
                    elif response.status == 404:
                        logger.error("❌ Страница не найдена (404)")
                        return None
                    elif response.status in (429, 503):
                        wait = (attempt + 1) * 2
                        logger.warning("⏳ Rate limit (%d), ждём %dс", response.status, wait)
                        await asyncio.sleep(wait)
                    else:
                        logger.warning("⚠️ HTTP %d", response.status)
                        await asyncio.sleep(1)
                        
            except asyncio.TimeoutError:
                logger.warning("⏰ Таймаут (попытка %d/%d)", attempt + 1, Config.MAX_RETRIES)
                await asyncio.sleep(1)
            except ClientError as e:
                logger.warning("⚠️ Ошибка клиента: %s", e)
                await asyncio.sleep(1)
            except Exception as e:
                logger.exception("❌ Неизвестная ошибка: %s", e)
                await asyncio.sleep(2)
        
        logger.error("❌ Не удалось получить HTML после %d попыток", Config.MAX_RETRIES)
        return None

# =====================================================
# ПАРСЕРЫ
# =====================================================

class PromoParser:
    @staticmethod
    def parse(html: str) -> tuple:
        try:
            soup = BeautifulSoup(html, "html.parser")
            
            promo = None
            promo_elem = soup.find(attrs={"data-promocode": True})
            if promo_elem:
                promo = promo_elem.get("data-promocode", "").strip()
            
            timer = "--:--:--"
            timer_elem = soup.find(id="promocode-countdown")
            if timer_elem:
                now = timer_elem.get("data-now")
                end = timer_elem.get("data-time")
                if now and end:
                    try:
                        left = max(0, int(end) - int(now))
                        hours, rem = divmod(left, 3600)
                        minutes, seconds = divmod(rem, 60)
                        timer = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                    except (ValueError, TypeError):
                        pass
            
            return promo, timer
            
        except Exception as e:
            logger.exception("❌ Ошибка парсинга промокода: %s", e)
            return None, "--:--:--"

class ProfileParser:
    @staticmethod
    def parse_items(html: str, profile_url: str = "") -> List[ProfileItem]:
        try:
            soup = BeautifulSoup(html, "html.parser")
            items = []
            
            items_block = soup.find("div", class_="items")
            if not items_block:
                items_block_other = soup.find("div", class_="items-block-other")
                if items_block_other:
                    items_block = items_block_other.find("div", class_="items")
            
            if not items_block:
                logger.warning("⚠️ Блок .items не найден")
                return []
            
            units = items_block.find_all("div", class_="unit")
            
            if not units:
                logger.warning("⚠️ Нет предметов в блоке .items")
                return []
            
            logger.info(f"🔍 Найдено {len(units)} предметов")
            
            for unit in units:
                weapon_div = unit.find("div", class_="weapon")
                if not weapon_div:
                    continue
                
                classes = weapon_div.get("class", [])
                rarity = "unknown"
                for cls in classes:
                    if cls in ["consumer", "industrial", "mil-spec", "restricted", "classified", "covert", "contraband"]:
                        rarity = cls
                        break
                
                title_div = weapon_div.find("div", class_="unit-title")
                if not title_div:
                    continue
                
                title_text = title_div.get_text(strip=True)
                parts = title_text.split("\n")
                clean_parts = [p.strip() for p in parts if p.strip()]
                
                if len(clean_parts) >= 2:
                    full_name = f"{clean_parts[0]} | {clean_parts[1]}"
                else:
                    full_name = clean_parts[0] if clean_parts else "Неизвестно"
                
                price = 0.0
                price_div = weapon_div.find("div", class_="price")
                if price_div:
                    price_text = price_div.get_text(strip=True)
                    price_text = price_text.replace("₽", "").replace(",", ".").strip()
                    try:
                        price = float(price_text)
                    except ValueError:
                        pass
                
                image = ""
                img = weapon_div.find("img", class_="skin-image")
                if img and img.get("src"):
                    image = img["src"]
                    if not image.startswith("http"):
                        image = "https://case-battle.xxx" + image
                
                source = "unknown"
                source_name = "Неизвестно"
                status = "active"
                
                source_ico = weapon_div.find("a", class_="source-ico")
                if source_ico:
                    tooltip = source_ico.get("data-tooltip-content", "")
                    if tooltip:
                        source_name = tooltip
                        if "апгрейд" in tooltip.lower():
                            source = "upgrade"
                        elif "контракт" in tooltip.lower():
                            source = "contract"
                        elif "кейс" in tooltip.lower():
                            source = "case"
                    else:
                        svg = source_ico.find("svg")
                        if svg:
                            use = svg.find("use")
                            if use:
                                href = use.get("xlink:href", "")
                                if "upgrade" in href:
                                    source = "upgrade"
                                    source_name = "Апгрейд"
                                elif "contract" in href:
                                    source = "contract"
                                    source_name = "Контракт"
                                elif "case" in href:
                                    source = "case"
                                    source_name = "Кейс"
                
                si_div = weapon_div.find("div", class_="si")
                if si_div:
                    svg = si_div.find("svg")
                    if svg:
                        use = svg.find("use")
                        if use:
                            href = use.get("xlink:href", "")
                            if "upgrade" in href:
                                status = "used_in_upgrade"
                                if source == "unknown":
                                    source_name = "Использован в апгрейде"
                            elif "contract" in href:
                                status = "used_in_contract"
                                if source == "unknown":
                                    source_name = "Использован в контракте"
                            elif "coins" in href:
                                status = "sold"
                                if source == "unknown":
                                    source_name = "Продан"
                
                clean_name = full_name.replace("<span>", "").replace("</span>", "")
                
                item = ProfileItem(
                    name=clean_name,
                    rarity=rarity,
                    price=price,
                    image=image,
                    source=source,
                    source_name=source_name,
                    status=status,
                    timestamp=datetime.now(),
                    profile_url=profile_url
                )
                items.append(item)
            
            logger.info(f"📦 Всего найдено предметов: {len(items)}")
            return items
            
        except Exception as e:
            logger.exception(f"❌ Ошибка парсинга профиля: {e}")
            return []

# =====================================================
# ФОРМАТТЕР
# =====================================================

class MessageFormatter:
    @staticmethod
    def format_promo(promo: Optional[str], timer: str) -> str:
        if not promo:
            return "❌ Промокод не найден или неактивен"
        
        lines = [
            "🎁 **Активный промокод**",
            "",
            f"`{promo}`",
            "",
            "⏳ **До следующего обновления:**",
            f"`{timer}`",
        ]
        return "\n".join(lines)
    
    @staticmethod
    def format_promo_change(old: str, new: str, timer: str) -> str:
        return (
            "🚨 **НОВЫЙ ПРОМОКОД!**\n\n"
            f"**Старый:** `{old}`\n"
            f"**Новый:** `{new}`\n\n"
            f"⏳ До следующего: `{timer}`"
        )
    
    @staticmethod
    def format_item(item: ProfileItem) -> str:
        emoji = item.get_rarity_emoji()
        rarity_name = item.get_rarity_name()
        # Извлекаем имя пользователя из URL
        user_name = item.profile_url.split("/")[-1] if item.profile_url else "Неизвестно"
        return (
            f"🎯 **ПРЕДМЕТ В ПРОФИЛЕ!**\n\n"
            f"👤 {user_name}\n"
            f"{emoji} **{item.name}**\n"
            f"📊 {rarity_name}\n"
            f"💰 {item.price:.2f} ₽\n"
            f"📦 {item.source_name}\n"
            f"🕐 {item.timestamp.strftime('%H:%M:%S')}"
        )

# =====================================================
# БОТ
# =====================================================

class PromoBot:
    def __init__(self):
        self.http_client = HttpClient()
        self.state_manager = StateManager(Config.STATE_FILE)
        self.application = None
        self.promo_task: Optional[asyncio.Task] = None
        self.profile_task: Optional[asyncio.Task] = None
        self._shutdown = False
    
    async def _check_auth(self) -> bool:
        await self.http_client.load_cookies()
        return self.http_client._cookies_loaded
    
    async def get_promo_and_timer(self) -> tuple:
        html = await self.http_client.get_html(Config.URL)
        if html is None:
            return None, "--:--:--"
        return PromoParser.parse(html)
    
    async def get_profile_items(self, url: str) -> List[ProfileItem]:
        html = await self.http_client.get_html(url)
        if html is None:
            return []
        
        if Config.DEBUG:
            debug_file = Path(f"debug_profile_{url.split('/')[-1]}.html")
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(html)
        
        return ProfileParser.parse_items(html, url)
    
    async def _send_message(self, chat_id: int, text: str) -> Optional[Dict]:
        try:
            message = await self.application.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
            return message.to_dict()
        except Exception as e:
            logger.error("❌ Ошибка отправки: %s", e)
            return None
    
    async def _edit_message(self, chat_id: int, message_id: int, text: str) -> bool:
        try:
            await self.application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="Markdown"
            )
            return True
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.warning("⚠️ Ошибка редактирования: %s", e)
            return False
    
    async def promo_watcher(self):
        state = self.state_manager.state
        logger.info("👁️ Watcher промокодов запущен (интервал: %dс)", Config.CHECK_INTERVAL)
        
        while state.running and not self._shutdown:
            try:
                promo, timer = await self.get_promo_and_timer()
                
                if promo is None:
                    await asyncio.sleep(Config.CHECK_INTERVAL)
                    continue
                
                text = MessageFormatter.format_promo(promo, timer)
                
                if state.promo_message_id is None:
                    result = await self._send_message(state.chat_id, text)
                    if result:
                        state.promo_message_id = result["message_id"]
                        state.current_promo = promo
                        state.promo_timer = timer
                        self.state_manager.update(
                            promo_message_id=state.promo_message_id,
                            current_promo=promo,
                            promo_timer=timer
                        )
                else:
                    await self._edit_message(state.chat_id, state.promo_message_id, text)
                
                if state.current_promo and state.current_promo != promo:
                    alert_text = MessageFormatter.format_promo_change(
                        state.current_promo, promo, timer
                    )
                    await self._send_message(state.chat_id, alert_text)
                    logger.info("🚨 Новый промокод: %s", promo)
                    
                    state.current_promo = promo
                    state.promo_timer = timer
                    self.state_manager.update(
                        current_promo=promo,
                        promo_timer=timer
                    )
                
            except asyncio.CancelledError:
                logger.info("👁️ Watcher промокодов остановлен")
                break
            except Exception as e:
                logger.exception("❌ Ошибка в watcher промокодов: %s", e)
                await asyncio.sleep(Config.CHECK_INTERVAL * 2)
            
            await asyncio.sleep(Config.CHECK_INTERVAL)
        
        logger.info("👁️ Watcher промокодов завершён")
    
    async def profile_watcher(self):
        """LIVE отслеживание предметов в нескольких профилях"""
        state = self.state_manager.state
        logger.info("👤 LIVE Watcher профилей запущен (интервал: %dс)", Config.DROP_CHECK_INTERVAL)
        logger.info(f"📡 Отслеживается {len(Config.USER_URLS)} профилей")
        
        # Инициализация состояний для всех профилей
        for url in Config.USER_URLS:
            profile_state = self.state_manager.get_profile_state(url)
            profile_state.profile_url = url
            profile_state.first_run = True
            self.state_manager.update_profile(url, profile_url=url, first_run=True)
        
        # Отправляем сообщение о старте
        await self._send_message(
            state.chat_id,
            f"✅ **Мониторинг {len(Config.USER_URLS)} профилей запущен!**\n\n"
            f"👀 Отслеживаю предметы...\n"
            f"⏱ Интервал: {Config.DROP_CHECK_INTERVAL}с\n"
            f"📊 Статус: 🟢 Активен"
        )
        
        while state.running and not self._shutdown:
            try:
                for url in Config.USER_URLS:
                    items = await self.get_profile_items(url)
                    
                    if not items:
                        logger.warning(f"⚠️ Не удалось получить предметы для {url}")
                        continue
                    
                    profile_state = self.state_manager.get_profile_state(url)
                    
                    # Если первый запуск для этого профиля - запоминаем все предметы
                    if profile_state.first_run:
                        profile_state.last_items_ids = set()
                        for item in items:
                            item_id = f"{item.name}_{item.price:.2f}"
                            profile_state.last_items_ids.add(item_id)
                        profile_state.total_items_count = len(items)
                        profile_state.first_run = False
                        self.state_manager.update_profile(
                            url,
                            last_items_ids=profile_state.last_items_ids,
                            total_items_count=profile_state.total_items_count,
                            first_run=False
                        )
                        logger.info(f"📦 Профиль {url}: запомнено {len(profile_state.last_items_ids)} предметов")
                        continue
                    
                    # Сравниваем с последним состоянием
                    current_ids = set()
                    for item in items:
                        item_id = f"{item.name}_{item.price:.2f}"
                        current_ids.add(item_id)
                    
                    # Проверяем каждый предмет
                    for item in items:
                        item_id = f"{item.name}_{item.price:.2f}"
                        
                        # Если предмета не было в прошлый раз - отправляем уведомление
                        if item_id not in profile_state.last_items_ids:
                            logger.info(f"🎉 Новый предмет в {url}: {item.name}")
                            msg = MessageFormatter.format_item(item)
                            await self._send_message(state.chat_id, msg)
                            await asyncio.sleep(0.3)
                    
                    # Обновляем состояние профиля
                    if current_ids != profile_state.last_items_ids:
                        profile_state.last_items_ids = current_ids
                        profile_state.total_items_count = len(items)
                        profile_state.last_item = items[0] if items else None
                        self.state_manager.update_profile(
                            url,
                            last_items_ids=profile_state.last_items_ids,
                            total_items_count=profile_state.total_items_count,
                            last_item=profile_state.last_item
                        )
                    
            except asyncio.CancelledError:
                logger.info("👤 LIVE Watcher профилей остановлен")
                break
            except Exception as e:
                logger.exception(f"❌ Ошибка в watcher профилей: {e}")
                await asyncio.sleep(Config.DROP_CHECK_INTERVAL * 2)
            
            await asyncio.sleep(Config.DROP_CHECK_INTERVAL)
        
        logger.info("👤 LIVE Watcher профилей завершён")

# =====================================================
# ОБРАБОТЧИКИ КОМАНД
# =====================================================

class Handlers:
    def __init__(self, bot: PromoBot):
        self.bot = bot
    
    async def _check_user(self, update: Update) -> bool:
        if not Config.ALLOWED_USERS:
            return True
        user_id = update.effective_user.id
        if user_id not in Config.ALLOWED_USERS:
            await update.message.reply_text("⛔ У вас нет доступа")
            logger.warning("⛔ Доступ запрещён для %d", user_id)
            return False
        return True
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user(update):
            return
        
        state = self.bot.state_manager.state
        
        if not await self.bot._check_auth():
            await update.message.reply_text(
                "❌ **Ошибка авторизации**\n\n"
                "Файл cookies не найден или недействителен.\n"
                f"Путь: `{Config.COOKIE_FILE}`",
                parse_mode="Markdown"
            )
            return
        
        if state.running:
            await update.message.reply_text("✅ Мониторинг уже запущен")
            return
        
        chat_id = update.effective_chat.id
        state.chat_id = chat_id
        state.running = True
        state.promo_message_id = None
        self.bot.state_manager.update(
            chat_id=chat_id,
            running=True,
            promo_message_id=None
        )
        
        if self.bot.promo_task and not self.bot.promo_task.done():
            self.bot.promo_task.cancel()
        
        if self.bot.profile_task and not self.bot.profile_task.done():
            self.bot.profile_task.cancel()
        
        self.bot.promo_task = asyncio.create_task(self.bot.promo_watcher())
        self.bot.profile_task = asyncio.create_task(self.bot.profile_watcher())
        
        await update.message.reply_text(
            "✅ **Мониторинг запущен**\n\n"
            "📡 **Отслеживается:**\n"
            f"• Промокоды на `{Config.URL}`\n"
            f"• {len(Config.USER_URLS)} профилей\n\n"
            f"⏱ Интервалы: `{Config.CHECK_INTERVAL}с / {Config.DROP_CHECK_INTERVAL}с`\n"
            f"📊 Статус: {'🟢 Активен' if state.running else '🔴 Остановлен'}",
            parse_mode="Markdown"
        )
        logger.info("✅ Мониторинг запущен пользователем %d", update.effective_user.id)
    
    async def stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user(update):
            return
        
        state = self.bot.state_manager.state
        
        if not state.running:
            await update.message.reply_text("ℹ️ Мониторинг уже остановлен")
            return
        
        state.running = False
        self.bot.state_manager.update(running=False)
        
        for task in [self.bot.promo_task, self.bot.profile_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        self.bot.promo_task = None
        self.bot.profile_task = None
        
        await update.message.reply_text(
            "🛑 **Мониторинг остановлен**\n\n"
            "📊 Статус: 🔴 Остановлен",
            parse_mode="Markdown"
        )
        logger.info("🛑 Мониторинг остановлен пользователем %d", update.effective_user.id)
    
    async def promo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user(update):
            return
        
        if not await self.bot._check_auth():
            await update.message.reply_text("❌ Ошибка авторизации", parse_mode="Markdown")
            return
        
        await update.message.reply_text("🔄 Получение промокода...")
        
        promo, timer = await self.bot.get_promo_and_timer()
        text = MessageFormatter.format_promo(promo, timer)
        
        await update.message.reply_text(text, parse_mode="Markdown")
    
    async def items(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user(update):
            return
        
        if not await self.bot._check_auth():
            await update.message.reply_text("❌ Ошибка авторизации")
            return
        
        # Показываем предметы из всех профилей
        for url in Config.USER_URLS:
            await update.message.reply_text(f"🔄 Получение предметов из {url}...")
            
            items = await self.bot.get_profile_items(url)
            
            if not items:
                await update.message.reply_text(f"❌ Не удалось получить предметы из {url}")
                continue
            
            user_name = url.split("/")[-1]
            lines = [f"📦 **Предметы в профиле {user_name}** (всего: {len(items)})", ""]
            for item in items[:10]:
                lines.append(item.format_message())
                lines.append("")
            if len(items) > 10:
                lines.append(f"... и ещё {len(items) - 10} предметов")
            
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    
    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user(update):
            return
        
        state = self.bot.state_manager.state
        
        lines = [
            "📊 **Статус мониторинга**",
            "",
            f"Состояние: {'🟢 Активен' if state.running else '🔴 Остановлен'}",
            f"Интервал промо: `{Config.CHECK_INTERVAL}с`",
            f"Интервал профилей: `{Config.DROP_CHECK_INTERVAL}с`",
            f"URL: `{Config.URL}`",
            f"Всего профилей: `{len(Config.USER_URLS)}`",
        ]
        
        # Показываем статус по каждому профилю
        for url in Config.USER_URLS:
            profile_state = state.profiles.get(url)
            if profile_state:
                user_name = url.split("/")[-1]
                lines.append(f"• {user_name}: {profile_state.total_items_count} предметов")
        
        if state.current_promo:
            lines.append("")
            lines.append("**Текущий промокод:**")
            lines.append(MessageFormatter.format_promo(state.current_promo, state.promo_timer))
        
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    
    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Сброс состояния"""
        if not await self._check_user(update):
            return
        
        state = self.bot.state_manager.state
        state.profiles = {}
        self.bot.state_manager.update(profiles={})
        
        await update.message.reply_text(
            "🔄 **Состояние сброшено!**\n\n"
            "При следующей проверке бот запомнит все текущие предметы\n"
            "и будет отслеживать новые.\n\n"
            "📌 Отправьте `/start` чтобы перезапустить мониторинг."
        )
    
    async def debug(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user(update):
            return
        
        if not await self.bot._check_auth():
            await update.message.reply_text("❌ Ошибка авторизации")
            return
        
        for url in Config.USER_URLS:
            await update.message.reply_text(f"🔄 Получение HTML для {url}...")
            
            html = await self.bot.http_client.get_html(url)
            if html is None:
                await update.message.reply_text(f"❌ Не удалось получить HTML для {url}")
                continue
            
            debug_file = Path(f"debug_profile_{url.split('/')[-1]}.html")
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(html)
            
            soup = BeautifulSoup(html, "html.parser")
            items_block = soup.find("div", class_="items")
            if not items_block:
                items_block_other = soup.find("div", class_="items-block-other")
                if items_block_other:
                    items_block = items_block_other.find("div", class_="items")
            
            if items_block:
                units = items_block.find_all("div", class_="unit")
                user_name = url.split("/")[-1]
                msg = (
                    f"📊 **Отладка профиля {user_name}**\n\n"
                    f"Блок .items найден ✅\n"
                    f"Всего предметов: {len(units)}\n"
                    f"HTML сохранён в: `{debug_file}`\n"
                )
                await update.message.reply_text(msg, parse_mode="Markdown")
            else:
                await update.message.reply_text(f"❌ Блок .items не найден для {url}!")
    
    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = (
            "🤖 **Помощь по боту**\n\n"
            "**Команды:**\n"
            "/start - Запустить мониторинг\n"
            "/stop - Остановить мониторинг\n"
            "/promo - Получить текущий промокод\n"
            "/items - Показать все предметы из профилей\n"
            "/status - Статус мониторинга\n"
            "/reset - Сбросить состояние\n"
            "/debug - Отладка парсинга\n"
            "/help - Эта справка\n\n"
            "**Отслеживание:**\n"
            "• Промокоды на главной странице\n"
            "• Предметы в нескольких профилях\n\n"
            "При появлении нового предмета в любом профиле\n"
            "бот отправит уведомление!"
        )
        await update.message.reply_text(help_text, parse_mode="Markdown")

# =====================================================
# ЗАПУСК
# =====================================================

async def main():
    try:
        promo_bot = PromoBot()
        
        await promo_bot.http_client.init_session()
        
        if await promo_bot._check_auth():
            logger.info("✅ Авторизация успешна")
        else:
            logger.warning("⚠️ Авторизация не настроена")
        
        application = (
            ApplicationBuilder()
            .token(Config.BOT_TOKEN)
            .build()
        )
        
        promo_bot.application = application
        
        handlers = Handlers(promo_bot)
        application.add_handler(CommandHandler("start", handlers.start))
        application.add_handler(CommandHandler("stop", handlers.stop))
        application.add_handler(CommandHandler("promo", handlers.promo))
        application.add_handler(CommandHandler("items", handlers.items))
        application.add_handler(CommandHandler("status", handlers.status))
        application.add_handler(CommandHandler("reset", handlers.reset))
        application.add_handler(CommandHandler("debug", handlers.debug))
        application.add_handler(CommandHandler("help", handlers.help))
        
        async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
            logger.error("❌ Ошибка: %s", context.error)
            if update and update.effective_chat:
                try:
                    await update.effective_chat.send_message(
                        "⚠️ Произошла ошибка. Попробуйте позже."
                    )
                except Exception:
                    pass
        
        application.add_error_handler(error_handler)
        
        logger.info("=" * 60)
        logger.info("🚀 БОТ ЗАПУЩЕН!")
        logger.info("=" * 60)
        logger.info(f"📡 URL промо: {Config.URL}")
        logger.info(f"📡 Профилей: {len(Config.USER_URLS)}")
        for url in Config.USER_URLS:
            logger.info(f"   • {url}")
        logger.info(f"⏱ Интервал промо: {Config.CHECK_INTERVAL}с")
        logger.info(f"⏱ Интервал профилей: {Config.DROP_CHECK_INTERVAL}с")
        logger.info(f"📁 Cookies: {Config.COOKIE_FILE}")
        logger.info(f"📁 Состояние: {Config.STATE_FILE}")
        logger.info(f"📁 Логи: logs/")
        logger.info("=" * 60)
        
        def signal_handler(signum, frame):
            logger.info("🛑 Получен сигнал остановки")
            promo_bot._shutdown = True
            
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        await application.initialize()
        await application.start()
        await application.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message"]
        )
        
        while not promo_bot._shutdown:
            await asyncio.sleep(1)
            
    except Exception as e:
        logger.exception("💥 Критическая ошибка: %s", e)
        raise
    finally:
        logger.info("🛑 Остановка бота...")
        
        for task in [promo_bot.promo_task, promo_bot.profile_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        if promo_bot.application:
            await promo_bot.application.updater.stop()
            await promo_bot.application.stop()
            await promo_bot.application.shutdown()
        
        await promo_bot.http_client.close()
        
        logger.info("✅ Бот остановлен")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен пользователем")
    except Exception as e:
        logger.exception("💥 Неожиданная ошибка: %s", e)
        sys.exit(1)
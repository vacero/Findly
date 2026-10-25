import asyncio
import random
import logging
import time
from datetime import datetime, timedelta
from functools import lru_cache
from telegram.ext import Application
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from database import SessionLocal, Search, SeenItem, get_setting
from i18n import t
from wallapop import search_items
from vinted import search_vinted
import urllib.parse
from html import escape

logger = logging.getLogger(__name__)

def format_price(price, currency):
    """Format a price like 12.5 -> '12.50 EUR', 500.0 -> '500 EUR'; '?' if missing."""
    try:
        amount = f"{float(price):.2f}".removesuffix(".00")
    except (TypeError, ValueError):
        return "?"
    return f"{amount} {currency or ''}".strip()

@lru_cache(maxsize=8)
def parse_active_hours(value):
    """Parse ACTIVE_HOURS 'HH:MM-HH:MM' into (start, end) minutes since midnight; None means always active."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        start_str, end_str = value.split("-")
        start_h, start_m = (int(p) for p in start_str.split(":"))
        end_h, end_m = (int(p) for p in end_str.split(":"))
        if not all(0 <= h < 24 for h in (start_h, end_h)) or not all(0 <= m < 60 for m in (start_m, end_m)):
            raise ValueError
    except ValueError:
        logger.warning(f"Ignoring invalid ACTIVE_HOURS '{value}', expected HH:MM-HH:MM (e.g. 08:00-00:30)")
        return None
    start, end = start_h * 60 + start_m, end_h * 60 + end_m
    return None if start == end else (start, end)

def seconds_until_active(hours, now=None):
    """Seconds until the active window opens, or 0 if checks are allowed now."""
    if hours is None:
        return 0
    now = now or datetime.now()
    start, end = hours
    minute = now.hour * 60 + now.minute
    if start < end:
        active = start <= minute < end
    else:
        # Window crosses midnight, e.g. 08:00-00:30
        active = minute >= start or minute < end
    if active:
        return 0
    return ((start - minute) % (24 * 60)) * 60 - now.second

async def telegram_notifier_loop(application: Application, queue: asyncio.Queue):
    logger.info("Telegram notifier worker started.")
    while True:
        try:
            message_data = await queue.get()
            msg_type = message_data.get('type')
            
            from database import get_setting
            allowed_str = get_setting("allowed_chat_ids", "")
            try:
                chat_ids = [int(cid.strip("'\" ")) for cid in allowed_str.split(',') if cid.strip()]
            except ValueError:
                chat_ids = []
                
            for chat_id in chat_ids:
                try:
                    if msg_type == 'photo':
                        try:
                            await application.bot.send_photo(
                                chat_id=chat_id,
                                photo=message_data.get('photo'),
                                caption=message_data.get('caption'),
                                parse_mode=message_data.get('parse_mode', 'HTML'),
                                reply_markup=message_data.get('reply_markup')
                            )
                        except Exception as pe:
                            logger.warning(f"Failed to send photo to {chat_id}, falling back to text: {pe}")
                            await application.bot.send_message(
                                chat_id=chat_id,
                                text=message_data.get('caption') or message_data.get('text', ''),
                                parse_mode=message_data.get('parse_mode', 'HTML'),
                                reply_markup=message_data.get('reply_markup')
                            )
                    elif msg_type == 'message':
                        await application.bot.send_message(
                            chat_id=chat_id,
                            text=message_data.get('text'),
                            parse_mode=message_data.get('parse_mode', 'HTML'),
                            reply_markup=message_data.get('reply_markup')
                        )
                except Exception as e:
                    logger.error(f"Failed to send message to {chat_id}: {e}")
                
            await asyncio.sleep(0.5)
            queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Unexpected error in telegram_notifier_loop: {e}")
            await asyncio.sleep(1)

async def check_single_search(search_id: int, queue: asyncio.Queue, platform_override=None, since_ts=None):
    """
    Check for new items for a single search.
    If platform_override is provided, only check that platform.
    If since_ts (epoch seconds) is provided, catch up on everything listed since then.
    """
    db = SessionLocal()
    try:
        search = db.query(Search).filter(Search.id == search_id).first()
        if not search:
            return

        # First check of a new search: record existing listings without notifying
        is_first_check = search.last_checked_at is None
            
        logger.info(f"Checking updates for search {search.id}")
        items = []
        
        if is_first_check:
            # Check all platforms so the other platform loop doesn't notify on its first run
            target_platform = search.platform or "both"
        else:
            target_platform = platform_override or search.platform or "both"

        # A first check only records listings, so it never needs to look back further
        since = None if is_first_check else since_ts

        if target_platform in ["wallapop", "both"]:
            w_items = await asyncio.to_thread(search_items, search.keywords, search.min_price, search.max_price, search.distance_in_km, search.condition, since)
            items.extend(w_items)

        if target_platform in ["vinted", "both"]:
            v_items = await asyncio.to_thread(search_vinted, search.keywords, search.min_price, search.max_price, search.condition, since)
            items.extend(v_items)
        
        new_items_count = 0
        max_items_to_notify = 10
        notifications = []
        batch_ids = set()
        
        for item in items:
            item_id = item.get("id")
            if not item_id or str(item_id) in batch_ids:
                continue
            batch_ids.add(str(item_id))
            exists = db.query(SeenItem).filter(SeenItem.wallapop_id == str(item_id), SeenItem.search_id == search.id).first()
            if exists:
                continue
                
            new_items_count += 1
            seen = SeenItem(
                wallapop_id=str(item_id), 
                search_id=search.id,
                title=item.get('title'),
                price=item.get('price'),
                url=item.get('url')
            )
            db.add(seen)
            if is_first_check:
                continue

            if new_items_count <= max_items_to_notify:
                plat_name = item.get('platform', 'Wallapop')
                msg = (
                    f"🎁 <b>Nuevo artículo encontrado en {plat_name}!</b>\n\n"
                    f"<b>{escape(item['title'], quote=False)}</b>\n"
                    f"💶 Precio: {format_price(item.get('price'), item.get('currency'))}\n"
                    f"<a href=\"{escape(item['url'] or '', quote=False)}\">Ver en {plat_name}</a>"
                )
                
                keyboard = [[InlineKeyboardButton("❌ Dejar de seguir", callback_data=f"delete_{search.id}")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                if item.get('image'):
                    notifications.append({
                        'type': 'photo',
                        'chat_id': search.chat_id,
                        'photo': item['image'],
                        'caption': msg,
                        'parse_mode': 'HTML',
                        'reply_markup': reply_markup
                    })
                else:
                    notifications.append({
                        'type': 'message',
                        'chat_id': search.chat_id,
                        'text': msg,
                        'parse_mode': 'HTML',
                        'reply_markup': reply_markup
                    })
        

        if is_first_check:
            logger.info(f"First check for search {search.id}: recorded {new_items_count} existing items without notifying")

        if new_items_count > max_items_to_notify and not is_first_check:
            region = get_setting("region", "es").lower()
            wallapop_region = region if region in ["es", "it", "pt"] else "es"
            vinted_domain = "co.uk" if region == "uk" else region
            
            base_web_url = f"https://{wallapop_region}.wallapop.com/search"
            params = {
                "keywords": search.keywords,
                "order_by": "newest",
                "time_filter": "today"
            }
            if search.min_price: params["min_sale_price"] = search.min_price
            if search.max_price: params["max_sale_price"] = search.max_price
            
            if search.condition:
                w_map = {"new": "new", "mint": "as_good_as_new", "good": "good", "fair": "fair", "poor": "has_given_it_all"}
                if search.condition in w_map: params["condition"] = w_map[search.condition]
                
            web_url = escape(f"{base_web_url}?{urllib.parse.urlencode(params)}", quote=False)
            
            v_params = {"search_text": search.keywords}
            if search.condition:
                v_map = {"new": "6,1", "mint": "2", "good": "3", "fair": "4"}
                if search.condition in v_map: v_params["status_ids"] = v_map[search.condition]
            vinted_url = escape(f"https://www.vinted.{vinted_domain}/catalog?{urllib.parse.urlencode(v_params)}", quote=False)
            
            summary_msg = t("new_items_found", region, count=new_items_count)
            summary_msg += t("showing_first", region, count=max_items_to_notify)
            
            if target_platform == "wallapop":
                 summary_msg += f"<a href='{web_url}'>{t('view_wallapop_all', region)}</a>"
            elif target_platform == "vinted":
                 summary_msg += f"<a href='{vinted_url}'>{t('view_vinted_all', region)}</a>"
            else:
                 summary_msg += f"<a href='{web_url}'>{t('view_wallapop', region)}</a> | <a href='{vinted_url}'>{t('view_vinted', region)}</a>"

                 
            keyboard = [[InlineKeyboardButton("❌ Dejar de seguir", callback_data=f"delete_{search.id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
                 
            notifications.append({
                'type': 'message',
                'chat_id': search.chat_id,
                'text': summary_msg,
                'parse_mode': 'HTML',
                'reply_markup': reply_markup
            })
            
        search.last_checked_at = datetime.utcnow()
        db.commit()

        # Only notify once the items are saved, so a failed commit can't cause repeats
        for notification in notifications:
            await queue.put(notification)
    except Exception as e:
        logger.error(f"Error processing search {search_id}: {e}")
    finally:
        db.close()


async def platform_scheduler_loop(queue: asyncio.Queue, platform: str):
    """
    Runs check for a specific platform based on its interval.
    """
    logger.info(f"Scheduler started for {platform}.")
    last_cycle_ts = None
    catch_up_since = None
    while True:
        # Outside ACTIVE_HOURS: pause, then catch up on what was listed in the meantime
        pause = seconds_until_active(parse_active_hours(get_setting("active_hours", "")))
        if pause > 0:
            if catch_up_since is None:
                catch_up_since = (last_cycle_ts or time.time()) - 60
            resume = datetime.now() + timedelta(seconds=pause)
            logger.info(f"Outside active hours, pausing {platform} checks until {resume:%H:%M}")
            await asyncio.sleep(pause + random.uniform(0, 300))  # Don't resume at exactly the same time every day
            continue

        try:
            interval_str = get_setting(f"{platform}_interval", "5")
            interval_mins = int(interval_str)
        except ValueError:
            interval_mins = 5
            
        search_ids = []
        db = SessionLocal()
        try:
            # Get searches that include this platform or 'both'
            searches = db.query(Search.id).filter(Search.platform.in_([platform, "both"])).all()
            search_ids = [s[0] for s in searches]
        except Exception as e:
            logger.error(f"Unexpected error in {platform} scheduler querying searches: {e}")
        finally:
            db.close()

        if catch_up_since is not None:
            logger.info(f"Catching up on {platform} listings since {datetime.fromtimestamp(catch_up_since):%H:%M}")
        last_cycle_ts = time.time()
        # Vary the order and spacing so requests don't follow a fixed pattern
        random.shuffle(search_ids)
        for s_id in search_ids:
            await check_single_search(s_id, queue, platform_override=platform, since_ts=catch_up_since)
            await asyncio.sleep(random.uniform(10, 30))  # Wait 10-30s between searches to avoid flooding
        catch_up_since = None

        # Add random jitter of +/-20% of the interval
        wait_time = interval_mins * 60 * random.uniform(0.8, 1.2)
        logger.info(f"Next check for {platform} in {wait_time/60:.1f} minutes")
        await asyncio.sleep(max(wait_time, 60))  # Ensure at least 60s

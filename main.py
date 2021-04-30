import json
import logging
import re
from enum import Enum
from typing import List

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, CallbackQueryHandler
from jinja2 import Template

from secrets import TELEGRAM_BOT_TOKEN
from cowinapi import CoWinAPI

PINCODE_PREFIX_REGEX = r'^\s*(pincode)?\s*(?P<pincode_mg>\d{6})\s*'
AGE_BUTTON_REGEX = r'^age: (?P<age_mg>\d+)'
CMD_BUTTON_REGEX = r'^cmd: (?P<cmd_mg>.+)'

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)

logger = logging.getLogger(__name__)

users = {}

CoWinAPIObj = CoWinAPI()


class AgeRangePref(Enum):
    Min0 = 0
    Min18 = 1
    Min45 = 2


def get_main_buttons() -> List[InlineKeyboardButton]:
    return [
        InlineKeyboardButton("Setup Alert", callback_data='cmd: setup_alert'),
        InlineKeyboardButton("Check Available Slots", callback_data='cmd: check_slots'),
    ]


def get_age_kb() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("18-45", callback_data='age: 18'),
            InlineKeyboardButton("45+", callback_data='age: 45'),
            InlineKeyboardButton("Both", callback_data='age: 0'),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            *get_main_buttons()
        ],
        [
            InlineKeyboardButton("Help", callback_data='cmd: help'),
            InlineKeyboardButton("About", callback_data='cmd: about')
        ],
    ])


def start(update: Update, _: CallbackContext) -> None:
    update.message.reply_text(
        f"Hey {update.effective_user.username}! Welcome to Cowin Assist Bot\n\nThis bot will help you"
        " check available slots and also, set an alert when a slot becomes available",
        reply_markup=get_main_keyboard(),
    )


def cmd_button_handler(update: Update, ctx: CallbackContext) -> None:
    query = update.callback_query
    query.answer()

    if cmd := ctx.match.groupdict().get("cmd_mg"):
        cmd = cmd.strip()
        if cmd == "setup_alert":
            setup_alert_command(update, ctx)
            return
        elif cmd == "check_slots":
            check_slots_command(update, ctx)
            return
        else:
            update.effective_chat.send_message("cmd not implemented yet")
            return


def setup_alert_command(update: Update, _: CallbackContext) -> None:
    global users
    user_data: dict = users.get(update.effective_user.id, {})
    if "age_pref" not in user_data:
        update.effective_chat.send_message("select age pref", reply_markup=get_age_kb())
        return
    if "pincode" not in user_data:
        update.effective_chat.send_message("pincode not setup")
        return


def check_slots_command(update: Update, _: CallbackContext) -> None:
    global users
    user_data: dict = users.get(update.effective_user.id, {})
    if "age_pref" not in user_data:
        update.effective_chat.send_message("select age pref", reply_markup=get_age_kb())
        return
    if "pincode" not in user_data:
        update.effective_chat.send_message("pincode not setup")
        return

    pincode = user_data["pincode"]
    vaccination_centers = CoWinAPIObj.calender_by_pin(pincode, CoWinAPI.today())
    vaccination_centers = [vc for vc in vaccination_centers if vc.has_available_sessions()]
    if not vaccination_centers:
        update.effective_chat.send_message("sorry, not slots available")
        return
    # TODO: Fix this shit
    template = """Following slots are available:
{% for c in centers %}
*{{ c }}*:{% for s in c.get_available_sessions() %}
    • {{s.date}}: {{s.capacity}}{% endfor %}
{% endfor %}"""
    tm = Template(template)
    msg = tm.render(centers=vaccination_centers)
    update.effective_chat.send_message(msg, parse_mode='markdown')
    return


def help_command(update: Update, _: CallbackContext) -> None:
    update.message.reply_text('Help!')


def echo(update: Update, _: CallbackContext) -> None:
    update.message.reply_text("okay bye")


def set_age_preference(update: Update, ctx: CallbackContext) -> None:
    query = update.callback_query
    query.answer()

    if age_pref := ctx.match.groupdict().get("age_mg"):
        user_data: dict = users.get(update.effective_user.id, {})
        user_data["age_pref"] = age_pref
        users[update.effective_user.id] = user_data
        update.effective_chat.send_message("Age preference has been set. Please enter your pincode to proceed next")


def set_pincode(update: Update, ctx: CallbackContext) -> None:
    global users
    pincode = ctx.match.groupdict().get("pincode_mg")
    if not pincode:
        return
    user_data: dict = users.get(update.effective_user.id, {})
    user_data['pincode'] = pincode
    users[update.effective_user.id] = user_data
    update.effective_chat.send_message(F"Pincode is set to {pincode}",
                                       reply_markup=InlineKeyboardMarkup([[*get_main_buttons()]]))


def main() -> None:
    updater = Updater(TELEGRAM_BOT_TOKEN)

    # Add handlers
    updater.dispatcher.add_handler(CommandHandler("start", start))
    updater.dispatcher.add_handler(CommandHandler("help", help_command))
    updater.dispatcher.add_handler(CommandHandler("alert", setup_alert_command))
    updater.dispatcher.add_handler(CallbackQueryHandler(set_age_preference, pattern=AGE_BUTTON_REGEX))
    updater.dispatcher.add_handler(CallbackQueryHandler(cmd_button_handler, pattern=CMD_BUTTON_REGEX))
    updater.dispatcher.add_handler(MessageHandler(Filters.regex(
        re.compile(PINCODE_PREFIX_REGEX, re.IGNORECASE)), set_pincode))
    updater.dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, echo))

    # Start the Bot
    updater.start_polling()

    updater.idle()


if __name__ == '__main__':
    main()

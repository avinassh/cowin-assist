import json
import logging
import re
from enum import Enum
from typing import List
from datetime import datetime

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, CallbackQueryHandler
from jinja2 import Template
from peewee import SqliteDatabase, Model, DateTimeField, CharField, FixedCharField, IntegerField, BooleanField

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

CoWinAPIObj = CoWinAPI()

db = SqliteDatabase('users.db')


# storage classes
class User(Model):
    created_at = DateTimeField(default=datetime.now)
    updated_at = DateTimeField(default=datetime.now)
    telegram_id = CharField(max_length=220, unique=True)
    chat_id = CharField(max_length=220)
    pincode = FixedCharField(max_length=6, null=True)
    age_pref = IntegerField(null=True)
    enabled = BooleanField(default=True)

    class Meta:
        database = db


class AgeRangePref(Enum):
    Min0 = 1
    Min18 = 18
    Min45 = 45


def get_enum_by_string(age_pref: str) -> AgeRangePref:
    if age_pref == "18":
        return AgeRangePref.Min18
    elif age_pref == "45":
        return AgeRangePref.Min45
    else:
        return AgeRangePref.Min0


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
            InlineKeyboardButton("Both", callback_data='age: 1'),
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


def check_if_preferences_are_set(update: Update, _: CallbackContext) -> User:
    user: User
    user, _ = User.get_or_create(telegram_id=update.effective_user.id,
                                 defaults={'chat_id': update.effective_chat.id})
    if user.age_pref is None:
        update.effective_chat.send_message("select age pref", reply_markup=get_age_kb())
        return
    if user.pincode is None:
        update.effective_chat.send_message("pincode not setup")
        return
    return user


def setup_alert_command(update: Update, ctx: CallbackContext) -> None:
    user = check_if_preferences_are_set(update, ctx)
    if not user:
        return


def check_slots_command(update: Update, ctx: CallbackContext) -> None:
    user = check_if_preferences_are_set(update, ctx)
    if not user:
        return

    pincode = user.pincode
    vaccination_centers = CoWinAPIObj.calender_by_pin(pincode, CoWinAPI.today())
    if vaccination_centers:
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

    age_pref = ctx.match.groupdict().get("age_mg")
    if age_pref is None:
        return

    user: User
    user, _ = User.get_or_create(telegram_id=update.effective_user.id,
                                 defaults={'chat_id': update.effective_chat.id})
    user.age_pref = int(age_pref)
    user.updated_at = datetime.now()
    user.save()

    if user.pincode:
        update.effective_chat.send_message("Age preference has been set",
                                           reply_markup=InlineKeyboardMarkup([[*get_main_buttons()]]))
    else:
        update.effective_chat.send_message("Age preference has been set. Please enter your pincode to proceed next")


def set_pincode(update: Update, ctx: CallbackContext) -> None:
    pincode = ctx.match.groupdict().get("pincode_mg")
    if not pincode:
        return
    user: User
    user, _ = User.get_or_create(telegram_id=update.effective_user.id,
                                 defaults={'chat_id': update.effective_chat.id})
    user.pincode = pincode
    user.updated_at = datetime.now()
    user.save()

    if user.age_pref:
        update.effective_chat.send_message(F"Pincode is set to {pincode}",
                                           reply_markup=InlineKeyboardMarkup([[*get_main_buttons()]]))
    else:
        update.effective_chat.send_message("Pincode is set. Please set the age preference to proceed next",
                                           reply_markup=get_age_kb())


def main() -> None:
    # connect and create tables
    db.connect()
    db.create_tables([User, ])

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

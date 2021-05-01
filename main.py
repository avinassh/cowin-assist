import logging
import re
import time
from copy import deepcopy
from enum import Enum
from typing import List
from datetime import datetime
import threading
import traceback
import html
import json

import peewee
import telegram.error
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, Bot, MAX_MESSAGE_LENGTH, BotCommand, ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, CallbackQueryHandler
from jinja2 import Template
from peewee import SqliteDatabase, Model, DateTimeField, CharField, FixedCharField, IntegerField, BooleanField

from cowinapi import CoWinAPI, VaccinationCenter
from secrets import TELEGRAM_BOT_TOKEN, DEVELOPER_CHAT_ID

PINCODE_PREFIX_REGEX = r'^\s*(pincode)?\s*(?P<pincode_mg>\d{6})\s*'
AGE_BUTTON_REGEX = r'^age: (?P<age_mg>\d+)'
CMD_BUTTON_REGEX = r'^cmd: (?P<cmd_mg>.+)'
DISABLE_TEXT_REGEX = r'\s*disable|stop|pause\s*'

# all the really complex configs:
# following says, how often we should poll CoWin APIs for age group 18+. In seconds
MIN_18_WORKER_INTERVAL = 30
# following says, how often we should poll CoWin APIs for age group 45+. In seconds
MIN_45_WORKER_INTERVAL = 60 * 15  # 15 minutes
# following decides, should we send a notification to user about 45+ or not
# if we have sent an alert in the last 30 minutes, we will not bother them
MIN_45_NOTIFICATION_DELAY = 60 * 30

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)

logger = logging.getLogger(__name__)

CoWinAPIObj = CoWinAPI()

db = SqliteDatabase('users.db', pragmas={
    'journal_mode': 'wal',
    'cache_size': -1 * 64000,  # 64MB
    'foreign_keys': 1,
    'ignore_check_constraints': 0
})


class AgeRangePref(Enum):
    Unknown = 0
    MinAge18 = 1
    MinAge45 = 2
    MinAgeAny = 3

    def __str__(self) -> str:
        if self == AgeRangePref.MinAge18:
            return "18+"
        elif self == AgeRangePref.MinAge45:
            return "45+"
        else:
            return "Both"


class EnumField(IntegerField):

    def __init__(self, choices, *args, **kwargs):
        super(IntegerField, self).__init__(*args, **kwargs)
        self.choices = choices

    def db_value(self, value):
        return value.value

    def python_value(self, value):
        return self.choices(value)


# storage classes
class User(Model):
    created_at = DateTimeField(default=datetime.now)
    updated_at = DateTimeField(default=datetime.now)
    deleted_at = DateTimeField(null=True)
    last_alert_sent_at: datetime = DateTimeField(default=datetime.now)
    total_alerts_sent = IntegerField(default=0)
    telegram_id = CharField(max_length=220, unique=True)
    chat_id = CharField(max_length=220)
    pincode: str = FixedCharField(max_length=6, null=True, index=True)
    age_limit: AgeRangePref = EnumField(choices=AgeRangePref, default=AgeRangePref.Unknown)
    enabled = BooleanField(default=False, index=True)

    class Meta:
        database = db


def sanitise_msg(msg: str) -> str:
    if len(msg) < MAX_MESSAGE_LENGTH:
        return msg

    help_text = "\n\n (message truncated due to size)"
    msg_length = MAX_MESSAGE_LENGTH - len(help_text)
    return msg[:msg_length] + help_text


def get_main_buttons() -> List[InlineKeyboardButton]:
    return [
        InlineKeyboardButton("🔔 Setup Alert", callback_data='cmd: setup_alert'),
        InlineKeyboardButton("💼 Check Open Slots", callback_data='cmd: check_slots'),
    ]


def get_age_kb() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("18+", callback_data='age: 1'),
            InlineKeyboardButton("45+", callback_data='age: 2'),
            InlineKeyboardButton("Both", callback_data='age: 3'),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            *get_main_buttons()
        ],
        [
            InlineKeyboardButton("💡 Help", callback_data='cmd: help'),
            InlineKeyboardButton("🔒 Privacy Policy", callback_data='cmd: privacy')
        ],
    ])


def start(update: Update, _: CallbackContext) -> None:
    update.message.reply_text(
        f"Hey {update.effective_user.mention_markdown_v2()}! Welcome to Cowin Assist Bot\n\n" + get_help_text_short(),
        reply_markup=get_main_keyboard(), parse_mode="markdown"
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
        elif cmd == "privacy":
            privacy_policy_handler(update, ctx)
            return
        elif cmd == "help":
            help_handler(update, ctx)
            return
        else:
            update.effective_chat.send_message("cmd not implemented yet")
            return


def get_help_text_short() -> str:
    return """This bot will help you to check current available slots in one week and also, send an alert as soon as a slot becomes available. To start, either click on "Setup Alert" or "Check Open Slots". For first time users, bot will ask for age preference and pincode."""  ## noqa


def get_help_text() -> str:
    return """\n\n*Setup Alert*\nUse this to setup an alert, it will send a message as soon as a slot becomes available. Select the age preference and provide the area pincode of the vaccination center you would like to monitor. Do note that 18+ slots are monitored more often than 45+. Click on /pause to stop alerts and /resume to enable them back.\n\n*Check Open Slots*\nUse this to check the slots availability manually.\n\n*Age Preference*\nTo change age preference, click on /age\n\n*Pincode*\nClick on /pincode to change the pincode. Alternatively, you can send pincode any time and bot will update it."""  ## noqa


def help_handler(update: Update, ctx: CallbackContext):
    header = "💡 Help\n\n"
    msg = header + get_help_text_short() + get_help_text()
    update.effective_chat.send_message(msg, parse_mode="markdown",
                                       reply_markup=InlineKeyboardMarkup([[*get_main_buttons()]]))


def delete_cmd_handler(update: Update, _: CallbackContext):
    user: User
    try:
        user = User.get(User.telegram_id == update.effective_user.id, User.deleted_at.is_null(True))
    except peewee.DoesNotExist:
        update.effective_chat.send_message("No data exists to delete.")
        return

    user.deleted_at = datetime.now()
    user.enabled = False
    user.pincode = None
    user.age_limit = AgeRangePref.Unknown
    user.save()
    update.effective_chat.send_message("Your data has been successfully deleted. Click on /start to restart the bot.")


def help_command(update: Update, ctx: CallbackContext) -> None:
    help_handler(update, ctx)


def privacy_policy_handler(update: Update, _: CallbackContext):
    header = "🔒 Privacy Policy\n\n"
    msg = F"CoWin Assist Bot stores minimal and only the information which is necessary. This includes:\n" \
          "  • Telegram account user id ({id})\n" \
          "  • The pincode to search in CoWin site\n" \
          "  • Age preference\n" \
          "\nThe bot *does not have access* to your real name or phone number." \
          "\n\nClick on /delete to delete all your data."
    msg = header + msg.format(id=update.effective_user.id)
    update.effective_chat.send_message(msg, parse_mode="markdown")


def age_command(update: Update, ctx: CallbackContext):
    update.effective_chat.send_message("Select age preference", reply_markup=get_age_kb())
    return


def pincode_command(update: Update, ctx: CallbackContext):
    update.effective_chat.send_message("Enter your pincode")


def check_if_preferences_are_set(update: Update, ctx: CallbackContext) -> User:
    user: User
    user, _ = get_or_create_user(telegram_id=update.effective_user.id, chat_id=update.effective_chat.id)
    if user.age_limit is None or user.age_limit == AgeRangePref.Unknown:
        age_command(update, ctx)
        return
    if user.pincode is None:
        pincode_command(update, ctx)
        return
    return user


def setup_alert_command(update: Update, ctx: CallbackContext) -> None:
    user = check_if_preferences_are_set(update, ctx)
    if not user:
        return
    user.enabled = True
    user.save()

    msg = "I have setup alerts for you. "
    msg_18 = "For age group 18+, as soon as a slot becomes available I will send a message. "
    msg_45 = "For age group 45+, I will check slots availability for every 15 minutes and send a message if they are " \
             "available. "
    if user.age_limit == AgeRangePref.MinAge18:
        msg = msg + msg_18
    elif user.age_limit == AgeRangePref.MinAge45:
        msg = msg + msg_45
    else:
        msg = msg + msg_18 + msg_45
    update.effective_chat.send_message(msg + "\n\nClick on /pause to pause the alerts.")


def disable_alert_command(update: Update, _: CallbackContext) -> None:
    user: User
    user, _ = get_or_create_user(telegram_id=update.effective_user.id, chat_id=update.effective_chat.id)
    user.enabled = False
    user.save()
    update.effective_chat.send_message("Alerts are disabled. Click on /resume to resume the alerts")


def get_available_centers_by_pin(pincode: str) -> List[VaccinationCenter]:
    vaccination_centers = CoWinAPIObj.calender_by_pin(pincode, CoWinAPI.today())
    if vaccination_centers:
        vaccination_centers = [vc for vc in vaccination_centers if vc.has_available_sessions()]
    return vaccination_centers


# age_limit param is only used for display purposes. If the user's selected preference is both
# then we should show the age limit of the vaccination center
def get_formatted_message(centers: List[VaccinationCenter], age_limit: AgeRangePref) -> str:
    header = ""
    if len(centers) > 10:
        header = F"Showing 10 centers out of {len(centers)}. Check [CoWin Site](https://www.cowin.gov.in/home) for full list\n"  ## noqa

    display_age = True if age_limit == AgeRangePref.MinAgeAny else False

    # TODO: Fix this shit
    template = """
{%- for c in centers[:10] %}
*{{ c.name }}* {%- if c.fee_type == 'Paid' %}*(Paid)*{%- endif %}:{% for s in c.get_available_sessions() %}
    • {{s.date}}: {{s.capacity}}{%- if display_age %} *({{s.min_age_limit}}+)*{%- endif %}{% endfor %}
{% endfor %}"""
    tm = Template(template)
    return header + tm.render(centers=centers, display_age=display_age)


# filtering based on the age preferences set by the user
# this method always returns a copy of the centers list
def filter_centers_by_age_limit(age_limit: AgeRangePref, centers: List[VaccinationCenter]) -> List[VaccinationCenter]:
    # if user hasn't set any age preferences, then just show everything
    if age_limit in [None, AgeRangePref.MinAgeAny, AgeRangePref.Unknown]:
        return centers

    if not centers:
        return centers

    filter_age: int
    if age_limit == AgeRangePref.MinAge18:
        filter_age = 18
    else:
        filter_age = 45

    # TODO: FIX THIS! This makes a deep copy of Vaccination Center objects
    centers_copy: List[VaccinationCenter] = deepcopy(centers)
    for vc in centers_copy:
        vc.sessions = vc.get_available_sessions_by_age_limit(filter_age)

    results: List[VaccinationCenter] = [vc for vc in centers_copy if vc.has_available_sessions()]
    return results


def get_message_header(user: User) -> str:
    return F"Following slots are available (pincode: {user.pincode}, age preference: {user.age_limit})\n"


def check_slots_command(update: Update, ctx: CallbackContext) -> None:
    user = check_if_preferences_are_set(update, ctx)
    if not user:
        return

    vaccination_centers = get_available_centers_by_pin(user.pincode)
    vaccination_centers = filter_centers_by_age_limit(user.age_limit, vaccination_centers)
    if not vaccination_centers:
        update.effective_chat.send_message(
            F"Sorry, no free slots available (pincode: {user.pincode}, age preference: {user.age_limit})")
        return

    msg: str = get_formatted_message(centers=vaccination_centers, age_limit=user.age_limit)
    msg = get_message_header(user=user) + msg
    update.effective_chat.send_message(sanitise_msg(msg), parse_mode='markdown')
    return


def default(update: Update, _: CallbackContext) -> None:
    update.message.reply_text("Sorry, I did not understand. Click on /help to know valid commands")


def get_or_create_user(telegram_id: str, chat_id) -> (User, bool):
    return User.get_or_create(telegram_id=telegram_id,
                              defaults={'chat_id': chat_id})


def set_age_preference(update: Update, ctx: CallbackContext) -> None:
    query = update.callback_query
    query.answer()

    age_pref = ctx.match.groupdict().get("age_mg")
    if age_pref is None:
        return

    user: User
    user, _ = get_or_create_user(telegram_id=update.effective_user.id, chat_id=update.effective_chat.id)
    user.age_limit = AgeRangePref(int(age_pref))
    user.updated_at = datetime.now()
    user.deleted_at = None
    user.save()

    if user.pincode:
        update.effective_chat.send_message(F"Age preference has been set to {user.age_limit}",
                                           reply_markup=InlineKeyboardMarkup([[*get_main_buttons()]]))
    else:
        update.effective_chat.send_message(
            F"Age preference has been set to {user.age_limit}. Please enter your pincode to proceed")


def set_pincode(update: Update, ctx: CallbackContext) -> None:
    pincode = ctx.match.groupdict().get("pincode_mg")
    if not pincode:
        return
    # validating pincode is the third difficult problem of computer science
    if pincode in ["000000", "111111", "123456"]:
        update.effective_chat.send_message("Uh oh! That doesn't look like a valid pincode."
                                           "Please enter a valid pincode to proceed")
        return
    user: User
    user, _ = get_or_create_user(telegram_id=update.effective_user.id, chat_id=update.effective_chat.id)
    user.pincode = pincode
    user.updated_at = datetime.now()
    user.deleted_at = None
    user.save()

    msg: str = F"Pincode is set to {pincode}. If you'd like to change it, send a valid pincode any time to the bot."
    reply_markup: InlineKeyboardMarkup
    if user.age_limit is None or user.age_limit == AgeRangePref.Unknown:
        reply_markup = get_age_kb()
        msg = msg + "\n\nSelect age preference:"
    else:
        reply_markup = InlineKeyboardMarkup([[*get_main_buttons()]])
    update.effective_chat.send_message(msg, reply_markup=reply_markup)


def send_alert_to_user(bot: telegram.Bot, user: User, centers: List[VaccinationCenter]) -> None:
    if not centers:
        return
    msg: str = get_formatted_message(centers=centers, age_limit=user.age_limit)
    msg = "*[ALERT!]* " + get_message_header(user=user) + msg + \
          "\n Click on /pause to disable the notifications"
    try:
        bot.send_message(chat_id=user.chat_id, text=sanitise_msg(msg), parse_mode='markdown')
    except telegram.error.Unauthorized:
        # looks like this user blocked us. simply disable them
        user.enabled = False
        user.save()
    else:
        user.last_alert_sent_at = datetime.now()
        user.total_alerts_sent += 1
        user.save()


def periodic_background_worker(_: telegram.ext.CallbackContext):
    background_worker(age_limit=AgeRangePref.MinAge45)


def frequent_background_worker():
    while True:
        try:
            background_worker(age_limit=AgeRangePref.MinAge18)
            time.sleep(MIN_18_WORKER_INTERVAL)  # sleep for 30 seconds
        except Exception as e:
            logger.exception("frequent_background_worker", exc_info=e)
            time.sleep(MIN_18_WORKER_INTERVAL)


def background_worker(age_limit: AgeRangePref):
    # query db
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    time_now = datetime.now()
    # find all distinct pincodes where pincode is not null and at least one user exists with alerts enabled
    query = User.select(User.pincode).where(
        (User.pincode.is_null(False)) & (User.enabled == True) & (
                (User.age_limit == AgeRangePref.MinAgeAny) | (User.age_limit == age_limit))).distinct()
    for distinct_user in query:
        # get all the available vaccination centers with open slots
        vaccination_centers = get_available_centers_by_pin(distinct_user.pincode)
        if not vaccination_centers:
            continue
        # find all users for this pincode and alerts enabled
        user_query = User.select().where(
            (User.pincode == distinct_user.pincode) & (User.enabled == True) & (
                    (User.age_limit == AgeRangePref.MinAgeAny) | (User.age_limit == age_limit)
            ))
        for user in user_query:
            delta = time_now - user.last_alert_sent_at
            # if user age limit is 45, then we shouldn't ping them too often
            if user.age_limit == AgeRangePref.MinAge45:
                if delta.seconds < MIN_45_NOTIFICATION_DELAY:
                    continue
                filtered_centers = filter_centers_by_age_limit(user.age_limit, vaccination_centers)
                if not filtered_centers:
                    continue
                send_alert_to_user(bot, user, filtered_centers)

            # for users with age limit of 18, we send the alert
            if user.age_limit == AgeRangePref.MinAge18:
                filtered_centers = filter_centers_by_age_limit(user.age_limit, vaccination_centers)
                if not filtered_centers:
                    continue
                filtered_centers = filter_centers_by_age_limit(user.age_limit, vaccination_centers)
                if not filtered_centers:
                    continue
                send_alert_to_user(bot, user, filtered_centers)

            # here comes the tricky part. for users who have set up both
            # we would want to send 18+ alerts more often than 45+
            if user.age_limit == AgeRangePref.MinAgeAny:
                filtered_centers: List[VaccinationCenter]
                if delta.seconds < MIN_45_NOTIFICATION_DELAY:
                    # include only 18+ results
                    filtered_centers = filter_centers_by_age_limit(AgeRangePref.MinAge18, vaccination_centers)
                else:
                    # include both results
                    filtered_centers = filter_centers_by_age_limit(user.age_limit, vaccination_centers)
                if not filtered_centers:
                    continue
                send_alert_to_user(bot, user, filtered_centers)


# works in the background to remove all the deleted user rows permanently
def clean_up() -> None:
    # delete all the users permanently whose deleted_at value is not null
    User.delete().where(User.deleted_at.is_null(False))


# source: https://github.com/python-telegram-bot/python-telegram-bot/blob/master/examples/errorhandlerbot.py
def error_handler(update: object, context: CallbackContext) -> None:
    """Log the error and send a telegram message to notify the developer."""
    # Log the error before we do anything else, so we can see it even if something breaks.
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    # traceback.format_exception returns the usual python message about an exception, but as a
    # list of strings rather than a single string, so we have to join them together.
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = ''.join(tb_list)

    # Build the message with some markup and additional information about what happened.
    # You might need to add some logic to deal with messages longer than the 4096 character limit.
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        f'An exception was raised while handling an update\n'
        f'<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}'
        '</pre>\n\n'
        f'<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre>\n\n'
        f'<pre>context.user_data = {html.escape(str(context.user_data))}</pre>\n\n'
        f'<pre>{html.escape(tb_string)}</pre>'
    )

    # Finally, send the message
    context.bot.send_message(chat_id=DEVELOPER_CHAT_ID, text=message, parse_mode=ParseMode.HTML)


def main() -> None:
    # initialise bot and set commands
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    bot.set_my_commands([
        BotCommand(command='start', description='start the bot session'),
        BotCommand(command='alert', description='enable alerts on new slots'),
        BotCommand(command='help', description='provide help on how to use the bot'),
        BotCommand(command='resume', description='enable alerts on new slots'),
        BotCommand(command='pause', description='disable alerts on new slots'),
        BotCommand(command='pincode', description='change pincode'),
        BotCommand(command='age', description='change age preference'),
    ])

    # connect and create tables
    db.connect()
    db.create_tables([User, ])

    # initialise the handler
    updater = Updater(TELEGRAM_BOT_TOKEN)

    # Add handlers
    updater.dispatcher.add_handler(CommandHandler("start", start))
    updater.dispatcher.add_handler(CommandHandler("help", help_command))
    updater.dispatcher.add_handler(CommandHandler("alert", setup_alert_command))
    updater.dispatcher.add_handler(CommandHandler("resume", setup_alert_command))
    updater.dispatcher.add_handler(CommandHandler("pause", disable_alert_command))
    updater.dispatcher.add_handler(CommandHandler("age", age_command))
    updater.dispatcher.add_handler(CommandHandler("pincode", pincode_command))
    updater.dispatcher.add_handler(CommandHandler("delete", delete_cmd_handler))
    updater.dispatcher.add_handler(CallbackQueryHandler(set_age_preference, pattern=AGE_BUTTON_REGEX))
    updater.dispatcher.add_handler(CallbackQueryHandler(cmd_button_handler, pattern=CMD_BUTTON_REGEX))
    updater.dispatcher.add_handler(MessageHandler(Filters.regex(
        re.compile(PINCODE_PREFIX_REGEX, re.IGNORECASE)), set_pincode))
    updater.dispatcher.add_handler(MessageHandler(Filters.regex(
        re.compile(DISABLE_TEXT_REGEX, re.IGNORECASE)), disable_alert_command))
    updater.dispatcher.add_handler(MessageHandler(~Filters.command, default))
    updater.dispatcher.add_error_handler(error_handler)

    updater.job_queue.run_repeating(
        periodic_background_worker, interval=MIN_45_WORKER_INTERVAL, first=10)
    # start a bg thread in the background
    threading.Thread(target=frequent_background_worker).start()

    # Start the Bot
    updater.start_polling()
    updater.idle()


if __name__ == '__main__':
    main()

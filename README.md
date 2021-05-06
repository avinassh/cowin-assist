<div align="center">
<h1>Cowin Assist Telegram Bot</h1>
<br>
<img src="https://user-images.githubusercontent.com/640792/117273073-698c2480-ae79-11eb-988f-0770728f0d2c.jpeg" width="200"/>
</div>

Check the bot here [@cowinassistbot](https://t.me/cowinassistbot).

This is a simple Telegram bot to

- Check slots availability
- Get an alert when slots become available
- ~~Book an available slot~~

all this with a single click of button.

## Note

On 6th May 2021, CoWin API added caching and rate limits. The public API data would be cached upto 30 minutes, so the alerts wouldn't be so instant in busy areas, which reduced this bot's functionality to being a nice UI for public CoWin site in Telegram.

## Installation and Deployment

Following section helps you host the bot on your own servers. 

### Prerequisites

You need a bot account on Telegram. Use [@BotFather](https://t.me/BotFather) to create one. If you are new to Telegram Bots, you may start from here [Bots: An introduction for developers](https://core.telegram.org/bots).

### System Requirements

This bot is built and tested on Linux and Mac OS X. It should work on Windows machines as well, but I haven't tested it. Other requirements:

- Python 3 (version 3.8+)
- SQLite 3

### Installation

Install the project requirements from `requirements.txt`:

```shell
$ pip install -r requirements.txt
```

Rename `sample_secrets.py` to `secrets.py` and fill it with appropriate details. Then you can run:

```shell
$ python main.py

2021-05-06 09:59:29,238 - __main__ - INFO - starting a bg worker - frequent_background_worker
2021-05-06 09:59:29,239 - __main__ - INFO - starting a bg worker - periodic_background_worker
2021-05-06 09:59:29,239 - apscheduler.scheduler - INFO - Scheduler started
```

You may use the `supervisor.conf` provided for deployment using [Supervisord](http://supervisord.org/).

## Development

Open an issue for any discussions and feel free to send a PR.

## Disclaimer

Not affiliated with Ministry of Health and Family Welfare OR Government of India in any capacity.

## License

Released under MIT License. Check `LICENSE` file more info.

import logging
import os
import datetime as dt
import requests
import json

from remind import constants
from discord.ext import commands

from pathlib import Path

logger = logging.getLogger(__name__)
URL_BASE = 'https://clist.by/api/v2/contest/'
_CLIST_API_TIME_DIFFERENCE = 30 * 60  # seconds


class ClistApiError(commands.CommandError):
    """Base class for all API related errors."""

    def __init__(self, message=None):
        super().__init__(message or 'Clist API error')


class ClientError(ClistApiError):
    """An error caused by a request to the API failing."""

    def __init__(self):
        super().__init__('Error connecting to Clist API')


def _query_api():
    clist_username = os.getenv('CLIST_API_USERNAME')
    clist_api_key = os.getenv('CLIST_API_KEY')
    if not clist_username or not clist_api_key:
        logger.error('CLIST API credentials are missing')
        raise ClientError

    contests_start_time = dt.datetime.utcnow() - dt.timedelta(days=2)
    contests_start_time_string = contests_start_time.strftime("%Y-%m-%dT%H:%M:%S")

    param = {
        "order_by": "start",
        "limit": "500",
        "start__gte": contests_start_time_string,
    }
    headers = {'Authorization': f'ApiKey {clist_username}:{clist_api_key}'}

    try:
        resp = requests.get(URL_BASE, params=param, headers=headers, timeout=15, allow_redirects=False)
    except requests.RequestException as error:
        logger.error('CLIST API request failed (%s)', type(error).__name__)
        raise ClientError from error

    if resp.status_code != 200:
        logger.error('CLIST API returned HTTP %s', resp.status_code)
        raise ClientError

    try:
        contests = resp.json()['objects']
        if not isinstance(contests, list):
            raise ValueError('Expected a contest list')
    except (KeyError, TypeError, ValueError) as error:
        logger.error('CLIST API returned an invalid contest response')
        raise ClientError from error
    return contests


def cache(forced=False):
    current_time_stamp = dt.datetime.utcnow().timestamp()
    db_file = Path(constants.CONTESTS_DB_FILE_PATH)

    db = None
    try:
        with db_file.open() as f:
            db = json.load(f)
    except BaseException:
        pass

    last_time_stamp = db['querytime'] if db and db['querytime'] else 0

    if not forced and current_time_stamp - last_time_stamp < _CLIST_API_TIME_DIFFERENCE:
        return

    try:
        contests = _query_api()
    except:
        return

    db = {'querytime': current_time_stamp, 'objects': contests}
    with open(db_file, 'w') as f:
        json.dump(db, f)

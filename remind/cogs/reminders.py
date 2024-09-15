import asyncio
import random
import json
import pickle
import logging
import time
import datetime as dt
from pathlib import Path
import re
import copy

from collections import defaultdict
from recordtype import recordtype
from datetime import datetime

import discord
from discord.ext import commands

from remind.util.rounds import Round
from remind.util import discord_common
from remind.util import paginator
from remind import constants
from remind.util import clist_api as clist
from remind.util.website_schema import WebsitePatterns
from remind.util import website_schema


class RemindersCogError(commands.CommandError):
    pass


_CONTESTS_PER_PAGE = 5
_CONTEST_PAGINATE_WAIT_TIME = 5 * 60
_FINISHED_CONTESTS_LIMIT = 5
_CONTEST_REFRESH_PERIOD = 10 * 60  # seconds
_GUILD_SETTINGS_BACKUP_PERIOD = 6 * 60 * 60  # seconds

GuildSettings = recordtype(
    'GuildSettings', [
        ('remind_channel_id_div1', None),
        ('remind_role_id_div1', None),
        ('remind_before_div1', None),
        ('finalcall_channel_id_div1', None),
        ('finalcall_before_div1', None),
        ('subscribed_websites_div1', set()),

        ('remind_channel_id_all', None),
        ('remind_role_id_all', None),
        ('remind_before_all', None),
        ('finalcall_channel_id_all', None),
        ('finalcall_before_all', None),
        ('subscribed_websites_all', set())
    ]
)


class RemindRequest:
    def __init__(self, channel, role, contest: Round, before_secs, send_time):
        self.channel = channel
        self.role = role
        self.contest = contest
        self.before_secs = before_secs
        self.send_time = send_time


class FinalCallRequest:
    def __init__(self, *, embed, role_id, msg_id=None):
        self.role_id = role_id
        self.msg_id = msg_id
        self.embed_desc = embed.description
        self.embed_fields = [(field.name, field.value) for field in embed.fields]


def get_default_guild_settings():
    settings = GuildSettings()
    settings.subscribed_websites_div1 = set()
    settings.subscribed_websites_all = set()
    return settings


def _contest_start_time_format(contest):
    seconds = int(contest.start_time.replace(tzinfo=dt.timezone.utc).timestamp())
    return f'<t:{seconds}:F>'


def _contest_duration_format(contest):
    duration_days, duration_hrs, duration_mins, _ = discord_common.time_format(contest.duration.total_seconds())
    duration = f'{duration_hrs}h {duration_mins}m'
    if duration_days > 0:
        duration = f'{duration_days}d ' + duration
    return duration


def _get_formatted_contest_desc(start, duration, url):
    em = '\N{EN SPACE}'
    return f'{start}\nDuration:{em}{duration}{em}|{em}[link]({url})'


def _get_contest_website_prefix(contest):
    website_details = website_schema.schema[contest.website]
    return website_details.prefix


def _get_display_name(website, name):
    return (website + " || " + name) if website.lower() not in name.lower() else name


def _get_embed_fields_from_contests(contests):
    fields = []
    for contest in contests:
        start = _contest_start_time_format(contest)
        duration = _contest_duration_format(contest)
        value = _get_formatted_contest_desc(start, duration, contest.url)
        website = _get_contest_website_prefix(contest)
        fields.append((website, contest.name, value))
    return fields


async def _send_reminder_at(request):
    delay = request.send_time - dt.datetime.utcnow().timestamp()
    if delay <= 0:
        return

    await asyncio.sleep(delay)
    values = discord_common.time_format(request.before_secs)

    def make(value, label):
        tmp = f'{value} {label}'
        return tmp if value == 1 else tmp + 's'

    labels = 'day hr min sec'.split()
    before_str = ' '.join(make(value, label) for label, value in zip(labels, values) if value > 0)
    desc = f'About to start in {before_str}!'
    embed = discord_common.color_embed(description=desc)
    if request.contest.is_rare():
        embed.set_footer(text=f"Its once in a while contest, you wouldn't wanna miss 👀")
    for website, name, value in _get_embed_fields_from_contests([request.contest]):
        embed.add_field(name=_get_display_name(website, name), value=value, inline=False)
    await request.channel.send(request.role.mention + f' Its {website} time!', embed=embed)


def filter_contests(filters, contests):
    if not filters:
        return contests

    filtered_contests = []
    for contest in contests:
        eligible = False
        for contest_filter in filters:
            if contest_filter[0] == "+":
                filter = contest_filter[1:]
                for website, data in website_schema.schema.items():
                    eligible |= (website == contest.website and filter in data.shorthands)
        if eligible:
            filtered_contests.append(contest)
    return filtered_contests


def create_tuple_defaultdict():
    return defaultdict(FinalCallRequest)


class Reminders(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        self.future_contests_div1 = None
        self.contest_cache_div1 = None
        self.active_contests_div1 = None
        self.finished_contests_div1 = None
        self.start_time_map_div1 = defaultdict(list)
        self.task_map_div1 = defaultdict(list)

        self.future_contests_all = None
        self.contest_cache_all = None
        self.active_contests_all = None
        self.finished_contests_all = None
        self.start_time_map_all = defaultdict(list)
        self.task_map_all = defaultdict(list)

        # Maps guild_id to `GuildSettings`
        self.guild_map = defaultdict(get_default_guild_settings)
        self.last_guild_backup_time = -1
        self.reaction_emoji = "✅"
        self.nope_emoji = 973583086174498847

        self.finalcall_map_div1 = defaultdict(create_tuple_defaultdict)
        self.finaltasks_div1 = defaultdict(lambda: dict())
        self.finalcall_map_all = defaultdict(create_tuple_defaultdict)
        self.finaltasks_all = defaultdict(lambda: dict())

        self.member_converter = commands.MemberConverter()
        self.role_converter = commands.RoleConverter()

        self.logger = logging.getLogger(self.__class__.__name__)

    @commands.Cog.listener()
    @discord_common.once
    async def on_ready(self):
        guild_map_path = Path(constants.GUILD_SETTINGS_MAP_PATH)
        try:
            with guild_map_path.open('rb') as guild_map_file:
                data = pickle.load(guild_map_file)
                guild_map = data["guild_map"]
                self.finalcall_map_div1 = data["finalcall_map_div1"]
                self.finalcall_map_all = data["finalcall_map_all"]
                for guild_id, guild_settings in guild_map.items():
                    self.guild_map[guild_id] = GuildSettings(**{key: value
                                                                for key, value
                                                                in guild_settings._asdict().items()
                                                                if key in GuildSettings._fields})
        except BaseException:
            pass
        asyncio.create_task(self._update_task())

    async def cog_after_invoke(self, ctx):
        self._serialize_guild_map()
        self._backup_serialize_guild_map()
        self._reschedule_reminder_tasks(ctx.guild.id)
        self._reschedule_finalcall_tasks(ctx.guild.id)

    async def _update_task(self):
        self.logger.info(f'Invoking Scheduled Reminder Updates')
        self._generate_contest_cache()
        contest_cache_div1 = self.contest_cache_div1
        contest_cache_all = self.contest_cache_all
        current_time = dt.datetime.utcnow()

        self.future_contests_div1 = [
            contest for contest in contest_cache_div1
            if contest.start_time > current_time
        ]
        self.finished_contests_div1 = [
            contest for contest in contest_cache_div1
            if contest.start_time + contest.duration < current_time
        ]
        self.active_contests_div1 = [
            contest for contest in contest_cache_div1
            if contest.start_time <= current_time <= contest.start_time + contest.duration
        ]

        self.future_contests_all = [
            contest for contest in contest_cache_all
            if contest.start_time > current_time
        ]
        self.finished_contests_all = [
            contest for contest in contest_cache_all
            if contest.start_time + contest.duration < current_time
        ]
        self.active_contests_all = [
            contest for contest in contest_cache_all
            if contest.start_time <= current_time <= contest.start_time + contest.duration
        ]

        self.active_contests_div1.sort(key=lambda contest: contest.start_time)
        self.active_contests_all.sort(key=lambda contest: contest.start_time)
        self.finished_contests_div1.sort(key=lambda contest: contest.start_time + contest.duration, reverse=True)
        self.finished_contests_all.sort(key=lambda contest: contest.start_time + contest.duration, reverse=True)
        self.future_contests_div1.sort(key=lambda contest: contest.start_time)
        self.future_contests_all.sort(key=lambda contest: contest.start_time)
        # Keep most recent _FINISHED_LIMIT
        self.finished_contests_div1 = self.finished_contests_div1[:_FINISHED_CONTESTS_LIMIT]
        self.finished_contests_all = self.finished_contests_all[:_FINISHED_CONTESTS_LIMIT]
        self.start_time_map_div1.clear()
        for contest in self.future_contests_div1:
            self.start_time_map_div1[time.mktime(contest.start_time.timetuple())].append(contest)
        self.start_time_map_all.clear()
        for contest in self.future_contests_all:
            self.start_time_map_all[time.mktime(contest.start_time.timetuple())].append(contest)
        self._reschedule_all_tasks()
        await asyncio.sleep(_CONTEST_REFRESH_PERIOD)
        asyncio.create_task(self._update_task())

    def _generate_contest_cache(self):
        clist.cache(forced=False)
        db_file = Path(constants.CONTESTS_DB_FILE_PATH)
        with db_file.open() as f:
            data = json.load(f)
        contests = [Round(contest) for contest in data['objects']]
        self.contest_cache_div1 = [contest for contest in contests if contest.is_desired_for_div1(website_schema.schema)]
        self.contest_cache_all = [contest for contest in contests if contest.is_desired_for_all(website_schema.schema)]

    def get_guild_contests(self, contests, guild_id):
        settings = self.guild_map[guild_id]

        desired_contests_for_div1 = []
        desired_contests_for_all = []

        for contest in contests:
            if contest.is_desired_for_div1(settings.subscribed_websites_div1):
                desired_contests_for_div1.append(contest)
            if contest.is_desired_for_all(settings.subscribed_websites_all):
                desired_contests_for_all.append(contest)

        return desired_contests_for_div1, desired_contests_for_all

    def _reschedule_all_tasks(self):
        for guild in self.bot.guilds:
            self._reschedule_reminder_tasks(guild.id)
            self._reschedule_finalcall_tasks(guild.id)

    def _reschedule_reminder_tasks(self, guild_id):
        for task in self.task_map_div1[guild_id]:
            task.cancel()
        for task in self.task_map_all[guild_id]:
            task.cancel()
        self.task_map_div1[guild_id].clear()
        self.task_map_all[guild_id].clear()

        self.logger.info(f'Tasks for guild "{self.bot.get_guild(guild_id)}" cleared')

        settings = self.guild_map[guild_id]

        if self.start_time_map_div1 and not settings.remind_role_id_div1 is None:
            guild = self.bot.get_guild(guild_id)

            channel_div1 = guild.get_channel(settings.remind_channel_id_div1)
            role_div1 =  guild.get_role(settings.remind_role_id_div1)

            for start_time, contests in self.start_time_map_div1.items():
                contests_for_div1 = self.get_guild_contests(contests, guild_id)[0]

                if not contests_for_div1:
                    continue

                website_seggregated_contests_for_div1 = dict()
                for contest_for_div1 in contests_for_div1:
                    website_seggregated_contests_for_div1[contest_for_div1.url] = contest_for_div1  # an url can uniquely identify a contest

                for _, seg_contest in website_seggregated_contests_for_div1.items():
                    for before_mins in settings.remind_before_div1:
                        before_secs = 60 * before_mins
                        request = RemindRequest(channel_div1, role_div1, seg_contest, before_secs, start_time - before_secs)
                        task = asyncio.create_task(_send_reminder_at(request))
                        self.task_map_div1[guild_id].append(task)

            self.logger.info(
                f'{len(self.task_map_div1[guild_id])} div1 reminder tasks scheduled for guild "{self.bot.get_guild(guild_id)}"')

        if self.start_time_map_all and not settings.remind_role_id_all is None:
            guild = self.bot.get_guild(guild_id)

            channel_all = guild.get_channel(settings.remind_channel_id_all)
            role_all = guild.get_role(settings.remind_role_id_all)

            for start_time, contests in self.start_time_map_all.items():
                contests_for_all = self.get_guild_contests(contests, guild_id)[1]

                if not contests_for_all:
                    continue

                website_seggregated_contests_for_all = dict()
                for contest_for_all in contests_for_all:
                    website_seggregated_contests_for_all[contest_for_all.url] = contest_for_all  # an url can uniquely identify a contest

                for _, seg_contest in website_seggregated_contests_for_all.items():
                    for before_mins in settings.remind_before_all:
                        before_secs = 60 * before_mins
                        request = RemindRequest(channel_all, role_all, seg_contest, before_secs, start_time - before_secs)
                        task = asyncio.create_task(_send_reminder_at(request))
                        self.task_map_all[guild_id].append(task)

            self.logger.info(
                f'{len(self.task_map_all[guild_id])} reminder tasks scheduled for guild "{self.bot.get_guild(guild_id)}"')

    def _reschedule_finalcall_tasks(self, guild_id):
        if self.finalcall_map_div1[guild_id]:
            pending_reschedule_div1 = []
            for link, data in self.finalcall_map_div1[guild_id].items():
                try:
                    pending_reschedule_div1.append(data)
                    task = self.finaltasks_div1[guild_id][link]
                    task.cancel()
                except KeyError:
                    pass

            self.finalcall_map_div1[guild_id].clear()
            for data in pending_reschedule_div1:
                embed_desc, embed_fields = data.embed_desc, data.embed_fields
                embed = discord_common.color_embed()
                embed.description = embed_desc
                for (name, value) in embed_fields:
                    embed.add_field(name=name, value=value, inline=False)
                link, start_time = self.get_values_from_embed(embed)
                send_time = start_time - self.guild_map[guild_id].finalcall_before_div1 * 60

                reaction_role = self.bot.get_guild(guild_id).get_role(data.role_id)
                if reaction_role is not None:
                    task = asyncio.create_task(
                        self.send_finalcall_reminder(embed, guild_id, reaction_role, send_time, link, for_all = False))
                    self.finalcall_map_div1[guild_id][link] = FinalCallRequest(role_id=reaction_role.id, embed=embed,
                                                                          msg_id=data.msg_id)
                    self.finaltasks_div1[guild_id][link] = task

            self.logger.info(
                f'{len(self.finalcall_map_div1[guild_id])} div1 final calls scheduled for guild "{self.bot.get_guild(guild_id)}"')

        if self.finalcall_map_all[guild_id]:
            pending_reschedule_all = []
            for link, data in self.finalcall_map_all[guild_id].items():
                try:
                    pending_reschedule_all.append(data)
                    task = self.finaltasks_all[guild_id][link]
                    task.cancel()
                except KeyError:
                    pass

            self.finalcall_map_all[guild_id].clear()
            for data in pending_reschedule_all:
                embed_desc, embed_fields = data.embed_desc, data.embed_fields
                embed = discord_common.color_embed()
                embed.description = embed_desc
                for (name, value) in embed_fields:
                    embed.add_field(name=name, value=value, inline=False)
                link, start_time = self.get_values_from_embed(embed)
                send_time = start_time - self.guild_map[guild_id].finalcall_before_all * 60
                reaction_role = self.bot.get_guild(guild_id).get_role(data.role_id)
                if reaction_role is not None:
                    task = asyncio.create_task(
                        self.send_finalcall_reminder(embed, guild_id, reaction_role, send_time, link, for_all = True))
                    self.finalcall_map_all[guild_id][link] = FinalCallRequest(role_id=reaction_role.id, embed=embed,
                                                                          msg_id=data.msg_id)
                    self.finaltasks_all[guild_id][link] = task

            self.logger.info(
                f'{len(self.finalcall_map_all[guild_id])} all final calls scheduled for guild "{self.bot.get_guild(guild_id)}"')

    @staticmethod
    def _make_contest_pages(contests, title):
        pages = []
        chunks = paginator.chunkify(contests, _CONTESTS_PER_PAGE)
        for chunk in chunks:
            embed = discord_common.color_embed()
            for website, name, value in _get_embed_fields_from_contests(chunk):
                embed.add_field(name=_get_display_name(website, name), value=value, inline=False)
            pages.append((title, embed))
        return pages

    async def _send_contest_list(self, ctx, contests, *, title, empty_msg):
        if contests is None:
            raise RemindersCogError('Contest list not present')
        if len(contests) == 0:
            await ctx.send(embed=discord_common.embed_neutral(empty_msg))
            return
        pages = self._make_contest_pages(contests, title)
        paginator.paginate(self.bot, ctx.channel, pages, wait_time=_CONTEST_PAGINATE_WAIT_TIME,
                           set_pagenum_footers=True)

    def _serialize_guild_map(self):
        self.logger.info("Serializing db to local file")
        data = {"guild_map": self.guild_map, "finalcall_map_div1": self.finalcall_map_div1, "finalcall_map_all": self.finalcall_map_all}
        out_path = Path(constants.GUILD_SETTINGS_MAP_PATH)
        with out_path.open(mode='wb') as out_file:
            pickle.dump(data, out_file)

    def _backup_serialize_guild_map(self):
        current_time_stamp = int(dt.datetime.utcnow().timestamp())
        if current_time_stamp - self.last_guild_backup_time < _GUILD_SETTINGS_BACKUP_PERIOD:
            return

        self.last_guild_backup_time = current_time_stamp
        out_path = Path(constants.GUILD_SETTINGS_MAP_PATH + "_" + str(current_time_stamp))
        data = {"guild_map": self.guild_map, "finalcall_map_div1": self.finalcall_map_div1, "finalcall_map_all": self.finalcall_map_all}
        with out_path.open(mode='wb') as out_file:
            pickle.dump(data, out_file)

    @commands.group(brief='Commands for contest reminders', invoke_without_command=True)
    async def remind(self, ctx):
        await ctx.send_help(ctx.command)

    @remind.command(name='configure_div1', brief='Set reminder settings for div1')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def set_remind_settings_for_div1(self, ctx, role: discord.Role, *before: int):
        """Sets reminder channel to current channel for div1,
        role to the given role, and reminder
        times to the given values in minutes.

        e.g t;remind configure_div1 @Subscriber 10 60 180
        """
        if not role.mentionable:
            raise RemindersCogError('The role for reminders must be mentionable')
        if not before or any(before_mins < 0 for before_mins in before):
            raise RemindersCogError('Please provide valid `before` values')

        before = list(before)
        before = sorted(before, reverse=True)
        self.guild_map[ctx.guild.id].remind_channel_id_div1 = ctx.channel.id
        self.guild_map[ctx.guild.id].remind_role_id_div1 = role.id
        self.guild_map[ctx.guild.id].remind_before_div1 = before

        remind_channel = ctx.guild.get_channel(self.guild_map[ctx.guild.id].remind_channel_id_div1)
        remind_role = ctx.guild.get_role(self.guild_map[ctx.guild.id].remind_role_id_div1)
        remind_before_str = f"At {', '.join(str(mins) for mins in self.guild_map[ctx.guild.id].remind_before_div1)} " \
                            f"mins before contest "

        embed = discord_common.embed_success('Reminder settings saved successfully')
        embed.add_field(name='Reminder channel for div1', value=remind_channel.mention)
        embed.add_field(name='Reminder Role for div1', value=remind_role.mention)
        embed.add_field(name='Reminder Before for div1', value=remind_before_str)

        await ctx.send(embed=embed)

    @remind.command(name='configure', brief='Set reminder settings')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def set_remind_settings(self, ctx, role: discord.Role, *before: int):
        """Sets reminder channel to current channel for all,
        role to the given role, and reminder
        times to the given values in minutes.

        e.g t;remind configure @Subscriber 10 60 180
        """
        if not role.mentionable:
            raise RemindersCogError('The role for reminders must be mentionable')
        if not before or any(before_mins < 0 for before_mins in before):
            raise RemindersCogError('Please provide valid `before` values')

        before = list(before)
        before = sorted(before, reverse=True)
        self.guild_map[ctx.guild.id].remind_channel_id_all = ctx.channel.id
        self.guild_map[ctx.guild.id].remind_role_id_all = role.id
        self.guild_map[ctx.guild.id].remind_before_all = before

        remind_channel = ctx.guild.get_channel(self.guild_map[ctx.guild.id].remind_channel_id_all)
        remind_role = ctx.guild.get_role(self.guild_map[ctx.guild.id].remind_role_id_all)
        remind_before_str = f"At {', '.join(str(mins) for mins in self.guild_map[ctx.guild.id].remind_before_all)} " \
                            f"mins before contest "

        embed = discord_common.embed_success('Reminder settings saved successfully')
        embed.add_field(name='Reminder channel for all', value=remind_channel.mention)
        embed.add_field(name='Reminder Role for all', value=remind_role.mention)
        embed.add_field(name='Reminder Before for all', value=remind_before_str)

        await ctx.send(embed=embed)

    @remind.command(brief='Resets the subscribed websites to the default ones')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def reset_subscriptions(self, ctx):
        """ Resets the judges settings to the default ones.
        """
        self.guild_map[ctx.guild.id].subscribed_websites_div1 = set()
        self.guild_map[ctx.guild.id].subscribed_websites_all = set()
        await ctx.send(embed=discord_common.embed_success('Succesfully reset the subscriptions to the default ones'))

    def _set_guild_setting(self, guild_id, websites, unsubscribe, for_all):

        guild_settings = self.guild_map[guild_id]
        supported_websites, unsupported_websites = [], []
        for website in websites:
            if website not in website_schema.schema:
                unsupported_websites.append(website)
                continue

            if unsubscribe:
                if not for_all:
                    guild_settings.subscribed_websites_div1.discard(website)
                else:
                    guild_settings.subscribed_websites_all.discard(website)
            else:
                if not for_all:
                    guild_settings.subscribed_websites_div1.add(website)
                else:
                    guild_settings.subscribed_websites_all.add(website)

            supported_websites.append(website)

        self.guild_map[guild_id] = guild_settings
        return supported_websites, unsupported_websites

    @remind.command(brief='Start div1 contest reminders from websites.')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def subscribe_div1(self, ctx, *websites: str):
        """Start contest reminders from websites."""

        if all(website not in website_schema.schema for website in websites):
            supported_websites = ", ".join(website_schema.supported_websites)
            embed = discord_common.embed_alert(
                f'None of these websites are supported for div1 contest reminders.'
                f'\nSupported websites -\n {supported_websites}.')
        else:
            guild_id = ctx.guild.id
            subscribed, unsupported = self._set_guild_setting(guild_id, websites, unsubscribe = False, for_all = False)
            subscribed_websites_str = ", ".join(subscribed)
            unsupported_websites_str = ", ".join(unsupported)
            success_str = f'Successfully subscribed from  {subscribed_websites_str} for div1 contest reminders.'
            success_str += f'\n{unsupported_websites_str} {"are" if len(unsupported) > 1 else "is"} \
                not supported.' if unsupported_websites_str else ""
            embed = discord_common.embed_success(success_str)
        await ctx.send(embed=embed)

    @remind.command(brief='Stop div1 contest reminders from websites.')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def unsubscribe_div1(self, ctx, *websites: str):
        """Stop contest reminders from websites."""

        if all(website not in website_schema.schema for website in websites):
            supported_websites = ", ".join(website_schema.supported_websites)
            embed = discord_common.embed_alert(f'None of these websites are supported for div1 contest reminders.'
                                               f'\nSupported websites -\n {supported_websites}.')
        else:
            guild_id = ctx.guild.id
            unsubscribed, unsupported = self._set_guild_setting(guild_id, websites, unsubscribe = True, for_all = False)
            unsubscribed_websites_str = ", ".join(unsubscribed)
            unsupported_websites_str = ", ".join(unsupported)
            success_str = f'Successfully unsubscribed from {unsubscribed_websites_str} for div1 contest reminders.'
            success_str += f'\n{unsupported_websites_str} \
                {"are" if len(unsupported) > 1 else "is"} \
                not supported.' if unsupported_websites_str else ""
            embed = discord_common.embed_success(success_str)
        await ctx.send(embed=embed)

    @remind.command(brief='Start contest reminders from websites.')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def subscribe(self, ctx, *websites: str):
        """Start contest reminders from websites."""

        if all(website not in website_schema.schema for website in websites):
            supported_websites = ", ".join(website_schema.supported_websites)
            embed = discord_common.embed_alert(
                f'None of these websites are supported for contest reminders.'
                f'\nSupported websites -\n {supported_websites}.')
        else:
            guild_id = ctx.guild.id
            subscribed, unsupported = self._set_guild_setting(guild_id, websites, unsubscribe = False, for_all = True)
            subscribed_websites_str = ", ".join(subscribed)
            unsupported_websites_str = ", ".join(unsupported)
            success_str = f'Successfully subscribed from  {subscribed_websites_str} for contest reminders.'
            success_str += f'\n{unsupported_websites_str} {"are" if len(unsupported) > 1 else "is"} \
                not supported.' if unsupported_websites_str else ""
            embed = discord_common.embed_success(success_str)
        await ctx.send(embed=embed)

    @remind.command(brief='Stop contest reminders from websites.')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def unsubscribe(self, ctx, *websites: str):
        """Stop contest reminders from websites."""

        if all(website not in website_schema.schema for website in websites):
            supported_websites = ", ".join(website_schema.supported_websites)
            embed = discord_common.embed_alert(f'None of these websites are supported for contest reminders.'
                                               f'\nSupported websites -\n {supported_websites}.')
        else:
            guild_id = ctx.guild.id
            unsubscribed, unsupported = self._set_guild_setting(guild_id, websites, unsubscribe = True, for_all = True)
            unsubscribed_websites_str = ", ".join(unsubscribed)
            unsupported_websites_str = ", ".join(unsupported)
            success_str = f'Successfully unsubscribed from {unsubscribed_websites_str} for contest reminders.'
            success_str += f'\n{unsupported_websites_str} \
                {"are" if len(unsupported) > 1 else "is"} \
                not supported.' if unsupported_websites_str else ""
            embed = discord_common.embed_success(success_str)
        await ctx.send(embed=embed)

    @remind.command(brief='Clear all reminder settings')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def clear(self, ctx):
        del self.guild_map[ctx.guild.id]
        await ctx.send(embed=discord_common.embed_success('Reminder settings cleared'))

    @commands.group(brief='Commands for listing contests', invoke_without_command=True)
    async def clist(self, ctx):
        """
        Show past, present and future contests.Use filters to get contests from specific website

        Supported Filters : +cf/+codeforces +ac/+atcoder +cc/+codechef +hackercup +google +usaco +leetcode

        Eg: t;clist future +ac +codeforces
        will show contests from atcoder and codeforces
        """
        await ctx.send_help(ctx.command)

    @clist.command(brief='List future div1 contests')
    async def future_div1(self, ctx, *filters):
        """List future contests."""
        contests = filter_contests(filters, self.get_guild_contests(self.future_contests_div1, ctx.guild.id)[0])
        await self._send_contest_list(ctx, contests, title='Future div1 contests', empty_msg='No future div1 contests scheduled')

    @clist.command(brief='List active div1 contests')
    async def active_div1(self, ctx, *filters):
        """List active contests."""
        contests = filter_contests(filters, self.get_guild_contests(self.active_contests_div1, ctx.guild.id)[0])
        await self._send_contest_list(ctx, contests, title='Active div1 contests', empty_msg='No div1 contests currently active')

    @clist.command(brief='List recent div1 finished contests')
    async def finished_div1(self, ctx, *filters):
        """List recently concluded contests."""
        contests = filter_contests(filters, self.get_guild_contests(self.finished_contests_div1, ctx.guild.id)[0])
        await self._send_contest_list(ctx, contests, title='Recently finished div1 contests',
                                      empty_msg='No finished contests found')

    @clist.command(brief='List future contests')
    async def future(self, ctx, *filters):
        """List future contests."""
        contests = filter_contests(filters, self.get_guild_contests(self.future_contests_all, ctx.guild.id)[1])
        await self._send_contest_list(ctx, contests, title='Future contests', empty_msg='No future contests scheduled')

    @clist.command(brief='List active contests')
    async def active(self, ctx, *filters):
        """List active contests."""
        contests = filter_contests(filters, self.get_guild_contests(self.active_contests_all, ctx.guild.id)[1])
        await self._send_contest_list(ctx, contests, title='Active contests', empty_msg='No contests currently active')

    @clist.command(brief='List recent finished contests')
    async def finished(self, ctx, *filters):
        """List recently concluded contests."""
        contests = filter_contests(filters, self.get_guild_contests(self.finished_contests_all, ctx.guild.id)[1])
        await self._send_contest_list(ctx, contests, title='Recently finished contests',
                                      empty_msg='No finished contests found')

    async def send_finalcall_reminder(self, embed, guild_id, role, send_time, link, for_all):
        send_msg = "GLHF!"
        settings = self.guild_map[guild_id]
        finalcall_before = settings.finalcall_before_div1 if not for_all else settings.finalcall_before_all
        finalcall_channel_id = settings.finalcall_channel_id_div1 if not for_all else settings.finalcall_channel_id_all

        # sleep till the ping time
        delay = send_time - dt.datetime.now().timestamp()
        if delay >= 0:
            await asyncio.sleep(delay)

            def make(value, label):
                tmp = f'{value} {label}'
                return tmp if value == 1 else tmp + 's'

            labels = 'day hr min sec'.split()
            values = discord_common.time_format(finalcall_before * 60)
            before_str = ' '.join(make(value, label) for label, value in zip(labels, values) if value > 0)
            desc = f'About to start in {before_str}!'
            embed.description = desc
            channel = self.bot.get_channel(finalcall_channel_id)
            msg = await channel.send(role.mention + " " + send_msg, embed=embed)
            if not for_all:
                self.finalcall_map_div1[guild_id][link].msg_id = msg.id
            else:
                self.finalcall_map_all[guild_id][link].msg_id = msg.id
            self._serialize_guild_map()

        # sleep till contest starts
        time_to_contest = max(0, send_time + finalcall_before * 60 - dt.datetime.utcnow().timestamp())
        await asyncio.sleep(time_to_contest)

        # delete role and task
        if not for_all:
            if link in self.finalcall_map_div1[guild_id]:
                msg_id = self.finalcall_map_div1[guild_id][link].msg_id
                message = await self.bot.get_channel(finalcall_channel_id).fetch_message(msg_id)
                await message.edit(content=send_msg)
                del self.finalcall_map_div1[guild_id][link]
                del self.finaltasks_div1[guild_id][link]
        else:
            if link in self.finalcall_map_all[guild_id]:
                msg_id = self.finalcall_map_all[guild_id][link].msg_id
                message = await self.bot.get_channel(finalcall_channel_id).fetch_message(msg_id)
                await message.edit(content=send_msg)
                del self.finalcall_map_all[guild_id][link]
                del self.finaltasks_all[guild_id][link]
        if role is not None:
            await role.delete()
        self._serialize_guild_map()

    @staticmethod
    def get_values_from_embed(embed):
        desc = embed.fields[0].value
        link = re.findall(r']\((http.+)\)', desc)[0]
        start_time = int(re.findall(r'<t:(\d+):[A-za-z]>', desc)[0])
        return link, start_time

    async def create_finalcall_role(self, guild_id, embed, for_all):
        contest_name = embed.fields[0].name
        name = f"Final Call {'(Div1)' if not for_all else '(All)'} - {contest_name}"
        role = await self.bot.get_guild(guild_id).create_role(name=name, mentionable=True)
        return role

    async def get_finalcall_taskrole(self, guild_id, embed, remove, for_all):
        guild = self.bot.get_guild(guild_id)
        link, start_time = self.get_values_from_embed(embed)
        finalcall_before = self.guild_map[guild_id].finalcall_before_div1 if not for_all else self.guild_map[guild_id].finalcall_before_all
        send_time = start_time - finalcall_before * 60

        if not for_all:
            if link in self.finalcall_map_div1[guild_id]:
                reaction_role = guild.get_role(self.finalcall_map_div1[guild_id][link].role_id)
            elif (not remove) and send_time > dt.datetime.utcnow().timestamp():
                reaction_role = await self.create_finalcall_role(guild_id, embed, for_all)
                task = asyncio.create_task(self.send_finalcall_reminder(embed, guild_id, reaction_role, send_time, link, for_all = False))
                self.finalcall_map_div1[guild_id][link] = FinalCallRequest(embed=embed, role_id=reaction_role.id)
                self.finaltasks_div1[guild_id][link] = task
            else:
                reaction_role = None
        else:
            if link in self.finalcall_map_all[guild_id]:
                reaction_role = guild.get_role(self.finalcall_map_all[guild_id][link].role_id)
            elif (not remove) and send_time > dt.datetime.utcnow().timestamp():
                reaction_role = await self.create_finalcall_role(guild_id, embed, for_all)
                task = asyncio.create_task(self.send_finalcall_reminder(embed, guild_id, reaction_role, send_time, link, for_all = True))
                self.finalcall_map_all[guild_id][link] = FinalCallRequest(embed=embed, role_id=reaction_role.id)
                self.finaltasks_all[guild_id][link] = task
            else:
                reaction_role = None

        return reaction_role

    async def do_validation_check(self, payload, for_all):
        settings = self.guild_map[payload.guild_id]
        member = self.bot.get_guild(payload.guild_id).get_member(payload.user_id)
        remind_channel_id = settings.remind_channel_id_div1 if not for_all else settings.remind_channel_id_all
        finalcall_channel_id = settings.finalcall_channel_id_div1 if not for_all else settings.finalcall_channel_id_all

        if member.bot or remind_channel_id is None or remind_channel_id != payload.channel_id \
            or payload.emoji.name != self.reaction_emoji or finalcall_channel_id is None:
            return None

        channel = self.bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        reaction_count = sum(reaction.count for reaction in message.reactions if str(reaction) == self.reaction_emoji)

        if not message.embeds:
            return None

        return reaction_count, message.embeds[0]

    async def victim_card(self, member):
        self.logger.error(f'Failed to send DM to {member}')

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        for_all = payload.channel_id == self.guild_map[payload.guild_id].remind_channel_id_all

        response = await self.do_validation_check(payload, for_all)
        if response is None:
            return

        settings = self.guild_map[payload.guild_id]

        _, embed = response
        _, start_time = self.get_values_from_embed(embed)
        finalcall_before = settings.finalcall_before_div1 if not for_all else settings.finalcall_before_all
        send_time = start_time - finalcall_before * 60

        if send_time < dt.datetime.utcnow().timestamp():
            return

        reaction_role = await self.get_finalcall_taskrole(payload.guild_id, embed, remove = False, for_all = for_all)
        member = self.bot.get_guild(payload.guild_id).get_member(payload.user_id)
        self.logger.info(
            f'{member} reacted for {reaction_role} which will be sent at {datetime.fromtimestamp(send_time)}')
        await member.add_roles(reaction_role)
        member_dm = await member.create_dm()
        self._serialize_guild_map()
        try:
            await member_dm.send(f"Final Call Alarm Set. You are alloted `{reaction_role.name}` which will be pinged"
                                 f" {finalcall_before} mins before the contest")
        except:
            await self.victim_card(member)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        for_all = payload.channel_id == self.guild_map[payload.guild_id].remind_channel_id_all

        response = await self.do_validation_check(payload, for_all)
        if response is None:
            return

        reaction_count, embed = response
        reaction_role = await self.get_finalcall_taskrole(payload.guild_id, embed, remove = True, for_all = for_all)

        link, _ = self.get_values_from_embed(embed)
        if reaction_role is None:
            assert link not in (self.finalcall_map_div1[payload.guild_id] if not for_all else self.finalcall_map_all[payload.guild_id])
            return

        member = self.bot.get_guild(payload.guild_id).get_member(payload.user_id)
        self.logger.info(f'{member} unreacted for {reaction_role.name} {"(div1)" if not for_all else "(all)"}')
        await member.remove_roles(reaction_role)
        member_dm = await member.create_dm()
        try:
            await member_dm.send(f"Final Call Alarm Cleared for '{reaction_role.name}' {'(div1)' if not for_all else '(all)'}")
        except:
            await self.victim_card(member)

        if reaction_count == 1:
            if not for_all:
                if link in self.finalcall_map_div1[payload.guild_id]:
                    self.finaltasks_div1[payload.guild_id][link].cancel()
                    del self.finalcall_map_div1[payload.guild_id][link]
                    del self.finaltasks_div1[payload.guild_id][link]
            else:
                if link in self.finalcall_map_all[payload.guild_id]:
                    self.finaltasks_all[payload.guild_id][link].cancel()
                    del self.finalcall_map_all[payload.guild_id][link]
                    del self.finaltasks_all[payload.guild_id][link]
            await reaction_role.delete()
        self._serialize_guild_map()

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild is None:
            return
        settings = self.guild_map[message.guild.id]
        for_all = message.channel.id == settings.remind_channel_id_all
        remind_channel_id = settings.remind_channel_id_div1 if not for_all else settings.remind_channel_id_all
        if message.channel.id != remind_channel_id or not message.embeds:
            return

        remind_role_id = settings.remind_role_id_div1 if not for_all else settings.remind_role_id_all
        remind_role = self.bot.get_guild(message.guild.id).get_role(remind_role_id)
        if remind_role in message.role_mentions:
            await message.add_reaction(self.reaction_emoji)

    @commands.group(brief="Manage Final Call Reminder", invoke_without_command=True)
    async def final(self, ctx):
        await ctx.send_help(ctx.command)

    @final.command(name='configure_div1', brief='Set channel for the div1 final call')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def set_finalcall_settings_div1(self, ctx, before: int):
        if not before or before < 0:
            raise RemindersCogError('Please provide valid `before` values')

        self.guild_map[ctx.guild.id].finalcall_before_div1 = before
        self.guild_map[ctx.guild.id].finalcall_channel_id_div1 = ctx.channel.id

        finalcall_channel = ctx.guild.get_channel(self.guild_map[ctx.guild.id].finalcall_channel_id_div1)

        embed = discord_common.embed_success('Final call settings for division 1 saved successfully')
        embed.add_field(name='Final Call Channel (Div1)', value=finalcall_channel.mention)
        embed.add_field(name='Final Call Before (Div1)',
                        value=f"{self.guild_map[ctx.guild.id].finalcall_before_div1} mins before contest")

        await ctx.send(embed=embed)

    @final.command(name='configure', brief='Set channel for the final call')
    @commands.has_any_role('Admin', constants.REMIND_MODERATOR_ROLE)
    async def set_finalcall_settings(self, ctx, before: int):
        if not before or before < 0:
            raise RemindersCogError('Please provide valid `before` values')

        self.guild_map[ctx.guild.id].finalcall_before_all = before
        self.guild_map[ctx.guild.id].finalcall_channel_id_all = ctx.channel.id

        finalcall_channel = ctx.guild.get_channel(self.guild_map[ctx.guild.id].finalcall_channel_id_all)

        embed = discord_common.embed_success('Final call settings for all saved successfully')
        embed.add_field(name='Final Call Channel (All)', value=finalcall_channel.mention)
        embed.add_field(name='Final Call Before (All)',
                        value=f"{self.guild_map[ctx.guild.id].finalcall_before_all} mins before contest")

        await ctx.send(embed=embed)

    @commands.command(brief='Get Info about guild', invoke_without_command=True)
    async def settings(self, ctx):
        """Shows the current settings for the guild"""
        settings = self.guild_map[ctx.guild.id]

        for for_all in [False, True]:
            remind_channel = ctx.guild.get_channel(settings.remind_channel_id_div1 if not for_all else settings.remind_channel_id_all)
            remind_role = ctx.guild.get_role(settings.remind_role_id_div1 if not for_all else settings.remind_role_id_all)
            finalcall_channel = ctx.guild.get_channel(settings.finalcall_channel_id_div1 if not for_all else settings.finalcall_channel_id_all)
            subscribed_websites_str = ", ".join(settings.subscribed_websites_div1 if not for_all else settings.subscribed_websites_all)

            remind_before_str = "Not Set"
            final_before_str = "Not Set"
            remind_before = settings.remind_before_div1 if not for_all else settings.remind_before_all
            if remind_before is not None:
                remind_before_str = f"At {', '.join(str(before_mins) for before_mins in remind_before)}" \
                                    f" mins before contest"
            finalcall_before = settings.finalcall_before_div1 if not for_all else settings.finalcall_before_all
            if finalcall_before is not None:
                final_before_str = f"At {finalcall_before} mins before contest"
            embed = discord_common.embed_success(f'Current {"div1 " if not for_all else ""}settings')

            if remind_channel is not None:
                embed.add_field(name='Remind Channel', value=remind_channel.mention)
            else:
                embed.add_field(name='Remind Channel', value="Not Set")

            if remind_role is not None:
                embed.add_field(name='Remind Role', value=remind_role.mention)
            else:
                embed.add_field(name='Remind Role', value="Not Set")

            embed.add_field(name='Remind Before', value=remind_before_str)

            if finalcall_channel is not None:
                embed.add_field(name='Final Call Channel', value=finalcall_channel.mention)
            else:
                embed.add_field(name='Final Call Channel', value="Not Set")

            embed.add_field(name='Final Call Before', value=final_before_str)
            embed.add_field(name="\u200b", value="\u200b")

            embed.add_field(name='Subscribed websites', value=f'{subscribed_websites_str}', inline=False)

            embed.set_footer(text=ctx.guild.name, icon_url=ctx.guild.icon)
            await ctx.send(embed=embed)

    @discord_common.send_error_if(RemindersCogError)
    async def cog_command_error(self, ctx, error):
        pass


async def setup(bot):
    await bot.add_cog(Reminders(bot))

import logging
import datetime as dt
from remind.util import website_schema


class Round:
    def __init__(self, contest):
        self.id = contest['id']
        self.start_time = dt.datetime.strptime(contest['start'], '%Y-%m-%dT%H:%M:%S')
        self.duration = dt.timedelta(seconds=contest['duration'])
        self.url = contest['href']
        self.website = contest['resource']
        self.name = website_schema.schema[self.website].normalize(contest['event'])

    def __str__(self):
        st = "ID = " + str(self.id) + ", "
        st += "Name = " + self.name + ", "
        st += "Start_time = " + str(self.start_time) + ", "
        st += "Duration = " + str(self.duration) + ", "
        st += "URL = " + self.url + ", "
        st += "Website = " + self.website + ", "
        st = "(" + st[:-2] + ")"
        return st

    def is_eligible(self, site):
        return site == self.website

    def is_rare(self):
        schema = website_schema.schema[self.website]
        return schema.rare

    def is_desired_for_div1(self, subscribed_websites):
        if self.website not in subscribed_websites:
            return False
        return website_schema.schema[self.website].is_matched(self.name, for_all = False)

    def is_desired_for_all(self, subscribed_websites):
        if self.website not in subscribed_websites:
            return False
        return website_schema.schema[self.website].is_matched(self.name, for_all = True)

    def __repr__(self):
        return "Round - " + self.name

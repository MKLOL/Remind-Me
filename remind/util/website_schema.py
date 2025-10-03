from collections import defaultdict
import re


class WebsitePatterns:
    def __init__(self, *,
                 _is_matched=lambda website, for_all = True: False,
                 _shorthands=None,
                 _prefix="",
                 _normalize_regex=".*",
                 _rare=False):
        self.is_matched = _is_matched
        self.shorthands = _shorthands or []
        self.prefix = _prefix
        self._normalize_regex = _normalize_regex
        self.rare = _rare

    def normalize(self, name):
        try:
            name = re.compile(self._normalize_regex).search(name).group()
        except AttributeError:
            pass
        return name

# Todo : Move these to external db
supported_websites = ['codeforces.com', 'codechef.com', 'atcoder.jp', 'facebook.com/hackercup', 'tlx.toki.id']
schema = defaultdict(WebsitePatterns)

def _is_matched_for_codeforces(name, for_all = True):
    name = name.lower()
    forceMatch = False
    for div1Pattern in ['div. 1', 'rated for all', 'rated for both', 'rated for everyone']:
      if div1Pattern in name:
        forceMatch = True

    for div2Pattern in ['educational', 'div. 2', 'div. 3', 'div. 4']:
      if div2Pattern in name:
          forceMatch = True

    if not forceMatch:
      for bad_pattern in ['wild', 'fools', 'kotlin', 'unrated', 'icpc', 'challenge']:
          if bad_pattern in name:
              return False

    if not for_all:
        for good_pattern in ['div. 1', 'rated for all', 'rated for both', 'rated for everyone']:
            if good_pattern in name:
                return True

        for bad_pattern in ['educational', 'div. 2', 'div. 3', 'div. 4']:
            if bad_pattern in name:
                return False

    return True

schema['codeforces.com'] = WebsitePatterns(
    _is_matched=_is_matched_for_codeforces,
    _shorthands=['cf', 'codeforces'],
    _prefix='CodeForces',
)

def _is_matched_for_codechef(name, for_all = True):
    name = name.lower()

    # for bad_pattern in ['unrated']:
    #     if bad_pattern in name:
    #         return False

    # if all(must_pattern not in name for must_pattern in ['rated']):
    #     return False

    # if not for_all:
    #     if all(must_pattern not in name for must_pattern in ['7 star', '7 stars', '7-star', '7-stars', 'rated till 7', 'rated for all']):
    #         return False

    return for_all

schema['codechef.com'] = WebsitePatterns(
    _is_matched=_is_matched_for_codechef,
    _shorthands=['cc', 'codechef'],
    _prefix='CodeChef',
)

def _is_matched_for_atcoder(name, for_all = True):
    name = name.lower()

    if all(must_pattern not in name for must_pattern in ['abc:', 'beginner', 'arc:', 'regular', 'agc:', 'grand']):
        return False

    if not for_all:
        if all(must_pattern not in name for must_pattern in ['arc:', 'regular', 'agc:', 'grand']):
            return False

    return True

schema['atcoder.jp'] = WebsitePatterns(
    _is_matched=_is_matched_for_atcoder,
    _shorthands=['ac', 'atcoder'],
    _prefix='AtCoder',
    _normalize_regex="AtCoder .* Contest [0-9]+"
)

def _is_matched_for_hackercup(name, for_all = True):
    return True

schema['facebook.com/hackercup'] = WebsitePatterns(
    _is_matched=_is_matched_for_hackercup,
    _shorthands=['hackercup', 'fbhc'],
    _prefix='Meta Hackercup',
    _rare=True
)

def _is_matched_for_troc(name, for_all = True):
    name = name.lower()

    if all(must_pattern not in name for must_pattern in ['TLX Regular Open Contest', 'TROC']):
        return False

    return True

schema['tlx.toki.id'] = WebsitePatterns(
    _is_matched=_is_matched_for_troc,
    _shorthands=['toki', 'troc'],
    _prefix='TOKI Regular Open Contest',
    _rare=True
)

"""Reads the announcements of MEXC, Binance and Paribu and turns their swaps, renames,
ratio changes and delistings into draft rows for the lists.

    python duyurular.py             reads the new announcements and keeps the drafts
    python duyurular.py --gun 30    reads the announcements of the last 30 days as well
    python duyurular.py --borsa binance --gun 900
                                    the same for one exchange alone
    python duyurular.py --deneme    only shows the drafts, keeps nothing

Nothing is published here. The drafts wait in kaynak/taslak.json, which is not
published, until guncelle.py shows them with their links and publishes them with the
lists (guncelle.py --deneme shows them first, --sec takes them one by one). A misread
ratio in a public list would spoil the accounts of every user: the drafts give their
sources, and a swap that is not 1:1 is marked.

Times are UTC. The time of a row follows the rule of its list:
    MEXC     the opening of the new pair, or the announcement that the swap was
             completed, otherwise the close of trading. A delisting at the time it was
             announced for, or on the day of the ST table at about 14:00 once the coin
             has no pair left; a day of the ST table still to come is waited for.
    Binance  the close of the old coin (Old Coin Closing Date).
    Paribu   the end of the Turkish day of the last candle of the old coin (20:59).

Each run also looks whether the coins of the delisting lists whose time has passed are
gone from their exchange. A coin found gone is noted as such and not looked at again;
one still traded is drafted out of its list, since the app counts nothing left of a
delisted coin.
"""
import argparse
import gzip
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / 'kaynak'
DRAFTS = SOURCE / 'taslak.json'
CACHE = SOURCE / 'duyuru_onbellek.json'
# Each run reads this many days again: MEXC fills in its monthly ST tables for weeks.
LOOKBACK = 60
AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
         '(KHTML, like Gecko) Chrome/130 Safari/537.36')
UTC = timezone.utc
TURKEY = timezone(timedelta(hours=3))

MEXC_LIST = 'https://www.mexc.com/help/announce/api/en-US/section/{}/articles?page={}&perPage=100'
MEXC_ALL = 360000254192
# The sections that hold the monthly ST tables: those of delistings and of news. A table
# is filled in for months, so that one of March may give a day of November; the tables
# of this many days are read again whenever MEXC updates them.
ST_SECTIONS = (15425930840822, 15425930840826)
ST_TABLES = 420
MEXC_PAGE = 'https://www.mexc.com/announcements/article/{}'
BINANCE_LIST = ('https://www.binance.com/bapi/composite/v1/public/cms/article/list/query'
                '?type=1&catalogId={}&pageNo={}&pageSize=50')
BINANCE_BODY = ('https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query'
                '?articleCode={}')
BINANCE_PAGE = 'https://www.binance.com/en/support/announcement/detail/{}'
# Delisting, Maintenance Updates where Binance announces its token swaps, and News.
BINANCE_CATALOGS = (161, 157, 49)
PARIBU_LIST = 'https://www.paribu.com/destek/duyurular'
PARIBU_PAGE = 'https://www.paribu.com/destek/duyurular/{}'
# The pairs that each exchange trades now, against any quote: a coin is delisted only once
# none is left, though most delistings close the USDT (or TL) pair first or alone.
MARKETS = {
    'mexc': 'https://api.mexc.com/api/v3/exchangeInfo',
    'binance': 'https://api.binance.com/api/v3/exchangeInfo?symbolStatus=TRADING',
    'paribu': 'https://www.paribu.com/ticker',
}
MARKET_PAGES = {
    'mexc': 'https://www.mexc.com/markets',
    'binance': 'https://www.binance.com/en/markets/overview',
    'paribu': 'https://www.paribu.com/markets',
}
# A delisting is looked at once this long has passed since its time.
GRACE = timedelta(days=1)
# How the coins were found gone. When the rule changes, every coin is looked at again:
# 2 counts the pairs of any quote, where Binance's were once its USDT pairs alone; 3 also
# reads the candles of every pair that Binance closed, for a coin that went on trading
# on another pair after the day of the list.
CHECK_RULE = 3
# Binance keeps its closed pairs, with their candles.
BINANCE_SYMBOLS = 'https://api.binance.com/api/v3/exchangeInfo'
BINANCE_CANDLES = ('https://api.binance.com/api/v3/klines?symbol={}&interval={}&startTime={}'
                   '&limit=1000')

NAMES = {'mexc': 'MEXC', 'binance': 'Binance', 'paribu': 'Paribu'}
LISTS = {
    'mexc': ('mexc_swap', 'mexc_delist'),
    'binance': ('binance_spot_swap', 'binance_spot_delist'),
    'paribu': ('paribu_swap', 'paribu_delist'),
}

MEXC_TITLE = re.compile(
    r'token swap|contract swap|swap (?:of|to|into)|migration|rebrand|renam|ticker|'
    r'redenominat|token merge|merger|token split|delist|st warning|suspension of', re.I)
MEXC_NOISE = re.compile(
    r'will list|futures|perpetual|kickstarter|launches|airdrop|\betf\b|margin|convert|'
    r'guide|tutorial|competition|carnival|system upgrade|deposit address|meme\+|'
    r'on [a-z0-9 ]*network$', re.I)
# Words of an amount that changes while the coin keeps its name.
RESCALE = re.compile(r'redenominat|split|consolidat|ratio', re.I)
# A suspension of deposits only counts when its text speaks of one of these.
MEXC_SUSPENSION = re.compile(r'swap|rebrand|renam|ticker|redenominat|merge|delist|ratio', re.I)
BINANCE_SWAP = re.compile(r'token swap|rebrand|token merge|redenominat|renam|mainnet swap|'
                          r'token upgrade', re.I)
# A delisting from a product other than spot names the product first.
BINANCE_PRODUCT = re.compile(r'Binance (?:Margin|Futures|Alpha|(?:VIP )?Loans?|Options|Simple Earn|'
                             r'Spot Copy|Copy Trading|Convert|Pay)\b', re.I)
# Products other than spot, which the news catalog speaks of most.
BINANCE_OTHER = re.compile(r'futures|perpetual|margin|options|bstocks|stock|earn|copy trading',
                           re.I)
PARIBU_TITLE = re.compile(
    r'dönüşüm|kaldırıl|birleşim|birleşme|yeniden adland|ad değiş|isim değiş|sembol|'
    r'kontrat değiş|bölünme|redenom|takas', re.I)
# An update of a coin is read too, but only noted when its text speaks of a swap.
PARIBU_UPDATE = re.compile(r'güncelleme', re.I)
PARIBU_NOISE = re.compile(r'ağında|ağının|ağı |planlı|bakım|akıllı kontrat|chain|entegrasyon|'
                          r'yarışma', re.I)
PARIBU_SWAP_WORDS = re.compile(r'dönüşüm|takas|oranında|oranda|kaldırıl', re.I)

TICKER = r'[A-Z0-9][A-Z0-9$._-]{0,29}'
MONTHS = {name: number for number, name in enumerate(
    ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'), 1)}
TURKISH_MONTHS = {name: number for number, name in enumerate(
    ('ocak', 'şubat', 'mart', 'nisan', 'mayıs', 'haziran', 'temmuz', 'ağustos', 'eylül',
     'ekim', 'kasım', 'aralık'), 1)}
MONTH_NAME = r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?'
ENGLISH_TIME = re.compile(
    rf'(?P<mon>{MONTH_NAME})\s+(?P<day>\d{{1,2}}),?\s+(?P<year>\d{{4}})'
    r'(?:,?\s*(?:at\s*)?(?P<hm>\d{1,2}:\d{2}))?'
    r'|(?P<iso>\d{4}-\d{2}-\d{2})(?:[ T,]+(?:at\s*)?(?P<hm2>\d{1,2}:\d{2}))?'
    rf'|(?P<hm3>\d{{1,2}}:\d{{2}})\s*(?:\(UTC\))?\s*on\s+(?P<mon3>{MONTH_NAME})\s+'
    r'(?P<day3>\d{1,2}),?\s+(?P<year3>\d{4})')
TURKISH_TIME = re.compile(
    r'(?:(?P<d1>\d{1,2})\.(?P<m1>\d{1,2})\.(?P<y1>\d{4})'
    r'|(?P<d2>\d{1,2})\s+(?P<m2>Ocak|Şubat|Mart|Nisan|Mayıs|Haziran|Temmuz|Ağustos|Eylül|'
    r'Ekim|Kasım|Aralık)\s+(?P<y2>\d{4}))'
    r'(?:\s+\w+\s+günü)?(?:,?\s+saat\s+(?P<h>\d{1,2})[.:](?P<mi>\d{2}))?', re.I)


class Unreadable(Exception):
    """An announcement that seems to change a list but gave no row: to be read by hand."""


# --- Small helpers ------------------------------------------------------------------

def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def stamp(moment):
    return moment.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def moment_of(text):
    return datetime.strptime(text[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=UTC)


def fetch(url, tries=4):
    request = urllib.request.Request(url, headers={'User-Agent': AGENT,
                                                   'Accept-Encoding': 'gzip'})
    for attempt in range(tries):
        wait = 3 * (attempt + 1)
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                raw = response.read()
                if response.headers.get('Content-Encoding') == 'gzip':
                    raw = gzip.decompress(raw)
                time.sleep(0.3)
                return raw.decode('utf-8', 'replace')
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                raise
            if error.code == 429:
                after = error.headers.get('Retry-After', '')
                wait = int(after) if after.isdigit() else 20 * (attempt + 1)
        except OSError:
            if attempt == tries - 1:
                raise
        time.sleep(min(wait, 120))
    return ''


def tidy(text):
    text = text.replace('：', ':').replace('\u200b', '').replace('\ufeff', '')
    text = re.sub(r'[ \t\u00a0\u3000]+', ' ', text)
    return re.sub(r'\n\s*\n+', '\n', text).strip()


def plain(markup):
    text = re.sub(r'<script.*?</script>|<style.*?</style>', ' ', markup, flags=re.S | re.I)
    text = re.sub(r'<(?:br|/p|/li|/tr|/h\d|/div|/table)[^>]*>', '\n', text, flags=re.I)
    text = re.sub(r'<t[dh][^>]*>', ' | ', text, flags=re.I)
    return tidy(html.unescape(re.sub(r'<[^>]+>', ' ', text)))


def amount(text):
    """An English number: 24,000 or 0.044."""
    try:
        value = Decimal(text.strip().rstrip('.,').replace(',', ''))
    except InvalidOperation:
        return None
    return value if value > 0 else None


def turkish_amount(text):
    """A Turkish number: 24.000 or 0,08634."""
    text = text.strip().rstrip('.,')
    if ',' in text:
        text = text.replace('.', '').replace(',', '.')
    elif re.fullmatch(r'[1-9]\d{0,2}(?:\.\d{3})+', text):
        text = text.replace('.', '')
    return amount(text)


def rate_of(old, new):
    """The rate old:new as it is written in the lists, and the ratio new/old."""
    def written(value):
        return format(value.normalize(), 'f')
    return f'{written(old)}:{written(new)}', float(new / old)


def english_times(line):
    found = []
    for m in ENGLISH_TIME.finditer(line):
        if m.group('iso'):
            day = datetime.strptime(m.group('iso'), '%Y-%m-%d')
            clock = m.group('hm2')
        else:
            mon = m.group('mon') or m.group('mon3')
            number = MONTHS.get(mon[:3].lower())
            year = int(m.group('year') or m.group('year3'))
            date = int(m.group('day') or m.group('day3'))
            try:
                day = datetime(year, number, date)
            except (TypeError, ValueError):
                continue
            clock = m.group('hm') or m.group('hm3')
        hour, minute = (int(part) for part in clock.split(':')) if clock else (0, 0)
        if hour > 23 or minute > 59:
            continue
        found.append((day.replace(hour=hour, minute=minute, tzinfo=UTC), bool(clock),
                      m.start()))
    return found


def turkish_times(text):
    found = []
    for m in TURKISH_TIME.finditer(text):
        if m.group('d1'):
            year, month, date = int(m.group('y1')), int(m.group('m1')), int(m.group('d1'))
        else:
            year, date = int(m.group('y2')), int(m.group('d2'))
            month = TURKISH_MONTHS[m.group('m2').lower().replace('i̇', 'i')]
        hour, minute = (int(m.group('h')), int(m.group('mi'))) if m.group('h') else (12, 0)
        try:
            found.append((datetime(year, month, date, hour, minute, tzinfo=TURKEY),
                          m.start()))
        except ValueError:
            continue
    return found


def title_tickers(title):
    """The tickers that a title gives in brackets, in their order."""
    found = []
    for part in re.findall(r'\(([^()]{1,30})\)', title):
        part = part.strip()
        if re.fullmatch(r'[A-Z0-9][A-Za-z0-9$._-]{0,29}', part) and re.search('[A-Za-z]', part) \
                and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', part):
            found.append(part.upper())
    return found


def ratios(text, number=amount):
    """Every ratio that a text gives: (old amount, old ticker, new amount, new ticker, line)."""
    found = []
    value = r'\d[\d,.]*'
    word = r'\$?[A-Z][A-Z0-9]*'
    patterns = (
        rf'(?i:ratio)(?:\s+(?i:of|is|will be|at))?\s*:?\s*(?P<a>{value})\s*(?P<at>{word})?\s*'
        rf'[:=]\s*(?P<b>{value})\s*(?P<bt>{word})?',
        rf'(?P<a>{value})\s*(?i:old\s+)?(?P<at>{word})\s*=\s*(?P<b>{value})\s*(?i:new\s+)?'
        rf'(?P<bt>{word})',
        rf'(?P<a>{value})\s*:\s*(?P<b>{value})\s*(?i:oranında|oranda|ratio)',
        rf'(?i:her\s+)?(?P<a>{value})\s*(?i:adet)\s*(?P<at>{word})\s*(?i:için)\s*(?P<b>{value})\s*'
        rf'(?i:adet)\s*(?P<bt>{word})',
    )
    for line in text.splitlines():
        seen = set()
        for pattern in patterns:
            for m in re.finditer(pattern, line):
                if m.start('a') in seen:
                    continue
                seen.add(m.start('a'))
                old, new = number(m.group('a')), number(m.group('b'))
                if old and new:
                    named = m.groupdict()
                    found.append((old, (named.get('at') or '').lstrip('$') or None,
                                  new, (named.get('bt') or '').lstrip('$') or None, line))
    return found


def ratio_for(text, old, new, number=amount):
    """The ratio of the swap of old into new, when the text gives one that fits it."""
    found = [r for r in ratios(text, number)
             if r[1] in (None, old) and r[3] in (None, new)]
    if not found:
        return None

    def mentions(line, ticker):
        return re.search(rf'(?<![A-Z0-9]){re.escape(ticker)}(?![A-Z0-9])', line) is not None

    for test in (lambda r: r[1] == old and r[3] == new,
                 lambda r: mentions(r[4], old) and mentions(r[4], new),
                 lambda r: mentions(r[4], old)):
        best = [r for r in found if test(r)]
        if best:
            return best[0][0], best[0][2]
    return (found[0][0], found[0][2]) if len(found) == 1 else None


# --- Reading the announcements ------------------------------------------------------

_markets = {}


def markets(exchange):
    """The coins that an exchange lists now: {coin: (pairs traded, pairs paused)}. Read
    once a run."""
    if exchange in _markets:
        return _markets[exchange]
    found = {}
    data = json.loads(fetch(MARKETS[exchange]))
    if exchange == 'paribu':
        for name in data:
            coin, _, quote = name.partition('_')
            found.setdefault(coin.upper(), ([], []))[0].append(f'{coin.upper()}/{quote.upper()}')
    else:
        for symbol in data['symbols']:
            coin = symbol['baseAsset'].upper()
            traded = symbol.get('status') in ('TRADING', '1') and \
                symbol.get('isSpotTradingAllowed', True)
            found.setdefault(coin, ([], []))[0 if traded else 1].append(
                f"{coin}/{symbol['quoteAsset']}")
    _markets[exchange] = found
    return found


class Reader:
    """The announcements of one run, with the texts kept from the runs before."""

    def __init__(self, cache):
        self.cache = cache
        self.used = set()
        self.fetched = 0

    def text(self, key, updated, load):
        kept = self.cache.get(key)
        self.used.add(key)
        if kept and kept.get('updated') == updated:
            return kept['text']
        text = load()
        self.fetched += 1
        self.cache[key] = {'updated': updated, 'text': text}
        time.sleep(0.3)
        return text


def mexc_articles(since, section=MEXC_ALL):
    found = {}
    for page in range(1, 80):
        data = json.loads(fetch(MEXC_LIST.format(section, page)))['data']
        rows = data.get('results') or []
        for row in rows:
            if (row.get('createdAt') or '') >= since:
                found[row['id']] = {
                    'exchange': 'mexc', 'id': str(row['id']), 'title': row.get('title') or '',
                    'section': row.get('sectionId'),
                    'created': row['createdAt'][:19] + 'Z',
                    'updated': (row.get('updateTime') or row['createdAt'])[:19] + 'Z',
                    'url': MEXC_PAGE.format(row['id']),
                }
        # The first page starts with pinned rows of any age.
        if not rows or page > 1 and all((r.get('createdAt') or '') < since for r in rows):
            break
    return sorted(found.values(), key=lambda a: a['created'])


def mexc_text(article):
    markup = fetch(article['url'])
    text = plain(markup)
    start = text.find('Share Now')
    if start < 0:
        raise Unreadable('metin bulunamadı')
    ends = [text.find(mark, start) for mark in (
        'Related Articles', 'Sign up now to receive', 'MEXC Team', 'Subscribe to MEXC Digest')]
    ends = [end for end in ends if end > 0]
    return text[start + len('Share Now'):min(ends) if ends else start + 12000].strip()


def binance_articles(since):
    found = {}
    for catalog in BINANCE_CATALOGS:
        for page in range(1, 40):
            data = json.loads(fetch(BINANCE_LIST.format(catalog, page)))['data']
            rows = data['catalogs'][0]['articles'] if data.get('catalogs') else []
            for row in rows:
                created = stamp(datetime.fromtimestamp(row['releaseDate'] / 1000, UTC))
                if created >= since:
                    found[row['code']] = {
                        'exchange': 'binance', 'id': row['code'], 'title': row['title'],
                        'created': created, 'updated': created, 'catalog': catalog,
                        'url': BINANCE_PAGE.format(row['code']),
                    }
            if not rows or all(row['releaseDate'] / 1000 < moment_of(since).timestamp()
                               for row in rows):
                break
    return sorted(found.values(), key=lambda a: a['created'])


def binance_text(article):
    data = json.loads(fetch(BINANCE_BODY.format(article['id'])))['data']
    out = []

    def walk(node):
        if isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, dict):
            if node.get('node') == 'text':
                out.append(node.get('text', ''))
            walk(node.get('child') or [])
            if node.get('tag') in ('p', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'br', 'div', 'table'):
                out.append('\n')
            elif node.get('tag') in ('td', 'th'):
                out.append(' | ')

    walk(json.loads(data['body']))
    return tidy(html.unescape(''.join(out)))


def nuxt_data(markup):
    """The data that a Nuxt page carries for its client."""
    found = re.search(r'<script type="application/json"[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>',
                      markup, re.S)
    if not found:
        raise Unreadable('sayfa verisi bulunamadı')
    table = json.loads(found.group(1))
    done = {}

    def value(index):
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(table):
            return None
        if index in done:
            return done[index]
        item = table[index]
        if isinstance(item, list) and item and isinstance(item[0], str):
            tag = item[0]
            if tag in ('ShallowReactive', 'Reactive', 'Ref', 'ShallowRef'):
                result = value(item[1]) if len(item) > 1 else None
            elif tag == 'Date':
                result = item[1]
            elif tag == 'Set':
                result = [value(i) for i in item[1:]]
            elif tag in ('Map', 'null'):
                result = {str(value(item[i])): value(item[i + 1]) for i in range(1, len(item) - 1, 2)}
            else:
                result = None
        elif isinstance(item, list):
            result = [value(i) for i in item]
        elif isinstance(item, dict):
            result = {key: value(i) for key, i in item.items()}
        else:
            result = item
        done[index] = result
        return result

    return value(0)['data']


def paribu_articles(since):
    rows = nuxt_data(fetch(PARIBU_LIST)).get('support-announcements') or []
    found = []
    for row in rows:
        created = (row.get('published_at') or '')[:19] + 'Z'
        if created < since:
            continue
        found.append({
            'exchange': 'paribu', 'id': row['slug'], 'title': row.get('title') or '',
            'created': created, 'updated': row.get('date_updated') or created,
            'url': PARIBU_PAGE.format(row['slug']),
        })
    return sorted(found, key=lambda a: a['created'])


def paribu_text(article):
    data = nuxt_data(fetch(article['url']))
    item = data.get(f"support-announcement-{article['id']}") or {}
    if not item.get('body'):
        raise Unreadable('metin bulunamadı')
    return tidy(item['body'])


# --- Turning the texts into rows ----------------------------------------------------

def swap_row(exchange, old, new, rate, when, article, note, sources=None):
    text, ratio = rate_of(*rate)
    return {
        'list': LISTS[exchange][0], 'kind': 'swap', 'old': old, 'new': new,
        'ratio': ratio, 'rate': text, 'date': stamp(when.replace(second=0)), 'note': note,
        'title': article['title'], 'announced': article['created'], 'source': article['url'],
        'sources': sources or [article['url']],
    }


def delist_row(exchange, coin, when, article, note):
    return {
        'list': LISTS[exchange][1], 'kind': 'delist', 'coin': coin,
        'date': stamp(when.replace(second=0)),
        'note': note, 'title': article['title'], 'announced': article['created'],
        'source': article['url'], 'sources': [article['url']],
    }


def coin_names(text):
    parts = re.split(r',|，|、|\band\b|&', text)
    return [part.strip().upper() for part in parts if part.strip()]


def mexc_swap_parts(article, text):
    tickers = title_tickers(article['title'])
    if not tickers:
        found = re.search(rf'\(({TICKER})\)[^()]{{0,80}}?\b(?:to|into)\b[^()]{{0,60}}?\(({TICKER})\)',
                          text)
        tickers = [found.group(1), found.group(2)] if found else []
    if not tickers:
        raise Unreadable('coin adları okunamadı')
    old, new = tickers[0], tickers[1] if len(tickers) > 1 else tickers[0]
    close = opened = None
    for line in text.splitlines():
        if not re.search(r'\bt\w?ading\b|\btrade\b', line, re.I):
            continue
        for when, has_clock, _ in english_times(line):
            if re.search(r'\b(?:open|resum|reopen|enabl|restor|commenc|start|launch)', line, re.I):
                if re.search(rf'(?<![A-Z0-9]){re.escape(new)}(?![A-Z0-9])', line):
                    opened = min(opened, when) if opened else when
            elif re.search(r'clos|ceas|halt|suspend|stop|delist|remov', line, re.I):
                close = close or when
    rate = ratio_for(text, old, new)
    if not rate and old != new and re.search(r'renam|ticker change|assume the ticker',
                                             article['title'] + text, re.I):
        rate = (Decimal(1), Decimal(1))
    done = re.match(r'MEXC (?:Completes|Has Completed)', article['title'], re.I)
    return {'old': old, 'new': new, 'close': close, 'open': opened, 'rate': rate,
            'completed': moment_of(article['created']) if done else None, 'article': article,
            'text': text}


def mexc_swaps(parts):
    """Gathers the announcements of each swap, the first and the one that completes it."""
    rows, notes, quiet = [], [], 0
    events = {}
    for part in parts:
        events.setdefault((part['old'], part['new']), []).append(part)
    for (old, new), found in events.items():
        found.sort(key=lambda p: p['article']['created'])
        rate = next((p['rate'] for p in reversed(found) if p['completed'] and p['rate']), None) \
            or next((p['rate'] for p in reversed(found) if p['rate']), None)
        last = found[-1]['article']
        if old == new and (rate and rate[0] == rate[1] or not rate and not any(
                RESCALE.search(p['text']) for p in found)):
            quiet += 1  # a new contract or network, the same coin and amount
            continue
        if not rate:
            notes.append((last, 'oran okunamadı'))
            continue
        opened = min((p['open'] for p in found if p['open']), default=None)
        completed = min((p['completed'] for p in found if p['completed']), default=None)
        close = min((p['close'] for p in found if p['close']), default=None)
        if opened:
            when, note, source = opened, 'yeni paritenin açılışı', \
                next(p for p in found if p['open'])['article']
        elif completed:
            when, note, source = completed, 'tamamlanma duyurusu', \
                next(p for p in found if p['completed'])['article']
        elif close:
            when, note, source = close, ('işlem kapanışı; tamamlanma duyurusu gelince saat '
                                         'güncellenebilir'), next(p for p in found if p['close'])['article']
        else:
            notes.append((last, 'tarih okunamadı'))
            continue
        rows.append(swap_row('mexc', old, new, rate, when, source, note,
                             sorted({p['article']['url'] for p in found})))
    return rows, notes, quiet


def st_table(text):
    """The rows of an ST table: (coins, day of the ST tag, day of the delisting or TBC)."""
    start = re.search(r'Est\.?\s*Delisting\s*Time', text, re.I)
    if not start:
        return []
    cells = [cell.strip() for cell in re.split(r'\||\n', text[start.end():]) if cell.strip()]
    day = re.compile(r'\d{4}-\d{2}-\d{2}|TBC', re.I)
    rows = []
    for i in range(len(cells) - 2):
        if not day.fullmatch(cells[i]) and day.fullmatch(cells[i + 1]) and \
                day.fullmatch(cells[i + 2]):
            rows.append((cells[i], cells[i + 1], cells[i + 2].upper()))
    return rows


def st_coins(cell):
    coins = []
    for part in re.split(r'[,，、]', cell):
        part = part.strip().upper()
        if not part:
            continue
        pieces = part.split()
        # A row may leave out the comma between two tickers.
        if len(pieces) > 1 and all(re.fullmatch(TICKER, piece) for piece in pieces):
            coins += pieces
        elif ' ' not in part and len(part) <= 30:
            coins.append(part)
    return coins


def mexc_rows(articles, reader, now, since, sections=None):
    """The rows of the announcements of MEXC. sections are those where ST tables were
    seen; one found elsewhere among the announcements is added to them."""
    rows, notes, counts = [], [], {'etkisiz': 0, 'bekleyen ST': 0, 'ST paritesi duran': 0}
    parts, explicit, tables, suspended = [], set(), [], []
    sections = set(ST_SECTIONS) if sections is None else sections
    st_articles = {a['id']: a for a in articles if re.search(r'ST Warning', a['title'], re.I)}
    sections |= {a['section'] for a in st_articles.values() if a.get('section')}
    for section in sorted(sections):
        for article in mexc_articles(stamp(now - timedelta(days=ST_TABLES)), section):
            if re.search(r'ST Warning', article['title'], re.I):
                st_articles[article['id']] = article
    for article in sorted(st_articles.values(), key=lambda a: a['created']):
        try:
            text = reader.text(f"mexc:{article['id']}", article['updated'],
                               lambda a=article: mexc_text(a))
        except Unreadable as reason:
            notes.append((article, str(reason)))
            continue
        tables.append((article, st_table(text)))
    for article in articles:
        title = article['title']
        if not MEXC_TITLE.search(title) or MEXC_NOISE.search(title) or \
                re.search(r'ST Warning', title, re.I):
            continue
        try:
            text = reader.text(f"mexc:{article['id']}", article['updated'],
                               lambda a=article: mexc_text(a))
            if re.search(r'delist', title, re.I):
                found = mexc_delistings(article, text)
                rows += found
                explicit |= {row['coin'] for row in found}
            elif re.match(r'Suspension of', title, re.I):
                if MEXC_SUSPENSION.search(text):
                    suspended.append(article)
            else:
                parts.append(mexc_swap_parts(article, text))
        except Unreadable as reason:
            notes.append((article, str(reason)))
    swaps, swap_notes, quiet = mexc_swaps(parts)
    rows += swaps
    notes += swap_notes
    counts['etkisiz'] += quiet
    spoken = {p['old'] for p in parts} | {p['new'] for p in parts} | explicit
    for article in suspended:
        found = re.match(r'Suspension of (?:Deposits and Withdrawals for )?(.+?)'
                         r'(?: Deposits| Withdrawals|$)', article['title'], re.I)
        coins = set(coin_names(found.group(1))) if found else set()
        if not coins & spoken:
            notes.append((article, 'askı duyurusu; metinde swap ya da delist geçiyor'))
    if tables:
        listed = set(markets('mexc'))
        latest = {}
        for article, table in tables:
            for cell, _, day in table:
                if day == 'TBC':
                    continue
                for coin in st_coins(cell):
                    if coin not in latest or day >= latest[coin][0]:
                        latest[coin] = (day, article)
        for coin, (day, article) in latest.items():
            if coin in explicit:
                continue
            # MEXC delists on the day of its table at about 14:00 UTC.
            when = datetime.strptime(day, '%Y-%m-%d').replace(hour=14, tzinfo=UTC)
            if when < since:
                continue  # read in an earlier run
            if when > now:
                counts['bekleyen ST'] += 1
            elif coin in listed:
                counts['ST paritesi duran'] += 1
            else:
                rows.append(delist_row('mexc', coin, when, article, 'ST tablosu; paritesi kalmadı'))
    return rows, notes, counts


def mexc_delistings(article, text):
    rows = []
    for line in text.splitlines():
        times = [t for t in english_times(line) if t[1]]
        if not times:
            continue
        found = re.search(
            rf'(?P<coins>{TICKER}(?:\s*(?:,|and|&)\s*{TICKER})*)\s+(?:Spot\s+)?trading\s+'
            r'(?:pairs?\s+)?(?:will|has|have)\s+(?:be(?:en)?\s+)?'
            r'(?:suspended|ceased|closed|delisted|halted|removed)', line)
        if found:
            for coin in coin_names(found.group('coins')):
                rows.append(delist_row('mexc', coin, times[0][0], article, 'delist duyurusu'))
    if rows:
        return rows
    pairs = re.findall(rf'({TICKER})/(USDT|USDC|USD1|USDE|BTC|ETH)\b', text)
    if pairs and not any(quote == 'USDT' for _, quote in pairs):
        return []  # only pairs of other quotes; the coins keep trading
    raise Unreadable('delist satırları okunamadı')


MOVED = re.compile(
    r'(?:delist\w*|closing|trading|swap)[^.\n]{0,40}(?:dates?|times?)[^.\n]{0,20}'
    r'(?:has|have|had)\s+(?:been\s+)?(?:updated|changed|postponed|brought forward|amended)', re.I)


def moved_coins(text):
    """The coins whose time the text says was changed after it was first written: the
    time it gives for them is no longer the one that holds."""
    found = set()
    for line in text.splitlines():
        for sentence in re.split(r'(?<=[.!?])\s+', line):
            if MOVED.search(sentence):
                found |= set(re.findall(r'(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,19})(?![A-Za-z0-9])',
                                        sentence))
    return found


def binance_rows(articles, reader):
    rows, notes, counts = [], [], {'etkisiz': 0}
    done = {}
    for article in articles:
        title = article['title']
        if article['catalog'] == 161:
            if not re.search(r'\bWill Delist\b', title) or BINANCE_PRODUCT.match(title):
                continue
        elif not BINANCE_SWAP.search(title) or re.search(r'network migration', title, re.I) or \
                article['catalog'] == 49 and BINANCE_OTHER.search(title):
            continue
        try:
            text = reader.text(f"binance:{article['id']}", article['updated'],
                               lambda a=article: binance_text(a))
            moved = moved_coins(text)
            if article['catalog'] == 161:
                found = binance_delistings(article, text)
                rows += [row for row in found if row['coin'] not in moved]
                late = sorted({row['coin'] for row in found} & moved)
                if late:
                    notes.append((article, f"{', '.join(late)} için tarih sonradan değişmiş; "
                                           'güncel duyuruya bakın'))
                continue
            tickers = title_tickers(title)
            if not tickers:
                raise Unreadable('coin adları okunamadı')
            old, new = tickers[0], tickers[-1]
            rate = ratio_for(text, old, new)
            if re.match(r'Binance Has Completed', title, re.I):
                if rate:
                    done[(old, new)] = rate
                continue
            if old == new and (not rate or rate[0] == rate[1]) and \
                    not re.search(r'redenominat', title, re.I):
                counts['etkisiz'] += 1
                continue
            if not rate:
                raise Unreadable('oran okunamadı')
            if old in moved:
                raise Unreadable(f'{old} için tarih sonradan değişmiş; güncel duyuruya bakın')
            close = binance_close(text, old)
            if not close:
                raise Unreadable('kapanış saati okunamadı')
            note = 'eski coinin kapanışı'
            if re.search(r'\balpha\b', title, re.I):
                note += '; Binance Alpha, spot değil'
            rows.append(swap_row('binance', old, new, rate, close, article, note))
        except Unreadable as reason:
            notes.append((article, str(reason)))
    for row in rows:
        rate = done.get((row.get('old'), row.get('new')))
        if rate and rate_of(*rate)[1] != row['ratio']:
            text, row['ratio'] = rate_of(*rate)
            row['rate'] = text
            row['note'] += '; oran tamamlanma duyurusundan'
    return rows, notes, counts


def binance_close(text, old):
    coin = re.escape(old)
    for pattern in (
            rf'(?:At|Starting from|From) (\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}) \(UTC\),? Binance'
            rf'[\w .]{{0,20}}? will (?:temporarily )?(?:remove|delist|suspend|cease)[^\n]*?'
            rf'(?<![A-Z0-9]){coin}(?![A-Z0-9])',
            rf'Binance will (?:remove|delist|suspend|cease)[^\n]*?(?<![A-Z0-9]){coin}(?![A-Z0-9])'
            rf'[^\n]*? at (\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}) \(UTC\)'):
        found = re.search(pattern, text)
        if found:
            return datetime.strptime(found.group(1), '%Y-%m-%d %H:%M').replace(tzinfo=UTC)
    return None


def binance_delistings(article, text):
    found = re.search(r'\bWill Delist (.+?) on (\d{4}-\d{2}-\d{2})', article['title'])
    if not found:
        raise Unreadable('başlık okunamadı')
    coins = coin_names(found.group(1))
    if not coins or not all(re.fullmatch(TICKER, coin) for coin in coins):
        raise Unreadable('coin adları okunamadı')
    when = re.search(r'(?:delist|cease trading)[^\n]*?spot trading pairs[^\n]*?at '
                     r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \(UTC\)', text, re.I)
    if not when:
        raise Unreadable('saat okunamadı')
    moment = datetime.strptime(when.group(1), '%Y-%m-%d %H:%M').replace(tzinfo=UTC)
    if when.group(1)[:10] != found.group(2):
        raise Unreadable('başlıktaki gün metindekiyle tutmuyor')
    for coin in coins:
        if not re.search(rf'\({re.escape(coin)}\)|(?<![A-Z0-9]){re.escape(coin)}(?![A-Z0-9])', text):
            raise Unreadable(f'{coin} metinde geçmiyor')
    return [delist_row('binance', coin, moment, article, 'delist duyurusu') for coin in coins]


def paribu_close(text):
    """The coins whose TL markets close, with the time they close at."""
    times = turkish_times(text)
    found = []
    position = 0
    for segment in text.split('\n'):
        start, end = position, position + len(segment)
        position = end + 1
        # A market closed for a while opens again with the same coin.
        if not re.search(r'kapat|kapan', segment, re.I) or re.search(r'geçici', segment, re.I):
            continue
        markets = list(re.finditer(rf'({TICKER})/TL\b', segment))
        if not markets:
            continue
        at = start + markets[0].start()
        # The time said last before the market, else later in its paragraph, else in a
        # paragraph before ("on the same day and hour").
        before = [t for t, place in times if start <= place < at]
        after = [t for t, place in times if at <= place < end]
        earlier = [t for t, place in times if place < start]
        when = before[-1] if before else after[0] if after else earlier[-1] if earlier else None
        if when:
            found.append(([m.group(1) for m in markets], when))
    return found


def paribu_rows(articles, reader):
    rows, notes, counts = [], [], {'etkisiz': 0}
    for article in articles:
        title = article['title']
        named = PARIBU_TITLE.search(title)
        if not (named or PARIBU_UPDATE.search(title)) or not named and PARIBU_NOISE.search(title):
            continue
        text = ''
        try:
            text = reader.text(f"paribu:{article['id']}", article['updated'],
                               lambda a=article: paribu_text(a))
            closes = paribu_close(text)
            delisting = re.search(r'kaldırıl', title, re.I)
            if not closes:
                if not delisting and paribu_quiet(title, text):
                    counts['etkisiz'] += 1
                    continue
                raise Unreadable('piyasa kapanışı okunamadı')
            for coins, when in closes:
                local = when.astimezone(TURKEY)
                day = local.date() - timedelta(days=1) if (local.hour, local.minute) == (0, 0) \
                    else local.date()
                # The end of the Turkish day of the last candle.
                last = datetime(day.year, day.month, day.day, 20, 59, tzinfo=UTC)
                for old in dict.fromkeys(coins):
                    if delisting:
                        rows.append(delist_row('paribu', old, last, article, 'son mum günü'))
                        continue
                    new, rate = paribu_swap(title, text, old)
                    if not new or not rate:
                        raise Unreadable(f'{old} için yeni coin ya da oran okunamadı')
                    if old == new and rate[0] == rate[1]:
                        counts['etkisiz'] += 1
                        continue
                    note = 'son mum günü'
                    if re.search(r'yaklaşık\s*' + re.escape(rate_of(*rate)[0].split(':')[0]), text):
                        note += '; duyuruda oran "yaklaşık"'
                    rows.append(swap_row('paribu', old, new, rate, last, article, note))
        except Unreadable as reason:
            if named or PARIBU_SWAP_WORDS.search(text):
                notes.append((article, str(reason)))
    return rows, notes, counts


def paribu_quiet(title, text):
    """A new contract or network for the same coin, at 1:1."""
    tickers = set(title_tickers(title) + title_tickers(text))
    rates = [(a, b) for a, _, b, _, _ in ratios(text, turkish_amount)]
    return len(tickers) == 1 and bool(rates) and all(a == b for a, b in rates)


def paribu_swap(title, text, old):
    """The new coin of old and the ratio: "1 STG = 0,08634 ZRO", "Her 1 adet BTT için
    1000 adet BTTC", or "1:1 oranında" with the new coin named elsewhere."""
    found = ratios(text, turkish_amount)
    named = next((r for r in found if r[1] == old and r[3] and r[3] != old), None)
    if named:
        return named[3], (named[0], named[2])
    rate = next(((a, b) for a, at, b, bt, _ in found if at is None and bt is None), None)
    coin = re.escape(old)
    named = re.search(rf'(?<![A-Z0-9]){coin}\s*[-–]\s*({TICKER})(?![A-Z0-9])', text)
    if named:
        return named.group(1), rate
    others = [t for t in title_tickers(title) + title_tickers(text) if t != old]
    return (others[0] if others else old), rate


# --- Comparing with the lists -------------------------------------------------------

def load_lists():
    lists = {}
    for names in LISTS.values():
        for name in names:
            path = HERE / f'{name}.json'
            lists[name] = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    return lists


def event_of(row):
    if row['kind'] == 'undelist':
        return f"{row['list']}|{row['coin']}|çıkar"
    if row['kind'] == 'delist':
        return f"{row['list']}|{row['coin']}"
    return f"{row['list']}|{row['old']}>{row['new']}"


def key_of(row):
    if row['kind'] in ('delist', 'undelist'):
        return f"{event_of(row)}|{row['date']}"
    return f"{event_of(row)}|{row['date']}|{row['rate']}"


def same_ratio(a, b):
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def standing(row, lists):
    """Where a row stands against its list: ('listede', None), ('yeni', None),
    ('düzeltme', the row of the list that it would replace) or ('eski ad', None) for
    the delisting of a coin whose name the list already holds for an earlier one."""
    data = lists.get(row['list'], {})
    when = moment_of(row['date'])
    if row['kind'] == 'undelist':
        # Done once the list no longer holds the coin at that time.
        held = data.get('coins', {}).get(row['coin']) == row['date']
        return ('yeni', None) if held else ('listede', None)
    if row['kind'] == 'delist':
        have = data.get('coins', {}).get(row['coin'])
        if have == row['date']:
            return 'listede', None
        if not have:
            return 'yeni', None
        # The list holds one time for each name; the earlier coin keeps it.
        if abs((moment_of(have) - when).days) > 60:
            return 'eski ad', None
        return 'düzeltme', {'coin': row['coin'], 'date': have}
    near = [r for r in data.get('swaps', []) if r['old'] == row['old'] and r['new'] == row['new']
            and abs((moment_of(r['date']) - when).days) <= 60]
    if any(r['date'] == row['date'] and same_ratio(r['ratio'], row['ratio']) for r in near):
        return 'listede', None
    if near:
        return 'düzeltme', min(near, key=lambda r: abs((moment_of(r['date']) - when).total_seconds()))
    return 'yeni', None


# --- The coins that should be gone --------------------------------------------------

def binance_pairs(coin):
    """Every pair of the coin that Binance has or had."""
    if 'binance-all' not in _markets:
        found = {}
        for symbol in json.loads(fetch(BINANCE_SYMBOLS))['symbols']:
            found.setdefault(symbol['baseAsset'].upper(), []).append(
                (symbol['symbol'], f"{symbol['baseAsset'].upper()}/{symbol['quoteAsset']}"))
        _markets['binance-all'] = found
    return _markets['binance-all'].get(coin, [])


def last_candle(symbol, interval, start):
    """The open time (ms) of the last candle with trades from start (ms) on, or None."""
    last = None
    while True:
        candles = json.loads(fetch(BINANCE_CANDLES.format(symbol, interval, start)))
        traded = [c for c in candles if float(c[5]) > 0]
        if traded:
            last = traded[-1][0]
        if len(candles) < 1000:
            return last
        start = candles[-1][0] + 1


def binance_last_trade(coin, since):
    """The minute after the last trade of the coin on any of its pairs from the day of
    since on, with the pair; or None when it did not trade after that day."""
    day = since.replace(hour=0, minute=0, second=0)
    start = int(day.timestamp() * 1000)
    best = None
    for symbol, pair in binance_pairs(coin):
        last = last_candle(symbol, '1d', start)
        if last is not None and (best is None or last > best[0]):
            best = (last, symbol, pair)
    if best is None or best[0] <= start:
        return None
    # The day of the last trade, then its hour, then its minute.
    last_day, symbol, pair = best
    hour = last_candle(symbol, '1h', last_day) or last_day
    minute = last_candle(symbol, '1m', hour) or hour
    return datetime.fromtimestamp(minute / 1000, UTC) + timedelta(minutes=1), pair


def check_delistings(exchange, lists, state, now):
    """Looks whether the coins of the delisting list of an exchange, whose time has
    passed, are gone from it. A coin found gone is kept as checked, with the time of the
    list, and not looked at again; one still traded comes back as a draft out of the list."""
    name = LISTS[exchange][1]
    coins = lists.get(name, {}).get('coins', {})
    if state.get('check_rule') != CHECK_RULE:
        state['checked'] = {}
        state['check_rule'] = CHECK_RULE
    checked = state.setdefault('checked', {}).setdefault(name, {})
    for coin in [c for c in checked if c not in coins]:
        del checked[coin]
    counts = {'önceden doğrulanmış': 0, 'doğrulandı': 0, 'hâlâ işlemde': 0,
              'sonra da işlem görmüş': 0, 'duraklatılmış': 0, 'tarihi gelmemiş': 0}
    rows = []
    due = {}
    for coin, when in coins.items():
        if checked.get(coin) == when:
            counts['önceden doğrulanmış'] += 1
        elif moment_of(when) > now - GRACE:
            counts['tarihi gelmemiş'] += 1
        else:
            due[coin] = when
    if not due:
        return rows, counts
    pairs = markets(exchange)
    for coin, when in due.items():
        traded, paused = pairs.get(coin, ([], []))
        if traded:
            counts['hâlâ işlemde'] += 1
            rows.append({
                'list': name, 'kind': 'undelist', 'coin': coin, 'date': when,
                'note': 'delist günü geçti ama işlemde: ' + ', '.join(sorted(traded)[:4])
                        + (f' (+{len(traded) - 4})' if len(traded) > 4 else ''),
                'title': 'Delist günü geçtiği halde işlemde olan coinler',
                'announced': stamp(now), 'source': MARKET_PAGES[exchange],
                'sources': [MARKET_PAGES[exchange]], 'exchange': exchange, 'replaces': None,
            })
        elif paused:
            counts['duraklatılmış'] += 1  # looked at again on the next run
        elif exchange == 'binance' and (later := binance_last_trade(coin, moment_of(when))):
            # The USDT pair closed first; another pair went on trading.
            counts['sonra da işlem görmüş'] += 1
            moment, pair = later
            rows.append({
                'list': name, 'kind': 'delist', 'coin': coin, 'date': stamp(moment),
                'note': f'listedeki günden sonra da işlem görmüş; son işlem {pair}',
                'title': 'Listedeki delist gününden sonra da işlem gören coinler',
                'announced': stamp(now), 'source': MARKET_PAGES[exchange],
                'sources': [MARKET_PAGES[exchange]], 'exchange': exchange,
                'replaces': {'coin': coin, 'date': when}, 'check': True,
            })
        else:
            checked[coin] = when
            counts['doğrulandı'] += 1
    return rows, counts


# --- The drafts ---------------------------------------------------------------------

def load_state():
    state = json.loads(DRAFTS.read_text(encoding='utf-8')) if DRAFTS.exists() else {}
    state.setdefault('version', 1)
    for name in ('read', 'drafts', 'left', 'notes', 'checked'):
        state.setdefault(name, [] if name == 'left' else {})
    return state


def save(path, data):
    path.parent.mkdir(exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    path.write_text(text, encoding='utf-8', newline='\n')


def scan(days=None, keep=True, quiet=False, only=None):
    """Reads the announcements and refreshes the drafts. Returns the state, and how many
    drafts and notes came in new on this run."""
    say = (lambda *a: None) if quiet else print
    now = now_utc()
    _markets.clear()
    state = load_state()
    cache = json.loads(CACHE.read_text(encoding='utf-8')) if CACHE.exists() else {}
    reader = Reader(cache)
    lists = load_lists()
    left = set(state['left'])
    before = set(state['drafts'])
    before_notes = {k for k, n in state['notes'].items() if not n.get('seen')}
    seen_events = set()
    read_from = {}
    sections = set(state.get('mexc_st_sections') or ST_SECTIONS)
    readers = {
        'mexc': (mexc_articles, lambda a: mexc_rows(a, reader, now, since, sections)),
        'binance': (binance_articles, lambda a: binance_rows(a, reader)),
        'paribu': (paribu_articles, lambda a: paribu_rows(a, reader)),
    }
    chosen = [only] if only else list(LISTS)
    for exchange, (articles_of, rows_of) in readers.items():
        if exchange not in chosen:
            continue
        back = max(LOOKBACK, days or 0)
        read = state['read'].get(exchange)
        since = now - timedelta(days=back)
        if read:
            since = min(since, moment_of(read) - timedelta(days=2))
        try:
            articles = articles_of(stamp(since))
            rows, notes, counts = rows_of(articles)
        except (OSError, ValueError, KeyError, TypeError, IndexError, Unreadable) as error:
            say(f'{NAMES[exchange]}: duyurular okunamadı ({error}). Sonraki çalışmada yeniden denenir.')
            continue
        state['read'][exchange] = stamp(now)
        read_from[exchange] = stamp(since)
        added = {'yeni': 0, 'düzeltme': 0, 'listede': 0, 'eski ad': 0}
        for row in rows:
            event = event_of(row)
            where, replaces = standing(row, lists)
            added[where] += 1
            if where in ('listede', 'eski ad'):
                seen_events.add(event)
                continue
            if key_of(row) in left:
                continue
            seen_events.add(event)
            state['drafts'][event] = {**row, 'replaces': replaces, 'exchange': exchange}
        for article, reason in notes:
            key = f"{exchange}:{article['id']}"
            if key not in state['notes']:
                state['notes'][key] = {
                    'exchange': exchange, 'title': article['title'], 'announced': article['created'],
                    'source': article['url'], 'reason': reason, 'seen': False}
        extra = ', '.join(f'{v} {k}' for k, v in counts.items() if v)
        say(f"{NAMES[exchange]}: {len(articles)} duyuru, {len(rows)} satır "
            f"({added['listede']} listede, {added['yeni']} yeni, {added['düzeltme']} düzeltme"
            + (f", {added['eski ad']} aynı adlı eski coin" if added['eski ad'] else '') + ')'
            + (f'; {extra}' if extra else '') + (f'; {len(notes)} elle bakılacak' if notes else ''))
    state['mexc_st_sections'] = sorted(sections)
    checked = set()
    for exchange in chosen:
        try:
            rows, counts = check_delistings(exchange, lists, state, now)
        except (OSError, ValueError, KeyError, TypeError) as error:
            say(f'{NAMES[exchange]}: pariteler okunamadı ({error}); delist kontrolü sonraki '
                'çalışmada yapılır.')
            continue
        checked.add(exchange)
        for row in rows:
            event = event_of(row)
            seen_events.add(event)
            if key_of(row) not in left:
                state['drafts'][event] = row
        say(f'{NAMES[exchange]} delist kontrolü: '
            + ', '.join(f'{v} {k}' for k, v in counts.items() if v))
    # A draft that the list now holds, by hand or otherwise, is done; so is one whose
    # announcement was read again without giving it, or a coin found gone after all.
    for event, row in list(state['drafts'].items()):
        if event in seen_events:
            continue
        exchange = row.get('exchange')
        if standing(row, lists)[0] == 'listede' or \
                (row['kind'] == 'undelist' or row.get('check')) and exchange in checked or \
                row['kind'] != 'undelist' and not row.get('check') and exchange in read_from and \
                row['announced'] >= read_from[exchange]:
            del state['drafts'][event]
    save(CACHE, {key: cache[key] for key in cache if key in reader.used})
    if keep:
        save(DRAFTS, state)
    new_drafts = len(set(state['drafts']) - before)
    new_notes = len({k for k, n in state['notes'].items() if not n.get('seen')} - before_notes)
    return state, new_drafts, new_notes


# --- Showing and taking the drafts --------------------------------------------------

def describe(row):
    if row['kind'] == 'undelist':
        return f"listeden çıkar {row['coin']} (delist {row['date'][:16].replace('T', ' ')}Z)"
    if row['kind'] == 'delist':
        text = f"delist {row['coin']} {row['date'][:16].replace('T', ' ')}Z"
        if row.get('replaces'):
            text += f" (listede {row['replaces']['date'][:16].replace('T', ' ')}Z)"
        return text
    text = f"swap {row['old']} → {row['new']} {row['rate']} {row['date'][:16].replace('T', ' ')}Z"
    old = row.get('replaces')
    if old:
        text += f" (listede {old['rate']} {old['date'][:16].replace('T', ' ')}Z)"
    return text


def ordered(state):
    return sorted(state['drafts'].values(),
                  key=lambda r: (r['exchange'], r['announced'], r['source'], r['list'],
                                 r.get('coin') or r.get('old'), r['date']))


def show(state):
    """Prints the waiting drafts, numbered, under the announcements they come from."""
    rows = ordered(state)
    if not rows:
        print('Bekleyen taslak yok.')
    else:
        print(f'{len(rows)} taslak satır (listelere işlenmedi, yayımlanmadı):')
        source = None
        for number, row in enumerate(rows, 1):
            if row['source'] != source:
                source = row['source']
                print(f"  {NAMES[row['exchange']]} {row['announced'][:10]} {row['title']}")
                for url in row.get('sources') or [source]:
                    print(f'    {url}')
            sign = '-' if row['kind'] == 'undelist' else '~' if row.get('replaces') else '+'
            print(f"    {number:>3}. {sign} {row['list']}: {describe(row)}  [{row['note']}]")
    notes = [n for n in state['notes'].values() if not n.get('seen')]
    if notes:
        print(f'Elle bakılacak {len(notes)} duyuru (satır çıkarılamadı):')
        for note in sorted(notes, key=lambda n: n['announced']):
            print(f"  {NAMES[note['exchange']]} {note['announced'][:10]} {note['title']} "
                  f"[{note['reason']}]")
            print(f"    {note['source']}")
    return rows


def applied(lists, rows):
    """New copies of the lists with the rows taken in; the lists given stay as they are."""
    out = json.loads(json.dumps(lists))
    touched = set()
    for row in rows:
        name = row['list']
        data = out.setdefault(name, {'version': 1})
        touched.add(name)
        if name.endswith('_delist'):
            coins = data.setdefault('coins', {})
            if row['kind'] == 'undelist':
                coins.pop(row['coin'], None)
            else:
                coins[row['coin']] = row['date']
            continue
        swaps = data.setdefault('swaps', [])
        old = row.get('replaces')
        if old:
            swaps[:] = [r for r in swaps if not (
                r['old'] == row['old'] and r['new'] == row['new'] and r['date'] == old['date'])]
        swaps.append({'old': row['old'], 'new': row['new'], 'ratio': row['ratio'],
                      'rate': row['rate'], 'date': row['date']})
    for name in touched:
        data = out[name]
        if name.endswith('_delist'):
            data['coins'] = dict(sorted(data['coins'].items(), key=lambda item: (item[1], item[0])))
        else:
            data['swaps'].sort(key=lambda r: r['date'])
    return out


def pick(state, rows):
    """Asks of each draft whether it goes in. Returns those taken; a draft refused is not
    shown again, one passed over waits for the next time."""
    taken = []
    for number, row in enumerate(rows, 1):
        answer = input(f"  {number}. {row['list']}: {describe(row)}  alınsın mı? "
                       '(e = evet, h = hayır, bir daha gösterme, Enter = sonra): ').strip().lower()
        if answer == 'e':
            taken.append(row)
        elif answer == 'h':
            leave(state, row)
    return taken


def settle(state, taken):
    """Forgets the drafts that went into the published lists, and the notes shown."""
    for row in taken:
        state['drafts'].pop(event_of(row), None)
    for note in state['notes'].values():
        note['seen'] = True


def leave(state, row):
    state['left'].append(key_of(row))
    state['drafts'].pop(event_of(row), None)
    if row.get('check'):
        state.setdefault('checked', {}).setdefault(row['list'], {})[row['coin']] = \
            row['replaces']['date']


def notify(drafts, notes):
    """A Windows notification, for the runs that nobody watches."""
    words = []
    if drafts:
        words.append(f'{drafts} yeni taslak')
    if notes:
        words.append(f'{notes} elle bakılacak duyuru')
    message = ', '.join(words) + '. Görmek için: python guncelle.py'
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] > $null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x = $t.GetElementsByTagName('text');"
        "$x.Item(0).AppendChild($t.CreateTextNode($env:TOAST_TITLE)) > $null;"
        "$x.Item(1).AppendChild($t.CreateTextNode($env:TOAST_TEXT)) > $null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($t))")
    env = {**os.environ, 'TOAST_TITLE': 'Borsa listeleri', 'TOAST_TEXT': message}
    subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                   env=env, capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def main():
    if sys.stdout is None:
        # Run by pythonw, from the task scheduler: the output goes to a log.
        SOURCE.mkdir(exist_ok=True)
        sys.stdout = sys.stderr = open(SOURCE / 'duyuru_tarama.log', 'a', encoding='utf-8')
        print(f'--- {stamp(now_utc())}')
    else:
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--gun', type=int, help='bu kadar gün geriye de bakar')
    parser.add_argument('--borsa', choices=sorted(LISTS), help='yalnız bu borsayı okur')
    parser.add_argument('--deneme', action='store_true', help='yalnız gösterir, bir şey yazmaz')
    parser.add_argument('--bildir', action='store_true',
                        help='yeni taslak gelince Windows bildirimi gösterir')
    args = parser.parse_args()
    state, drafts, notes = scan(args.gun, keep=not args.deneme, only=args.borsa)
    show(state)
    if args.bildir and (drafts or notes):
        notify(drafts, notes)


if __name__ == '__main__':
    main()

"""Builds the public lists from the two spreadsheets, shows what changed and, once
confirmed, publishes them.

    python guncelle.py            shows the changes, asks, and publishes
    python guncelle.py --deneme   only shows the changes

The spreadsheets stay in the kaynak folder, which is not published:
    kaynak/binance_spot_delist.xlsx   Coin, Delist tarihi
    kaynak/binance_spot_swap.xlsx     Old coin, New coin, Rate, Old Coin Closing Date
Rate is old:new, so 100:1 means 100 old coins became 1 new coin.

The lists of Paribu and MEXC are edited by hand in this folder, and published with the
rest. So that nothing of MEXC is missed, its new swap, rename and delisting announcements
are listed first, with their links.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import date
from fractions import Fraction
from pathlib import Path

import openpyxl

sys.stdout.reconfigure(encoding='utf-8')
HERE = Path(__file__).resolve().parent
DELIST = 'binance_spot_delist'
SWAP = 'binance_spot_swap'
BY_HAND = ('paribu_delist', 'paribu_swap', 'mexc_delist', 'mexc_swap')
MEXC_NEWS = ('https://www.mexc.com/help/announce/api/en-US/section/360000254192/'
             'articles?page={}&perPage=100')
MEXC_ARTICLE = 'https://www.mexc.com/announcements/article/{}'
# How far MEXC's announcements were gone through. The folder is not published.
NEWS_SEEN = HERE / 'kaynak' / 'mexc_son_duyuru.txt'
NEWS_FROM = '2026-11-07T00:00:00Z'
NEWS_WORDS = re.compile(
    r'token swap|contract swap|swap (?:of|to|into)|migration|rebrand|renam|ticker|'
    r'redenominat|token merge|merger|token split|delist|suspension of', re.I)
NEWS_NOISE = re.compile(
    r'will list|futures|perpetual|kickstarter|launches|airdrop|\betf\b|margin|convert|'
    r'guide|tutorial|competition|carnival', re.I)
# The app refuses a file that lost more than this share of its rows.
KEPT_SHARE = 0.8


def stop(message):
    print(f'DURDU: {message}')
    sys.exit(1)


def rows(folder, name):
    path = folder / f'{name}.xlsx'
    if not path.exists():
        stop(f'{path} bulunamadı.')
    try:
        sheet = openpyxl.load_workbook(path, data_only=True).active
    except PermissionError:
        stop(f'{path.name} açık görünüyor. Excel\'de kapatıp yeniden deneyin.')
    found = list(sheet.iter_rows(values_only=True))[1:]
    return [row for row in found if any(cell is not None for cell in row)]


def stamp(value, where):
    if not hasattr(value, 'strftime'):
        stop(f'{where}: tarih okunamadı ({value!r}).')
    return value.strftime('%Y-%m-%dT%H:%M:%SZ')


def ratio(text, where):
    try:
        old, new = (Fraction(str(part).strip().replace(',', '.')) for part in str(text).split(':'))
        value = float(new / old)
    except (ValueError, ZeroDivisionError):
        stop(f'{where}: oran okunamadı ({text!r}). Beklenen biçim 100:1 ya da 1:3,26.')
    if value <= 0:
        stop(f'{where}: oran sıfırdan büyük olmalı ({text!r}).')
    return value


def coin(value, where):
    text = str(value or '').strip().upper()
    if not text:
        stop(f'{where}: coin adı boş.')
    return text


def build_delist(found):
    coins = {}
    ordered = sorted(found, key=lambda r: (str(r[1]), str(r[0])))
    for name, when in ordered:
        key = coin(name, 'Delist')
        time = stamp(when, f'Delist {key}')
        if coins.get(key, time) != time:
            stop(f'{key} iki farklı tarihle yazılmış: {coins[key]} ve {time}.')
        coins[key] = time
    return {'version': 1, 'coins': coins}


def build_swap(found):
    swaps = []
    seen = set()
    for old, new, rate, when in sorted(found, key=lambda r: str(r[3])):
        old_coin = coin(old, 'Swap')
        new_coin = coin(new, f'Swap {old_coin}')
        where = f'Swap {old_coin}→{new_coin}'
        item = {
            'old': old_coin,
            'new': new_coin,
            'ratio': ratio(rate, where),
            'rate': str(rate),
            'date': stamp(when, where),
        }
        key = (old_coin, new_coin, item['date'])
        if key in seen:
            stop(f'{where} aynı tarihle iki kez yazılmış.')
        seen.add(key)
        swaps.append(item)
    return {'version': 1, 'swaps': swaps}


def current(name):
    path = HERE / f'{name}.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def show_delist(before, after):
    old, new = before.get('coins', {}), after['coins']
    lines = [f'  + delist {k} {new[k][:10]}' for k in new if k not in old]
    lines += [f'  - delist {k} {old[k][:10]}' for k in old if k not in new]
    lines += [f'  ~ delist {k} {old[k][:10]} → {new[k][:10]}' for k in new if k in old and old[k] != new[k]]
    return lines


def show_swap(before, after):
    def by_key(data):
        return {(r['old'], r['new'], r['date']): r for r in data.get('swaps', [])}

    old, new = by_key(before), by_key(after)

    def line(sign, r):
        return f"  {sign} swap {r['old']} → {r['new']} {r['rate']} {r['date'][:10]}"

    lines = [line('+', new[k]) for k in new if k not in old]
    lines += [line('-', old[k]) for k in old if k not in new]
    lines += [line('~', new[k]) for k in new if k in old and old[k]['ratio'] != new[k]['ratio']]
    return lines


def write(name, data):
    text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    (HERE / f'{name}.json').write_text(text, encoding='utf-8', newline='\n')


def git(*args):
    done = subprocess.run(['git', *args], cwd=HERE, text=True, capture_output=True)
    if done.returncode != 0:
        stop(f'git {args[0]} başarısız: {done.stderr.strip()}')
    return done.stdout.strip()


def mexc_news():
    """The announcements that may change MEXC's lists, since the last look."""
    seen = NEWS_SEEN.read_text(encoding='utf-8').strip() if NEWS_SEEN.exists() else NEWS_FROM
    found, newest = {}, seen
    for page in range(1, 41):
        request = urllib.request.Request(MEXC_NEWS.format(page),
                                         headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                articles = json.load(response)['data']['results']
        except (OSError, ValueError, KeyError) as error:
            print(f'MEXC duyuruları okunamadı: {error}')
            return [], seen
        fresh = [row for row in articles if (row.get('createdAt') or '') > seen]
        for row in fresh:
            newest = max(newest, row['createdAt'])
            title = row.get('title') or ''
            if NEWS_WORDS.search(title) and not NEWS_NOISE.search(title):
                found[row['id']] = row
        # The first page starts with pinned rows of any age.
        if page > 1 and not fresh:
            break
    return sorted(found.values(), key=lambda row: row['createdAt']), newest


def show_mexc_news(trying):
    news, newest = mexc_news()
    if news:
        print(f'MEXC: son bakıştan beri {len(news)} duyuru. Gerekenleri mexc_swap.json ya da '
              'mexc_delist.json dosyasına ekleyin:')
        for row in news:
            print(f"  {row['createdAt'][:10]} {row['title']}")
            print(f"    {MEXC_ARTICLE.format(row['id'])}")
    else:
        print('MEXC: yeni swap ya da delist duyurusu yok.')
    if trying:
        return
    if news and input('Bu duyurular bir daha gösterilmesin mi? (e/h): ').strip().lower() != 'e':
        return
    NEWS_SEEN.parent.mkdir(exist_ok=True)
    NEWS_SEEN.write_text(newest + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deneme', action='store_true')
    parser.add_argument('--kaynak', default=str(HERE / 'kaynak'))
    args = parser.parse_args()
    folder = Path(args.kaynak)

    show_mexc_news(args.deneme)
    delist = build_delist(rows(folder, DELIST))
    swap = build_swap(rows(folder, SWAP))
    old_delist, old_swap = current(DELIST), current(SWAP)

    if len(delist['coins']) < len(old_delist.get('coins', {})) * KEPT_SHARE:
        stop('Delist listesi çok kısaldı; uygulama bu dosyayı reddeder.')
    if len(swap['swaps']) < len(old_swap.get('swaps', [])) * KEPT_SHARE:
        stop('Swap listesi çok kısaldı; uygulama bu dosyayı reddeder.')

    changes = show_delist(old_delist, delist) + show_swap(old_swap, swap)
    print(f"Delist: {len(delist['coins'])} coin, swap: {len(swap['swaps'])} kayıt")
    for name in BY_HAND:
        json.loads((HERE / f'{name}.json').read_text(encoding='utf-8'))
    waiting = git('status', '--porcelain', '--', *[f'{n}.json' for n in BY_HAND])
    changes += [f'  * {line[3:]}' for line in waiting.splitlines()]
    if not changes:
        print('Değişiklik yok.')
        return
    print('Değişiklikler:')
    print('\n'.join(changes))
    if args.deneme:
        print('Deneme: hiçbir şey yazılmadı.')
        return
    if input('Yayımlansın mı? Dosyalar herkese açık olur. (e/h): ').strip().lower() != 'e':
        print('Vazgeçildi.')
        return

    write(DELIST, delist)
    write(SWAP, swap)
    # The copy that comes with the app follows, for its next release.
    bundled = HERE.parent / 'kripto_portfoy' / 'assets' / 'lists'
    if bundled.exists():
        for name in (DELIST, SWAP, *BY_HAND):
            shutil.copyfile(HERE / f'{name}.json', bundled / f'{name}.json')
    git('add', f'{DELIST}.json', f'{SWAP}.json', *[f'{n}.json' for n in BY_HAND],
        'guncelle.py', '.gitignore', 'README.md')
    git('commit', '-m', f'Update the lists of {date.today().isoformat()}')
    git('push', '-q', 'origin', 'main')
    print('Yayımlandı. Kullanıcılar en geç bir gün içinde alır.')


main()

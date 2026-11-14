"""Takes the drafts of the announcements into the lists, shows what changed and
publishes the lists. Running it is the consent to publish: it asks nothing.

    python guncelle.py            takes all the drafts and publishes
    python guncelle.py --deneme   only shows what would be published, writes nothing
    python guncelle.py --sec      asks of each draft first, then publishes

The lists are the JSON files of this folder, which the app reads; they are edited here.
First of all, duyurular.py reads the new announcements of the exchanges and drafts their
swaps and delistings, and the coins still traded after their delisting time. The drafts
are shown with their links and taken into the lists; each list is checked and compared
with what was last published, and a list that a person could not have meant (a time
written wrong, a ratio against its rate, a list much shorter) stops the run before
anything is written. A swap whose ratio is not 1:1 is marked among the changes, for a
misread ratio would spoil every account: --deneme shows them before.

The spreadsheets that once made the Binance lists stay in the kaynak folder, which is
not published, as an archive.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from fractions import Fraction
from pathlib import Path

import duyurular

sys.stdout.reconfigure(encoding='utf-8')
HERE = Path(__file__).resolve().parent
LISTS = ('binance_spot_delist', 'binance_spot_swap', 'paribu_delist', 'paribu_swap',
         'mexc_delist', 'mexc_swap')
# The app refuses a file that lost more than this share of its rows.
KEPT_SHARE = 0.8
TIME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z')


def stop(message):
    print(f'DURDU: {message}')
    sys.exit(1)


def current(name):
    path = HERE / f'{name}.json'
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        stop(f'{path.name} okunamadı: {error}')


def published(name):
    """The list as it was last published."""
    done = subprocess.run(['git', 'show', f'HEAD:{name}.json'], cwd=HERE,
                          capture_output=True, encoding='utf-8')
    return json.loads(done.stdout) if done.returncode == 0 else {}


def check(name, data):
    """Stops at a row that the app could not read, or would read wrong."""
    if data.get('version') != 1:
        stop(f'{name}: version 1 olmalı.')
    if name.endswith('_delist'):
        for coin, when in data.get('coins', {}).items():
            if not coin.strip() or coin != coin.strip().upper():
                stop(f'{name}: coin adı büyük harfle ve boşluksuz olmalı ({coin!r}).')
            if not isinstance(when, str) or not TIME.fullmatch(when):
                stop(f'{name} {coin}: tarih 2026-01-31T03:00:00Z biçiminde olmalı ({when!r}).')
        return
    for row in data.get('swaps', []):
        where = f"{name} {row.get('old')}→{row.get('new')}"
        for field in ('old', 'new'):
            value = row.get(field)
            if not isinstance(value, str) or not value.strip() or value != value.strip().upper():
                stop(f'{where}: {field} büyük harfle ve boşluksuz olmalı ({value!r}).')
        if not isinstance(row.get('date'), str) or not TIME.fullmatch(row['date']):
            stop(f"{where}: tarih 2026-01-31T03:00:00Z biçiminde olmalı ({row.get('date')!r}).")
        try:
            old, new = (Fraction(part.strip().replace(',', '.')) for part in row['rate'].split(':'))
            rate = float(new / old)
        except (KeyError, AttributeError, ValueError, ZeroDivisionError):
            stop(f"{where}: rate eski:yeni biçiminde olmalı ({row.get('rate')!r}).")
        ratio = row.get('ratio')
        if not isinstance(ratio, (int, float)) or ratio <= 0 or \
                abs(ratio - rate) > 1e-9 * max(1.0, abs(rate)):
            stop(f"{where}: ratio ({ratio!r}) rate ile ({row['rate']}) tutmuyor; ratio = yeni/eski.")


def size(data):
    return len(data.get('coins', {})) if 'coins' in data else len(data.get('swaps', []))


def moment(text):
    return text[:16].replace('T', ' ')


def show_delist(before, after, label):
    old, new = before.get('coins', {}), after.get('coins', {})
    lines = [f'  + {label} {k} {moment(new[k])}' for k in new if k not in old]
    lines += [f'  - {label} {k} {moment(old[k])}' for k in old if k not in new]
    lines += [f'  ~ {label} {k} {moment(old[k])} → {moment(new[k])}'
              for k in new if k in old and old[k] != new[k]]
    return lines


def show_swap(before, after, label):
    def by_key(data):
        return {(r['old'], r['new'], r['date']): r for r in data.get('swaps', [])}

    old, new = by_key(before), by_key(after)

    def line(sign, r):
        text = f"  {sign} {label} {r['old']} → {r['new']} {r['rate']} {moment(r['date'])}"
        return text + ('   ← oranı kontrol et' if sign != '-' and r['ratio'] != 1 else '')

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


def changes_of(lists):
    """Checks the lists and tells what changed since they were last published."""
    changes = []
    for name in LISTS:
        after = lists[name]
        check(name, after)
        before = published(name)
        if size(after) < size(before) * KEPT_SHARE:
            stop(f'{name} çok kısaldı ({size(before)} → {size(after)}); uygulama bu dosyayı reddeder.')
        changes += (show_delist(before, after, name) if name.endswith('_delist')
                    else show_swap(before, after, name))
    return changes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deneme', action='store_true', help='yalnız gösterir, bir şey yazmaz')
    parser.add_argument('--sec', action='store_true', help='taslakları tek tek sorar')
    args = parser.parse_args()

    try:
        state, _, _ = duyurular.scan(keep=not args.deneme)
    except (OSError, ValueError) as error:
        print(f'Duyurular okunamadı ({error}); bekleyen taslaklar kullanılıyor.')
        state = duyurular.load_state()
    drafts = duyurular.show(state)
    taken = duyurular.pick(state, drafts) if args.sec and not args.deneme else drafts
    lists = duyurular.applied({name: current(name) for name in LISTS}, taken)
    changes = changes_of(lists)
    print()
    print('Listeler: ' + ', '.join(f'{name} {size(lists[name])}' for name in LISTS))
    if not changes:
        print('Değişiklik yok.')
        if not args.deneme:
            duyurular.save(duyurular.DRAFTS, state)
        return
    print(f'Değişiklikler ({len(taken)} taslak dahil):' if taken else 'Değişiklikler:')
    print('\n'.join(changes))
    if args.deneme:
        print('Deneme: hiçbir şey yazılmadı.')
        return

    for name in LISTS:
        write(name, lists[name])
    duyurular.settle(state, taken)
    duyurular.save(duyurular.DRAFTS, state)
    # The copy that comes with the app follows, for its next release.
    bundled = HERE.parent / 'kripto_portfoy' / 'assets' / 'lists'
    if bundled.exists():
        for name in LISTS:
            shutil.copyfile(HERE / f'{name}.json', bundled / f'{name}.json')
    git('add', *[f'{n}.json' for n in LISTS], 'guncelle.py', 'duyurular.py', '.gitignore',
        'README.md')
    git('commit', '-m', f'Update the lists of {date.today().isoformat()}')
    git('push', '-q', 'origin', 'main')
    print('Yayımlandı. Kullanıcılar en geç bir gün içinde alır.')


if __name__ == '__main__':
    main()

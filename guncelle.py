"""Takes the drafts of the announcements into the lists, shows what changed and
publishes the lists. Running it is the consent to publish: it asks nothing.

    python guncelle.py              takes all the drafts and publishes
    python guncelle.py --deneme     only shows what would be published, writes nothing
    python guncelle.py --sec        asks of each draft first, then publishes
    python guncelle.py --otomatik   the daily run of the task scheduler (see below)

The lists are the JSON files of this folder, which the app reads; they are edited here.
First of all, duyurular.py reads the new announcements of the exchanges and drafts their
swaps and delistings, and the coins still traded after their delisting time. The drafts
are shown with their links and taken into the lists; each list is checked and compared
with what was last published, and a list that a person could not have meant (a time
written wrong, a ratio against its rate, a list much shorter) stops the run before
anything is written. A swap whose ratio is not 1:1 is marked among the changes, for a
misread ratio would spoil every account: --deneme shows them before.

The task scheduler runs it every day with --otomatik, under pythonw, into the log
kaynak/duyuru_tarama.log. That run takes the drafts a person would take, but one: a swap
to a coin of another name whose ratio is not 1:1 waits until the market after the swap
bears its ratio out, and is taken by itself then. Until then its holders only miss a
price, while a misread ratio would count them wrong. A ratio the market does not bear
out, or could not, waits for a person: python guncelle.py takes it, --sec asks of it.
A coin that keeps its name is taken at once, for its step cannot be seen in the market
and without the row its holders would be counted wrong all the same. The run publishes
the lists only; when they hold changes made by hand it publishes nothing and leaves them
to a person. A Windows notification tells what it published and what waits for a person.
The copy that comes with the app is left alone, and so are the changes of the scripts;
python guncelle.py brings the copy up to date and publishes the scripts.

The spreadsheets that once made the Binance lists stay in the kaynak folder, which is
not published, as an archive.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from datetime import date
from fractions import Fraction
from pathlib import Path

import duyurular

HERE = Path(__file__).resolve().parent
LISTS = ('binance_spot_delist', 'binance_spot_swap', 'paribu_delist', 'paribu_swap',
         'mexc_delist', 'mexc_swap')
FILES = [f'{name}.json' for name in LISTS]
SCRIPTS = ['guncelle.py', 'duyurular.py', '.gitignore', 'README.md']
# The app refuses a file that lost more than this share of its rows.
KEPT_SHARE = 0.8
TIME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z')
BUNDLED = HERE.parent / 'kripto_portfoy' / 'assets' / 'lists'
# git opens no window under pythonw.
QUIET = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class Stop(Exception):
    """A list that a person could not have meant, or a step that failed."""


def stop(message):
    raise Stop(message)


def current(name):
    path = HERE / f'{name}.json'
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        stop(f'{path.name} okunamadı: {error}')


def run_git(*args):
    return subprocess.run(['git', *args], cwd=HERE, capture_output=True, encoding='utf-8',
                          errors='replace', creationflags=QUIET)


def git(*args):
    done = run_git(*args)
    if done.returncode != 0:
        stop(f'git {args[0]} başarısız: {(done.stderr or done.stdout).strip()}')
    return done.stdout


def published(name):
    """The list as it was last published."""
    done = run_git('show', f'HEAD:{name}.json')
    return json.loads(done.stdout) if done.returncode == 0 else {}


def hand_edits():
    """The lists that hold changes not yet committed."""
    return [line.split()[-1] for line in git('status', '--porcelain', '--', *FILES).splitlines()
            if line.strip()]


def unpushed():
    done = run_git('rev-list', '--count', '@{u}..HEAD')
    return int(done.stdout.strip() or 0) if done.returncode == 0 else 0


def push():
    git('push', '-q', 'origin', 'main')


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


def show_swap(before, after, label, marks=None):
    def by_key(data):
        return {(r['old'], r['new'], r['date']): r for r in data.get('swaps', [])}

    old, new = by_key(before), by_key(after)

    def line(sign, r):
        text = f"  {sign} {label} {r['old']} → {r['new']} {r['rate']} {moment(r['date'])}"
        if sign == '-':
            return text
        mark = (marks or {}).get((label, r['old'], r['new'], r['date']))
        if mark and mark['state'] == 'doğrulandı':
            return text + f"   ✓ piyasa oranı {mark['market']}"
        if mark and mark['state'] == 'şüpheli':
            return text + f"   ← piyasa oranı {mark['market']}, tutmuyor"
        if mark and mark['state'] == 'bekliyor':
            return text + '   (oran swap sonrası piyasadan doğrulanacak)'
        return text + ('   ← oranı kontrol et' if r['ratio'] != 1 else '')

    lines = [line('+', new[k]) for k in new if k not in old]
    lines += [line('-', old[k]) for k in old if k not in new]
    lines += [line('~', new[k]) for k in new if k in old and old[k]['ratio'] != new[k]['ratio']]
    return lines


def write(name, data):
    text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    (HERE / f'{name}.json').write_text(text, encoding='utf-8', newline='\n')


def changes_of(lists, marks=None):
    """Checks the lists and tells what changed since they were last published."""
    changes = []
    for name in LISTS:
        after = lists[name]
        check(name, after)
        before = published(name)
        if size(after) < size(before) * KEPT_SHARE:
            stop(f'{name} çok kısaldı ({size(before)} → {size(after)}); uygulama bu dosyayı reddeder.')
        changes += (show_delist(before, after, name) if name.endswith('_delist')
                    else show_swap(before, after, name, marks))
    return changes


def held(row):
    """Why the daily run leaves a draft: ('sonra', why) for one it takes by itself on a
    later day, ('kişi', why) for one that waits for a person; None for one it takes."""
    market = (row.get('market') or {}).get('state')
    if market == 'şüpheli':
        return 'kişi', 'oranı piyasayla tutmuyor'
    if row['kind'] != 'swap' or row.get('fix') or row['ratio'] == 1 or \
            row['old'] == row['new'] or market == 'doğrulandı':
        return None
    if market == 'doğrulanamadı':
        return 'kişi', 'oranı piyasadan doğrulanamadı'
    return 'sonra', 'swap sonrası piyasa oranı tutunca alınacak'


def sync_bundled():
    """Brings the copy that comes with the app up to date, for its next release."""
    if not BUNDLED.exists():
        return
    changed = []
    for name in FILES:
        mine, theirs = HERE / name, BUNDLED / name
        if not theirs.exists() or theirs.read_bytes() != mine.read_bytes():
            shutil.copyfile(mine, theirs)
            changed.append(name)
    if changed:
        print('Uygulamadaki kopya güncellendi: ' + ', '.join(changed))


def run(args, news):
    """One run; what a notification should tell goes into news."""
    auto = args.otomatik
    try:
        state, _, new_notes = duyurular.scan(keep=not args.deneme)
    except (OSError, ValueError) as error:
        print(f'Duyurular okunamadı ({error}); bekleyen taslaklar kullanılıyor.')
        state, new_notes = duyurular.load_state(), 0
    drafts = duyurular.show(state)
    if args.sec and not args.deneme:
        taken = duyurular.pick(state, drafts)
    elif auto:
        taken, asks = [], []
        for row in drafts:
            why = held(row)
            if why is None:
                taken.append(row)
                continue
            print(f"Bekletildi: {row['list']}: {duyurular.describe(row)} ({why[1]})")
            if why[0] == 'kişi':
                asks.append(duyurular.key_of(row))
        fresh = [key for key in asks if key not in state.get('told', [])]
        state['told'] = asks
        if fresh:
            news.append(f'{len(fresh)} taslak onayını bekliyor')
        if new_notes:
            news.append(f'{new_notes} elle bakılacak duyuru')
    else:
        # A swap whose ratio the market does not bear out waits; --sec may still take it.
        taken = [row for row in drafts if (row.get('market') or {}).get('state') != 'şüpheli']
        if len(taken) < len(drafts):
            print(f'{len(drafts) - len(taken)} taslak bekletildi: oranı piyasayla tutmuyor.')
    lists = duyurular.applied({name: current(name) for name in LISTS}, taken)
    changes = changes_of(lists, duyurular.ratio_marks(state))
    print()
    print('Listeler: ' + ', '.join(f'{name} {size(lists[name])}' for name in LISTS))
    if not changes:
        print('Değişiklik yok.')
        if args.deneme:
            return
        scripts = False
        if not auto:
            duyurular.settle(state, [])
            sync_bundled()
            # The scripts go out with the lists; with no list to go, they go alone.
            if git('status', '--porcelain', '--', *SCRIPTS).strip():
                git('add', *SCRIPTS)
                git('commit', '-q', '-m', 'Update the scripts')
                scripts = True
        duyurular.save(duyurular.DRAFTS, state)
        if unpushed():
            # An earlier run committed but could not push, or the scripts were committed.
            push()
            if scripts:
                print('Script değişiklikleri yayımlandı.')
            else:
                print('Önceki çalışmada yayımlanamayan değişiklikler yayımlandı.')
                news.insert(0, 'Bekleyen değişiklikler yayımlandı')
        return
    print(f'Değişiklikler ({len(taken)} taslak dahil):' if taken else 'Değişiklikler:')
    print('\n'.join(changes))
    if args.deneme:
        print('Deneme: hiçbir şey yazılmadı.')
        return
    if auto:
        edited = hand_edits()
        if edited:
            duyurular.save(duyurular.DRAFTS, state)
            stop('listelerde elle yapılmış, yayımlanmamış değişiklik var ('
                 + ', '.join(edited) + '); python guncelle.py ile yayımla')

    for name in LISTS:
        write(name, lists[name])
    duyurular.settle(state, taken, notes_seen=not auto)
    duyurular.save(duyurular.DRAFTS, state)
    if auto:
        git('add', *FILES)
    else:
        sync_bundled()
        git('add', *FILES, *SCRIPTS)
    git('commit', '-q', '-m', f'Update the lists of {date.today().isoformat()}')
    news.insert(0, f'{len(changes)} değişiklik yayımlandı')
    try:
        push()
    except Stop as error:
        news[0] = f'{len(changes)} değişiklik kaydedildi ama gönderilemedi'
        stop(f'{error}; sonraki çalışmada yeniden denenecek')
    print('Yayımlandı. Kullanıcılar en geç bir gün içinde alır.')


def tell(news, hint=''):
    if news:
        duyurular.toast(', '.join(news) + '.' + (f' {hint}' if hint else ''))


def main():
    if sys.stdout is None:
        duyurular.to_log()
    else:
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--deneme', action='store_true', help='yalnız gösterir, bir şey yazmaz')
    parser.add_argument('--sec', action='store_true', help='taslakları tek tek sorar')
    parser.add_argument('--otomatik', action='store_true',
                        help='Görev Zamanlayıcı çalışması: kişi beklemeyen taslakları alır, '
                             'listeleri yayımlar, Windows bildirimi gösterir')
    args = parser.parse_args()
    if args.sec and args.otomatik:
        parser.error('--sec ile --otomatik birlikte kullanılmaz')
    if args.otomatik:
        # Nobody answers a question: git fails at once rather than waits.
        os.environ.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
    news = []
    try:
        run(args, news)
    except Stop as error:
        print(f'DURDU: {error}')
        if args.otomatik and not args.deneme:
            if not news or 'gönderilemedi' not in news[0]:
                news.insert(0, f'Yayımlanmadı: {str(error)[:160]}')
            tell(news, 'Ayrıntı: kaynak/duyuru_tarama.log')
        sys.exit(1)
    except Exception:
        if not args.otomatik:
            raise
        traceback.print_exc()
        if not args.deneme:
            duyurular.toast('Liste güncellemesi hata verdi; ayrıntı kaynak/duyuru_tarama.log')
        sys.exit(1)
    if args.otomatik and not args.deneme:
        waits = any('bekliyor' in n or 'elle' in n for n in news)
        tell(news, 'Görmek için: python guncelle.py --deneme' if waits else '')


if __name__ == '__main__':
    main()

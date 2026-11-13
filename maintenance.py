#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Maintenance — nettoyage, mises à jour et santé d'Arch Linux, pensé pour Cinnamon (GTK3).

Tout se fait dans une seule fenêtre. Le mot de passe administrateur est demandé
une seule fois à l'ouverture : un petit assistant lancé par sudo exécute ensuite
les commandes administrateur jusqu'à la fermeture de l'application.

Usage :
    maintenance              ouvre la fenêtre
    maintenance --version    affiche la version
"""

import os
import re
import sys
import json
import glob
import stat
import time
import shlex
import struct
import shutil
import signal
import socket
import hashlib
import platform
import threading
import queue
import subprocess
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

# ============================================================================
#  Assistant administrateur : lancé une seule fois par sudo à l'ouverture
# ============================================================================
def write_frame(f, kind, jid, data=b''):
    f.write(kind + struct.pack('!II', jid, len(data)) + data)
    f.flush()


def read_frame(f):
    head = f.read(9)
    if len(head) < 9:
        raise EOFError
    kind = head[:1]
    jid, n = struct.unpack('!II', head[1:])
    data = f.read(n) if n else b''
    if len(data) < n:
        raise EOFError
    return kind, jid, data


def root_helper_main():
    """Exécute en administrateur les commandes que l'application lui envoie sur son entrée.

    Seule l'application qui l'a lancé tient ses tubes : aucun autre programme ne peut lui parler.
    Quand l'application se ferme, l'entrée se ferme et l'assistant s'arrête après les commandes en cours.
    """
    out = sys.stdout.buffer
    lock = threading.Lock()
    procs = {}

    def send(kind, jid, data=b''):
        with lock:
            try:
                write_frame(out, kind, jid, data)
            except OSError:
                pass

    def job(jid, req):
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (req.get('env') or {}).items()})
        cwd = req.get('cwd')
        cwd = cwd if cwd and os.path.isdir(cwd) else '/'
        detach = bool(req.get('detach'))
        try:
            p = subprocess.Popen([str(a) for a in req['argv']], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL if detach else subprocess.PIPE,
                                 stderr=subprocess.DEVNULL if detach else subprocess.STDOUT,
                                 cwd=cwd, env=env, start_new_session=True)
        except (OSError, KeyError, TypeError) as e:
            send(b'O', jid, f'{e}\n'.encode())
            send(b'X', jid, struct.pack('!i', 127))
            return
        if detach:
            send(b'X', jid, struct.pack('!i', 0))
            p.wait()
            return
        procs[jid] = p
        for chunk in iter(lambda: p.stdout.read1(65536), b''):
            send(b'O', jid, chunk)
        rc = p.wait()
        procs.pop(jid, None)
        send(b'X', jid, struct.pack('!i', rc))

    def kill(jid):
        p = procs.get(jid)
        if p is None:
            return
        for sig, wait in ((signal.SIGTERM, 4), (signal.SIGKILL, 0)):
            try:
                os.killpg(p.pid, sig)
            except OSError:
                return
            try:
                p.wait(timeout=wait or None)
                return
            except subprocess.TimeoutExpired:
                continue

    send(b'R', 0)
    threads = []
    for line in sys.stdin.buffer:
        try:
            req = json.loads(line)
            jid = int(req.get('id', 0))
        except (ValueError, TypeError, AttributeError):
            continue
        if req.get('op') == 'ping':
            send(b'X', jid, struct.pack('!i', 0))
            continue
        if req.get('op') == 'kill':
            threading.Thread(target=kill, args=(jid,), daemon=True).start()
            continue
        t = threading.Thread(target=job, args=(jid, req))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return 0


if __name__ == '__main__' and sys.argv[1:2] == ['--root-helper']:
    sys.exit(root_helper_main())

import gi  # noqa: E402
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, Gio, GLib, Pango  # noqa: E402

APP_ID = 'io.github.theworms.Maintenance'
VERSION = '1.5.1'
REPO_URL = 'https://github.com/TheWorms/maintenance'
REPO_API = 'https://api.github.com/repos/TheWorms/maintenance'
PKG_NAME = 'maintenance-git'
HOME = os.path.expanduser('~')
XDG_CONFIG = os.environ.get('XDG_CONFIG_HOME') or os.path.join(HOME, '.config')
XDG_CACHE = os.environ.get('XDG_CACHE_HOME') or os.path.join(HOME, '.cache')
CONF_FILE = os.path.join(XDG_CONFIG, 'maintenance', 'reglages.json')
RUNTIME = os.environ.get('XDG_RUNTIME_DIR') or '/tmp'
SELF = os.path.realpath(__file__)
PACMAN_LOCK = '/var/lib/pacman/db.lck'
PKG_CACHE = '/var/cache/pacman/pkg'
MIRRORLIST = '/etc/pacman.d/mirrorlist'
APPS_PREFIX = '/usr/share/applications/'
TRASH_DIR = os.path.join(HOME, '.local', 'share', 'Trash')
BUILD_DIR = os.path.join(XDG_CACHE, 'maintenance', 'paquet')
SESSION_DIR = os.path.join(RUNTIME, f'maintenance-{os.getuid()}')
RELAY_SOCK = os.path.join(SESSION_DIR, 'sudo.sock')
JOURNAL_DAYS = 7
CACHE_AGE_DAYS = 30
MIRROR_COUNTRIES = 'France,Germany,Belgium,Switzerland'
KERNELS = ('linux', 'linux-lts', 'linux-zen', 'linux-hardened', 'linux-rt', 'linux-rt-lts')

# Paquets que l'application refuse de désinstaller
PROTECTED_RE = re.compile(
    r'^(base|base-devel|linux|linux-lts|linux-zen|linux-hardened|linux-rt|linux-firmware|'
    r'pacman|pacman-mirrorlist|archlinux-keyring|yay|yay-bin|paru|sudo|bash|systemd|glibc|'
    r'filesystem|coreutils|grub|efibootmgr|mkinitcpio|dracut|cinnamon|cinnamon-session|'
    r'cinnamon-desktop|muffin|nemo|lightdm|lightdm-slick-greeter|xorg-server|networkmanager|'
    r'python|python-gobject|gtk3|maintenance-git)$')
# Dossiers où la recherche de doublons est refusée
PROTECTED_DIRS = ('/', '/usr', '/etc', '/boot', '/bin', '/sbin', '/lib', '/lib64',
                  '/proc', '/sys', '/dev', '/run', '/var')
DUP_SKIP = {'.git', 'node_modules', '.cache', 'Trash', '.Trash'}

ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
POOL = ThreadPoolExecutor(max_workers=4)


# ============================================================================
#  Utilitaires
# ============================================================================
def run(argv, timeout=90, c_locale=True):
    """Lance une commande courte et renvoie (code, sortie, erreurs)."""
    env = dict(os.environ)
    if c_locale:
        env['LC_ALL'] = 'C'
    try:
        p = subprocess.run(argv, capture_output=True, text=True, errors='replace',
                           timeout=timeout, env=env, stdin=subprocess.DEVNULL)
        return p.returncode, ANSI.sub('', p.stdout), ANSI.sub('', p.stderr)
    except FileNotFoundError:
        return 127, '', 'commande introuvable'
    except subprocess.TimeoutExpired:
        return 124, '', 'délai dépassé'


def have(cmd):
    return shutil.which(cmd) is not None


def fmt_size(b):
    if b is None:
        return '?'
    units = ['o', 'Ko', 'Mo', 'Go', 'To']
    b = float(b)
    i = 0
    while b >= 1024 and i < len(units) - 1:
        b /= 1024
        i += 1
    txt = f'{b:.1f}' if (i > 0 and b < 10) else f'{b:.0f}'
    return txt.replace('.', ',') + ' ' + units[i]


UNIT = {'B': 1, 'K': 1024, 'M': 1024 ** 2, 'G': 1024 ** 3, 'T': 1024 ** 4}


def parse_size(num, unit='B'):
    try:
        n = float(str(num).replace(',', '.'))
    except ValueError:
        return 0
    u = (unit or 'B').strip()[:1].upper()
    return int(n * UNIT.get(u, 1))


def plural(n, one, many=None):
    return f'{nf(n)} {one if n == 1 else (many or one + "s")}'


def nf(n):
    return f'{n:,}'.replace(',', ' ')


def ago(ts):
    d = int((time.time() - ts) // 86400)
    if d <= 0:
        return 'aujourd’hui'
    if d == 1:
        return 'hier'
    if d < 60:
        return f'il y a {d} jours'
    return f'il y a {d // 30} mois'


def dir_size(path, skip=(), stop=None):
    """Taille et nombre de fichiers d'un dossier (sans suivre les liens). stop() vrai = on abandonne."""
    total = count = 0
    try:
        st = os.lstat(path)
    except OSError:
        return 0, 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size, 1
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        if stop is not None and stop():
            break
        if skip:
            dirs[:] = [d for d in dirs if os.path.join(root, d) not in skip]
        for f in files:
            try:
                s = os.lstat(os.path.join(root, f))
            except OSError:
                continue
            if stat.S_ISREG(s.st_mode):
                total += s.st_size
                count += 1
    return total, count


def pkg_info(names):
    """Version, description et taille installée des paquets donnés."""
    res = {}
    names = list(names)
    for i in range(0, len(names), 300):
        rc, out, _ = run(['pacman', '-Qi'] + names[i:i + 300], timeout=120)
        cur = None
        for line in out.splitlines():
            if ':' not in line:
                continue
            key, val = line.split(':', 1)
            key, val = key.strip(), val.strip()
            if key == 'Name':
                cur = val
                res[cur] = {'ver': '', 'desc': '', 'size': 0}
            elif cur and key == 'Version':
                res[cur]['ver'] = val
            elif cur and key == 'Description':
                res[cur]['desc'] = val
            elif cur and key == 'Installed Size':
                parts = val.split()
                res[cur]['size'] = parse_size(parts[0], parts[1] if len(parts) > 1 else 'B')
    return res


def pkg_desktop_files(names):
    """Fichiers .desktop installés par chaque paquet."""
    out_map = defaultdict(list)
    names = list(names)
    for i in range(0, len(names), 300):
        rc, out, _ = run(['pacman', '-Ql'] + names[i:i + 300], timeout=120)
        for line in out.splitlines():
            parts = line.split(' ', 1)
            if len(parts) == 2 and parts[1].startswith(APPS_PREFIX) and parts[1].endswith('.desktop'):
                out_map[parts[0]].append(parts[1])
    return out_map


def installed_set(names):
    rc, out, _ = run(['pacman', '-Qq'] + list(names))
    return set(out.split())


def first_visible_app(paths):
    for p in paths:
        try:
            ai = Gio.DesktopAppInfo.new_from_filename(p)
        except Exception:
            ai = None
        if ai and not ai.get_nodisplay():
            return ai
    return None


def disk_usage(path='/'):
    try:
        s = os.statvfs(path)
        return s.f_blocks * s.f_frsize, s.f_bavail * s.f_frsize
    except OSError:
        return 0, 0


def version_key(v):
    return tuple(int(x) for x in re.findall(r'\d+', v))


def distro_name():
    try:
        with open('/etc/os-release', encoding='utf-8') as f:
            for line in f:
                if line.startswith('NAME='):
                    return line.split('=', 1)[1].strip().strip('"') or 'Arch Linux'
    except OSError:
        pass
    return 'Arch Linux'


def duration(sec):
    sec = int(round(sec))
    if sec < 1:
        return 'moins d’une seconde'
    if sec < 60:
        return f'{sec} s'
    return f'{sec // 60} min {sec % 60:02d} s'


def load_settings():
    try:
        with open(CONF_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        os.makedirs(os.path.dirname(CONF_FILE), exist_ok=True)
        tmp = CONF_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONF_FILE)
    except OSError:
        pass


def askpass_counter_path():
    return os.path.join(RUNTIME, f'maintenance-askpass-{os.getuid()}.essais')


def helper_script(name, flag):
    """Petit lanceur qui rappelle l'application avec une option (askpass, relais sudo)."""
    path = os.path.join(RUNTIME, f'maintenance-{name}-{os.getuid()}')
    content = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(SELF)} {flag} "$@"\n'
    try:
        with open(path, encoding='utf-8') as f:
            same = f.read() == content
    except OSError:
        same = False
    if not same:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
    os.chmod(path, 0o700)
    return path


def askpass_wrapper():
    """Appelé par sudo pour demander le mot de passe dans une fenêtre."""
    return helper_script('askpass', '--askpass')


def relay_wrapper():
    """Donné à yay (--sudo) : ses commandes pacman passent par l'autorisation de l'application."""
    return helper_script('sudo', '--sudo-relay')


def sock_send(conn, kind, data=b''):
    conn.sendall(kind + struct.pack('!II', 0, len(data)) + data)


def is_descendant(pid, ancestor):
    for _ in range(64):
        if pid == ancestor:
            return True
        if pid <= 1:
            return False
        try:
            with open(f'/proc/{pid}/stat') as f:
                pid = int(f.read().rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return False
    return False


def relay_client(args):
    """Remplace sudo pour yay : demande à l'application ouverte de lancer pacman."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(RELAY_SOCK)
    except OSError:
        os.execvp('sudo', ['sudo'] + list(args))   # application fermée : sudo classique
    s.sendall(json.dumps({'argv': list(args), 'cwd': os.getcwd()}).encode() + b'\n')
    out = sys.stdout.buffer
    rf = s.makefile('rb')
    try:
        while True:
            kind, _jid, data = read_frame(rf)
            if kind == b'O':
                out.write(data)
                out.flush()
            elif kind == b'N':
                sys.stderr.write(data.decode('utf-8', 'replace') + '\n')
                return 126
            elif kind == b'X':
                return struct.unpack('!i', data)[0]
    except (EOFError, OSError):
        return 1


def reset_askpass_counter():
    try:
        with open(askpass_counter_path(), 'w') as f:
            f.write('0')
    except OSError:
        pass


# ============================================================================
#  Fenêtre de mot de passe (appelée par sudo via SUDO_ASKPASS)
# ============================================================================
def askpass_main(args):
    prompt = args[0] if args else 'Mot de passe :'
    ctx = os.environ.get('MAINTENANCE_ASKPASS_CTX', '')
    tries = 0
    try:
        with open(askpass_counter_path()) as f:
            tries = int(f.read().strip() or 0)
    except (OSError, ValueError):
        pass
    try:
        with open(askpass_counter_path(), 'w') as f:
            f.write(str(tries + 1))
    except OSError:
        pass

    # 1) L'application est ouverte : la fenêtre s'affiche dedans et reçoit directement le clavier.
    try:
        sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sk.connect(RELAY_SOCK)
        sk.sendall(json.dumps({'askpass': prompt, 'ctx': ctx, 'tries': tries}).encode() + b'\n')
        kind, _jid, data = read_frame(sk.makefile('rb'))
        if kind == b'P':
            sys.stdout.write(data.decode('utf-8') + '\n')
            sys.stdout.flush()
            return 0
        if kind == b'X':
            return 1
    except (OSError, EOFError, ValueError):
        pass
    # 2) Sinon, fenêtre autonome, mise au premier plan avec l'heure du serveur X
    #    (sans quoi le gestionnaire de fenêtres peut lui refuser le clavier).
    dlg, entry = password_dialog(prompt, ctx, tries)
    dlg.set_keep_above(True)
    dlg.set_position(Gtk.WindowPosition.CENTER)
    dlg.show_all()
    try:
        gi.require_version('GdkX11', '3.0')
        from gi.repository import GdkX11
        w = dlg.get_window()
        if isinstance(w, GdkX11.X11Window):
            ts = GdkX11.x11_get_server_time(w)
            w.set_user_time(ts)
            dlg.present_with_time(ts)
        else:
            dlg.present()
    except (ValueError, ImportError):
        dlg.present()
    entry.grab_focus()
    resp = dlg.run()
    pw = entry.get_text()
    dlg.destroy()
    if resp == Gtk.ResponseType.OK and pw:
        sys.stdout.write(pw + '\n')
        sys.stdout.flush()
        return 0
    return 1


def password_dialog(prompt, ctx, tries, parent=None):
    dlg = Gtk.Dialog(title='Authentification requise', transient_for=parent, modal=parent is not None)
    if parent is not None:
        dlg.get_style_context().add_class('maint')
    dlg.set_icon_name('dialog-password')
    dlg.set_default_size(440, -1)
    dlg.add_button('Annuler', Gtk.ResponseType.CANCEL)
    ok = dlg.add_button('Authentifier', Gtk.ResponseType.OK)
    ok.get_style_context().add_class('suggested-action')
    dlg.set_default_response(Gtk.ResponseType.OK)
    box = Gtk.Box(spacing=16)
    box.set_border_width(16)
    img = Gtk.Image.new_from_icon_name('dialog-password', Gtk.IconSize.DIALOG)
    img.set_valign(Gtk.Align.START)
    box.pack_start(img, False, False, 0)
    col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    t = Gtk.Label(xalign=0)
    t.set_markup('<b><big>Authentification requise</big></b>')
    col.pack_start(t, False, False, 0)
    col.pack_start(Gtk.Label(label='« Maintenance » a besoin des droits administrateur.', xalign=0, wrap=True,
                             max_width_chars=50), False, False, 0)
    if ctx:
        act = Gtk.Label(xalign=0, wrap=True, max_width_chars=50)
        act.set_markup(f'Action : <b>{GLib.markup_escape_text(ctx)}</b>')
        col.pack_start(act, False, False, 0)
    if tries >= 1:
        bad = Gtk.Label(xalign=0)
        bad.set_markup('<span foreground="#d4453f"><b>Mot de passe incorrect, réessaie.</b></span>')
        col.pack_start(bad, False, False, 0)
    col.pack_start(Gtk.Label(label=prompt.strip() or 'Mot de passe :', xalign=0), False, False, 0)
    entry = Gtk.Entry(visibility=False, activates_default=True)
    entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
    col.pack_start(entry, False, False, 0)
    box.pack_start(col, True, True, 0)
    dlg.get_content_area().add(box)
    return dlg, entry


# ============================================================================
#  Exécution des commandes
# ============================================================================
class Step:
    """Une étape : une commande (argv ou chaîne shell) ou une fonction Python."""

    def __init__(self, argv=None, func=None, root=False, modifies=True, label=None,
                 pacman=False, yay=False, askpass=False, c_locale=False, quiet=False,
                 capture=None, fallback=None, fallback_if=None, env=None, ok_codes=(0,), stop=False,
                 critical=None):
        self.argv = argv
        self.func = func
        self.root = root
        self.modifies = modifies
        self.label = label
        self.pacman = pacman
        self.yay = yay
        self.askpass = askpass or root or yay
        self.c_locale = c_locale
        self.quiet = quiet
        self.capture = capture
        self.fallback = fallback
        self.fallback_if = fallback_if or ()
        self.env = env or {}
        self.ok_codes = ok_codes
        self.stop = stop
        # Étape à ne jamais couper au milieu (pacman, yay, firmware, instantané) : l'arrêt attend sa fin.
        self.critical = critical if critical is not None else bool((pacman or yay) and modifies)

    def display(self):
        if self.label:
            return self.label
        if isinstance(self.argv, str):
            return self.argv
        if self.argv:
            return ' '.join(shlex.quote(a) for a in self.argv)
        return ''

    def prompt(self):
        return '#' if self.root else '$'


class Runner:
    def __init__(self, win):
        self.win = win
        self.busy = False
        self.cancel_req = False
        self.stopped = False     # la dernière opération a été arrêtée par le bouton
        self.cur_label = ''      # étape en cours et dernière ligne affichée, pour la ligne « En cours »
        self.last_line = ''
        self.cur = None          # ('user', Popen) ou ('root', numéro) : commande en cours
        self.cur_step = None

    def cancel(self):
        """Bouton « Arrêter » : coupe la commande en cours, sauf pacman/yay/firmware/instantané
        (on attend alors la fin de l'étape) ; les étapes suivantes ne sont pas lancées."""
        if not self.busy or self.cancel_req:
            return
        self.cancel_req = True
        st = self.cur_step
        if st is not None and st.critical:
            self.win.journal.add('■ Arrêt demandé : l’étape en cours ne peut pas être coupée sans risque '
                                 f'({st.display()[:60]}). L’opération s’arrêtera dès qu’elle sera finie.', 's')
        else:
            self.win.journal.add('■ Arrêt demandé…', 's')
            self._kill_current()
        self.win.update_stop()

    def _kill_current(self):
        cur = self.cur
        if cur is None:
            return
        kind, ref = cur
        if kind == 'root':
            self.win.session.kill(ref)
            return

        def term(p=ref):
            for sig, wait in ((signal.SIGTERM, 4), (signal.SIGKILL, 0)):
                try:
                    os.killpg(p.pid, sig)
                except OSError:
                    return
                try:
                    p.wait(timeout=wait or None)
                    return
                except subprocess.TimeoutExpired:
                    continue
        threading.Thread(target=term, daemon=True).start()

    def start(self, title, steps, done=None):
        if self.busy:
            return False
        self.t0 = time.monotonic()
        self.title = title
        self.cancel_req = False
        self.cur = self.cur_step = None
        self.busy = True
        self.win.set_busy(True)
        self.win.journal.add('')
        self.win.journal.add(f'==> {title}', 'h')
        self.win.show_journal(True)
        threading.Thread(target=self._work, args=(title, list(steps), done), daemon=True).start()
        return True

    def _log(self, text, tag=None):
        self.win.journal.log(text, tag)

    def _work(self, title, steps, done):
        ok_all = True
        needs_root = any(s.root for s in steps)
        if needs_root and os.geteuid() != 0 and not self.win.session.ensure():
            self._log('Authentification annulée ou refusée : rien n’a été lancé.', 'e')
            GLib.idle_add(self._finish, done, False)
            return
        for st in steps:
            if self.cancel_req:
                break
            self.cur_step = st
            if st.pacman and os.path.exists(PACMAN_LOCK):
                self._log('pacman est déjà utilisé par un autre programme (db.lck présent) : étape ignorée.', 'e')
                ok_all = False
                continue
            if st.yay and os.geteuid() == 0:
                self._log('yay ne doit pas être lancé en root : étape ignorée.', 'e')
                ok_all = False
                continue
            if st.func is not None:
                if st.display():
                    self._log(f'{st.prompt()} {st.display()}', 'c')
                try:
                    res = st.func(self._log)
                except Exception as e:  # noqa: BLE001
                    self._log(f'Erreur : {e}', 'e')
                    ok_all = False
                    res = None
                    if st.stop:
                        self._log('Arrêt : les étapes suivantes ne sont pas lancées.', 'e')
                        break
                if self.cancel_req:
                    break
                if st.capture:
                    GLib.idle_add(st.capture, res, 0)
                continue
            self._log(f'{st.prompt()} {st.display()}', 'c')
            self.cur_label, self.last_line = st.display(), ''
            GLib.idle_add(self.win.set_step, st.display())
            if st.yay:
                reset_askpass_counter()
            rc, out = self._exec(st.argv, title, root=st.root, askpass=st.askpass,
                                 quiet=st.quiet, c_locale=st.c_locale, env=st.env)
            if self.cancel_req and not st.critical:
                break                       # commande coupée par « Arrêter » : pas de résultat partiel
            if rc not in st.ok_codes and st.fallback is not None and \
                    (not st.fallback_if or any(k in out for k in st.fallback_if)):
                fb = st.fallback
                self._log(f'Nouvel essai : {fb.prompt()} {fb.display()}', 'd')
                if fb.yay:
                    reset_askpass_counter()
                rc, out = self._exec(fb.argv, title, root=fb.root, askpass=fb.askpass,
                                     quiet=fb.quiet, c_locale=fb.c_locale, env=fb.env)
            if st.capture:
                GLib.idle_add(st.capture, out, rc)
            if rc not in st.ok_codes:
                self._log(f'La commande s’est terminée avec le code {rc}.', 'e')
                ok_all = False
                if st.stop:
                    self._log('Arrêt : les étapes suivantes ne sont pas lancées.', 'e')
                    break
        GLib.idle_add(self._finish, done, ok_all)

    def _exec(self, argv, title, root=False, askpass=False, quiet=False, c_locale=False, env=None):
        if isinstance(argv, str):
            argv = ['sh', '-c', argv]
        lines = []
        if root and os.geteuid() != 0:
            extra = dict(env or {})
            if c_locale:
                extra['LC_ALL'] = 'C'
            buf = [b'']

            def on_out(data):
                buf[0] += data
                *complete, buf[0] = buf[0].split(b'\n')
                for raw in complete:
                    self._line(raw, lines, quiet)
            rc = self.win.session.run(argv, env=extra, on_output=on_out,
                                      on_start=lambda jid: setattr(self, 'cur', ('root', jid)))
            self.cur = None
            if buf[0]:
                self._line(buf[0], lines, quiet)
            if rc is None:
                self._log('Droits administrateur non accordés : commande non lancée.', 'e')
                return 1, ''
            return rc, '\n'.join(lines)
        e = dict(os.environ)
        e.update(env or {})
        if askpass:
            reset_askpass_counter()
            try:
                e['SUDO_ASKPASS'] = askpass_wrapper()
            except OSError:
                pass
            e['MAINTENANCE_ASKPASS_CTX'] = title
        if c_locale:
            e['LC_ALL'] = 'C'
        try:
            p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, env=e, start_new_session=True)
        except FileNotFoundError:
            self._log(f'Commande introuvable : {argv[0]}', 'e')
            return 127, ''
        self.cur = ('user', p)
        if self.cancel_req and not (self.cur_step and self.cur_step.critical):
            self._kill_current()
        for raw in iter(p.stdout.readline, b''):
            self._line(raw, lines, quiet)
        rc = p.wait()
        self.cur = None
        return rc, '\n'.join(lines)

    def _line(self, raw, lines, quiet):
        line = raw.decode('utf-8', 'replace').rstrip('\n')
        if '\r' in line:
            parts = [x for x in line.split('\r') if x.strip()]
            line = parts[-1] if parts else ''
        line = ANSI.sub('', line)
        lines.append(line)
        if line.strip():
            self.last_line = line.strip()
        if not quiet and line.strip():
            self._log('  ' + line)

    def _finish(self, done, ok):
        took = time.monotonic() - self.t0
        j = self.win.journal
        stopped = self.stopped = self.cancel_req
        if stopped:
            ok = False
            j.add(f'■ Arrêté au bout de {duration(took)} : les étapes suivantes n’ont pas été lancées.', 's')
        elif ok:
            j.add(f'✓ Terminé en {duration(took)}.', 'h')
        else:
            j.add(f'✗ Terminé avec des erreurs en {duration(took)} : voir les lignes en rouge ci-dessus.', 'e')
        app = self.win.get_application()
        if app and took > 5 and not self.win.is_active():
            n = Gio.Notification.new('Maintenance')
            n.set_body(f'{self.title} : ' + ('arrêté' if stopped else 'terminé' if ok
                                              else 'terminé avec des erreurs'))
            app.send_notification('operation', n)
        self.busy = False
        self.cancel_req = False
        self.cur = self.cur_step = None
        self.win.set_busy(False)
        if done:
            done(ok)
        return False


# ============================================================================
#  Petits constructeurs de widgets
# ============================================================================
def themed(names):
    names = [n for n in names if n] + ['application-x-executable']
    return Gio.ThemedIcon.new_from_names(names)


def icon(names, size=40):
    img = Gtk.Image.new_from_gicon(themed(names), Gtk.IconSize.DIALOG)
    img.set_pixel_size(size)
    return img


def symbolic(names, size=16):
    img = Gtk.Image.new_from_gicon(themed(names + ['image-missing']), Gtk.IconSize.MENU)
    img.set_pixel_size(size)
    return img


def label(text='', css=(), wrap=False, xalign=0.0, markup=False, selectable=False, max_chars=-1):
    lab = Gtk.Label(xalign=xalign)
    if markup:
        lab.set_markup(text)
    else:
        lab.set_text(text)
    if wrap:
        lab.set_line_wrap(True)
        lab.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if max_chars > 0:
        lab.set_max_width_chars(max_chars)
    lab.set_selectable(selectable)
    for c in css:
        lab.get_style_context().add_class(c)
    return lab


def chip(kind, text):
    return label(text, css=('chip', kind))


TONES = ('ok', 'warn', 'crit', 'info', 'off', 'done')


def set_tone(w, tone):
    ctx = w.get_style_context()
    for k in TONES:
        ctx.remove_class(k)
    ctx.add_class(tone if tone in TONES else 'off')


def tone_tile(names, tone='off', size=42, icon_size=20, big=False):
    """Icône symbolique sur un carré arrondi teinté de la couleur de l'état."""
    box = Gtk.Box()
    box.set_size_request(size, size)
    box.set_valign(Gtk.Align.START)
    box.set_halign(Gtk.Align.START)
    box.get_style_context().add_class('tile')
    if big:
        box.get_style_context().add_class('big')
    img = symbolic(names, icon_size)
    box.pack_start(img, True, True, 0)
    box.img = img
    set_tone(box, tone)
    return box


def clear(box):
    for c in box.get_children():
        box.remove(c)
        c.destroy()


def vbox(spacing=0):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)


def hbox(spacing=0):
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing)


def button(text, style=None, lock=False, icon_name=None):
    b = Gtk.Button.new_with_label(text)
    if style:
        b.get_style_context().add_class(style)
    name = 'changes-prevent-symbolic' if lock else icon_name
    if name:
        b.set_image(Gtk.Image.new_from_gicon(themed([name, 'dialog-password-symbolic']), Gtk.IconSize.BUTTON))
        b.set_always_show_image(True)
    b.set_valign(Gtk.Align.CENTER)
    return b


def boxed_list():
    lb = Gtk.ListBox()
    lb.set_selection_mode(Gtk.SelectionMode.NONE)
    lb.get_style_context().add_class('boxed')
    return lb


def page_header(title, sub):
    box = vbox(2)
    box.pack_start(label(title, css=('page-title',)), False, False, 0)
    box.pack_start(label(sub, css=('dim-label',), wrap=True, max_chars=90), False, False, 0)
    return box


def scrolled_page():
    sw = Gtk.ScrolledWindow()
    sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    inner = vbox(14)
    inner.set_margin_start(24)
    inner.set_margin_end(24)
    inner.set_margin_top(20)
    inner.set_margin_bottom(24)
    inner.set_hexpand(True)
    sw.add(inner)
    return sw, inner


def confirm(win, title, text, ok_label='Continuer', destructive=False, items=None, kind=Gtk.MessageType.QUESTION):
    d = Gtk.MessageDialog(transient_for=win, modal=True, message_type=kind,
                          buttons=Gtk.ButtonsType.NONE, text=title)
    d.get_style_context().add_class('maint')
    d.format_secondary_text(text)
    if items:
        lab = label('\n'.join(items), css=('mono',), selectable=True)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.set_min_content_height(min(200, 22 * len(items) + 10))
        sw.set_min_content_width(420)
        sw.add(lab)
        sw.show_all()
        d.get_message_area().pack_start(sw, True, True, 0)
    d.add_button('Annuler', Gtk.ResponseType.CANCEL)
    okb = d.add_button(ok_label, Gtk.ResponseType.OK)
    okb.get_style_context().add_class('destructive-action' if destructive else 'suggested-action')
    d.set_default_response(Gtk.ResponseType.OK)
    r = d.run()
    d.destroy()
    return r == Gtk.ResponseType.OK


def inform(win, title, text, kind=Gtk.MessageType.INFO):
    d = Gtk.MessageDialog(transient_for=win, modal=True, message_type=kind,
                          buttons=Gtk.ButtonsType.OK, text=title)
    d.get_style_context().add_class('maint')
    d.format_secondary_text(text)
    d.run()
    d.destroy()


def launch_desktop(path, log=None):
    try:
        ai = Gio.DesktopAppInfo.new_from_filename(path)
        if ai and ai.launch([], None):
            if log:
                log(f'« {ai.get_display_name()} » ouvert.', 'd')
            return True
    except GLib.Error as e:
        if log:
            log(f'Impossible d’ouvrir {path} : {e.message}', 'e')
    return False


def launch_cmd(argv, log=None):
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        if log:
            log(f'$ {" ".join(argv)}', 'c')
        return True
    except OSError as e:
        if log:
            log(f'Impossible de lancer {argv[0]} : {e}', 'e')
        return False


# ============================================================================
#  Éléments de maintenance
# ============================================================================
DEFAULT_STATE = {'chip': None, 'value': '', 'details': [], 'amount': None, 'todo': False,
                 'available': True, 'hint': '', 'count': 0, 'table': None}


class Action:
    def __init__(self, label, steps=None, style=None, confirm=None, refresh=(), handler=None,
                 refresh_only=False, needs_todo=False, destructive=False, after=None):
        self.label = label
        self.steps = steps or []
        self.style = style
        self.confirm = confirm
        self.refresh = refresh
        self.handler = handler
        self.refresh_only = refresh_only
        self.needs_todo = needs_todo
        self.destructive = destructive
        self.after = after

    def get_steps(self):
        return self.steps() if callable(self.steps) else self.steps

    @property
    def modifies(self):
        if callable(self.steps):
            return True
        return any(s.modifies for s in self.steps)

    @property
    def lock(self):
        if callable(self.steps):
            return False
        return any(s.root and s.modifies for s in self.steps)


def install_action(pkg, label=None, refresh=()):
    yay = Step(['yay', '-S', '--needed', '--noconfirm', pkg, '--sudo', relay_wrapper()], yay=True, pacman=True,
               label=f'yay -S --needed --noconfirm {pkg}') if have('yay') else None
    st = Step(['pacman', '-S', '--needed', '--noconfirm', pkg], root=True, pacman=True,
              fallback=yay, fallback_if=('target not found', 'cible introuvable'))
    act = Action(label or f'Installer {pkg}', [st], style='suggested-action', refresh=refresh)
    act.reload_pages = True
    return act


class Item:
    id = ''
    section = ''
    title = ''
    desc = ''
    icons = ['applications-system']
    sym = ['applications-system-symbolic']
    requires = None
    install_pkg = None
    selectable = False
    default_sel = True
    amount_upper = False

    def __init__(self, win):
        self.win = win
        self.state = dict(DEFAULT_STATE, chip=('info', 'Analyse…'))
        self.row = None
        self.busy = False
        self.probing = False
        self.done_msg = None
        self.open = False
        self.cmd_open = False
        self.extra = {}   # résultats d'analyse gardés entre deux rafraîchissements
        sel = win.settings.get('selection', {})
        self.selected = sel.get(self.id, self.default_sel)

    # --- à surcharger ---
    def probe(self):
        return {}

    def actions(self):
        return []

    # --- mécanique commune ---
    @property
    def available(self):
        return self.state.get('available', True)

    def get_actions(self):
        if not self.available:
            if self.install_pkg:
                ids = tuple(i.id for i in self.win.items.values() if i.install_pkg == self.install_pkg)
                return [install_action(self.install_pkg, refresh=ids)]
            return []
        return self.actions()

    def cmd_hint(self):
        acts = self.get_actions()
        for a in acts:
            if a.handler or a.refresh_only or callable(a.steps):
                continue
            shown = [f'{s.prompt()} {s.display()}' for s in a.steps if s.display()]
            if shown:
                return '   '.join(shown)
        return ''

    def safe_probe(self):
        if self.requires and not have(self.requires):
            return {'available': False}
        try:
            st = self.probe() or {}
        except Exception as e:  # noqa: BLE001
            st = {'chip': ('crit', 'Erreur'), 'value': f'Analyse impossible : {e}'}
        st.setdefault('available', True)
        return st

    def refresh(self):
        self.probing = True
        if self.row:
            self.row.update()

        def job():
            st = self.safe_probe()
            GLib.idle_add(self.apply, st)
        POOL.submit(job)

    def apply(self, st):
        self.state = dict(DEFAULT_STATE, **st)
        self.state.update(self.extra)
        self.probing = False
        if self.row:
            self.row.update()
        self.win.state_changed()
        return False

    def keep(self, **kw):
        """Enregistre un résultat d'analyse et met la ligne à jour (fil principal)."""
        self.extra.update(kw)
        self.state.update(kw)
        if self.row:
            self.row.update()
        self.win.state_changed()

    def trigger(self, action):
        win = self.win
        if win.runner.busy:
            return
        if action.handler:
            action.handler()
            return
        if action.refresh_only:
            self.done_msg = None
            self.refresh()
            return
        if action.confirm:
            title, text = action.confirm
            if not confirm(win, title, text, ok_label=action.label, destructive=action.destructive):
                return
        steps = action.get_steps()
        if not steps:
            return
        self.busy = True
        self.done_msg = None
        if self.row:
            self.row.update()
        modifies = any(s.modifies for s in steps)

        def done(ok):
            self.busy = False
            if win.runner.stopped:
                self.done_msg = ('off', 'Arrêté')
            elif not ok:
                self.done_msg = ('crit', 'Échec')
            elif not modifies:
                self.done_msg = ('done', 'Terminé')
            else:
                self.done_msg = ('done', 'Fait')
            self.refresh()
            for rid in action.refresh:
                if rid in win.items and rid != self.id:
                    win.items[rid].refresh()
            if getattr(action, 'reload_pages', False):
                win.pages['soft'].load()
                win.pages['apps'].load()
            win.after_action()
            if action.after:
                action.after(ok)

        if not win.runner.start(f'{self.title} · {action.label}', steps, done):
            self.busy = False
            if self.row:
                self.row.update()


# ---------------------------------------------------------------- Nettoyage
class DlRemnants(Item):
    id, section, selectable = 'dl', 'clean', True
    title = 'Restes de téléchargement pacman'
    icons = ['folder-download', 'emblem-downloads', 'folder']
    desc = ('Dossiers download-* laissés par des mises à jour interrompues. Ils ne servent plus '
            'et provoquent les erreurs « Error reading fd 8 ».')

    def probe(self):
        found = glob.glob(os.path.join(PKG_CACHE, 'download-*'))
        if not found:
            return {'chip': ('ok', 'Propre'), 'value': 'Aucun reste'}
        size = sum(dir_size(p)[0] for p in found)
        det = [os.path.basename(p) for p in found[:8]]
        if len(found) > 8:
            det.append(f'… et {len(found) - 8} autres')
        return {'chip': ('warn', 'À nettoyer'), 'value': plural(len(found), 'élément'),
                'amount': size or None, 'todo': True, 'details': det}

    def actions(self):
        return [Action('Nettoyer', [Step(f'rm -rf -- {PKG_CACHE}/download-*', root=True, pacman=True)],
                       needs_todo=True)]


class PkgCache(Item):
    id, section, selectable = 'pkg', 'clean', True
    title = 'Cache des paquets'
    icons = ['package-x-generic', 'system-software-install']
    requires, install_pkg = 'paccache', 'pacman-contrib'
    desc = ('Anciennes versions des paquets téléchargés. La version installée et la précédente '
            'sont gardées pour pouvoir revenir en arrière.')

    def probe(self):
        total = count = 0
        for args in (['-dk1'], ['-duk0']):
            rc, out, err = run(['paccache', '--nocolor'] + args)
            m = re.search(r'(\d+) candidates?.*?disk space saved: ([\d.]+) ?([KMGT]?i?B)', out + err)
            if m:
                count += int(m.group(1))
                total += parse_size(m.group(2), m.group(3))
        if not count:
            return {'chip': ('ok', 'Propre'), 'value': 'Rien en trop'}
        return {'chip': ('warn', 'À nettoyer'), 'value': plural(count, 'paquet') + ' en trop',
                'amount': total, 'todo': True}

    def actions(self):
        return [Action('Nettoyer', [Step(['paccache', '-rk1'], root=True, pacman=True),
                                    Step(['paccache', '-ruk0'], root=True, pacman=True)], needs_todo=True)]


class Orphans(Item):
    id, section, selectable = 'orph', 'clean', True
    title = 'Paquets orphelins'
    icons = ['package-purge', 'edit-delete', 'user-trash']
    desc = 'Dépendances qui ne servent plus à aucun paquet installé.'

    def probe(self):
        rc, out, _ = run(['pacman', '-Qdtq'])
        names = out.split()
        if not names:
            return {'chip': ('ok', 'Aucun'), 'value': 'Aucun paquet orphelin'}
        info = pkg_info(names)
        sizes = {n: info.get(n, {}).get('size', 0) for n in names}
        return {'chip': ('warn', 'À nettoyer'), 'value': plural(len(names), 'paquet'),
                'amount': sum(sizes.values()), 'todo': True, 'names': names,
                'details': [f'{n} · {fmt_size(sizes[n])}' for n in names]}

    def actions(self):
        names = self.state.get('names') or []
        return [Action('Désinstaller', [Step(['pacman', '-Rns', '--noconfirm'] + names, root=True, pacman=True,
                                             label='pacman -Rns $(pacman -Qdtq)')],
                       needs_todo=True, refresh=('pkg',))]


class Journals(Item):
    id, section, selectable = 'jrn', 'clean', True
    title = 'Journaux systemd'
    icons = ['text-x-log', 'logviewer', 'org.gnome.Logs', 'text-x-generic']
    desc = f'Historique des journaux du système. Les {JOURNAL_DAYS} derniers jours sont gardés.'

    def probe(self):
        rc, out, err = run(['journalctl', '--disk-usage'])
        m = re.search(r'take up ([\d.]+)\s*([KMGT]?)', out + err)
        if not m:
            return {'chip': None, 'value': 'Taille inconnue', 'todo': True}
        used = parse_size(m.group(1), m.group(2) or 'B')
        big = used > 200 * 1024 ** 2
        return {'chip': ('warn', 'À nettoyer') if big else ('ok', 'Raisonnable'),
                'value': f'Les journaux occupent {fmt_size(used)}', 'todo': True}

    def actions(self):
        return [Action('Nettoyer', [Step(['journalctl', f'--vacuum-time={JOURNAL_DAYS}d'], root=True)])]


class AurCache(Item):
    id, section, selectable = 'aur', 'clean', True
    title = 'Cache de compilation AUR'
    icons = ['applications-development', 'package-x-generic']
    requires = 'yay'
    amount_upper = True
    desc = 'Sources et paquets compilés par yay dans ~/.cache/yay.'

    def probe(self):
        size, count = dir_size(os.path.join(XDG_CACHE, 'yay'))
        if size < 20 * 1024 ** 2:
            return {'chip': ('ok', 'Propre'), 'value': 'Vide' if not size else fmt_size(size)}
        return {'chip': ('warn', 'À nettoyer'), 'value': plural(count, 'fichier'), 'amount': size, 'todo': True}

    def actions(self):
        return [Action('Nettoyer', [Step(['yay', '-Sc', '--aur', '--noconfirm'], yay=True)], needs_todo=True)]


def old_cache_files():
    limit = time.time() - CACHE_AGE_DAYS * 86400
    skip = {os.path.join(XDG_CACHE, 'yay'), os.path.join(XDG_CACHE, 'thumbnails')}
    files, total = [], 0
    for root, dirs, names in os.walk(XDG_CACHE, onerror=lambda e: None):
        dirs[:] = [d for d in dirs if os.path.join(root, d) not in skip]
        for f in names:
            p = os.path.join(root, f)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and max(st.st_atime, st.st_mtime) < limit:
                files.append(p)
                total += st.st_size
    return files, total


class AppCaches(Item):
    id, section, selectable, default_sel = 'uc', 'clean', True, False
    title = 'Caches des applications'
    icons = ['folder-temp', 'folder-cache', 'folder']
    desc = (f'Fichiers de ~/.cache inutilisés depuis {CACHE_AGE_DAYS} jours (navigateurs, éditeurs…). '
            'Les applications recréent ce dont elles ont besoin.')

    def probe(self):
        files, total = old_cache_files()
        if not files:
            return {'chip': ('ok', 'Propre'), 'value': 'Rien d’ancien'}
        return {'chip': ('warn', 'À nettoyer'), 'value': plural(len(files), 'fichier'), 'amount': total, 'todo': True}

    def actions(self):
        def clean(log):
            files, total = old_cache_files()
            removed = 0
            for p in files:
                try:
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass
            for root, dirs, names in os.walk(XDG_CACHE, topdown=False, onerror=lambda e: None):
                if root != XDG_CACHE and not dirs and not names:
                    try:
                        os.rmdir(root)
                    except OSError:
                        pass
            log(f'  {plural(removed, "fichier")} supprimé{"s" if removed > 1 else ""} ({fmt_size(total)}).')
        return [Action('Nettoyer', [Step(func=clean, label=f'suppression des fichiers de ~/.cache vieux de plus de {CACHE_AGE_DAYS} jours')],
                       needs_todo=True)]


class Thumbnails(Item):
    id, section, selectable = 'th', 'clean', True
    title = 'Miniatures'
    icons = ['image-x-generic', 'folder-pictures']
    desc = 'Aperçus d’images et de vidéos créés par le gestionnaire de fichiers. Ils sont recréés à la demande.'
    path = os.path.join(XDG_CACHE, 'thumbnails')

    def probe(self):
        size, count = dir_size(self.path)
        if not count:
            return {'chip': ('ok', 'Propre'), 'value': 'Aucune miniature'}
        return {'chip': ('warn', 'À nettoyer') if size > 50 * 1024 ** 2 else None,
                'value': plural(count, 'aperçu'), 'amount': size, 'todo': True}

    def actions(self):
        def clean(log):
            n = 0
            for child in glob.glob(os.path.join(self.path, '*')):
                try:
                    if os.path.isdir(child) and not os.path.islink(child):
                        shutil.rmtree(child)
                    else:
                        os.remove(child)
                    n += 1
                except OSError as e:
                    log(f'  impossible de supprimer {child} : {e}', 'e')
            log(f'  Miniatures supprimées ({plural(n, "dossier")}).')
        return [Action('Nettoyer', [Step(func=clean, label='rm -rf ~/.cache/thumbnails/*')], needs_todo=True)]


class TrashItem(Item):
    id, section, selectable, default_sel = 'tr', 'clean', True, False
    title = 'Corbeille'
    icons = ['user-trash-full', 'user-trash']
    desc = 'Fichiers mis à la corbeille. Vérifie qu’il n’y a rien à récupérer avant de la vider.'

    def probe(self):
        files = os.path.join(TRASH_DIR, 'files')
        try:
            n = len(os.listdir(files))
        except OSError:
            n = 0
        if not n:
            return {'chip': ('ok', 'Vide'), 'value': 'La corbeille est vide'}
        size, _ = dir_size(files)
        return {'chip': None, 'value': plural(n, 'élément'), 'amount': size, 'todo': True}

    def actions(self):
        return [Action('Vider', [Step(['gio', 'trash', '--empty'])], needs_todo=True, destructive=True,
                       confirm=('Vider la corbeille ?', 'Les fichiers de la corbeille seront supprimés définitivement.'))]


class FlatpakUnused(Item):
    id, section, selectable = 'fpu', 'clean', True
    title = 'Flatpak inutilisés'
    icons = ['flatpak', 'org.flatpak.Flatpak', 'application-vnd.flatpak.ref', 'package-x-generic']
    requires = 'flatpak'
    desc = 'Runtimes Flatpak qui ne servent plus à aucune application.'

    def probe(self):
        rc, out, _ = run(['flatpak', 'list', '--runtime', '--columns=application'])
        n = len([x for x in out.splitlines() if x.strip()])
        return {'chip': None, 'value': plural(n, 'runtime installé', 'runtimes installés'), 'todo': n > 0}

    def actions(self):
        return [Action('Nettoyer', [Step(['flatpak', 'uninstall', '--unused', '--system', '--noninteractive'],
                                         root=True),
                                    Step(['flatpak', 'uninstall', '--unused', '--user', '--noninteractive'])],
                       needs_todo=True)]


class DockerItem(Item):
    id, section, selectable = 'dk', 'clean', True
    title = 'Docker'
    icons = ['docker', 'docker-desktop', 'application-x-executable']
    requires = 'docker'
    desc = 'Images, conteneurs arrêtés, réseaux et cache de construction inutilisés.'

    def probe(self):
        if not os.path.exists('/var/run/docker.sock'):
            return {'chip': ('off', 'Service arrêté'), 'value': 'Docker est installé mais son service ne tourne pas'}
        return {'chip': None, 'value': 'Docker installé', 'todo': True}

    def actions(self):
        return [Action('Nettoyer', [Step(['docker', 'system', 'prune', '-f'], root=True)], needs_todo=True)]


# ---------------------------------------------------------------- Mises à jour
class SysUpdate(Item):
    id, section = 'sys', 'updates'
    title = 'Système et AUR'
    icons = ['system-software-update', 'software-update-available', 'system-upgrade']
    desc = 'Met à jour tous les paquets des dépôts Arch, puis ceux de l’AUR avec yay.'

    def probe(self):
        repo, note = [], ''
        if have('checkupdates'):
            rc, out, err = run(['checkupdates'], timeout=180)
            if rc == 0:
                repo = [x for x in out.splitlines() if '->' in x]
            elif rc != 2:
                note = 'Vérification des dépôts impossible (réseau ?).'
        else:
            rc, out, _ = run(['pacman', '-Qu'])
            repo = [x for x in out.splitlines() if '->' in x]
            note = 'Sans pacman-contrib, la liste peut être ancienne.'
        aur = []
        if have('yay'):
            rc, out, _ = run(['yay', '-Qua'], timeout=180)
            aur = [x for x in out.splitlines() if '->' in x]
        n = len(repo) + len(aur)
        det = repo[:12] + [x + '  (AUR)' for x in aur[:8]]
        if n > len(det):
            det.append(f'… et {n - len(det)} autres')
        if note:
            det.insert(0, note)
        if not n:
            return {'chip': ('ok', 'À jour'), 'value': 'Système à jour', 'details': det, 'count': 0}
        return {'chip': ('info', plural(n, 'disponible')), 'count': n, 'aur': len(aur),
                'value': f'{len(repo)} depuis les dépôts · {len(aur)} depuis l’AUR', 'details': det}

    def actions(self):
        n = self.state.get('count', 0)
        acts = []
        if n:
            def steps():
                st = [Step(['pacman', '-Syu', '--noconfirm'], root=True, pacman=True)]
                if self.state.get('aur') and have('yay'):
                    st.append(Step(['yay', '-Sua', '--noconfirm', '--cleanmenu=false', '--diffmenu=false',
                                    '--editmenu=false', '--removemake', '--sudo', relay_wrapper()],
                                   yay=True, pacman=True, label='yay -Sua --noconfirm --removemake'))
                return st
            acts.append(Action('Mettre à jour', steps, style='suggested-action',
                               confirm=('Mettre à jour le système ?',
                                        f'{plural(n, "mise à jour", "mises à jour")} {"sera installée" if n == 1 else "seront installées"} sans autre question. '
                                        'Pense à créer un instantané Timeshift avant une grosse mise à jour.'),
                               refresh=('orph', 'pkg', 'kern', 'pacnew', 'key', 'dl')))
        acts.append(Action('Vérifier à nouveau', refresh_only=True))
        return acts

    def cmd_hint(self):
        return '# pacman -Syu   $ yay -Sua'


class FlatpakUpdates(Item):
    id, section = 'fpk', 'updates'
    title = 'Applications Flatpak'
    icons = ['flatpak', 'org.flatpak.Flatpak', 'package-x-generic']
    requires = 'flatpak'
    desc = 'Met à jour les applications et runtimes Flatpak.'

    def probe(self):
        rc, out, _ = run(['flatpak', 'remote-ls', '--updates', '--columns=application,version'], timeout=120)
        lines = [x for x in out.splitlines() if x.strip()]
        if not lines:
            return {'chip': ('ok', 'À jour'), 'value': 'Aucune mise à jour', 'count': 0}
        return {'chip': ('info', plural(len(lines), 'disponible')), 'count': len(lines),
                'value': plural(len(lines), 'mise à jour', 'mises à jour'), 'details': lines[:12]}

    def actions(self):
        return [Action('Mettre à jour', [Step(['flatpak', 'update', '--system', '--noninteractive'], root=True),
                                         Step(['flatpak', 'update', '--user', '--noninteractive'])],
                       style='suggested-action'),
                Action('Vérifier à nouveau', refresh_only=True)]


class Firmware(Item):
    id, section = 'fw', 'updates'
    title = 'Firmware'
    icons = ['fwupd', 'firmware-manager', 'computer', 'drive-harddisk']
    requires, install_pkg = 'fwupdmgr', 'fwupd'
    desc = 'Met à jour le BIOS/UEFI, le SSD et les périphériques compatibles grâce au service LVFS.'

    def probe(self):
        return {'chip': ('info', 'Pas encore vérifié'), 'value': 'Clique sur « Rechercher »'}

    def _parse(self, out, rc):
        if rc == 2 or 'No updates available' in out or 'Aucune mise à jour' in out:
            self.keep(chip=('ok', 'À jour'), value='Aucune mise à jour du firmware', fw_updates=False)
        elif rc == 0:
            self.keep(chip=('warn', 'Mise à jour disponible'), value='Voir le détail dans le journal', fw_updates=True)
        else:
            self.keep(chip=('crit', 'Vérification impossible'), value='Voir le journal', fw_updates=False)
        return False

    def actions(self):
        acts = [Action('Rechercher', [Step(['fwupdmgr', 'get-updates'], modifies=False, capture=self._parse,
                                           ok_codes=(0, 2))])]
        if self.state.get('fw_updates'):
            acts.insert(0, Action('Installer', [Step(['fwupdmgr', 'update', '-y'], root=True, critical=True),
                                                Step(['fwupdmgr', 'get-updates'], modifies=False, quiet=True,
                                                     capture=self._parse, ok_codes=(0, 2))],
                                  style='suggested-action',
                                  confirm=('Mettre à jour le firmware ?',
                                           'Ne coupe pas l’alimentation pendant l’opération. Un redémarrage peut être demandé.')))
        return acts


class Mirrors(Item):
    id, section = 'mir', 'updates'
    title = 'Miroirs les plus rapides'
    icons = ['network-server', 'network-workgroup', 'applications-internet']
    requires, install_pkg = 'reflector', 'reflector'
    desc = (f'Teste les miroirs Arch en HTTPS ({MIRROR_COUNTRIES.replace(",", ", ")}) et garde les 20 plus rapides. '
            'L’ancienne liste est sauvegardée en mirrorlist.bak.')

    def probe(self):
        try:
            mt = os.path.getmtime(MIRRORLIST)
        except OSError:
            return {'chip': ('warn', 'Liste introuvable')}
        old = time.time() - mt > 60 * 86400
        return {'chip': ('warn', 'Ancienne') if old else ('ok', 'Récente'), 'value': f'Liste modifiée {ago(mt)}'}

    def actions(self):
        cmd = (f'cp {MIRRORLIST} {MIRRORLIST}.bak && reflector --country {MIRROR_COUNTRIES} --protocol https '
               f'--latest 20 --sort rate --save {MIRRORLIST}')
        return [Action('Classer les miroirs', [Step(cmd, root=True)], style='suggested-action')]


class Keyring(Item):
    id, section = 'key', 'updates'
    title = 'Trousseau archlinux-keyring'
    icons = ['dialog-password', 'seahorse', 'channel-secure', 'security-high']
    desc = ('Clés qui signent les paquets. À mettre à jour en premier si une mise à jour échoue '
            'sur une erreur de signature.')

    def probe(self):
        rc, out, _ = run(['pacman', '-Q', 'archlinux-keyring'])
        ver = out.split()[1] if len(out.split()) > 1 else '?'
        return {'chip': ('ok', 'Installé'), 'value': f'Version {ver}'}

    def actions(self):
        cmd = 'pacman -Sy --needed --noconfirm archlinux-keyring && pacman -Su --noconfirm'
        return [Action('Mettre à jour', [Step(cmd, root=True, pacman=True)],
                       confirm=('Mettre à jour le trousseau ?',
                                'Le trousseau est mis à jour en premier, puis le reste du système.'),
                       refresh=('sys', 'kern'))]


class SelfUpdate(Item):
    id, section = 'self', 'about'
    title = 'Mise à jour de Maintenance'
    icons = ['applications-system', 'system-software-update']
    desc = (f'Met à jour l’application depuis son dépôt GitHub ({REPO_URL.split("github.com/")[-1]}) : '
            'le paquet est reconstruit avec makepkg puis installé.')

    def probe(self):
        rc, out, _ = run(['pacman', '-Q', PKG_NAME])
        if rc != 0:
            return {'chip': ('off', 'Hors paquet'), 'standalone': True,
                    'value': f'Cette copie n’est pas installée par le paquet {PKG_NAME}',
                    'hint': 'Lance installer.sh depuis le dépôt pour recevoir les mises à jour automatiquement.'}
        ver = out.split()[1] if len(out.split()) > 1 else '?'
        m = re.match(r'r\d+\.([0-9a-f]{7,})', ver)
        local = m.group(1) if m else ''
        if not have('git'):
            return {'chip': ('warn', 'git absent'), 'value': f'Version {ver}', 'ver': ver}
        rc, out, _ = run(['git', 'ls-remote', REPO_URL + '.git', 'HEAD'], timeout=30)
        if rc != 0 or not out.strip():
            return {'chip': ('warn', 'Dépôt injoignable'), 'value': f'Version {ver}', 'ver': ver,
                    'hint': 'Vérifie la connexion, ou que le dépôt est public.'}
        remote = out.split()[0]
        checked = time.strftime('vérifié à %H:%M')
        if local and remote.startswith(local):
            return {'chip': ('ok', 'À jour'), 'value': f'Version {VERSION} ({ver}) · {checked}', 'ver': ver,
                    'count': 0}
        rver = self._remote_version(remote)
        if rver and version_key(rver) < version_key(VERSION):
            return {'chip': ('warn', 'GitHub en retard'), 'count': 0, 'ver': ver, 'remote_ver': rver, 'older': True,
                    'value': f'Installée : {VERSION} ({ver}) · sur GitHub : {rver} ({remote[:7]}) · {checked}',
                    'hint': 'La version publiée sur GitHub est plus ancienne que la tienne : as-tu bien poussé '
                            'tes changements (git push) ?'}
        det = self._changes(local, remote)
        n = len([d for d in det if not d.startswith('…')])
        newer = f'{rver} ({remote[:7]})' if rver else remote[:7]
        return {'chip': ('info', f'Version {rver} disponible' if rver else 'Nouvelle version'), 'count': 1,
                'new': True, 'ver': ver, 'remote_ver': rver,
                'value': f'Installée : {VERSION} ({ver}) · sur GitHub : {newer}'
                         + (f' · {plural(n, "nouveauté")}' if n else ''),
                'details': det}

    @staticmethod
    def _remote_version(sha):
        """Numéro de version (VERSION = '…') de la version publiée sur GitHub, si on peut le lire."""
        if not REPO_URL.startswith('https://github.com/'):
            return ''
        url = f'https://raw.githubusercontent.com/{REPO_URL.split("github.com/", 1)[1]}/{sha}/maintenance.py'
        try:
            req = urllib.request.Request(url, headers={'Range': 'bytes=0-32767', 'User-Agent': 'maintenance'})
            with urllib.request.urlopen(req, timeout=10) as r:
                head = r.read(32768).decode('utf-8', 'replace')
        except Exception:  # noqa: BLE001
            return ''
        m = re.search(r"^VERSION = '([^']+)'", head, re.M)
        return m.group(1) if m else ''

    @staticmethod
    def _changes(local, remote):
        """Titres des commits arrivés depuis la version installée (API GitHub, facultatif)."""
        if not local:
            return []
        try:
            req = urllib.request.Request(f'{REPO_API}/compare/{local}...{remote}',
                                         headers={'Accept': 'application/vnd.github+json',
                                                  'User-Agent': 'maintenance'})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.load(r)
        except Exception:  # noqa: BLE001
            return []
        commits = data.get('commits') or []
        titles = [(c.get('commit') or {}).get('message', '').split('\n')[0] for c in commits]
        titles = [t for t in titles if t][::-1]
        return titles[:10] + ([f'… et {len(titles) - 10} autres'] if len(titles) > 10 else [])

    def _steps(self):
        d = BUILD_DIR

        def prepare(log):
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(os.path.dirname(d), exist_ok=True)

        return [Step(func=prepare, stop=True, label=f'préparation de {d.replace(HOME, "~")}'),
                Step(['git', 'clone', '--depth', '1', REPO_URL + '.git', d], stop=True),
                Step(func=self._missing_deps, stop=True, label='recherche des dépendances manquantes'),
                Step(f'xargs -r -a {shlex.quote(d)}/.deps pacman -S --needed --noconfirm --asdeps', root=True,
                     pacman=True, stop=True, label='pacman -S --asdeps (dépendances manquantes, s’il y en a)'),
                Step(f'cd {shlex.quote(d)} && makepkg -f --noconfirm --nocolor', stop=True,
                     label='makepkg -f (construction du paquet)'),
                Step(f'pacman -U --noconfirm {shlex.quote(d)}/{PKG_NAME}-*.pkg.tar.*', root=True, pacman=True,
                     label=f'pacman -U {PKG_NAME}-….pkg.tar.zst')]

    @staticmethod
    def _missing_deps(log):
        d = BUILD_DIR
        p = subprocess.run(['bash', '-c', 'source ./PKGBUILD >/dev/null 2>&1; printf "%s\\n" "${depends[@]}"'],
                           cwd=d, capture_output=True, text=True, timeout=60)
        deps = [x.strip() for x in p.stdout.splitlines() if x.strip()]
        missing = []
        if deps:
            rc, out, _ = run(['pacman', '-T'] + deps)
            missing = [re.split(r'[<>=]', x)[0] for x in out.split()]
        with open(os.path.join(d, '.deps'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(missing) + ('\n' if missing else ''))
        log('  ' + (f'à installer : {" ".join(missing)}' if missing else 'toutes les dépendances sont déjà là'))

    def _after(self, ok):
        if ok:
            self.win.offer_restart()

    def actions(self):
        if self.state.get('standalone'):
            return [Action('Vérifier à nouveau', refresh_only=True)]
        acts = []
        if self.state.get('new'):
            acts.append(Action('Mettre à jour', self._steps, style='suggested-action', after=self._after))
        acts.append(Action('Vérifier à nouveau', refresh_only=True))
        return acts

    def cmd_hint(self):
        if self.state.get('standalone'):
            return ''
        return f'$ git clone {REPO_URL}   $ makepkg   # pacman -U {PKG_NAME}'


# ---------------------------------------------------------------- Santé
def list_disks():
    rc, out, _ = run(['lsblk', '-dnpJ', '-o', 'NAME,TYPE,ROTA,SIZE,MODEL,TRAN'])
    try:
        devs = json.loads(out).get('blockdevices', [])
    except ValueError:
        return []
    res = []
    for d in devs:
        name = d.get('name', '')
        if d.get('type') != 'disk' or re.match(r'/dev/(zram|loop|sr|ram)', name):
            continue
        rota = d.get('rota')
        rota = rota in (True, '1', 1)
        res.append({'name': name, 'size': d.get('size') or '', 'model': (d.get('model') or '').strip(), 'ssd': not rota})
    return res


class Smart(Item):
    id, section = 'smart', 'health'
    title = 'Santé des disques (SMART)'
    icons = ['gsmartcontrol', 'drive-harddisk', 'drive-harddisk-system']
    requires, install_pkg = 'smartctl', 'smartmontools'
    desc = 'Résultat de l’auto-diagnostic intégré aux disques. L’analyse ne modifie rien.'

    def probe(self):
        disks = list_disks()
        self.disks = disks
        det = [f'{os.path.basename(d["name"])} · {d["model"] or "modèle inconnu"} · {d["size"]} · '
               f'{"SSD" if d["ssd"] else "disque dur"}' for d in disks]
        return {'chip': ('info', 'Analyse à lancer'), 'value': plural(len(disks), 'disque'), 'details': det}

    def _parse(self, out, rc):
        det, bad, rows = [], 0, []
        for chunk in out.split('@@@ ')[1:]:
            dev, _, js = chunk.partition('\n')
            try:
                data = json.loads(js)
            except ValueError:
                det.append(f'{os.path.basename(dev)} · lecture impossible')
                rows.append([(os.path.basename(dev), 'strong'), '—', ('lecture impossible', 'crit'), '—', '—', '—'])
                continue
            passed = (data.get('smart_status') or {}).get('passed')
            temp = (data.get('temperature') or {}).get('current')
            model = data.get('model_name') or ''
            extra = []
            if temp is not None:
                extra.append(f'{temp} °C')
            wear = (data.get('nvme_smart_health_information_log') or {}).get('percentage_used')
            if wear is not None:
                extra.append(f'usure {wear} %')
            for a in (data.get('ata_smart_attributes') or {}).get('table', []):
                if a.get('id') == 5:
                    extra.append(f'{(a.get("raw") or {}).get("value", 0)} secteur(s) réalloué(s)')
            state = 'OK' if passed else ('EN DIFFICULTÉ' if passed is False else 'état inconnu')
            if passed is False:
                bad += 1
            det.append(' · '.join([os.path.basename(dev), model, state] + extra))
            realloc = next((str((a.get('raw') or {}).get('value', 0)) for a in
                            (data.get('ata_smart_attributes') or {}).get('table', []) if a.get('id') == 5), '—')
            rows.append([(os.path.basename(dev), 'strong'), model or '—',
                         (state, 'ok' if passed else ('crit' if passed is False else '')),
                         f'{temp} °C' if temp is not None else '—', f'{wear} %' if wear is not None else '—', realloc])
        if bad:
            chipv = ('crit', plural(bad, 'disque en difficulté', 'disques en difficulté'))
        elif det:
            chipv = ('ok', 'Disques en bon état')
        else:
            chipv = ('warn', 'Aucun résultat')
        table = {'cols': ['Disque', 'Modèle', 'État', 'Température', 'Usure', 'Secteurs réalloués'], 'rows': rows}
        if rows and all(r[4] == '—' for r in rows):
            table = {'cols': table['cols'][:4] + table['cols'][5:], 'rows': [r[:4] + r[5:] for r in rows]}
        if rows and all(r[-1] == '—' for r in table['rows']):
            table = {'cols': table['cols'][:-1], 'rows': [r[:-1] for r in table['rows']]}
        self.keep(chip=chipv, details=[], table=table if rows else None,
                  value='Analysé ' + time.strftime('le %d/%m à %H:%M'))
        for line in det:
            self.win.journal.add('  ' + line)
        return False

    def actions(self):
        disks = getattr(self, 'disks', [])
        if not disks:
            return []
        names = ' '.join(shlex.quote(d['name']) for d in disks)
        script = f'for d in {names}; do echo "@@@ $d"; smartctl -j -i -H -A "$d"; done'
        return [Action('Analyser', [Step(script, root=True, modifies=False, quiet=True, c_locale=True,
                                         capture=self._parse, label='smartctl -H -A (tous les disques)',
                                         ok_codes=tuple(range(256)))])]


class Trim(Item):
    id, section = 'trim', 'health'
    title = 'TRIM du SSD'
    icons = ['drive-harddisk-solidstate', 'media-flash', 'drive-harddisk']
    desc = ('Indique au SSD quels blocs sont libres pour garder de bonnes performances. '
            'Le mieux est de l’activer chaque semaine.')

    def probe(self):
        ssds = [d for d in list_disks() if d['ssd']]
        if not ssds:
            return {'chip': ('off', 'Aucun SSD'), 'value': 'Pas de SSD détecté', 'no_ssd': True}
        rc, out, _ = run(['systemctl', 'is-enabled', 'fstrim.timer'])
        on = out.strip() == 'enabled'
        return {'chip': ('ok', 'Actif chaque semaine') if on else ('warn', 'Désactivé'), 'enabled': on,
                'value': plural(len(ssds), 'SSD', 'SSD')}

    def actions(self):
        if self.state.get('no_ssd'):
            return []
        acts = []
        if not self.state.get('enabled'):
            acts.append(Action('Activer chaque semaine', [Step(['systemctl', 'enable', '--now', 'fstrim.timer'], root=True)],
                               style='suggested-action'))
        acts.append(Action('Lancer maintenant', [Step(['fstrim', '--listed-in', '/etc/fstab:/proc/self/mountinfo',
                                                        '--verbose', '--quiet-unsupported'], root=True,
                                                       label='fstrim --listed-in /etc/fstab --verbose '
                                                             '--quiet-unsupported')]))
        return acts


class FailedUnits(Item):
    id, section = 'fail', 'health'
    title = 'Services en échec'
    icons = ['dialog-error', 'emblem-important']
    desc = 'Services systemd qui ne se sont pas lancés correctement.'

    @staticmethod
    def _units(user):
        args = ['systemctl'] + (['--user'] if user else []) + ['--failed', '--no-legend', '--plain', '--no-pager']
        rc, out, _ = run(args)
        res = []
        for line in out.splitlines():
            parts = line.split(None, 4)
            if parts:
                res.append((parts[0], parts[4] if len(parts) > 4 else ''))
        return res

    def probe(self):
        sys_u, user_u = self._units(False), self._units(True)
        n = len(sys_u) + len(user_u)
        det = [f'{u} · {d}' for u, d in sys_u] + [f'{u} (session) · {d}' for u, d in user_u]
        if not n:
            return {'chip': ('ok', 'Aucun'), 'value': 'Tous les services fonctionnent'}
        return {'chip': ('crit', plural(n, 'service')), 'details': det, 'sys': [u for u, _ in sys_u],
                'user': [u for u, _ in user_u], 'count': n}

    def actions(self):
        sys_u, user_u = self.state.get('sys', []), self.state.get('user', [])
        if not (sys_u or user_u):
            return [Action('Revérifier', refresh_only=True)]
        logs = [Step(['journalctl', '-b', '-u', u, '-n', '25', '--no-pager'], modifies=False) for u in sys_u]
        logs += [Step(['journalctl', '--user', '-b', '-u', u, '-n', '25', '--no-pager'], modifies=False) for u in user_u]
        reset = []
        if sys_u:
            reset.append(Step(['systemctl', 'reset-failed'], root=True))
        if user_u:
            reset.append(Step(['systemctl', '--user', 'reset-failed']))
        return [Action('Voir les journaux', logs), Action('Effacer l’état d’échec', reset)]


class JournalErrors(Item):
    id, section = 'err', 'health'
    title = 'Erreurs du journal'
    icons = ['dialog-warning', 'text-x-log']
    desc = 'Messages d’erreur enregistrés depuis le dernier démarrage.'

    def probe(self):
        rc, out, _ = run(['journalctl', '-b', '-p', 'err', '-q', '--no-pager', '-o', 'cat'])
        lines = [x for x in out.splitlines() if x.strip()]
        if not lines:
            return {'chip': ('ok', 'Aucune'), 'value': 'Aucune erreur depuis le démarrage'}
        seen, det = set(), []
        for x in reversed(lines):
            if x not in seen:
                seen.add(x)
                det.append(x[:160])
            if len(det) >= 5:
                break
        return {'chip': ('warn', plural(len(lines), 'erreur')), 'details': det, 'count': len(lines)}

    def actions(self):
        acts = [Action('Afficher tout', [Step(['journalctl', '-b', '-p', 'err', '-q', '--no-pager', '-n', '300'],
                                              modifies=False)])]
        acts.append(Action('Vider le journal système',
                           [Step('journalctl --rotate && journalctl --vacuum-time=1s', root=True,
                                 label='journalctl --rotate && journalctl --vacuum-time=1s')],
                           confirm=('Vider le journal système ?',
                                    'Tous les messages enregistrés par systemd seront supprimés, y compris ceux des '
                                    'démarrages précédents. Les nouvelles erreurs réapparaîtront ici.'),
                           destructive=True, refresh=('jrn',)))
        return acts


class DiskSpace(Item):
    id, section = 'space', 'health'
    title = 'Espace par dossier'
    icons = ['org.gnome.baobab', 'baobab', 'drive-harddisk']
    desc = 'Les dossiers qui prennent le plus de place dans ton dossier personnel.'

    def probe(self):
        total, free = disk_usage(HOME)
        return {'chip': None, 'value': f'{fmt_size(free)} libres sur {fmt_size(total)} pour ton dossier personnel'}

    def _scan(self, log):
        res = []
        try:
            entries = os.listdir(HOME)
        except OSError:
            entries = []
        stop = lambda: self.win.runner.cancel_req  # noqa: E731
        for e in entries:
            if stop():
                return []
            p = os.path.join(HOME, e)
            if os.path.islink(p):
                continue
            res.append((dir_size(p, stop=stop)[0], e))
        res.sort(reverse=True)
        return res[:10]

    def _show(self, res, rc):
        if res:
            det = [f'{name} · {fmt_size(size)}' for size, name in res]
            self.keep(details=[], table={'cols': ['Dossier', 'Taille'], 'rows': [[(n, 'strong'), fmt_size(z)] for z, n in res]},
                      chip=('info', f'Le plus gros : {res[0][1]}'))
            for line in det:
                self.win.journal.add('  ' + line)
        return False

    def actions(self):
        acts = [Action('Analyser', [Step(func=self._scan, modifies=False, capture=self._show,
                                         label='calcul de la taille des dossiers de ~')])]
        if have('baobab'):
            acts.append(Action('Ouvrir l’analyseur', handler=lambda: launch_cmd(['baobab', HOME], self.win.journal.add)))
        return acts


# ---------------------------------------------------------------- Sauvegarde
class TimeshiftItem(Item):
    id, section = 'ts', 'backup'
    title = 'Instantané Timeshift'
    icons = ['timeshift', 'document-revert', 'deja-dup']
    requires, install_pkg = 'timeshift', 'timeshift'
    desc = 'Photo du système à un instant donné, pour revenir en arrière si une mise à jour pose problème.'

    def probe(self):
        return {'chip': ('info', 'Non vérifié'), 'value': 'Clique sur « Lister » pour voir les instantanés',
                'hint': 'Si Timeshift n’a jamais été configuré, ouvre-le une première fois pour choisir le disque.'}

    def _parse(self, out, rc):
        snaps = re.findall(r'^[ \t]*\d+[ \t]+>[ \t]+(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)[ \t]*(\S*)[ \t]*(.*)$', out, re.M)
        if not snaps:
            msg = 'Aucun instantané' if rc == 0 else 'Liste impossible (Timeshift est-il configuré ?)'
            self.keep(chip=('warn', msg), value='', listed=False)
        else:
            last = snaps[-1][0]
            ts = time.mktime(time.strptime(last, '%Y-%m-%d_%H-%M-%S'))
            old = time.time() - ts > 7 * 86400
            self.open = True
            self.keep(chip=('warn', f'Dernier {ago(ts)}') if old else ('ok', f'Dernier {ago(ts)}'),
                      value=plural(len(snaps), 'instantané'), listed=True, last=ts, count=len(snaps),
                      details=[f'{s[0].replace("_", " ")}  {s[2]}'.strip() for s in snaps[-5:]])
        return False

    def actions(self):
        return [Action('Créer un instantané',
                       [Step(['timeshift', '--create', '--comments', 'Maintenance', '--tags', 'O', '--scripted'], root=True,
                             critical=True),
                        Step(['timeshift', '--list', '--scripted'], root=True, modifies=False, quiet=True,
                             c_locale=True, capture=self._parse)],
                       style='suggested-action'),
                Action('Lister', [Step(['timeshift', '--list', '--scripted'], root=True, modifies=False,
                                       c_locale=True, capture=self._parse)]),
                Action('Ouvrir Timeshift', handler=lambda: self.win.session.launch(['timeshift-gtk'], 'Timeshift'))]


class PkgList(Item):
    id, section = 'pl', 'backup'
    title = 'Liste des paquets installés'
    icons = ['text-x-generic', 'document-save', 'x-office-document']
    desc = 'Exporte la liste de tes paquets pour tout réinstaller facilement sur une autre machine.'
    f_repo = os.path.join(HOME, 'paquets-depots.txt')
    f_aur = os.path.join(HOME, 'paquets-aur.txt')

    def probe(self):
        det = ['Réinstaller : sudo pacman -S --needed - < ~/paquets-depots.txt',
               '             yay -S --needed - < ~/paquets-aur.txt']
        if os.path.exists(self.f_repo):
            return {'chip': ('ok', 'Exportée ' + ago(os.path.getmtime(self.f_repo))), 'details': det,
                    'value': '~/paquets-depots.txt et ~/paquets-aur.txt'}
        return {'chip': ('info', 'Jamais exportée'), 'details': det}

    def actions(self):
        def export(log):
            for path, flag, what in ((self.f_repo, '-Qqen', 'des dépôts'), (self.f_aur, '-Qqem', 'AUR')):
                rc, out, _ = run(['pacman', flag])
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(out)
                log(f'  {plural(len(out.split()), "paquet")} {what} → {path.replace(HOME, "~")}')
        return [Action('Exporter', [Step(func=export, modifies=False,
                                         label='pacman -Qqen > ~/paquets-depots.txt ; pacman -Qqem > ~/paquets-aur.txt')])]


# ---------------------------------------------------------------- Configuration
def find_pacnew():
    res = []
    for root, dirs, files in os.walk('/etc', onerror=lambda e: None):
        for f in files:
            if f.endswith('.pacnew') or f.endswith('.pacsave'):
                res.append(os.path.join(root, f))
    return sorted(res)


class Pacnew(Item):
    id, section = 'pacnew', 'config'
    title = 'Fichiers .pacnew et .pacsave'
    icons = ['org.gnome.Meld', 'meld', 'text-x-generic']
    desc = 'Nouvelles versions de fichiers de configuration laissées par pacman, à fusionner avec les tiennes.'

    def probe(self):
        files = find_pacnew()
        if not files:
            return {'chip': ('ok', 'Aucun'), 'value': 'Rien à fusionner', 'count': 0}
        return {'chip': ('warn', plural(len(files), 'fichier')), 'details': files, 'files': files,
                'count': len(files)}

    def actions(self):
        if not self.state.get('files'):
            return [Action('Revérifier', refresh_only=True)]
        return [Action('Gérer les fichiers…', handler=self.manage, style='suggested-action')]

    def manage(self):
        win = self.win
        d = Gtk.Dialog(title='Fichiers de configuration à fusionner', transient_for=win, modal=True)
        d.set_default_size(720, 420)
        d.add_button('Fermer', Gtk.ResponseType.CLOSE)
        area = d.get_content_area()
        area.set_spacing(10)
        area.set_border_width(14)
        intro = ('Pour chaque fichier : compare-le avec ta version, garde la tienne, ou prends la nouvelle '
                 '(ton ancienne version est sauvegardée en .bak). ')
        if not have('meld'):
            intro += 'Installe Meld (section Logiciels de maintenance) pour comparer les fichiers.'
        else:
            intro += ('Meld ouvre ta version à gauche et la nouvelle à droite : reporte ce que tu veux garder à gauche, '
                      'enregistre et ferme Meld. Ferme les autres fenêtres de Meld avant.')
        area.pack_start(label(intro, wrap=True, max_chars=90, css=('dim-label',)), False, False, 0)
        lb = boxed_list()
        chosen = {}

        def pick(kind, path):
            chosen['v'] = (kind, path)
            d.response(Gtk.ResponseType.OK)

        for f in self.state.get('files', []):
            orig = re.sub(r'\.(pacnew|pacsave)$', '', f)
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            g = hbox(10)
            for m in ('start', 'end', 'top', 'bottom'):
                getattr(g, f'set_margin_{m}')(8)
            col = vbox(2)
            col.pack_start(label(f, css=('mono',), selectable=True), False, False, 0)
            kind = 'Nouvelle version proposée par pacman' if f.endswith('.pacnew') else \
                'Ancienne configuration gardée après une désinstallation'
            col.pack_start(label(kind, css=('dim-label', 'small')), False, False, 0)
            g.pack_start(col, True, True, 0)
            if have('meld') and os.path.exists(orig):
                b = button('Comparer')
                b.connect('clicked', lambda _b, p=f: pick('diff', p))
                g.pack_start(b, False, False, 0)
            if f.endswith('.pacnew'):
                b = button('Garder la mienne')
                b.connect('clicked', lambda _b, p=f: pick('keep', p))
                g.pack_start(b, False, False, 0)
                b = button('Prendre la nouvelle', style='suggested-action')
                b.connect('clicked', lambda _b, p=f: pick('take', p))
                g.pack_start(b, False, False, 0)
            else:
                b = button('Supprimer', style='destructive-action')
                b.connect('clicked', lambda _b, p=f: pick('keep', p))
                g.pack_start(b, False, False, 0)
            row.add(g)
            lb.add(row)
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(lb)
        area.pack_start(sw, True, True, 0)
        d.show_all()
        r = d.run()
        d.destroy()
        if r != Gtk.ResponseType.OK or 'v' not in chosen:
            return
        kind, path = chosen['v']
        orig = re.sub(r'\.(pacnew|pacsave)$', '', path)
        q, qo = shlex.quote(path), shlex.quote(orig)
        if kind == 'diff':
            work = os.path.join(XDG_CACHE, 'maintenance', 'fusion')
            a = os.path.join(work, os.path.basename(orig))
            b = os.path.join(work, os.path.basename(path))
            qa, qb = shlex.quote(a), shlex.quote(b)
            uid, gid = os.getuid(), os.getgid()
            try:
                os.makedirs(work, mode=0o700, exist_ok=True)   # dossier à toi : Meld doit pouvoir y enregistrer
            except OSError as e:
                self.win.journal.add(f'Impossible de créer {work} : {e}', 'e')
                return
            steps = [Step(f'install -m 600 -o {uid} -g {gid} -- {qo} {qa} && '
                          f'install -m 600 -o {uid} -g {gid} -- {q} {qb}', root=True, stop=True,
                          label='copies de travail des deux fichiers'),
                     Step(['meld', a, b], stop=True, label=f'meld {orig} {path}'),
                     Step(f'cmp -s -- {qa} {qo} || cat -- {qa} > {qo}; rm -f -- {qa} {qb}', root=True,
                          label=f'enregistrement de {orig} si tu l’as modifié')]
            self.trigger(Action('Comparer avec Meld', steps))
            return
        elif kind == 'keep':
            step = Step(['rm', '--', path], root=True)
            label_a = 'Garder ma version'
        else:
            step = Step(f'cp -a -- {qo} {qo}.bak && mv -- {q} {qo}', root=True)
            label_a = 'Prendre la nouvelle version'
        self.trigger(Action(label_a, [step]))


class Integrity(Item):
    id, section = 'integ', 'config'
    title = 'Fichiers des paquets'
    icons = ['security-high', 'emblem-ok', 'dialog-information']
    desc = ('Vérifie que les fichiers installés par les paquets n’ont été ni modifiés ni supprimés. '
            'Cela peut prendre une ou deux minutes.')

    def probe(self):
        return {'chip': ('info', 'Pas encore vérifié')}

    def _parse(self, out, rc):
        backup = missing = altered = 0
        problems = []
        for line in out.splitlines():
            if line.startswith('backup file:'):
                backup += 1
            elif line.startswith('warning:') and 'Permission denied' not in line:
                if 'No such file' in line:
                    missing += 1
                else:
                    altered += 1
                if len(problems) < 10:
                    problems.append(line.replace('warning: ', ''))
        det = problems + [f'{plural(backup, "fichier de configuration modifié", "fichiers de configuration modifiés")} '
                          'par toi (normal)']
        if missing or altered:
            chipv = ('warn', f'{missing} manquant(s), {altered} modifié(s)')
        else:
            chipv = ('ok', 'Aucun problème')
        self.open = True
        self.keep(chip=chipv, details=det, checked=True, value='Vérifié ' + time.strftime('le %d/%m à %H:%M'))
        for line in det:
            self.win.journal.add('  ' + line)
        return False

    def actions(self):
        return [Action('Vérifier', [Step(['pacman', '-Qkk'], modifies=False, quiet=True, c_locale=True,
                                         capture=self._parse, ok_codes=tuple(range(256)))])]


class Kernels(Item):
    id, section = 'kern', 'config'
    title = 'Noyaux installés'
    icons = ['preferences-system', 'computer']
    desc = 'Noyaux présents sur le système, celui en cours d’utilisation et besoin éventuel de redémarrer.'

    def probe(self):
        release = platform.release()
        mod_dir = f'/usr/lib/modules/{release}'
        running = ''
        try:
            with open(os.path.join(mod_dir, 'pkgbase')) as f:
                running = f.read().strip()
        except OSError:
            pass
        rc, out, _ = run(['pacman', '-Q'] + list(KERNELS))
        det = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                tag = ' · en cours d’utilisation' if parts[0] == running else ''
                det.append(f'{parts[0]} {parts[1]}{tag}')
        reboot = not os.path.isdir(mod_dir)
        if reboot:
            det.append(f'Le noyau en cours ({release}) a été mis à jour : redémarre pour utiliser le nouveau.')
        return {'chip': ('warn', 'Redémarrage conseillé') if reboot else ('info', plural(len(det), 'noyau', 'noyaux')),
                'details': det, 'reboot': reboot, 'value': f'En cours : {release}'}


SYM = {
    'dl': ['folder-download-symbolic', 'document-save-symbolic'], 'pkg': ['package-x-generic-symbolic'],
    'orph': ['package-x-generic-symbolic'], 'jrn': ['text-x-generic-symbolic'], 'aur': ['package-x-generic-symbolic'],
    'uc': ['folder-symbolic'], 'th': ['image-x-generic-symbolic'], 'tr': ['user-trash-symbolic'],
    'fpu': ['application-x-addon-symbolic', 'package-x-generic-symbolic'],
    'dk': ['network-server-symbolic', 'package-x-generic-symbolic'],
    'sys': ['software-update-available-symbolic', 'system-software-update-symbolic'],
    'fpk': ['view-app-grid-symbolic', 'application-x-addon-symbolic'],
    'fw': ['computer-chip-symbolic', 'computer-symbolic'],
    'mir': ['network-workgroup-symbolic', 'network-server-symbolic'],
    'key': ['dialog-password-symbolic', 'channel-secure-symbolic'],
    'self': ['software-update-available-symbolic', 'view-refresh-symbolic'],
    'smart': ['drive-harddisk-symbolic'], 'trim': ['drive-harddisk-solidstate-symbolic', 'drive-harddisk-symbolic'],
    'fail': ['system-run-symbolic', 'emblem-system-symbolic'], 'err': ['dialog-warning-symbolic'],
    'space': ['folder-symbolic', 'drive-harddisk-symbolic'],
    'ts': ['document-open-recent-symbolic', 'appointment-soon-symbolic'],
    'pl': ['text-x-generic-symbolic', 'document-save-symbolic'],
    'pacnew': ['document-edit-symbolic', 'text-x-generic-symbolic'],
    'integ': ['security-high-symbolic', 'emblem-ok-symbolic'], 'kern': ['computer-symbolic', 'system-run-symbolic'],
}

ITEM_CLASSES = [DlRemnants, PkgCache, Orphans, Journals, AurCache, AppCaches, Thumbnails, TrashItem,
                FlatpakUnused, DockerItem,
                SelfUpdate, SysUpdate, FlatpakUpdates, Firmware, Mirrors, Keyring,
                Smart, Trim, FailedUnits, JournalErrors, DiskSpace,
                TimeshiftItem, PkgList,
                Pacnew, Integrity, Kernels]
for _c in ITEM_CLASSES:
    _c.sym = SYM.get(_c.id, _c.sym) + ['applications-system-symbolic']


# ============================================================================
#  Ligne d'un élément
# ============================================================================
class ItemRow(Gtk.ListBoxRow):
    def __init__(self, item):
        super().__init__()
        self.item = item
        item.row = self
        self.set_activatable(False)
        self.set_selectable(False)
        self._table_key = None
        g = Gtk.Grid(column_spacing=16)
        g.set_margin_start(20)
        g.set_margin_end(20)
        g.set_margin_top(16)
        g.set_margin_bottom(16)
        col = 0
        self.check = None
        if item.selectable:
            self.check = Gtk.CheckButton()
            self.check.set_valign(Gtk.Align.START)
            self.check.set_margin_top(12)
            self.check.set_tooltip_text('Inclure dans « Nettoyer la sélection »')
            self.check.connect('toggled', self._toggled)
            g.attach(self.check, 0, 0, 1, 1)
            col = 1
        self.tile = tone_tile(item.sym)
        g.attach(self.tile, col, 0, 1, 1)

        mid = vbox(5)
        mid.set_hexpand(True)
        top = hbox(10)
        self.title_l = label(item.title, css=('item-title',))
        top.pack_start(self.title_l, False, False, 0)
        self.chips = hbox(6)
        top.pack_start(self.chips, False, False, 0)
        mid.pack_start(top, False, False, 0)
        self.desc_l = label('', css=('dim-label',), wrap=True, max_chars=90)
        mid.pack_start(self.desc_l, False, False, 0)
        self.value_l = label('', css=('value',), wrap=True, max_chars=90)
        self.value_l.set_no_show_all(True)
        mid.pack_start(self.value_l, False, False, 0)

        self.run_box = vbox(4)
        self.run_box.set_no_show_all(True)
        self.pbar = Gtk.ProgressBar()
        self.pbar.set_pulse_step(0.06)
        self.pbar.set_size_request(380, -1)
        self.pbar.set_halign(Gtk.Align.START)
        self.status_l = label('', css=('status-mono',))
        self.status_l.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_l.set_max_width_chars(80)
        self.run_box.pack_start(self.pbar, False, False, 2)
        self.run_box.pack_start(self.status_l, False, False, 0)
        mid.pack_start(self.run_box, False, False, 0)

        self.table = Gtk.Grid(column_spacing=28)
        self.table.set_halign(Gtk.Align.START)
        self.table.set_no_show_all(True)
        mid.pack_start(self.table, False, False, 2)

        links = hbox(16)
        self.det_btn = Gtk.Button(label='Détails')
        self.det_btn.get_style_context().add_class('link-btn')
        self.det_btn.set_no_show_all(True)
        self.det_btn.connect('clicked', self._toggle_details)
        links.pack_start(self.det_btn, False, False, 0)
        self.cmd_btn = Gtk.Button(label='Voir la commande')
        self.cmd_btn.get_style_context().add_class('link-btn')
        self.cmd_btn.set_no_show_all(True)
        self.cmd_btn.connect('clicked', self._toggle_cmd)
        links.pack_start(self.cmd_btn, False, False, 0)
        mid.pack_start(links, False, False, 0)
        self.cmd_rev = Gtk.Revealer()
        self.cmd_rev.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.cmd_l = label('', css=('cmd',), wrap=True, selectable=True)
        self.cmd_l.set_halign(Gtk.Align.START)
        self.cmd_rev.add(self.cmd_l)
        mid.pack_start(self.cmd_rev, False, False, 0)
        self.det_rev = Gtk.Revealer()
        self.det_rev.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.det_l = label('', css=('details',), wrap=True, selectable=True)
        self.det_rev.add(self.det_l)
        mid.pack_start(self.det_rev, False, False, 2)
        g.attach(mid, col + 1, 0, 1, 1)

        right = hbox(12)
        right.set_valign(Gtk.Align.START)
        self.amount_box = vbox(0)
        self.amount_box.set_no_show_all(True)
        self.amount_l = label('', css=('amount',), xalign=1)
        self.amount_s = label('récupérables', css=('dim-label', 'small'), xalign=1)
        self.amount_box.pack_start(self.amount_l, False, False, 0)
        self.amount_box.pack_start(self.amount_s, False, False, 0)
        right.pack_start(self.amount_box, False, False, 0)
        self.btn_box = hbox(8)
        right.pack_start(self.btn_box, False, False, 0)
        g.attach(right, col + 2, 0, 1, 1)
        self.add(g)
        self.show_all()
        self.update()

    def _toggled(self, cb):
        self.item.selected = cb.get_active()
        self.item.win.selection_changed()

    def _toggle_details(self, _b):
        self.item.open = not self.item.open
        self.update()

    def _toggle_cmd(self, _b):
        self.item.cmd_open = not self.item.cmd_open
        self.update()

    def tick(self, status):
        self.pbar.pulse()
        if self.status_l.get_text() != status:
            self.status_l.set_text(status)

    def _fill_table(self, table):
        key = json.dumps(table, ensure_ascii=False)
        if key == self._table_key:
            return
        self._table_key = key
        clear(self.table)
        for c, name in enumerate(table.get('cols', [])):
            self.table.attach(label(name, css=('th',)), c, 0, 1, 1)
        for r, row in enumerate(table.get('rows', []), 1):
            for c, cell in enumerate(row):
                text, cls = (cell if isinstance(cell, (list, tuple)) else (cell, ''))
                lab = label(str(text), css=('td',) + ((cls,) if cls else ()), selectable=True)
                self.table.attach(lab, c, r, 1, 1)
        for w in self.table.get_children():
            w.show()

    def update(self):
        it, st, win = self.item, self.item.state, self.item.win
        avail = it.available
        runner = win.runner
        running = it.busy and runner.busy
        clear(self.chips)
        kind = None
        if not avail:
            self.chips.pack_start(chip('off', 'non installé'), False, False, 0)
        elif it.probing and st.get('chip') is None:
            self.chips.pack_start(chip('info', 'Analyse…'), False, False, 0)
        elif st.get('chip'):
            kind = st['chip'][0]
            self.chips.pack_start(chip(*st['chip']), False, False, 0)
        if running:
            asked = runner.cancel_req
            self.chips.pack_start(chip('warn' if asked else 'info', 'Arrêt en cours' if asked else 'En cours'),
                                  False, False, 0)
        elif it.done_msg:
            self.chips.pack_start(chip(*it.done_msg), False, False, 0)
        self.chips.show_all()
        set_tone(self.tile, 'off' if not avail else ('info' if running else (kind or 'off')))
        ctx = self.get_style_context()
        (ctx.add_class if running else ctx.remove_class)('running')

        desc = it.desc
        if not avail:
            desc += f' Installe « {it.install_pkg} » pour l’utiliser.' if it.install_pkg else \
                ' Rien à faire tant qu’il n’est pas installé.'
        elif st.get('hint'):
            desc += ' ' + st['hint']
        self.desc_l.set_text(desc)

        val = st.get('value') if avail else ''
        self.value_l.set_text(val or '')
        self.value_l.set_visible(bool(val))

        self.run_box.set_visible(running)
        if running:
            self.pbar.show()
            self.status_l.show()
            self.status_l.set_text(runner.last_line or runner.cur_label or 'En cours…')

        table = st.get('table') if avail else None
        if table and table.get('rows'):
            self._fill_table(table)
            self.table.show()
        else:
            self.table.hide()

        hint = it.cmd_hint()
        self.cmd_l.set_text(hint)
        self.cmd_btn.set_visible(bool(hint))
        self.cmd_btn.set_label('Masquer la commande' if it.cmd_open else 'Voir la commande')
        self.cmd_rev.set_reveal_child(bool(hint) and it.cmd_open)

        det = st.get('details') or []
        self.det_btn.set_visible(bool(det) and avail)
        self.det_btn.set_label('Masquer le détail' if it.open else f'Détails ({len(det)})')
        self.det_l.set_text('\n'.join(det))
        self.det_rev.set_reveal_child(bool(det) and it.open and avail)

        amt = st.get('amount')
        show_amt = it.section == 'clean' and avail and st.get('todo') and bool(amt)
        if show_amt:
            self.amount_l.set_text(fmt_size(amt))
            self.amount_s.set_text('au plus' if it.amount_upper else 'récupérables')
        self.amount_box.set_visible(bool(show_amt))
        self.amount_l.set_visible(bool(show_amt))
        self.amount_s.set_visible(bool(show_amt))

        busy = runner.busy
        if self.check is not None:
            self.check.handler_block_by_func(self._toggled)
            self.check.set_active(it.selected)
            self.check.handler_unblock_by_func(self._toggled)
            self.check.set_sensitive(avail and bool(st.get('todo')) and not busy)

        clear(self.btn_box)
        if running:
            asked = runner.cancel_req
            b = button('Arrêt en cours…' if asked else 'Arrêter', style=None if asked else 'destructive-action',
                       icon_name='process-stop-symbolic')
            b.set_tooltip_text('Arrêter cette opération (Échap)')
            b.set_sensitive(not asked)
            b.connect('clicked', lambda _b: win.stop_current())
            self.btn_box.pack_start(b, False, False, 0)
        elif it.busy:
            self.btn_box.pack_start(label('En attente…', css=('dim-label',)), False, False, 0)
        else:
            for a in it.get_actions():
                b = button(a.label, style=a.style or ('destructive-action' if a.destructive else None),
                           lock=a.lock and not win.session.ok)
                b.set_sensitive(not busy and not (a.needs_todo and not st.get('todo')))
                b.connect('clicked', lambda _b, act=a: it.trigger(act))
                self.btn_box.pack_start(b, False, False, 0)
        self.btn_box.show_all()


# ============================================================================
#  Pages
# ============================================================================
SECTIONS = [
    ('overview', 'Vue d’ensemble', ['go-home-symbolic', 'user-home-symbolic']),
    ('clean', 'Nettoyage', ['edit-clear-all-symbolic', 'edit-clear-symbolic', 'user-trash-symbolic']),
    ('updates', 'Mises à jour', ['software-update-available-symbolic', 'view-refresh-symbolic']),
    ('health', 'Santé du système', ['utilities-system-monitor-symbolic', 'dialog-information-symbolic']),
    ('backup', 'Sauvegarde', ['document-open-recent-symbolic', 'document-save-symbolic']),
    ('config', 'Configuration', ['preferences-system-symbolic', 'emblem-system-symbolic']),
    ('dups', 'Doublons', ['edit-copy-symbolic', 'folder-symbolic']),
    ('apps', 'Applications', ['view-app-grid-symbolic', 'view-grid-symbolic', 'applications-other-symbolic']),
    ('soft', 'Logiciels de maintenance', ['applications-utilities-symbolic', 'applications-system-symbolic',
                                          'preferences-other-symbolic']),
    ('about', 'À propos', ['help-about-symbolic', 'dialog-information-symbolic']),
]
SECTION_TEXT = {
    'clean': ('Nettoyage', 'Coche ce que tu veux nettoyer puis lance la sélection. Chaque ligne peut aussi être lancée seule.'),
    'updates': ('Mises à jour', 'Système et AUR, applications Flatpak, firmware, miroirs et trousseau de clés. '
                                'La mise à jour de l’application elle-même est dans « À propos ».'),
    'health': ('Santé du système', 'Disques, SSD, services et erreurs du journal. Les analyses ne modifient rien.'),
    'backup': ('Sauvegarde', 'Instantanés du système et liste de tes paquets.'),
    'config': ('Configuration', 'Fichiers de configuration à fusionner et état des paquets installés.'),
}


class ItemsPage:
    def __init__(self, win, section):
        self.win = win
        self.widget, inner = scrolled_page()
        head = hbox(12)
        head.pack_start(page_header(*SECTION_TEXT[section]), True, True, 0)
        self.clean_btn = None
        if section == 'clean':
            self.clean_btn = button('Nettoyer la sélection', style='suggested-action', icon_name='edit-clear-all-symbolic')
            self.clean_btn.connect('clicked', lambda _b: win.clean_selection())
            head.pack_start(self.clean_btn, False, False, 0)
        inner.pack_start(head, False, False, 0)
        if section == 'updates':
            inner.pack_start(label('Conseil : crée un instantané Timeshift avant une grosse mise à jour '
                                   '(section Sauvegarde).', css=('banner', 'info'), wrap=True), False, False, 0)
        lb = boxed_list()
        for it in win.items.values():
            if it.section == section:
                lb.add(ItemRow(it))
        inner.pack_start(lb, False, False, 0)
        self.widget.show_all()

    def update(self):
        if self.clean_btn:
            tot, n = self.win.selection_total()
            self.clean_btn.set_label('Nettoyer la sélection' + (f' ({fmt_size(tot)})' if tot else ''))
            self.clean_btn.set_sensitive(n > 0 and not self.win.runner.busy)


class AboutPage:
    """Version, liens du projet, mise à jour de l'application et informations sur le système."""

    def __init__(self, win):
        self.win = win
        self.widget, inner = scrolled_page()
        hero = hbox(22)
        hero.get_style_context().add_class('card')
        hero.pack_start(tone_tile(['applications-system-symbolic', 'preferences-system-symbolic'], 'done',
                                  size=88, icon_size=44, big=True), False, False, 0)
        col = vbox(6)
        col.pack_start(label('Maintenance', css=('about-name',)), False, False, 0)
        self.ver_l = label('', css=('about-version',), selectable=True)
        col.pack_start(self.ver_l, False, False, 0)
        self.state_l = label('', markup=True)
        col.pack_start(self.state_l, False, False, 0)
        col.pack_start(label('Nettoyage, mises à jour et santé d’Arch Linux, en une seule fenêtre GTK3 pensée pour '
                             'Cinnamon.', wrap=True, max_chars=80), False, False, 2)
        links = hbox(2)
        for text, uri in (('Dépôt GitHub', REPO_URL), ('Historique des changements', REPO_URL + '/commits/main'),
                          ('Signaler un problème', REPO_URL + '/issues')):
            lk = Gtk.LinkButton.new_with_label(uri, text)
            lk.set_tooltip_text(uri)
            links.pack_start(lk, False, False, 0)
        col.pack_start(links, False, False, 0)
        col.pack_start(label('Licence MIT, © 2026 TheWorms', css=('dim-label', 'small')), False, False, 0)
        hero.pack_start(col, True, True, 0)
        inner.pack_start(hero, False, False, 0)

        inner.pack_start(label('Mise à jour', css=('section-title',)), False, False, 4)
        lb = boxed_list()
        for it in win.items.values():
            if it.section == 'about':
                lb.add(ItemRow(it))
        inner.pack_start(lb, False, False, 0)

        inner.pack_start(label('Système', css=('section-title',)), False, False, 4)
        grid = Gtk.Grid(column_spacing=24, row_spacing=6)
        grid.get_style_context().add_class('card')
        for i, (k, v) in enumerate(self._system()):
            grid.attach(label(k, css=('dim-label',)), 0, i, 1, 1)
            grid.attach(label(v, selectable=True), 1, i, 1, 1)
        inner.pack_start(grid, False, False, 0)
        self.widget.show_all()

    @staticmethod
    def _system():
        u = platform.uname()
        desk = os.environ.get('XDG_CURRENT_DESKTOP', '').replace('X-', '') or '—'
        gtk = f'{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}'
        return [('Distribution', distro_name()), ('Noyau Linux', u.release), ('Bureau', desk),
                ('Ordinateur', socket.gethostname()), ('GTK', gtk), ('Python', platform.python_version()),
                ('Programme', SELF)]

    def update(self):
        it = self.win.items['self']
        st = it.state
        pkg = st.get('ver')
        self.ver_l.set_text(f'Version {VERSION}' + (f' · paquet {PKG_NAME} {pkg}' if pkg else ''))
        cls = 'info-text'
        if it.probing:
            txt = 'Recherche d’une nouvelle version…'
        elif st.get('new'):
            rv = st.get('remote_ver')
            txt = f'Nouvelle version {rv + " " if rv else ""}disponible : clique sur « Mettre à jour » ci-dessous.'
        elif st.get('standalone'):
            txt, cls = 'Copie hors paquet : pas de mise à jour automatique.', 'warn-text'
        elif st.get('older'):
            txt, cls = (f'GitHub a encore la version {st["remote_ver"]} : tes derniers changements n’y sont pas '
                        'encore poussés.'), 'warn-text'
        elif (st.get('chip') or ('', ''))[0] == 'ok':
            txt, cls = 'Tu as la dernière version.', 'ok-text'
        else:
            txt, cls = (st.get('chip') or ('', ''))[1], 'dim-label'
        ctx = self.state_l.get_style_context()
        for k in ('info-text', 'warn-text', 'ok-text', 'dim-label'):
            ctx.remove_class(k)
        ctx.add_class(cls)
        self.state_l.set_text(txt)


class OverviewPage:
    def __init__(self, win):
        self.win = win
        self.widget, inner = scrolled_page()
        inner.set_spacing(18)
        head = vbox(6)
        self.h1 = label('', css=('hero-title',), wrap=True)
        self.lead = label('', css=('hero-text',), wrap=True, max_chars=86)
        head.pack_start(self.h1, False, False, 0)
        head.pack_start(self.lead, False, False, 0)
        inner.pack_start(head, False, False, 0)
        self.band = hbox(0)
        self.band.set_homogeneous(True)
        self.band.get_style_context().add_class('band')
        inner.pack_start(self.band, False, False, 0)
        inner.pack_start(label('À faire', css=('section-title',)), False, False, 6)
        self.todo = boxed_list()
        inner.pack_start(self.todo, False, False, 0)
        self.widget.show_all()

    def _seg(self, section, title, value, sub, tone, frac, pos):
        b = Gtk.Button()
        b.set_relief(Gtk.ReliefStyle.NONE)
        ctx = b.get_style_context()
        ctx.add_class('seg')
        if pos:
            ctx.add_class(pos)
        col = vbox(0)
        txt = vbox(2)
        txt.set_margin_start(20)
        txt.set_margin_end(20)
        txt.set_margin_top(16)
        txt.set_margin_bottom(14)
        txt.pack_start(label(title, css=('seg-label',)), False, False, 0)
        txt.pack_start(label(value, css=('seg-value',)), False, False, 0)
        txt.pack_start(label(sub, css=('seg-sub',), wrap=True), False, False, 0)
        col.pack_start(txt, True, True, 0)
        bar = Gtk.ProgressBar()
        bar.get_style_context().add_class('segbar')
        bar.get_style_context().add_class(tone)
        bar.set_fraction(max(0.0, min(1.0, frac)))
        col.pack_end(bar, False, False, 0)
        b.add(col)
        b.set_tooltip_text(f'Aller à : {dict((s_[0], s_[1]) for s_ in SECTIONS).get(section, "")}')
        b.connect('clicked', lambda _b: self.win.go(section))
        return b

    def update(self):
        win, items = self.win, self.win.items
        host = socket.gethostname()
        total, free = disk_usage('/')
        frac = (total - free) / total if total else 0
        rec = win.reclaim_total()
        upd = win.updates_count()
        upd_probing = items['sys'].probing
        issues = win.health_issues()
        ts = items['ts']
        reboot = bool(items['kern'].state.get('reboot'))
        pn = items['pacnew'].state.get('count', 0)
        err_n = items['err'].state.get('count', 0)
        crit = any(it.section == 'health' and it.available and (it.state.get('chip') or ('',))[0] == 'crit'
                   for it in items.values())
        ts_old = ts.available and ts.state.get('listed') and time.time() - ts.state['last'] > 7 * 86400

        # Phrase de bilan
        parts = []
        if rec > 50 * 1024 ** 2:
            parts.append(f'{fmt_size(rec)} à libérer')
        if upd:
            parts.append(f'{plural(upd, "mise à jour", "mises à jour")} à installer')
        if reboot:
            parts.append('un redémarrage pour le nouveau noyau')
        if pn:
            parts.append(f'{plural(pn, "fichier")} de configuration à fusionner')
        if items['fail'].state.get('count'):
            parts.append(f'{plural(items["fail"].state["count"], "service")} en échec')
        if err_n:
            parts.append('des erreurs dans le journal à regarder')
        if not ts.available:
            parts.append('aucune sauvegarde du système')
        elif ts_old:
            parts.append('un instantané à refaire')
        if crit:
            self.h1.set_text(f'{host} a besoin d’attention')
        elif not parts:
            self.h1.set_text(f'{host} est au propre')
        elif len(parts) <= 3:
            self.h1.set_text(f'{host} va bien')
        else:
            self.h1.set_text(f'{host} a besoin d’un peu d’entretien')
        if parts:
            joined = ', '.join(parts[:-1]) + ' et ' + parts[-1] if len(parts) > 1 else parts[0]
            lead = f'Il reste {joined}.'
        else:
            lead = 'Rien à faire : tout est propre et à jour.'
        if any(it.probing for it in items.values()):
            lead += ' Analyse en cours…'
        g = win.gained()
        if g > 1024 ** 2:
            lead += f' Gagné depuis l’ouverture : {fmt_size(g)}.'
        self.lead.set_text(lead)

        # Bandeau
        clear(self.band)
        dk = 'crit' if frac > 0.9 else 'warn' if frac > 0.8 else 'ok'
        if not ts.available:
            ts_v, ts_s, ts_k = 'Aucun outil', 'installe Timeshift', 'warn'
        elif ts.state.get('listed'):
            ts_v, ts_s, ts_k = ago(ts.state['last']), plural(ts.state['count'], 'instantané'), 'warn' if ts_old else 'ok'
        else:
            ts_v, ts_s, ts_k = 'Non vérifiée', 'liste les instantanés', 'info'
        if upd_probing:
            up_v, up_s, up_k = '…', 'recherche en cours', 'info'
        elif upd:
            up_v, up_s, up_k = plural(upd, 'disponible'), 'système, AUR et Flatpak', 'info'
        else:
            up_v, up_s, up_k = 'À jour', 'redémarrage conseillé' if reboot else 'système, AUR et Flatpak', \
                'warn' if reboot else 'ok'
        if err_n and issues == 1:
            he_v, he_s = plural(err_n, 'erreur'), 'dans le journal'
        elif issues:
            he_v, he_s = plural(issues, 'point à voir', 'points à voir'), 'disques, services, journal'
        else:
            he_v, he_s = 'Bonne', 'aucun problème détecté'
        segs = [
            ('clean', 'Disque système', f'{fmt_size(free)} libres', f'sur {fmt_size(total)}', dk, frac),
            ('clean', 'Nettoyage', fmt_size(rec) if rec > 50 * 1024 ** 2 else 'Propre',
             'à libérer' if rec > 50 * 1024 ** 2 else 'rien à nettoyer',
             'warn' if rec > 1024 ** 3 else ('info' if rec > 50 * 1024 ** 2 else 'ok'), 1),
            ('updates', 'Mises à jour', up_v, up_s, up_k, 1),
            ('health', 'Santé', he_v, he_s, 'crit' if crit else ('warn' if issues else 'ok'), 1),
            ('backup', 'Sauvegarde', ts_v, ts_s, ts_k, 1),
        ]
        for n, sg in enumerate(segs):
            self.band.pack_start(self._seg(*sg, 'first' if n == 0 else ('last' if n == len(segs) - 1 else '')),
                                 True, True, 0)
        self.band.show_all()

        # À faire
        clear(self.todo)
        todo = []
        tot, n = win.selection_total()
        if n:
            todo.append((['edit-clear-all-symbolic', 'user-trash-symbolic'], 'warn' if tot > 1024 ** 3 else 'info',
                         f'Libérer {fmt_size(tot)}' if tot else 'Nettoyer la sélection',
                         'Selon ta sélection dans Nettoyage.', 'Nettoyer la sélection', win.clean_selection,
                         'suggested-action'))
        if not items['pkg'].available:
            todo.append((['package-x-generic-symbolic'], 'warn', 'Installer pacman-contrib',
                         'Nécessaire pour le cache des paquets (paccache) et la recherche des mises à jour (checkupdates).',
                         'Installer', lambda: items['pkg'].trigger(items['pkg'].get_actions()[0]), 'suggested-action'))
        if items['self'].state.get('new'):
            rv = items['self'].state.get('remote_ver')
            todo.append((['software-update-available-symbolic'], 'done',
                         f'Nouvelle version de Maintenance{" " + rv if rv else ""}',
                         'Tu as poussé des changements sur GitHub : l’application peut se mettre à jour.',
                         'Voir', lambda: win.go('about'), 'suggested-action'))
        if upd:
            todo.append((['software-update-available-symbolic'], 'info',
                         f'{plural(upd, "mise à jour", "mises à jour")} disponible{"s" if upd > 1 else ""}',
                         'Système, AUR et Flatpak.', 'Voir', lambda: win.go('updates'), None))
        if reboot:
            todo.append((['system-reboot-symbolic', 'view-refresh-symbolic'], 'warn', 'Redémarrer l’ordinateur',
                         'Le noyau a été mis à jour : il sera utilisé au prochain démarrage.', None, None, None))
        if items['fail'].state.get('count'):
            todo.append((['dialog-error-symbolic'], 'crit', plural(items['fail'].state['count'], 'service') + ' en échec',
                         (items['fail'].state.get('details') or [''])[0], 'Voir', lambda: win.go('health'), None))
        tr = items['trim']
        if tr.state.get('enabled') is False and not tr.state.get('no_ssd'):
            todo.append((['drive-harddisk-solidstate-symbolic', 'drive-harddisk-symbolic'], 'warn',
                         'TRIM hebdomadaire désactivé', 'Garde les performances du SSD dans la durée.', 'Activer',
                         lambda: tr.trigger(tr.get_actions()[0]), None))
        if pn:
            todo.append((['document-edit-symbolic'], 'warn', f'Fusionner {plural(pn, "fichier")} de configuration',
                         'Fichiers .pacnew et .pacsave laissés par pacman.', 'Gérer', items['pacnew'].manage, None))
        if err_n:
            todo.append((['dialog-warning-symbolic'], 'warn', 'Regarder les erreurs du journal',
                         f'{plural(err_n, "message")} d’erreur depuis le dernier démarrage.', 'Voir',
                         lambda: win.go('health'), None))
        if not ts.available:
            todo.append((['document-open-recent-symbolic'], 'warn', 'Aucune sauvegarde du système',
                         'Timeshift permet de revenir en arrière après une mise à jour qui pose problème.',
                         'Installer Timeshift', lambda: ts.trigger(ts.get_actions()[0]), None))
        elif ts_old:
            todo.append((['document-open-recent-symbolic'], 'warn', 'Créer un instantané',
                         f'Le dernier instantané Timeshift date de {ago(ts.state["last"])}.', 'Voir',
                         lambda: win.go('backup'), None))
        if not todo:
            todo.append((['emblem-ok-symbolic', 'object-select-symbolic'], 'ok', 'Rien à faire',
                         'Tout est propre et à jour.', None, None, None))
        for icons_, tone, title, sub, btn_label, cb, style in todo:
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            g = hbox(16)
            g.set_margin_start(20)
            g.set_margin_end(20)
            g.set_margin_top(14)
            g.set_margin_bottom(14)
            tile = tone_tile(icons_, tone)
            tile.set_valign(Gtk.Align.CENTER)
            g.pack_start(tile, False, False, 0)
            col = vbox(2)
            col.set_valign(Gtk.Align.CENTER)
            col.pack_start(label(title, css=('item-title',)), False, False, 0)
            col.pack_start(label(sub, css=('dim-label',), wrap=True, max_chars=90), False, False, 0)
            g.pack_start(col, True, True, 0)
            if btn_label:
                b = button(btn_label, style=style)
                b.set_sensitive(not win.runner.busy)
                b.connect('clicked', lambda _b, f=cb: f())
                g.pack_start(b, False, False, 0)
            row.add(g)
            self.todo.add(row)
        self.todo.show_all()


# ---------------------------------------------------------------- Doublons
class DupFinder:
    def __init__(self, root, min_size, progress):
        self.root = root
        self.min_size = max(1, min_size)
        self.progress = progress
        self.cancelled = False

    def _hash(self, path, limit=None):
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            if limit:
                h.update(f.read(limit))
            else:
                for chunk in iter(lambda: f.read(1 << 20), b''):
                    if self.cancelled:
                        raise InterruptedError
                    h.update(chunk)
        return h.digest()

    def run(self):
        by_size = defaultdict(list)
        n = 0
        for root, dirs, files in os.walk(self.root, onerror=lambda e: None):
            if self.cancelled:
                return None, n
            dirs[:] = [d for d in dirs if d not in DUP_SKIP and not d.startswith('.Trash-')
                       and not os.path.islink(os.path.join(root, d))]
            for f in files:
                p = os.path.join(root, f)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_size < self.min_size:
                    continue
                by_size[st.st_size].append((p, (st.st_dev, st.st_ino), st.st_mtime))
                n += 1
                if n % 500 == 0:
                    self.progress(n, 'examen')
        cands = {s: lst for s, lst in by_size.items() if len(lst) > 1}
        groups = []
        done = 0
        for size, lst in cands.items():
            if self.cancelled:
                return None, n
            seen, uniq = set(), []
            for p, ino, mt in lst:
                if ino not in seen:
                    seen.add(ino)
                    uniq.append((p, mt))
            if len(uniq) < 2:
                continue
            part = defaultdict(list)
            for p, mt in uniq:
                try:
                    part[self._hash(p, 65536)].append((p, mt))
                except (OSError, InterruptedError):
                    continue
            for plist in part.values():
                if len(plist) < 2:
                    continue
                full = defaultdict(list)
                for p, mt in plist:
                    try:
                        full[self._hash(p) if size > 65536 else b'x'].append((p, mt))
                    except InterruptedError:
                        return None, n
                    except OSError:
                        continue
                for flist in full.values():
                    if len(flist) > 1:
                        groups.append({'size': size, 'files': [{'p': p, 'mt': mt, 'del': False} for p, mt in flist]})
            done += 1
            if done % 50 == 0:
                self.progress(done, 'comparaison')
        groups.sort(key=lambda g: g['size'] * (len(g['files']) - 1), reverse=True)
        return groups, n


class DupsPage:
    MAX_GROUPS = 200

    def __init__(self, win):
        self.win = win
        self.groups = []
        self.finder = None
        outer = vbox(0)
        sw, inner = scrolled_page()
        sw.set_vexpand(True)
        self.inner = inner
        inner.pack_start(page_header('Doublons', 'Trouve les fichiers identiques et mets les copies en trop à la corbeille. '
                                     'Les fichiers sont comparés par taille puis par empreinte SHA-256 ; '
                                     'les liens physiques ne comptent pas comme des doublons.'), False, False, 0)
        bar = hbox(12)
        f1 = vbox(3)
        f1.pack_start(label('Dossier', css=('dim-label', 'small')), False, False, 0)
        self.folder = Gtk.FileChooserButton(title='Dossier à analyser', action=Gtk.FileChooserAction.SELECT_FOLDER)
        self.folder.set_filename(HOME)
        self.folder.set_size_request(260, -1)
        f1.pack_start(self.folder, False, False, 0)
        bar.pack_start(f1, False, False, 0)
        f2 = vbox(3)
        f2.pack_start(label('Taille minimale (Mo)', css=('dim-label', 'small')), False, False, 0)
        self.min = Gtk.SpinButton.new_with_range(0, 100000, 1)
        self.min.set_value(1)
        f2.pack_start(self.min, False, False, 0)
        bar.pack_start(f2, False, False, 0)
        f3 = vbox(3)
        f3.pack_start(label('Copie à garder', css=('dim-label', 'small')), False, False, 0)
        self.rule = Gtk.ComboBoxText()
        self.rule.append('old', 'La plus ancienne')
        self.rule.append('new', 'La plus récente')
        self.rule.append('short', 'Le chemin le plus court')
        self.rule.set_active_id('old')
        self.rule.connect('changed', lambda _c: self._apply_rule())
        f3.pack_start(self.rule, False, False, 0)
        bar.pack_start(f3, False, False, 0)
        self.scan_btn = button('Rechercher', style='suggested-action', icon_name='edit-find-symbolic')
        self.scan_btn.set_valign(Gtk.Align.END)
        self.scan_btn.connect('clicked', lambda _b: self.scan())
        bar.pack_start(self.scan_btn, False, False, 0)
        self.stop_btn = button('Arrêter')
        self.stop_btn.set_valign(Gtk.Align.END)
        self.stop_btn.set_no_show_all(True)
        self.stop_btn.connect('clicked', lambda _b: self.stop())
        bar.pack_start(self.stop_btn, False, False, 0)
        inner.pack_start(bar, False, False, 0)
        self.results = vbox(12)
        inner.pack_start(self.results, False, False, 0)
        outer.pack_start(sw, True, True, 0)
        self.abar = Gtk.ActionBar()
        self.sum_l = label('')
        self.abar.pack_start(self.sum_l)
        self.trash_btn = button('Mettre à la corbeille', style='destructive-action', icon_name='user-trash-symbolic')
        self.trash_btn.connect('clicked', lambda _b: self.trash())
        self.abar.pack_end(self.trash_btn)
        self.abar.set_no_show_all(True)
        outer.pack_start(self.abar, False, False, 0)
        self.widget = outer
        outer.show_all()
        self._status('edit-copy-symbolic', 'Recherche de fichiers en double',
                     'Choisis un dossier puis lance la recherche.')

    def _status(self, icon_name, title, text):
        clear(self.results)
        box = vbox(6)
        box.set_margin_top(40)
        img = symbolic([icon_name, 'dialog-information-symbolic'], 56)
        img.get_style_context().add_class('dim-label')
        box.pack_start(img, False, False, 0)
        box.pack_start(label(f'<b><big>{GLib.markup_escape_text(title)}</big></b>', markup=True, xalign=0.5), False, False, 0)
        box.pack_start(label(text, css=('dim-label',), xalign=0.5, wrap=True, max_chars=70), False, False, 0)
        self.results.pack_start(box, False, False, 0)
        self.results.show_all()
        self.abar.hide()

    def _error(self, text):
        clear(self.results)
        ib = Gtk.InfoBar()
        ib.set_message_type(Gtk.MessageType.ERROR)
        ib.get_content_area().add(label(text, wrap=True))
        self.results.pack_start(ib, False, False, 0)
        self.results.show_all()
        self.abar.hide()

    def scan(self):
        root = self.folder.get_filename() or HOME
        root = os.path.realpath(root)
        if any(root == p or (p != '/' and root.startswith(p + '/')) for p in PROTECTED_DIRS):
            self._error(f'Dossier système protégé : la recherche de doublons est refusée dans {root}.')
            return
        min_b = int(self.min.get_value()) * 1024 * 1024
        clear(self.results)
        box = vbox(8)
        box.pack_start(label(f'Recherche dans <b>{GLib.markup_escape_text(root)}</b>…', markup=True), False, False, 0)
        self.pbar = Gtk.ProgressBar()
        box.pack_start(self.pbar, False, False, 0)
        self.plabel = label('0 fichier examiné', css=('dim-label',))
        box.pack_start(self.plabel, False, False, 0)
        self.results.pack_start(box, False, False, 0)
        self.results.show_all()
        self.abar.hide()
        self.scan_btn.set_sensitive(False)
        self.stop_btn.show()
        self._pulse = GLib.timeout_add(120, lambda: (self.pbar.pulse(), True)[1])
        self.finder = DupFinder(root, min_b, lambda n, ph: GLib.idle_add(self._progress, n, ph))
        self.win.update_stop()
        self.win.journal.add('')
        self.win.journal.add(f'==> Doublons · recherche dans {root}', 'h')

        def work():
            groups, n = self.finder.run()
            GLib.idle_add(self._done, groups, n, root)
        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        if self.finder:
            self.finder.cancelled = True
            self.win.update_stop()

    def _progress(self, n, phase):
        if hasattr(self, 'plabel'):
            self.plabel.set_text(f'{nf(n)} fichiers examinés' if phase == 'examen' else f'{nf(n)} tailles identiques comparées')
        return False

    def _done(self, groups, n, root):
        GLib.source_remove(self._pulse)
        self.scan_btn.set_sensitive(not self.win.runner.busy)
        self.stop_btn.hide()
        self.finder = None
        self.win.update_stop()
        if groups is None:
            self.win.journal.add('  Recherche arrêtée.', 'd')
            self._status('edit-copy-symbolic', 'Recherche arrêtée', 'Relance la recherche quand tu veux.')
            return False
        self.groups = groups
        self.examined = n
        self.root = root
        self._apply_rule(rebuild=False)
        waste = sum(g['size'] * (len(g['files']) - 1) for g in groups)
        self.win.journal.add(f'  {nf(n)} fichiers examinés, {plural(len(groups), "groupe")} de doublons, '
                             f'{fmt_size(waste)} récupérables.')
        self.rebuild()
        return False

    def _apply_rule(self, rebuild=True):
        rule = self.rule.get_active_id()
        for g in self.groups:
            fs = g['files']
            if rule == 'old':
                best = min(range(len(fs)), key=lambda i: fs[i]['mt'])
            elif rule == 'new':
                best = max(range(len(fs)), key=lambda i: fs[i]['mt'])
            else:
                best = min(range(len(fs)), key=lambda i: len(fs[i]['p']))
            for i, f in enumerate(fs):
                f['del'] = i != best
        if rebuild and self.groups:
            self.rebuild()

    def rebuild(self):
        clear(self.results)
        if not self.groups:
            self._status('emblem-ok-symbolic', 'Aucun doublon',
                         f'{nf(getattr(self, "examined", 0))} fichiers examinés dans {getattr(self, "root", HOME)}.')
            return
        waste = sum(g['size'] * (len(g['files']) - 1) for g in self.groups)
        ib = Gtk.InfoBar()
        ib.set_message_type(Gtk.MessageType.INFO)
        txt = (f'{plural(len(self.groups), "groupe")} de doublons · {fmt_size(waste)} récupérables · '
               f'{nf(self.examined)} fichiers examinés. Une copie par groupe est toujours gardée.')
        if len(self.groups) > self.MAX_GROUPS:
            txt += f' Seuls les {self.MAX_GROUPS} groupes qui libèrent le plus de place sont affichés.'
        ib.get_content_area().add(label(txt, wrap=True))
        self.results.pack_start(ib, False, False, 0)
        busy = self.win.runner.busy
        for gidx, g in enumerate(self.groups[:self.MAX_GROUPS]):
            lb = boxed_list()
            head = Gtk.ListBoxRow()
            head.set_activatable(False)
            head.get_style_context().add_class('group-head')
            hb = hbox(12)
            for m in ('start', 'end', 'top', 'bottom'):
                getattr(hb, f'set_margin_{m}')(10 if m in ('top', 'bottom') else 14)
            kept = [f['p'] for f in g['files'] if not f['del']]
            first = kept[0] if kept else g['files'][0]['p']
            ctype, _ = Gio.content_type_guess(first, None)
            img = Gtk.Image.new_from_gicon(Gio.content_type_get_icon(ctype), Gtk.IconSize.DIALOG)
            img.set_pixel_size(36)
            hb.pack_start(img, False, False, 0)
            col = vbox(1)
            col.pack_start(label(os.path.basename(first), css=('item-title',), wrap=True), False, False, 0)
            col.pack_start(label(f'{len(g["files"])} copies · {fmt_size(g["size"])} chacune', css=('dim-label', 'small')),
                           False, False, 0)
            hb.pack_start(col, True, True, 0)
            toast = label('Garde au moins une copie', css=('toast',))
            toast.set_no_show_all(True)
            hb.pack_start(toast, False, False, 0)
            head.add(hb)
            lb.add(head)
            for fi, f in enumerate(g['files']):
                row = Gtk.ListBoxRow()
                fb = hbox(12)
                for m in ('start', 'end', 'top', 'bottom'):
                    getattr(fb, f'set_margin_{m}')(7 if m in ('top', 'bottom') else 14)
                cb = Gtk.CheckButton()
                cb.set_active(f['del'])
                cb.set_sensitive(not busy)
                fb.pack_start(cb, False, False, 0)
                d, base = os.path.split(f['p'])
                d = d.replace(HOME, '~', 1) + '/'
                fb.pack_start(label(f'<span alpha="70%">{GLib.markup_escape_text(d)}</span>{GLib.markup_escape_text(base)}',
                                    markup=True, wrap=True), True, True, 0)
                fb.pack_start(label(time.strftime('%d/%m/%Y', time.localtime(f['mt'])), css=('dim-label', 'small')),
                              False, False, 0)
                ch = chip('crit', 'à supprimer') if f['del'] else chip('ok', 'gardée')
                fb.pack_start(ch, False, False, 0)
                cb.connect('toggled', self._toggled, g, fi, toast)
                row.add(fb)
                lb.add(row)
            self.results.pack_start(lb, False, False, 0)
        self.results.show_all()
        self._update_bar()

    def _toggled(self, cb, g, fi, toast):
        g['files'][fi]['del'] = cb.get_active()
        if all(f['del'] for f in g['files']):
            g['files'][fi]['del'] = False
            cb.handler_block_by_func(self._toggled)
            cb.set_active(False)
            cb.handler_unblock_by_func(self._toggled)
            toast.show()
            GLib.timeout_add(2600, lambda: (toast.hide(), False)[1])
            return
        row_box = cb.get_parent()
        kids = row_box.get_children()
        old = kids[-1]
        row_box.remove(old)
        old.destroy()
        ch = chip('crit', 'à supprimer') if g['files'][fi]['del'] else chip('ok', 'gardée')
        row_box.pack_start(ch, False, False, 0)
        ch.show()
        self._update_bar()

    def _selected(self):
        res = []
        for g in self.groups[:self.MAX_GROUPS]:
            for f in g['files']:
                if f['del']:
                    res.append((f['p'], g['size']))
        return res

    def _update_bar(self):
        sel = self._selected()
        tot = sum(s for _, s in sel)
        self.sum_l.set_markup(f'<b>{len(sel)}</b> fichier{"s" if len(sel) > 1 else ""} à mettre à la corbeille · {fmt_size(tot)}'
                              if sel else 'Aucun fichier coché.')
        self.trash_btn.set_sensitive(bool(sel) and not self.win.runner.busy)
        self.abar.show()
        self.abar.show_all()

    def trash(self):
        sel = self._selected()
        if not sel:
            return
        tot = sum(s for _, s in sel)
        shown = [p.replace(HOME, '~', 1) for p, _ in sel[:12]] + ([f'… et {len(sel) - 12} autres'] if len(sel) > 12 else [])
        txt = f'Ils resteront récupérables dans la corbeille. Environ {fmt_size(tot)}.'
        if not confirm(self.win, f'Mettre {plural(len(sel), "fichier")} à la corbeille ?', txt,
                       ok_label='Mettre à la corbeille', destructive=True, items=shown):
            return
        paths = [p for p, _ in sel]

        def job(log):
            ok = fail = 0
            for p in paths:
                try:
                    Gio.File.new_for_path(p).trash(None)
                    ok += 1
                    log('  corbeille : ' + p.replace(HOME, '~', 1))
                except GLib.Error as e:
                    fail += 1
                    log(f'  échec : {p} ({e.message})', 'e')
            log(f'  {plural(ok, "fichier")} mis à la corbeille, {plural(fail, "échec")}.', 'h')

        def done(ok):
            for g in self.groups:
                g['files'] = [f for f in g['files'] if os.path.exists(f['p'])]
            self.groups = [g for g in self.groups if len(g['files']) > 1]
            self.win.items['tr'].refresh()
            self.rebuild()
            self.win.after_action()

        self.win.runner.start('Doublons · mise à la corbeille',
                              [Step(func=job, label=f'gio trash ({plural(len(paths), "fichier")})')], done)

    def set_busy(self, busy):
        if self.finder is None:
            self.scan_btn.set_sensitive(not busy)
        if self.groups:
            self.trash_btn.set_sensitive(not busy and bool(self._selected()))


# ---------------------------------------------------------------- Applications
class AppsPage:
    def __init__(self, win):
        self.win = win
        self.apps = []
        self.sel = set()
        self.loaded = False
        outer = vbox(0)
        sw, inner = scrolled_page()
        sw.set_vexpand(True)
        inner.pack_start(page_header('Applications', 'Tes applications triées par taille, avec leur icône. '
                                     'Coche celles à désinstaller : les dépendances devenues inutiles partent avec.'),
                         False, False, 0)
        bar = hbox(12)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text('Rechercher une application')
        self.search.connect('search-changed', lambda _e: self.lb.invalidate_filter())
        bar.pack_start(self.search, True, True, 0)
        self.filter = Gtk.ComboBoxText()
        self.filter.append('app', 'Applications')
        self.filter.append('all', 'Tout ce que j’ai installé')
        self.filter.append('aur', 'AUR uniquement')
        self.filter.set_active_id('app')
        self.filter.connect('changed', lambda _c: self.lb.invalidate_filter())
        bar.pack_start(self.filter, False, False, 0)
        inner.pack_start(bar, False, False, 0)
        self.lb = boxed_list()
        self.lb.set_filter_func(self._filter)
        self.lb.connect('row-activated', self._activated)
        inner.pack_start(self.lb, False, False, 0)
        self.empty = label('Chargement de la liste des applications…', css=('dim-label',), xalign=0.5)
        inner.pack_start(self.empty, False, False, 20)
        outer.pack_start(sw, True, True, 0)
        abar = Gtk.ActionBar()
        self.sum_l = label('Coche les applications à désinstaller.')
        abar.pack_start(self.sum_l)
        self.rm_btn = button('Désinstaller', style='destructive-action', lock=True)
        self.rm_btn.set_sensitive(False)
        self.rm_btn.connect('clicked', lambda _b: self.remove())
        abar.pack_end(self.rm_btn)
        outer.pack_start(abar, False, False, 0)
        self.widget = outer
        outer.show_all()

    def load(self):
        self.empty.set_text('Chargement de la liste des applications…')
        self.empty.show()

        def work():
            rc, out, _ = run(['pacman', '-Qqe'])
            explicit = out.split()
            rc, out, _ = run(['pacman', '-Qqm'])
            foreign = set(out.split())
            info = pkg_info(explicit)
            desks = pkg_desktop_files(explicit)
            apps = []
            for n in explicit:
                ai = first_visible_app(desks.get(n, []))
                gi_ = ai.get_icon() if ai else None
                apps.append({'pkg': n, 'name': ai.get_display_name() if ai else n,
                             'desc': (ai.get_description() if ai else '') or info.get(n, {}).get('desc', ''),
                             'icon': gi_.to_string() if gi_ else None, 'size': info.get(n, {}).get('size', 0),
                             'aur': n in foreign, 'app': ai is not None})
            apps.sort(key=lambda a: a['size'], reverse=True)
            GLib.idle_add(self._build, apps)
        POOL.submit(work)

    def _build(self, apps):
        self.apps = apps
        self.sel = {p for p in self.sel if any(a['pkg'] == p for a in apps)}
        clear(self.lb)
        busy = self.win.runner.busy
        for a in apps:
            prot = bool(PROTECTED_RE.match(a['pkg']))
            row = Gtk.ListBoxRow()
            row.app = a
            row.set_activatable(not prot)
            g = hbox(14)
            for m in ('start', 'end', 'top', 'bottom'):
                getattr(g, f'set_margin_{m}')(9 if m in ('top', 'bottom') else 14)
            cb = Gtk.CheckButton()
            cb.set_active(a['pkg'] in self.sel)
            cb.set_sensitive(not prot and not busy)
            cb.connect('toggled', self._toggled, a['pkg'])
            row.cb = cb
            g.pack_start(cb, False, False, 0)
            if a['icon']:
                try:
                    img = Gtk.Image.new_from_gicon(Gio.Icon.new_for_string(a['icon']), Gtk.IconSize.DIALOG)
                except GLib.Error:
                    img = Gtk.Image.new_from_gicon(themed(['package-x-generic']), Gtk.IconSize.DIALOG)
                img.set_pixel_size(36)
            else:
                img = icon(['package-x-generic'], 36)
            g.pack_start(img, False, False, 0)
            col = vbox(2)
            top = hbox(8)
            top.pack_start(label(a['name'], css=('item-title',)), False, False, 0)
            if a['name'] != a['pkg']:
                top.pack_start(label(a['pkg'], css=('mono', 'dim-label')), False, False, 0)
            if a['aur']:
                top.pack_start(chip('info', 'AUR'), False, False, 0)
            if prot:
                top.pack_start(chip('off', 'protégé'), False, False, 0)
            col.pack_start(top, False, False, 0)
            if a['desc']:
                col.pack_start(label(a['desc'], css=('dim-label',), wrap=True, max_chars=80), False, False, 0)
            g.pack_start(col, True, True, 0)
            g.pack_start(label(fmt_size(a['size']), css=('amount',), xalign=1), False, False, 0)
            row.add(g)
            self.lb.add(row)
        self.lb.show_all()
        self.loaded = True
        self.empty.set_visible(False)
        self.lb.invalidate_filter()
        self._update_bar()
        return False

    def _filter(self, row):
        a = getattr(row, 'app', None)
        if not a:
            return True
        f = self.filter.get_active_id()
        if f == 'app' and not a['app']:
            return False
        if f == 'aur' and not a['aur']:
            return False
        q = self.search.get_text().strip().lower()
        return not q or q in f"{a['name']} {a['pkg']} {a['desc']}".lower()

    def _activated(self, lb, row):
        if getattr(row, 'cb', None) and row.cb.get_sensitive():
            row.cb.set_active(not row.cb.get_active())

    def _toggled(self, cb, pkg):
        if cb.get_active():
            self.sel.add(pkg)
        else:
            self.sel.discard(pkg)
        self._update_bar()

    def _update_bar(self):
        chosen = [a for a in self.apps if a['pkg'] in self.sel]
        tot = sum(a['size'] for a in chosen)
        self.sum_l.set_markup(f'<b>{len(chosen)}</b> sélectionnée{"s" if len(chosen) > 1 else ""} · {fmt_size(tot)}'
                              if chosen else 'Coche les applications à désinstaller.')
        self.rm_btn.set_sensitive(bool(chosen) and not self.win.runner.busy)

    def remove(self):
        chosen = [a for a in self.apps if a['pkg'] in self.sel and not PROTECTED_RE.match(a['pkg'])]
        if not chosen:
            return
        tot = sum(a['size'] for a in chosen)
        names = [a['pkg'] for a in chosen]
        txt = f'Environ {fmt_size(tot)}. Les dépendances devenues inutiles sont retirées aussi.'
        if not confirm(self.win, f'Désinstaller {plural(len(chosen), "application")} ?', txt, ok_label='Désinstaller',
                       destructive=True, items=[f'{a["name"]} ({a["pkg"]})' for a in chosen]):
            return

        def done(ok):
            self.sel.clear()
            self.load()
            for rid in ('orph', 'pkg'):
                self.win.items[rid].refresh()
            self.win.after_action()

        self.win.runner.start(f'Applications · désinstallation de {", ".join(names)}',
                              [Step(['pacman', '-Rns', '--noconfirm'] + names, root=True, pacman=True)], done)

    def set_busy(self, busy):
        for row in self.lb.get_children():
            cb = getattr(row, 'cb', None)
            if cb is not None:
                cb.set_sensitive(not busy and not PROTECTED_RE.match(row.app['pkg']))
        self._update_bar()


# ---------------------------------------------------------------- Logiciels
SOFTWARE = [
    ('timeshift', 'Timeshift', 'Sauvegardes et restauration du système.', ['timeshift', 'document-revert']),
    ('bleachbit', 'BleachBit', 'Nettoyage approfondi des caches et des traces des applications.',
     ['bleachbit', 'org.bleachbit.BleachBit', 'edit-clear-all']),
    ('gparted', 'GParted', 'Partitions des disques : créer, redimensionner, formater.', ['gparted', 'drive-harddisk']),
    ('gsmartcontrol', 'GSmartControl', 'Santé détaillée des disques et tests SMART.', ['gsmartcontrol', 'drive-harddisk']),
    ('baobab', 'Analyseur d’utilisation des disques', 'Carte de l’espace occupé, dossier par dossier.',
     ['org.gnome.baobab', 'baobab', 'drive-harddisk']),
    ('gnome-disk-utility', 'Disques', 'Montage, test de vitesse, images disque et état SMART.',
     ['org.gnome.DiskUtility', 'gnome-disks', 'drive-harddisk']),
    ('gnome-system-monitor', 'Moniteur système', 'Processus, mémoire, processeur et réseau.',
     ['org.gnome.SystemMonitor', 'utilities-system-monitor']),
    ('czkawka-gui', 'Czkawka', 'Doublons, images similaires, fichiers vides et liens cassés.',
     ['com.github.qarmin.czkawka', 'czkawka', 'edit-copy']),
    ('meld', 'Meld', 'Compare et fusionne des fichiers, pratique pour les .pacnew.', ['org.gnome.Meld', 'meld']),
    ('btop', 'btop', 'Moniteur de ressources complet dans le terminal.', ['btop', 'utilities-system-monitor']),
]


# Logiciels qui demandent eux-mêmes les droits administrateur (pkexec) : on les ouvre avec la session.
ROOT_GUI = {'gsmartcontrol': ['gsmartcontrol'], 'gparted': ['gparted'], 'timeshift': ['timeshift-gtk']}


class SoftPage:
    def __init__(self, win):
        self.win = win
        self.buttons = []
        self.widget, inner = scrolled_page()
        inner.pack_start(page_header('Logiciels de maintenance', 'Les outils graphiques utiles pour entretenir le système. '
                                     'Installe ceux qui manquent en un clic.'), False, False, 0)
        self.flow = Gtk.FlowBox()
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_min_children_per_line(1)
        self.flow.set_max_children_per_line(3)
        self.flow.set_column_spacing(12)
        self.flow.set_row_spacing(12)
        self.flow.set_homogeneous(True)
        inner.pack_start(self.flow, False, False, 0)
        self.widget.show_all()

    def load(self):
        def work():
            pkgs = [s[0] for s in SOFTWARE]
            inst = installed_set(pkgs)
            desks = pkg_desktop_files(sorted(inst)) if inst else {}
            data = []
            for pkg, name, desc, icons_ in SOFTWARE:
                path = None
                ai = None
                for p in desks.get(pkg, []):
                    try:
                        a = Gio.DesktopAppInfo.new_from_filename(p)
                    except Exception:
                        a = None
                    if a and not a.get_nodisplay():
                        ai, path = a, p
                        break
                gicon = ai.get_icon().to_string() if ai and ai.get_icon() else None
                data.append({'pkg': pkg, 'name': name, 'desc': desc, 'icons': icons_, 'inst': pkg in inst,
                             'desktop': path, 'gicon': gicon})
            GLib.idle_add(self._build, data)
        POOL.submit(work)

    def _build(self, data):
        clear(self.flow)
        self.buttons = []
        busy = self.win.runner.busy
        for s in data:
            card = vbox(10)
            card.get_style_context().add_class('card')
            top = hbox(12)
            if s['gicon']:
                try:
                    img = Gtk.Image.new_from_gicon(Gio.Icon.new_for_string(s['gicon']), Gtk.IconSize.DIALOG)
                    img.set_pixel_size(48)
                except GLib.Error:
                    img = icon(s['icons'], 48)
            else:
                img = icon(s['icons'], 48)
            if not s['inst']:
                img.set_opacity(0.55)
            top.pack_start(img, False, False, 0)
            col = vbox(2)
            col.pack_start(label(s['name'], css=('item-title',), wrap=True), False, False, 0)
            col.pack_start(label(s['desc'], css=('dim-label',), wrap=True, max_chars=34), False, False, 0)
            top.pack_start(col, True, True, 0)
            card.pack_start(top, True, True, 0)
            foot = hbox(8)
            foot.pack_start(chip('ok', 'installé') if s['inst'] else chip('off', 'non installé'), False, False, 0)
            foot.pack_start(label(s['pkg'], css=('mono', 'dim-label')), False, False, 0)
            if s['inst']:
                b = button('Ouvrir')
                b.connect('clicked', lambda _b, x=s: self.open(x))
            else:
                b = button('Installer', style='suggested-action', lock=True)
                b.connect('clicked', lambda _b, x=s: self.install(x))
                b.set_sensitive(not busy)
                self.buttons.append(b)
            foot.pack_end(b, False, False, 0)
            card.pack_start(foot, False, False, 0)
            self.flow.add(card)
        self.flow.show_all()
        return False

    def open(self, s):
        log = self.win.journal.add
        if s['pkg'] in ROOT_GUI:
            self.win.session.launch(ROOT_GUI[s['pkg']], s['name'])
            return
        if s['desktop'] and launch_desktop(s['desktop'], log):
            return
        cmd = {'btop': ['gnome-terminal', '--', 'btop'], 'timeshift': ['timeshift-launcher'],
               'gnome-disk-utility': ['gnome-disks'], 'czkawka-gui': ['czkawka_gui']}.get(s['pkg'], [s['pkg']])
        launch_cmd(cmd, log)

    def install(self, s):
        act = install_action(s['pkg'])

        def done(ok):
            self.load()
            for it in self.win.items.values():
                if it.install_pkg == s['pkg'] or it.requires == s['pkg']:
                    it.refresh()
            if s['pkg'] == 'baobab':
                self.win.items['space'].refresh()
            self.win.after_action()
        self.win.runner.start(f'Logiciels · installation de {s["name"]}', act.get_steps(), done)

    def set_busy(self, busy):
        for b in self.buttons:
            b.set_sensitive(not busy)


# ============================================================================
#  Journal d'activité
# ============================================================================
class Journal:
    MAX_LINES = 6000

    def __init__(self, win):
        self.win = win
        self.widget = vbox(0)
        self.widget.get_style_context().add_class('journal-pane')
        head = hbox(6)
        head.get_style_context().add_class('journal-head')
        head.pack_start(symbolic(['utilities-terminal-symbolic'], 16), False, False, 0)
        head.pack_start(label('<b>Journal d’activité</b>', markup=True), False, False, 0)
        for text, tip, cb in (('Réduire', 'Réduire le journal à sa dernière ligne',
                               lambda _b: GLib.idle_add(win.show_journal, False)),
                              ('Vider', 'Vider le journal d’activité de l’application', lambda _b: self.clear()),
                              ('Copier', 'Copier la sélection, ou tout le journal si rien n’est sélectionné',
                               self.copy)):
            b = Gtk.Button(label=text)
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_tooltip_text(tip)
            b.connect('clicked', cb)
            head.pack_end(b, False, False, 0)
        self.widget.pack_start(head, False, False, 0)
        self.view = Gtk.TextView()
        self.view.set_editable(False)
        self.view.set_cursor_visible(False)
        self.view.set_monospace(True)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_left_margin(12)
        self.view.set_right_margin(12)
        self.view.set_top_margin(6)
        self.view.set_bottom_margin(6)
        self.view.get_style_context().add_class('journal')
        self.buf = self.view.get_buffer()
        for name, props in (('h', {'foreground': '#a5d98b', 'weight': Pango.Weight.BOLD}),
                            ('c', {'foreground': '#8ec5ff'}), ('s', {'foreground': '#f0c46a'}),
                            ('e', {'foreground': '#ff8a82'}), ('d', {'foreground': '#9aa39a'})):
            self.buf.create_tag(name, **props)
        self.end = self.buf.create_mark('end', self.buf.get_end_iter(), False)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.view)
        self.widget.pack_start(sw, True, True, 0)
        self.widget.show_all()

        # Barre réduite : la dernière ligne et un bouton pour rouvrir le journal
        self.bar = hbox(10)
        self.bar.get_style_context().add_class('jbar')
        self.bar.pack_start(symbolic(['utilities-terminal-symbolic'], 16), False, False, 0)
        self.last_l = label('Le journal est vide.', css=('jl-d',))
        self.last_l.set_ellipsize(Pango.EllipsizeMode.END)
        self.bar.pack_start(self.last_l, True, True, 0)
        ob = Gtk.Button(label='Ouvrir le journal')
        ob.connect('clicked', lambda _b: win.show_journal(True))
        self.bar.pack_start(ob, False, False, 0)
        self.bar.set_no_show_all(True)
        for c in self.bar.get_children():
            c.show_all()

    def set_last(self, text, tag):
        if not text.strip():
            return
        ctx = self.last_l.get_style_context()
        for k in ('h', 's', 'e', 'c', 'd'):
            ctx.remove_class('jl-' + k)
        if tag:
            ctx.add_class('jl-' + tag)
        self.last_l.set_text(text.strip())

    def add(self, text, tag=None):
        it = self.buf.get_end_iter()
        if tag:
            self.buf.insert_with_tags_by_name(it, text + '\n', tag)
        else:
            self.buf.insert(it, text + '\n')
        extra = self.buf.get_line_count() - self.MAX_LINES
        if extra > 0:
            self.buf.delete(self.buf.get_start_iter(), self.buf.get_iter_at_line(extra))
        self.buf.move_mark(self.end, self.buf.get_end_iter())
        self.view.scroll_mark_onscreen(self.end)
        self.set_last(text, tag)
        return False

    def log(self, text, tag=None):
        GLib.idle_add(self.add, text, tag)

    def clear(self):
        self.buf.set_text('')
        self.last_l.set_text('Le journal est vide.')

    def copy(self, btn=None):
        sel = self.buf.get_selection_bounds()
        a, b = sel if sel else (self.buf.get_start_iter(), self.buf.get_end_iter())
        txt = self.buf.get_text(a, b, False)
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        cb.set_text(txt, -1)
        cb.store()
        if btn is not None:
            btn.set_label('Copié ✓')
            GLib.timeout_add(1500, lambda: (btn.set_label('Copier'), False)[1])


# ============================================================================
#  Session administrateur
# ============================================================================
class SudoSession:
    """Mot de passe demandé une fois à l'ouverture : un assistant administrateur (lancé par sudo)
    exécute ensuite toutes les commandes administrateur jusqu'à la fermeture de l'application.
    Ça marche même quand sudo ne garde pas le mot de passe en mémoire (timestamp_timeout=0)."""

    def __init__(self, win):
        self.win = win
        self.ok = False
        self.pending = False
        self.proc = None
        self.jobs = {}
        self.next_id = 1
        self.lock = threading.Lock()
        self.start_lock = threading.Lock()
        self.stopping = False
        self.sock = None

    # --- démarrage de l'assistant
    def start(self):
        """Le mot de passe n'est pas demandé à l'ouverture : seulement quand on clique sur
        « Droits non accordés » ou qu'on lance une action administrateur."""
        self._serve()

    def login(self):
        if self.pending or self.ok:
            return
        threading.Thread(target=self.ensure, daemon=True).start()

    def ensure(self):
        """Lance l'assistant si besoin (bloquant : à appeler hors du fil principal)."""
        if self.ok:
            return True
        with self.start_lock:
            if self.ok:
                return True
            self.pending = True
            GLib.idle_add(self.win.update_lock)
            reset_askpass_counter()
            env = dict(os.environ, MAINTENANCE_ASKPASS_CTX='ouverture de la session (le mot de passe reste valable '
                                                           'jusqu’à la fermeture de l’application)')
            try:
                env['SUDO_ASKPASS'] = askpass_wrapper()
            except OSError:
                pass
            p, ready = None, False
            try:
                p = subprocess.Popen(['sudo', '-A', '--', sys.executable, SELF, '--root-helper'],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env=env, start_new_session=True)
                kind, _jid, _d = read_frame(p.stdout)
                ready = kind == b'R'
            except (OSError, EOFError):
                ready = False
            self.pending = False
            if not ready:
                if p is not None:
                    try:
                        p.kill()
                    except OSError:
                        pass
                GLib.idle_add(self._logged, False)
                return False
            self.stopping = False
            self.proc = p
            self.ok = True
            threading.Thread(target=self._reader, args=(p,), daemon=True).start()
            GLib.idle_add(self._logged, True)
            return True

    def _logged(self, ok):
        if ok:
            self.win.journal.add('Droits administrateur accordés jusqu’à la fermeture de l’application.', 'd')
        else:
            self.win.journal.add('Droits administrateur non accordés : ils seront redemandés à la prochaine action '
                                 'administrateur, ou clique sur « Droits non accordés » en haut à droite.', 's')
        self.win.update_lock()
        return False

    def _reader(self, p):
        try:
            while True:
                kind, jid, data = read_frame(p.stdout)
                q = self.jobs.get(jid)
                if q is not None:
                    q.put((kind, data))
        except (EOFError, OSError, ValueError):
            pass
        if self.proc is p:
            self.proc = None
            self.ok = False
            if not self.stopping:
                GLib.idle_add(self._gone)
        with self.lock:
            pending = list(self.jobs.values())
        for q in pending:
            q.put((b'X', struct.pack('!i', 1)))

    def _gone(self):
        self.win.journal.add('L’assistant administrateur s’est arrêté : le mot de passe sera redemandé à la '
                             'prochaine action.', 's')
        self.win.update_lock()
        return False

    # --- exécution
    def run(self, argv, env=None, cwd=None, on_output=None, detach=False, on_start=None):
        """Exécute argv en administrateur ; renvoie le code de retour, ou None sans autorisation."""
        if not self.ensure():
            return None
        q = queue.Queue()
        with self.lock:
            jid = self.next_id
            self.next_id += 1
            self.jobs[jid] = q
            if on_start:
                on_start(jid)
            req = json.dumps({'id': jid, 'argv': [str(a) for a in argv], 'env': env or {},
                              'cwd': cwd or '/', 'detach': detach}) + '\n'
            try:
                self.proc.stdin.write(req.encode('utf-8'))
                self.proc.stdin.flush()
            except (OSError, AttributeError, ValueError):
                self.jobs.pop(jid, None)
                return 1
        try:
            while True:
                kind, data = q.get()
                if kind == b'O':
                    if on_output:
                        on_output(data)
                elif kind == b'X':
                    return struct.unpack('!i', data)[0]
        finally:
            with self.lock:
                self.jobs.pop(jid, None)

    def kill(self, jid):
        """Arrête une commande administrateur en cours (et tout ce qu'elle a lancé)."""
        with self.lock:
            try:
                self.proc.stdin.write((json.dumps({'id': jid, 'op': 'kill'}) + '\n').encode())
                self.proc.stdin.flush()
            except (OSError, AttributeError, ValueError):
                pass

    def launch(self, argv, name):
        """Ouvre un logiciel graphique en administrateur (GParted, GSmartControl…) sans redemander le mot de passe."""
        disp = os.environ.get('DISPLAY', ':0')
        xauth = os.environ.get('XAUTHORITY') or os.path.join(HOME, '.Xauthority')

        def work():
            rc = self.run(['env', f'DISPLAY={disp}', f'XAUTHORITY={xauth}', 'NO_AT_BRIDGE=1'] + list(argv), detach=True)
            if rc == 0:
                GLib.idle_add(self.win.journal.add, f'# {" ".join(argv)}   (« {name} » ouvert avec la session '
                                                     'administrateur)', 'c')
            else:
                GLib.idle_add(self.win.journal.add, f'Impossible d’ouvrir « {name} » en administrateur.', 'e')
        threading.Thread(target=work, daemon=True).start()

    # --- arrêt
    def logout(self, quiet=False):
        p, self.proc = self.proc, None
        self.stopping = True
        self.ok = False
        if p is not None:
            try:
                p.stdin.close()
            except OSError:
                pass
        if not quiet:
            self.win.journal.add('Droits administrateur retirés.', 'd')
            self.win.update_lock()

    def stop(self):
        self.logout(quiet=True)
        s, self.sock = self.sock, None
        if s:
            try:
                s.close()
                os.unlink(RELAY_SOCK)
            except OSError:
                pass

    # --- relais local : fenêtre de mot de passe et « sudo pacman » de yay
    def _serve(self):
        try:
            os.makedirs(SESSION_DIR, mode=0o700, exist_ok=True)
            os.chmod(SESSION_DIR, 0o700)
            try:
                os.unlink(RELAY_SOCK)
            except FileNotFoundError:
                pass
            sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sk.bind(RELAY_SOCK)
            os.chmod(RELAY_SOCK, 0o600)
            sk.listen(4)
            self.sock = sk
            threading.Thread(target=self._accept, daemon=True).start()
        except OSError as e:
            self.win.journal.add(f'Relais local indisponible ({e}) : yay demandera son propre mot de passe.', 's')

    def _accept(self):
        while self.sock is not None:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            try:
                raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
                pid, uid, _gid = struct.unpack('3i', raw)
                if uid != os.getuid() or not is_descendant(pid, os.getpid()):
                    sock_send(conn, b'N', 'maintenance : demande refusée (programme extérieur à l’application).'.encode())
                    return
                line = conn.makefile('rb').readline(1 << 20)
                req = json.loads(line.decode('utf-8'))
                if 'askpass' in req:
                    pw = self._ask_in_app(req)
                    if pw is None:
                        sock_send(conn, b'X', struct.pack('!i', 1))
                    else:
                        sock_send(conn, b'P', pw.encode('utf-8'))
                    return
                argv = [str(a) for a in req.get('argv', [])]
                if not argv or all(a.startswith('-') for a in argv):
                    # « sudo -v » (boucle sudo de yay) : la session est-elle ouverte ?
                    sock_send(conn, b'X', struct.pack('!i', 0 if self.ensure() else 1))
                    return
                if os.path.basename(argv[0]) != 'pacman':
                    sock_send(conn, b'N', f'maintenance : seule la commande pacman est relayée (reçu : {argv[0]}).'.encode())
                    return
                rc = self.run(['pacman'] + argv[1:], cwd=req.get('cwd'),
                              on_output=lambda d: sock_send(conn, b'O', d))
                sock_send(conn, b'X', struct.pack('!i', 126 if rc is None else rc))
            except (OSError, ValueError, EOFError, struct.error):
                pass

    def _ask_in_app(self, req):
        done = threading.Event()
        res = {}

        def show():
            dlg, entry = password_dialog(str(req.get('askpass') or ''), str(req.get('ctx') or ''),
                                         int(req.get('tries') or 0), parent=self.win)

            def answer(d, r):
                txt = entry.get_text()
                res['pw'] = txt if r == Gtk.ResponseType.OK and txt else None
                d.destroy()
                done.set()
            dlg.connect('response', answer)
            dlg.show_all()
            dlg.present()
            entry.grab_focus()
            return False
        GLib.idle_add(show)
        done.wait()
        return res.get('pw')


# ============================================================================
#  Fenêtre principale
# ============================================================================
PALETTES = {
    'sombre': {'bg': '#1A1D1F', 'head': '#202427', 'side': '#16191B', 'surface': '#222629', 'surface2': '#2C3134',
               'line': '#33393D', 'fg': '#E6E9E7', 'dim': '#A4ADA8', 'accent': '#93BE6B', 'on_accent': '#142009',
               'ok': '#86C98A', 'warn': '#EDB656', 'crit': '#F27A68', 'info': '#79B8EC', 'term': '#14181A',
               'term_fg': '#D3DAD5', 'term_dim': '#8E9893', 'term_line': '#262C2F', 'hover': 'rgba(255,255,255,0.05)'},
    'clair': {'bg': '#F3F5F4', 'head': '#E7EBE9', 'side': '#EBEFED', 'surface': '#FFFFFF', 'surface2': '#EEF2F0',
              'line': '#D5DCD8', 'fg': '#1B211F', 'dim': '#545E59', 'accent': '#4C7D29', 'on_accent': '#FFFFFF',
              'ok': '#2E7A38', 'warn': '#8F5B00', 'crit': '#B83A2C', 'info': '#1D69A8', 'term': '#1A1F21',
              'term_fg': '#D3DAD5', 'term_dim': '#8E9893', 'term_line': '#2C3336', 'hover': 'rgba(0,0,0,0.045)'},
}

CSS = """
@define-color m_accent_soft alpha(@m_accent, 0.16);
@define-color m_ok_soft alpha(@m_ok, 0.15);
@define-color m_warn_soft alpha(@m_warn, 0.15);
@define-color m_crit_soft alpha(@m_crit, 0.14);
@define-color m_info_soft alpha(@m_info, 0.14);
@define-color m_run_bg alpha(@m_info, 0.06);

window.maint, window.maint.background { background-color: @m_bg; color: @m_fg; FONTSANS }
window.maint label { text-shadow: none; }
window.maint list, window.maint list > row, window.maint viewport, window.maint scrolledwindow, window.maint stack,
window.maint grid, window.maint messagedialog .dialog-vbox { color: @m_fg; }
window.maint label selection, window.maint entry selection, window.maint textview text selection {
  background-color: @m_accent; color: @m_on_accent; }
window.maint tooltip { text-shadow: none; }

/* Barre de titre */
window.maint headerbar { background: @m_head; border: none; border-bottom: 1px solid @m_line; box-shadow: none;
  color: @m_fg; min-height: 50px; padding: 0 8px; }
window.maint headerbar:backdrop { background: @m_head; }
window.maint headerbar .title { color: @m_fg; font-weight: 600; }
window.maint headerbar .subtitle { color: @m_dim; opacity: 1; }
window.maint headerbar button.titlebutton { background: transparent; border: none; box-shadow: none; color: @m_fg;
  min-width: 28px; min-height: 28px; padding: 2px; border-radius: 999px; }
window.maint headerbar button.titlebutton:hover { background: @m_hover; }

/* Boutons */
window.maint button { background: @m_surface2; background-image: none; color: @m_fg; border: 1px solid @m_line;
  border-radius: 8px; box-shadow: none; text-shadow: none; -gtk-icon-shadow: none; min-height: 24px; padding: 5px 14px; }
window.maint button:hover { background: shade(@m_surface2, 1.08); }
window.maint button:active, window.maint button:checked { background: @m_accent_soft; border-color: @m_accent; }
window.maint button:disabled { background: @m_surface2; border-color: @m_line; color: @m_dim; opacity: 0.55; }
window.maint button:disabled label { color: @m_dim; }
window.maint button label, window.maint button image { color: inherit; }
window.maint button.suggested-action { background: @m_accent; color: @m_on_accent; border-color: @m_accent; font-weight: 600; }
window.maint button.suggested-action:hover { background: shade(@m_accent, 1.06); }
window.maint button.suggested-action:disabled { background: @m_accent; color: @m_on_accent; }
window.maint button.suggested-action:disabled label { color: @m_on_accent; }
window.maint button.destructive-action { background: @m_crit_soft; color: @m_crit; border-color: @m_crit; font-weight: 600; }
window.maint button.destructive-action:hover { background: alpha(@m_crit, 0.22); }
window.maint button.destructive-action:disabled label { color: @m_crit; }
window.maint button.flat, window.maint button.m-flat { background: transparent; border-color: transparent; }
window.maint button.flat:hover, window.maint button.m-flat:hover { background: @m_hover; }
window.maint button.icon-btn { padding: 6px; min-width: 24px; background: @m_surface; }
window.maint button.pill { border-radius: 999px; padding: 4px 14px 4px 10px; background: @m_surface; }
window.maint button.pill.ok image { color: @m_ok; }
window.maint button.pill.off image { color: @m_dim; }
window.maint box.pill { border: 1px solid @m_line; border-radius: 999px; background: @m_surface; padding: 2px 6px 2px 12px; }
window.maint button.link-btn { background: transparent; border: none; padding: 0 2px; min-height: 0; }
window.maint button.link-btn label { color: @m_accent; font-size: 93%; }
window.maint button.link-btn:hover label { text-decoration: underline; }
window.maint button.link { background: transparent; border: none; padding: 2px 4px; }
window.maint *:link, window.maint button.link label { color: @m_accent; }
window.maint spinbutton button { border: none; border-left: 1px solid @m_line; border-radius: 0; background: transparent; }

/* Champs */
window.maint entry, window.maint spinbutton { background: @m_bg; background-image: none; color: @m_fg;
  border: 1px solid @m_line; border-radius: 8px; box-shadow: none; min-height: 30px; }
window.maint spinbutton entry { border: none; background: transparent; min-height: 0; }
window.maint entry:focus, window.maint spinbutton:focus { border-color: @m_accent; }
window.maint entry image { color: @m_dim; }

/* Cases, interrupteurs, barres */
window.maint check { min-width: 16px; min-height: 16px; border: 1px solid @m_dim; border-radius: 4px;
  background: @m_surface; background-image: none; box-shadow: none; -gtk-icon-source: none; color: @m_on_accent; }
window.maint check:checked { background: @m_accent; border-color: @m_accent;
  -gtk-icon-source: -gtk-icontheme("object-select-symbolic"); }
window.maint check:disabled { opacity: 0.5; }
window.maint switch { background: @m_surface2; background-image: none; border: 1px solid @m_line; border-radius: 999px;
  color: transparent; box-shadow: none; min-width: 40px; min-height: 20px; }
window.maint switch image { color: transparent; -gtk-icon-source: none; }
window.maint switch:checked { background: @m_warn; border-color: @m_warn; }
window.maint switch slider { background: @m_dim; background-image: none; border: none; border-radius: 999px;
  min-width: 18px; min-height: 18px; margin: 1px; box-shadow: none; }
window.maint switch:checked slider { background: @m_surface; }
window.maint progressbar trough { background: @m_surface2; background-image: none; border: none; border-radius: 999px;
  min-height: 5px; min-width: 0; box-shadow: none; }
window.maint progressbar progress { background: @m_info; background-image: none; border: none; border-radius: 999px;
  min-height: 5px; box-shadow: none; }
window.maint progressbar.ok progress { background: @m_ok; }
window.maint progressbar.warn progress { background: @m_warn; }
window.maint progressbar.crit progress { background: @m_crit; }
window.maint progressbar.segbar trough, window.maint progressbar.segbar progress { border-radius: 0; }
window.maint paned > separator { background: @m_line; background-image: none; min-height: 1px; min-width: 1px; }
window.maint actionbar > revealer > box { background: @m_head; background-image: none; border-top: 1px solid @m_line;
  box-shadow: none; color: @m_fg; }

/* Menu de gauche */
window.maint box.sidebar { background: @m_side; border-right: 1px solid @m_line; }
window.maint list.sidebar { background: @m_side; padding: 8px 0; }
window.maint list.sidebar > row { border-radius: 8px; margin: 1px 8px; background: transparent; color: @m_fg; }
window.maint list.sidebar > row:hover { background: @m_hover; }
window.maint list.sidebar > row:selected { background: @m_accent_soft; color: @m_fg; }
window.maint list.sidebar > row:selected label { color: @m_fg; font-weight: 600; }
window.maint list.sidebar > row image { color: @m_dim; }
window.maint list.sidebar > row:selected image { color: @m_accent; }
window.maint list.sidebar separator { background: @m_line; margin: 8px 16px; min-height: 1px; }
window.maint .side-group { color: @m_dim; font-size: 86%; margin: 14px 20px 4px 20px; }
window.maint .sidebar-foot { padding: 12px 20px 14px 20px; border-top: 1px solid @m_line; }
window.maint .badge { border-radius: 999px; padding: 0 8px; font-size: 82%; font-weight: 600; }
window.maint .badge.warn { color: @m_warn; background-color: @m_warn_soft; }
window.maint .badge.crit { color: @m_crit; background-color: @m_crit_soft; }
window.maint .badge.info { color: @m_info; background-color: @m_info_soft; }

/* Pages */
window.maint .page-title { font-size: 185%; font-weight: 600; }
window.maint .hero-title { font-size: 245%; font-weight: 600; }
window.maint .hero-text { font-size: 115%; color: @m_dim; }
window.maint .section-title { font-size: 118%; font-weight: 600; }
window.maint .dim-label { color: @m_dim; opacity: 1; }
window.maint .small { font-size: 88%; }
window.maint .item-title { font-weight: 600; font-size: 104%; }
window.maint .value { font-weight: 500; }
window.maint .mono { MONO font-size: 92%; }
window.maint list.boxed { background: @m_surface; border: 1px solid @m_line; border-radius: 14px; }
window.maint list.boxed > row { background: transparent; border-bottom: 1px solid @m_line; }
window.maint list.boxed > row:last-child { border-bottom-width: 0; }
window.maint list.boxed > row:hover { background: transparent; }
window.maint list.boxed > row.running { background: @m_run_bg; }
window.maint list.boxed > row.group-head { background: alpha(@m_fg, 0.04); }
window.maint list.boxed > row.activatable:hover { background: @m_hover; }
window.maint .chip { border-radius: 999px; padding: 1px 9px; font-size: 85%; font-weight: 600; }
window.maint .chip.ok { color: @m_ok; background-color: @m_ok_soft; }
window.maint .chip.warn { color: @m_warn; background-color: @m_warn_soft; }
window.maint .chip.crit { color: @m_crit; background-color: @m_crit_soft; }
window.maint .chip.info { color: @m_info; background-color: @m_info_soft; }
window.maint .chip.off { color: @m_dim; background-color: @m_surface2; }
window.maint .chip.done { color: @m_accent; background-color: @m_accent_soft; }
window.maint box.tile { border-radius: 10px; }
window.maint box.tile.big { border-radius: 22px; }
window.maint box.tile.ok { background-color: @m_ok_soft; } window.maint box.tile.ok image { color: @m_ok; }
window.maint box.tile.warn { background-color: @m_warn_soft; } window.maint box.tile.warn image { color: @m_warn; }
window.maint box.tile.crit { background-color: @m_crit_soft; } window.maint box.tile.crit image { color: @m_crit; }
window.maint box.tile.info { background-color: @m_info_soft; } window.maint box.tile.info image { color: @m_info; }
window.maint box.tile.off { background-color: @m_surface2; } window.maint box.tile.off image { color: @m_dim; }
window.maint box.tile.done { background-color: @m_accent_soft; } window.maint box.tile.done image { color: @m_accent; }
window.maint .cmd { MONO font-size: 88%; padding: 4px 10px; border-radius: 6px; background-color: @m_surface2; }
window.maint .details { MONO font-size: 88%; padding: 8px 12px; border-radius: 8px; background-color: @m_surface2; }
window.maint .status-mono { MONO font-size: 88%; color: @m_dim; }
window.maint .th { color: @m_dim; font-weight: 500; padding: 4px 0; border-bottom: 1px solid @m_line; }
window.maint .td { padding: 5px 0; border-bottom: 1px solid @m_line; }
window.maint .td.ok { color: @m_ok; font-weight: 600; }
window.maint .td.crit { color: @m_crit; font-weight: 600; }
window.maint .td.strong { font-weight: 600; }
window.maint .amount { font-size: 112%; font-weight: 600; }
window.maint .toast { color: @m_crit; font-weight: 600; }
window.maint .card { border: 1px solid @m_line; border-radius: 14px; background-color: @m_surface; padding: 18px 20px; }
window.maint .about-name { font-size: 190%; font-weight: 600; }
window.maint .about-version { color: @m_dim; }
window.maint .ok-text { color: @m_ok; font-weight: 600; }
window.maint .warn-text { color: @m_warn; font-weight: 600; }
window.maint .info-text { color: @m_info; font-weight: 600; }
window.maint .banner { padding: 9px 16px; }
window.maint .banner.warn { background-color: @m_warn_soft; border-bottom: 1px solid @m_line; }
window.maint .banner.info { background-color: @m_info_soft; border-radius: 10px; padding: 10px 14px; }

/* Bandeau de la vue d'ensemble */
window.maint box.band { background-color: @m_surface; border: 1px solid @m_line; border-radius: 14px; }
window.maint button.seg { background: transparent; border: none; border-right: 1px solid @m_line; border-radius: 0;
  padding: 0; }
window.maint button.seg.first { border-radius: 14px 0 0 14px; }
window.maint button.seg.last { border-right: none; border-radius: 0 14px 14px 0; }
window.maint button.seg:hover { background: @m_hover; }
window.maint .seg-label, window.maint .seg-sub { color: @m_dim; font-size: 92%; }
window.maint .seg-value { font-size: 160%; font-weight: 600; }

/* Journal */
window.maint .journal-head { background-color: @m_term; color: @m_term_fg; padding: 3px 10px 3px 16px;
  border-top: 1px solid @m_line; border-bottom: 1px solid @m_term_line; }
window.maint .journal-head label, window.maint .journal-head image, window.maint .jbar label,
window.maint .jbar image { color: @m_term_fg; }
window.maint .journal-head button, window.maint .jbar button { background: transparent; border: 1px solid transparent;
  color: @m_term_fg; padding: 3px 12px; }
window.maint .journal-head button:hover, window.maint .jbar button:hover { background: alpha(#ffffff, 0.08); }
window.maint .jbar button { border-color: @m_term_line; }
window.maint textview.journal, window.maint textview.journal text { background-color: @m_term; color: @m_term_fg; MONO }
window.maint .jbar { background-color: @m_term; padding: 4px 10px 4px 16px; border-top: 1px solid @m_line; }
window.maint .jbar label.jl-h { color: #a5d98b; }
window.maint .jbar label.jl-s { color: #f0c46a; }
window.maint .jbar label.jl-e { color: #ff8a82; }
window.maint .jbar label.jl-c { color: #8ec5ff; }
window.maint .jbar label.jl-d { color: @m_term_dim; }
"""


def build_css(pal, sans, mono):
    head = ''.join(f'@define-color m_{k} {v};\n' for k, v in pal.items())
    body = CSS.replace('FONTSANS', f'font-family: {sans};' if sans else '')
    body = body.replace('MONO', f'font-family: {mono};')
    return (head + body).encode('utf-8')




class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app=None):
        super().__init__(application=app) if app else super().__init__()
        self.settings = load_settings()
        self.runner = Runner(self)
        self.set_title('Maintenance')
        self.set_icon_name('applications-system')
        w, h = self.settings.get('taille', [1180, 820])
        self.set_default_size(w, h)
        self._dirty = False
        self.start_free = disk_usage('/')[1]

        self.get_style_context().add_class('maint')
        gs = Gtk.Settings.get_default()
        ok_c, c = self.get_style_context().lookup_color('theme_bg_color')
        self.sys_dark = (bool(gs.get_property('gtk-application-prefer-dark-theme'))
                         or 'dark' in (gs.get_property('gtk-theme-name') or '').lower()
                         or (bool(ok_c) and (0.2126 * c.red + 0.7152 * c.green + 0.0722 * c.blue) < 0.5))
        fams = {f.get_name() for f in self.get_pango_context().list_families()}
        self.font_sans = "'IBM Plex Sans', sans-serif" if 'IBM Plex Sans' in fams else ''
        self.font_mono = "'IBM Plex Mono', monospace" if 'IBM Plex Mono' in fams else 'monospace'
        self.css_prov = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), self.css_prov,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.apply_theme()

        # Barre de titre
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.props.title = 'Maintenance'
        desk = os.environ.get('XDG_CURRENT_DESKTOP', 'Linux').split(':')[0]
        desk = desk[2:] if desk.startswith('X-') else desk
        self.base_sub = f'{distro_name()} · {desk} · {socket.gethostname()}'
        hb.props.subtitle = self.base_sub
        self.hb = hb
        self.set_titlebar(hb)
        self.refresh_btn = Gtk.Button()
        self.refresh_btn.get_style_context().add_class('icon-btn')
        self.refresh_btn.set_image(symbolic(['view-refresh-symbolic'], 16))
        self.refresh_btn.set_tooltip_text('Tout analyser à nouveau (F5)')
        self.refresh_btn.connect('clicked', lambda _b: self.refresh_all())
        hb.pack_start(self.refresh_btn)
        self.spinner = Gtk.Spinner()
        hb.pack_start(self.spinner)
        self.jbtn = Gtk.ToggleButton()
        self.jbtn.get_style_context().add_class('icon-btn')
        self.jbtn.set_image(symbolic(['utilities-terminal-symbolic'], 16))
        self.jbtn.set_tooltip_text('Journal d’activité')
        self.jbtn.connect('toggled', lambda b: self.show_journal(b.get_active()))
        hb.pack_end(self.jbtn)
        self.theme_btn = Gtk.Button()
        self.theme_btn.get_style_context().add_class('icon-btn')
        self.theme_btn.connect('clicked', lambda _b: self.toggle_theme())
        hb.pack_end(self.theme_btn)
        self.update_theme_btn()
        self.lock_btn = Gtk.Button()
        self.lock_btn.get_style_context().add_class('pill')
        lb_box = hbox(8)
        self.lock_img = symbolic(['changes-prevent-symbolic'], 16)
        self.lock_lbl = label('Droits non accordés')
        lb_box.pack_start(self.lock_img, False, False, 0)
        lb_box.pack_start(self.lock_lbl, False, False, 0)
        self.lock_btn.add(lb_box)
        self.lock_btn.set_valign(Gtk.Align.CENTER)
        self.lock_btn.connect('clicked', lambda _b: self._lock_clicked())
        hb.pack_end(self.lock_btn)
        self.session = SudoSession(self)

        # Éléments
        self.items = {}
        for cls in ITEM_CLASSES:
            it = cls(self)
            self.items[it.id] = it

        # Corps
        body = hbox(0)
        side = vbox(0)
        side.set_size_request(240, -1)
        side.get_style_context().add_class('sidebar')
        self.side_list = Gtk.ListBox()
        self.side_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.side_list.get_style_context().add_class('sidebar')
        self.badges = {}
        for sid, name, icons_ in SECTIONS:
            row = Gtk.ListBoxRow()
            row.sid = sid
            b = hbox(10)
            for m in ('start', 'end'):
                getattr(b, f'set_margin_{m}')(12)
            b.set_margin_top(8)
            b.set_margin_bottom(8)
            b.pack_start(symbolic(icons_, 16), False, False, 0)
            b.pack_start(label(name), True, True, 0)
            badge = label('', css=('badge',))
            badge.set_no_show_all(True)
            b.pack_start(badge, False, False, 0)
            self.badges[sid] = badge
            row.add(b)
            self.side_list.add(row)
        self.side_list.set_header_func(self._side_header)
        self.side_list.connect('row-selected', self._section_selected)
        ssw = Gtk.ScrolledWindow()
        ssw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        ssw.add(self.side_list)
        side.pack_start(ssw, True, True, 0)
        foot = vbox(4)
        foot.get_style_context().add_class('sidebar-foot')
        self.disk_l = label('', css=('small',), markup=True)
        self.disk_pb = Gtk.ProgressBar()
        self.disk_pb.get_style_context().add_class('ok')
        self.gain_l = label('', css=('small', 'dim-label'))
        self.gain_l.set_no_show_all(True)
        foot.pack_start(self.disk_l, False, False, 0)
        foot.pack_start(self.disk_pb, False, False, 0)
        foot.pack_start(self.gain_l, False, False, 0)
        side.pack_start(foot, False, False, 0)
        body.pack_start(side, False, False, 0)

        right = vbox(0)
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(120)
        self.journal = Journal(self)
        self.pages = {}
        self.pages['overview'] = OverviewPage(self)
        for sec in ('clean', 'updates', 'health', 'backup', 'config'):
            self.pages[sec] = ItemsPage(self, sec)
        self.pages['dups'] = DupsPage(self)
        self.pages['apps'] = AppsPage(self)
        self.pages['soft'] = SoftPage(self)
        self.pages['about'] = AboutPage(self)
        for sid, _n, _i in SECTIONS:
            self.stack.add_named(self.pages[sid].widget, sid)
        self.paned.pack1(self.stack, True, False)
        self.paned.pack2(self.journal.widget, False, False)
        right.pack_start(self.paned, True, True, 0)
        right.pack_start(self.journal.bar, False, False, 0)
        body.pack_start(right, True, True, 0)
        self.add(body)

        self.connect('delete-event', self._on_close)
        self.connect('key-press-event', self._on_key)
        self.show_all()
        self.paned.set_position(max(320, h - 330))
        GLib.idle_add(self._fit_journal)
        self.show_journal(self.settings.get('journal', True))
        rc, out, _ = run(['pacman', '-Q', PKG_NAME], timeout=5)
        pkg = f' ({out.split()[1]})' if rc == 0 and len(out.split()) > 1 else ''
        self.journal.add(f'Maintenance {VERSION}{pkg} prête.', 'd')
        self.side_list.select_row(self.side_list.get_row_at_index(0))
        self.update_disk()
        self.update_lock()
        self.refresh_all()
        self.session.start()

    def _fit_journal(self):
        hgt = self.paned.get_allocated_height()
        if hgt > 400:
            self.paned.set_position(hgt - 190)
        self.journal.view.scroll_mark_onscreen(self.journal.end)
        return False

    # --- navigation ---
    @staticmethod
    def _side_header(row, _before):
        if row.get_header() is not None:
            return
        groups = {'clean': 'Entretien', 'health': 'Diagnostic', 'dups': 'Outils'}
        if row.sid in groups:
            row.set_header(label(groups[row.sid], css=('side-group',)))
        elif row.sid == 'about':
            row.set_header(Gtk.Separator())

    def _section_selected(self, _lb, row):
        if row is not None:
            self.stack.set_visible_child_name(row.sid)

    def go(self, sid):
        for row in self.side_list.get_children():
            if row.sid == sid:
                self.side_list.select_row(row)
                break

    # --- état global ---
    def refresh_all(self):
        for it in self.items.values():
            it.done_msg = None
            it.refresh()
        self.pages['apps'].load()
        self.pages['soft'].load()
        self.update_disk()
        self.state_changed()

    def state_changed(self):
        if not self._dirty:
            self._dirty = True
            GLib.idle_add(self._refresh_ui)

    def _refresh_ui(self):
        self._dirty = False
        self.pages['overview'].update()
        for sec in ('clean', 'updates', 'health', 'backup', 'config', 'about'):
            self.pages[sec].update()
        rec = self.reclaim_total()
        upd = self.updates_count()
        issues = self.health_issues()
        pn = self.items['pacnew'].state.get('count', 0)
        values = {'clean': ('warn', fmt_size(rec)) if rec > 50 * 1024 ** 2 else None,
                  'updates': ('info', str(upd)) if upd else None,
                  'health': ('crit', str(issues)) if issues else None,
                  'config': ('warn', str(pn)) if pn else None,
                  'about': ('info', '1') if self.items['self'].state.get('new') else None}
        for sid, badge in self.badges.items():
            v = values.get(sid)
            ctx = badge.get_style_context()
            for k in ('warn', 'crit', 'info'):
                ctx.remove_class(k)
            if v:
                ctx.add_class(v[0])
                badge.set_text(v[1])
                badge.show()
            else:
                badge.hide()
        return False

    def updates_count(self):
        return sum(self.items[i].state.get('count', 0) for i in ('sys', 'fpk')
                   if self.items[i].available)

    def reclaim_total(self):
        return sum(it.state.get('amount') or 0 for it in self.items.values()
                   if it.section == 'clean' and it.available and it.state.get('todo'))

    def selection_total(self):
        chosen = [it for it in self.items.values() if it.section == 'clean' and it.selectable
                  and it.available and it.selected and it.state.get('todo')]
        return sum(it.state.get('amount') or 0 for it in chosen), len(chosen)

    def health_issues(self):
        n = 0
        for it in self.items.values():
            if it.section == 'health' and it.available:
                c = it.state.get('chip')
                if c and c[0] in ('warn', 'crit'):
                    n += 1
        return n

    def gained(self):
        return max(0, disk_usage('/')[1] - self.start_free)

    def update_disk(self):
        total, free = disk_usage('/')
        frac = (total - free) / total if total else 0
        self.disk_l.set_markup(f'Disque système : <b>{fmt_size(free)}</b> libres')
        self.disk_pb.set_fraction(frac)
        g = self.gained()
        self.gain_l.set_text(f'Gagné depuis l’ouverture : {fmt_size(g)}')
        self.gain_l.set_visible(g > 1024 ** 2)

    def after_action(self):
        self.update_disk()
        self.state_changed()

    def selection_changed(self):
        self.settings['selection'] = {it.id: it.selected for it in self.items.values() if it.selectable}
        save_settings(self.settings)
        self.state_changed()

    # --- actions groupées ---
    def clean_selection(self):
        if self.runner.busy:
            return
        chosen = [it for it in self.items.values() if it.section == 'clean' and it.selectable
                  and it.available and it.selected and it.state.get('todo')]
        if not chosen:
            inform(self, 'Rien à nettoyer', 'Aucune ligne cochée n’a quelque chose à nettoyer.')
            return
        tot = sum(it.state.get('amount') or 0 for it in chosen)
        lines = [f'{it.title}' + (f' · {fmt_size(it.state["amount"])}' if it.state.get('amount') else '') for it in chosen]
        txt = f'Environ {fmt_size(tot)} au moins.' if tot else ''
        if any(it.id == 'tr' for it in chosen):
            txt += ' La corbeille sera vidée définitivement.'
        if not confirm(self, f'Nettoyer {plural(len(chosen), "élément")} ?', txt.strip(), ok_label='Nettoyer', items=lines):
            return
        steps = []
        for it in chosen:
            acts = it.get_actions()
            if acts:
                steps += acts[0].get_steps()
            it.busy = True
            it.done_msg = None
            if it.row:
                it.row.update()

        def done(ok):
            for it in chosen:
                it.busy = False
                it.done_msg = ('off', 'Arrêté') if self.runner.stopped else \
                    (('done', 'Fait') if ok else ('crit', 'À vérifier'))
                it.refresh()
            self.journal.add(f'Gagné depuis l’ouverture : {fmt_size(self.gained())}.', 'h')
            self.after_action()

        self.runner.start('Nettoyage de la sélection', steps, done)

    def offer_restart(self):
        if not confirm(self, 'Maintenance est à jour',
                       'Redémarrer l’application maintenant pour utiliser la nouvelle version ?',
                       ok_label='Redémarrer'):
            return
        w, h = self.get_size()
        self.settings['taille'] = [w, h]
        save_settings(self.settings)
        self.session.stop()
        exe = shutil.which('maintenance') or SELF
        launch_cmd(['sh', '-c', f'sleep 1; exec {shlex.quote(exe)}'])
        app = self.get_application()
        if app:
            app.quit()
        else:
            self.destroy()

    def update_lock(self):
        s = self.session
        if s.ok:
            names, text, kind = ['security-high-symbolic', 'changes-allow-symbolic'], 'Administrateur', 'ok'
            tip = 'Droits administrateur accordés jusqu’à la fermeture. Clique pour les retirer.'
        elif s.pending:
            names, text, kind = ['changes-prevent-symbolic'], 'Authentification…', 'off'
            tip = 'Saisis ton mot de passe dans la fenêtre qui vient de s’ouvrir.'
        else:
            names, text, kind = ['changes-prevent-symbolic'], 'Droits non accordés', 'off'
            tip = ('Clique pour saisir ton mot de passe : il restera valable jusqu’à la fermeture de l’application. '
                   'Il est aussi demandé quand tu lances une action administrateur.')
        self.lock_img.set_from_gicon(themed(names + ['dialog-password-symbolic']), Gtk.IconSize.MENU)
        self.lock_img.set_pixel_size(16)
        self.lock_lbl.set_text(text)
        ctx = self.lock_btn.get_style_context()
        for k in ('ok', 'off'):
            ctx.remove_class(k)
        ctx.add_class(kind)
        self.lock_btn.set_tooltip_text(tip)
        self.lock_btn.set_sensitive(not s.pending and not self.runner.busy)
        if getattr(self, '_lock_shown', None) != s.ok and hasattr(self, 'items'):
            self._lock_shown = s.ok
            for it in self.items.values():
                if it.row:
                    it.row.update()      # cadenas sur les boutons administrateur
        return False

    # --- thème ---
    def is_dark(self):
        mode = self.settings.get('theme', 'auto')
        return self.sys_dark if mode == 'auto' else mode == 'sombre'

    def apply_theme(self):
        dark = self.is_dark()
        self.css_prov.load_from_data(build_css(PALETTES['sombre' if dark else 'clair'], self.font_sans, self.font_mono))
        if self.settings.get('theme', 'auto') != 'auto':
            Gtk.Settings.get_default().set_property('gtk-application-prefer-dark-theme', dark)

    def toggle_theme(self):
        self.settings['theme'] = 'clair' if self.is_dark() else 'sombre'
        save_settings(self.settings)
        self.apply_theme()
        self.update_theme_btn()

    def update_theme_btn(self):
        dark = self.is_dark()
        self.theme_btn.set_image(symbolic(['weather-clear-symbolic', 'display-brightness-symbolic'] if dark else
                                          ['weather-clear-night-symbolic', 'night-light-symbolic'], 16))
        self.theme_btn.set_tooltip_text('Passer en thème clair' if dark else 'Passer en thème sombre')

    def _lock_clicked(self):
        if self.session.ok:
            self.session.logout()
        else:
            self.session.login()
            self.update_lock()

    # --- interface ---
    def set_step(self, text):
        if self.runner.busy:
            short = text if len(text) <= 70 else text[:67] + '…'
            self.hb.props.subtitle = f'En cours : {short}'
        return False

    def stop_current(self):
        if self.runner.busy:
            self.runner.cancel()
        dups = self.pages.get('dups')
        if dups is not None and dups.finder is not None:
            dups.stop()
        self.update_stop()

    def can_stop(self):
        dups = self.pages.get('dups') if hasattr(self, 'pages') else None
        if dups is not None and dups.finder is not None and not dups.finder.cancelled:
            return True
        return self.runner.busy and not self.runner.cancel_req

    def update_stop(self):
        """Met à jour le bouton « Arrêter » de la ligne en cours (« Arrêt en cours… » une fois demandé)."""
        if self.runner.busy and hasattr(self, 'items'):
            for it in self.items.values():
                if it.busy and it.row:
                    it.row.update()
        return False

    def _tick(self):
        if not self.runner.busy:
            self._tick_id = 0
            return False
        status = self.runner.last_line or self.runner.cur_label or 'En cours…'
        for it in self.items.values():
            if it.busy and it.row:
                it.row.tick(status)
        return True

    def set_busy(self, busy):
        if busy and not getattr(self, '_tick_id', 0):
            self._tick_id = GLib.timeout_add(120, self._tick)
        self.update_stop()
        self.spinner.start() if busy else self.spinner.stop()
        self.hb.props.subtitle = self.base_sub + (' · opération en cours…' if busy else '')
        self.refresh_btn.set_sensitive(not busy)
        self.update_lock()
        for it in self.items.values():
            if it.row:
                it.row.update()
        self.pages['dups'].set_busy(busy)
        self.pages['apps'].set_busy(busy)
        self.pages['soft'].set_busy(busy)
        self.state_changed()

    def show_journal(self, show):
        self.journal.widget.set_visible(show)
        self.journal.bar.set_visible(not show)
        if self.jbtn.get_active() != show:
            self.jbtn.set_active(show)
        if self.settings.get('journal') != show:
            self.settings['journal'] = show
            save_settings(self.settings)

    def _on_key(self, _w, ev):
        if ev.keyval == Gdk.KEY_F5 and not self.runner.busy:
            self.refresh_all()
            return True
        if ev.keyval in (Gdk.KEY_q, Gdk.KEY_Q) and ev.state & Gdk.ModifierType.CONTROL_MASK:
            self.close()
            return True
        if ev.keyval == Gdk.KEY_Escape and self.can_stop():
            self.stop_current()
            return True
        return False

    def _on_close(self, *_a):
        if self.runner.busy:
            inform(self, 'Une opération est en cours',
                   'Attends la fin de l’opération avant de fermer : interrompre pacman peut abîmer le système.',
                   Gtk.MessageType.WARNING)
            return True
        w, h = self.get_size()
        self.settings['taille'] = [w, h]
        save_settings(self.settings)
        self.session.stop()
        return False


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.win = None

    def do_activate(self):
        if self.win is None:
            self.win = MainWindow(self)
        self.win.present()


def main():
    args = sys.argv[1:]
    if args[:1] == ['--askpass']:
        return askpass_main(args[1:])
    if args[:1] == ['--sudo-relay']:
        return relay_client(args[1:])
    if args[:1] in (['--version'], ['-v']):
        print(f'Maintenance {VERSION}')
        return 0
    if args[:1] in (['--help'], ['-h']):
        print(__doc__.strip())
        return 0
    if os.geteuid() == 0:
        print('Lance Maintenance avec ton compte habituel, pas en root : '
              'les droits administrateur sont demandés quand il le faut.', file=sys.stderr)
        return 1
    GLib.set_prgname(APP_ID)
    GLib.set_application_name('Maintenance')
    return App().run([sys.argv[0]])


if __name__ == '__main__':
    sys.exit(main())

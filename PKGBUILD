# Maintainer: TheWorms
# Paquet construit depuis le dépôt GitHub : chaque commit poussé devient une nouvelle version
# (rNOMBRE.COMMIT), que l'application détecte et installe d'elle-même.

pkgname=maintenance-git
pkgver=r1.0000000
pkgrel=1
pkgdesc="Nettoyage, mises à jour et santé d'Arch Linux, en une seule fenêtre GTK3 (pensé pour Cinnamon)"
arch=('any')
url="https://github.com/TheWorms/maintenance"
license=('MIT')
depends=('python' 'python-gobject' 'gtk3' 'sudo' 'git' 'pacman-contrib')
optdepends=('yay: mises à jour et cache de l’AUR'
            'smartmontools: santé des disques (SMART)'
            'timeshift: instantanés du système'
            'reflector: classement des miroirs'
            'fwupd: mises à jour du firmware'
            'flatpak: applications Flatpak'
            'meld: fusion des fichiers .pacnew')
provides=('maintenance')
conflicts=('maintenance')
source=("$pkgname::git+$url.git")
sha256sums=('SKIP')

pkgver() {
  cd "$pkgname"
  printf "r%s.%s" "$(git rev-list --count HEAD)" "$(git rev-parse --short=7 HEAD)"
}

package() {
  cd "$pkgname"
  install -Dm755 maintenance.py "$pkgdir/usr/bin/maintenance"
  ln -s maintenance "$pkgdir/usr/bin/nettoyage"
  install -Dm644 io.github.theworms.Maintenance.desktop "$pkgdir/usr/share/applications/io.github.theworms.Maintenance.desktop"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
  install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
}

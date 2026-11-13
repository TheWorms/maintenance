#!/usr/bin/env bash
# Première installation du paquet maintenance-git depuis GitHub.
# Ensuite, l'application se met à jour d'elle-même (section Mises à jour).

set -euo pipefail
REPO="https://github.com/TheWorms/maintenance.git"

if [[ $EUID -eq 0 ]]; then
  echo "Lance ce script avec ton compte habituel, sans sudo : le mot de passe sera demandé si besoin."
  exit 1
fi

echo "==> Vérification du dépôt GitHub"
if ! git ls-remote "$REPO" HEAD >/dev/null 2>&1; then
  echo "Le dépôt $REPO est introuvable ou privé."
  echo "Pousse d'abord le code sur GitHub (dépôt public), puis relance ce script. Rien n'a été modifié."
  exit 1
fi
echo "  dépôt accessible"

echo "==> Outils de construction"
sudo pacman -S --needed base-devel git

echo "==> Construction et installation du paquet"
work=$(mktemp -d)
trap 'rm -rf -- "${work:?}"' EXIT
git clone --depth 1 "$REPO" "$work/depot"
cd "$work/depot"
makepkg -si

# L'ancienne installation dans ~/.local passerait devant le paquet : on la retire, maintenant
# que le paquet est en place.
echo "==> Ancienne installation dans ~/.local"
old_bin="$HOME/.local/bin"
found=0
if [[ -e $old_bin/maintenance ]]; then rm -f -- "${old_bin:?}/maintenance"; echo "  retiré : $old_bin/maintenance"; found=1; fi
if [[ -L $old_bin/nettoyage ]]; then rm -f -- "${old_bin:?}/nettoyage"; echo "  retiré : $old_bin/nettoyage (le paquet fournit la commande)"; found=1; fi
((found)) || echo "  rien à retirer"

echo
echo "Maintenance est installée : menu Cinnamon › Administration › Maintenance, ou la commande « maintenance »."
echo "Pour les prochaines versions : pousse sur GitHub, puis Mises à jour › Maintenance (cette application) dans l'application."

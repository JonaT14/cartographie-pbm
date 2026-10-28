# Annuaire PBM — mise en ligne sur Render

## Contenu
- `server.py` : le serveur (connexion, administrateurs, fiches, import, historique). Python seul, sans dépendance.
- `public/` : la page du site et les fonds de carte.
- `donnees.json` : les 105 fiches corrigées, chargées **une seule fois** au premier démarrage.
- `equipment.py`, `xlsx.py` : nettoyage des équipements et lecture des fichiers Excel à l'import.
- `render.yaml` : réglages Render.

## Réglages Render (service « Web Service »)
| Réglage | Valeur |
|---|---|
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python server.py` |
| Disque (Disks) | Mount Path `/var/data`, 1 Go |
| Variable `DATA_DIR` | `/var/data` |
| Variable `MASTER_EMAIL` | votre e-mail d'administrateur principal |
| Variable `MASTER_PASSWORD` | votre mot de passe (12 caractères minimum) |

**Le disque est indispensable** : sans lui, toutes les modifications sont effacées à chaque redémarrage.

## Rôles
- **Administrateur principal** (l'e-mail de `MASTER_EMAIL`) : tout, plus l'ajout et le retrait des administrateurs. Son accès ne peut pas être retiré.
- **Administrateurs** : ajouter, modifier, supprimer des fiches, importer, exporter, consulter l'historique, changer leur mot de passe.

## Mot de passe principal oublié
Dans Render, mettez un nouveau `MASTER_PASSWORD`, ajoutez la variable `RESET_MASTER_PASSWORD` = `1`, redémarrez, puis **supprimez** `RESET_MASTER_PASSWORD`.

## Import Yapla
Bouton « Importer Yapla / CSV » (Excel .xlsx ou CSV). Seules les personnes équipées sont reprises.
Les nouvelles personnes sont ajoutées et localisées. Les fiches existantes ne sont pas modifiées,
sauf si vous cochez la case prévue ; les adresses différentes sont seulement signalées.

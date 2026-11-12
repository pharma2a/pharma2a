# Contribuer

**Qui décide.** SyderaAI tient le protocole : voir `GOVERNANCE.md` (qui décide, comment un
changement se propose, les versions), `MAINTAINERS.md` et `CODE_OF_CONDUCT.md`. Site :
pharma2a.org, contact : contact@pharma2a.org.

**Proposer un changement.** Ouvrez une issue qui dit le besoin en une phrase, avec un
exemple de message. Pour le dictionnaire, un champ nouveau doit être dit par quelqu'un dans
la réalité de l'échange (le fournisseur, l'officine) : rien de déduit, jamais un prix de
l'officine. Pour
le code, une pull request avec ses tests ; `python -m pytest` et `ruff check .` doivent
rester verts.

**Les deux guides.** `docs/guide.md` (anglais) et `docs/guide-fr.md` (français) disent la
même chose : une modification se reporte dans les deux, dans la même pull request.
`tests/test_guides.py` vérifie qu'ils gardent les mêmes sections et les mêmes exemples.

**Le site pointe ici.** pharma2a.org renvoie à `docs/guide.md`, `docs/guide-fr.md` et
`SPECIFICATION.md` : déplacer ou renommer l'un d'eux demande de mettre à jour le site.

**La règle des versions.** Une version ne casse jamais la précédente : un champ ajouté est
facultatif, un champ retiré ou rendu obligatoire fait une nouvelle version du schéma, et un
agent qui parle la version N continue de comprendre la version N moins un. Les schémas
refusent tout champ non prévu, c'est ce qui permet de tenir cette règle.

**Le ton.** Les messages, les noms de champs et les commentaires sont en français, dans les
mots du métier. Pas de jargon d'éditeur quand un mot de pharmacien existe.

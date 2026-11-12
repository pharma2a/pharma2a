"""Les deux guides, anglais et français, restent jumeaux : mêmes sections, mêmes exemples JSON,
mêmes blocs de code. Une modification de l'un sans l'autre fait échouer ce test."""
import pathlib
import re

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"


def lire(nom):
    return (DOCS / nom).read_text(encoding="utf-8")


def titres(texte, niveau):
    return re.findall(rf"^{'#' * niveau} (?!#)", texte, re.M)


def test_les_guides_ont_les_memes_sections():
    en, fr = lire("guide.md"), lire("guide-fr.md")
    for niveau in (2, 3, 4):
        assert len(titres(en, niveau)) == len(titres(fr, niveau)), f"titres de niveau {niveau}"


def test_les_guides_ont_les_memes_exemples():
    en, fr = lire("guide.md"), lire("guide-fr.md")
    assert re.findall(r"```json\n(.*?)```", en, re.S) == re.findall(r"```json\n(.*?)```", fr, re.S)
    assert en.count("```") == fr.count("```")
    # les noms de variables d'environnement cités sont les mêmes
    assert sorted(set(re.findall(r"PHARMA2A_[A-Z_]+", en))) == sorted(set(re.findall(r"PHARMA2A_[A-Z_]+", fr)))

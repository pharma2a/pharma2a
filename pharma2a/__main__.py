"""python -m pharma2a : démarre l'agent avec la configuration de l'environnement."""
import asyncio
import logging
import sys
import uvicorn
from .config import Config
from .serveur import creer_app


def jeton(config, args):
    """python -m pharma2a jeton creer|lister|revoquer : les jetons des officines (jetons.py)."""
    import argparse
    from . import jetons
    p = argparse.ArgumentParser(prog="python -m pharma2a jeton")
    p.add_argument("action", choices=["creer", "lister", "revoquer"])
    p.add_argument("--fichier", default=config.jetons, help="le registre (défaut : PHARMA2A_JETONS)")
    p.add_argument("--compte", help="le numéro de compte client de l'officine")
    p.add_argument("--nom", help="le nom de l'officine, pour s'y retrouver")
    p.add_argument("--empreinte", help="révoquer un seul jeton : le début de son empreinte (sha256:…)")
    a = p.parse_args(args)
    if not a.fichier:
        raise SystemExit("le registre : --fichier, ou PHARMA2A_JETONS")
    if a.action == "creer":
        if not a.compte:
            raise SystemExit("--compte : le numéro de compte client auquel le jeton se rattache")
        j = jetons.creer(a.fichier, a.compte, a.nom)
        print(j)
        print(f"jeton délivré au compte {a.compte}, à transmettre à l'officine par un canal sûr. "
              "Il ne sera plus affiché : le registre n'en garde que l'empreinte.", file=sys.stderr)
    elif a.action == "lister":
        for e in jetons.lister(a.fichier):
            print(f"{e.get('compte_client')}\t{e.get('nom') or '-'}\t{e.get('cree_le') or '-'}\t{str(e.get('empreinte'))[:19]}…")
    else:
        n = jetons.revoquer(a.fichier, a.compte, a.empreinte)
        print(f"{n} jeton(s) révoqué(s), refusés dès la requête suivante.")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    config = Config.depuis_env()
    if len(sys.argv) > 1 and sys.argv[1] == "jeton":
        return jeton(config, sys.argv[2:])
    if "--test-mail" in sys.argv:
        # un mail de contrôle, envoyé pour de bon à l'adresse donnée, puis relu dans la boîte :
        # vérifie l'expédition et la lecture avant de lever le brouillon
        import dataclasses
        from . import backends
        a = sys.argv[sys.argv.index("--test-mail") + 1]
        c = dataclasses.replace(config, brouillon=False)
        b = backends.charger(c)
        ident = b.envoyer(a, "", "PharmA2A, courriel de contrôle", "L'agent PharmA2A peut envoyer depuis cette adresse. Répondez à ce message pour vérifier la lecture.",
                          "<p>L'agent PharmA2A peut envoyer depuis cette adresse. <b>Répondez à ce message</b> pour vérifier la lecture.</p>",
                          reference="TEST-PHARMA2A", nom_affiche="Test PharmA2A")
        print(f"envoyé à {a} ({ident}). Boîte de réception : {len(b.lire_reponses())} message(s) non lu(s) à l'instant.")
        return
    app = creer_app(config)
    if "--relire" in sys.argv:
        # une passe de relecture, à la main (il faut PHARMA2A_BASE : le magasin en mémoire
        # d'un autre processus n'est pas visible d'ici)
        if not config.base:
            raise SystemExit("--relire exige PHARMA2A_BASE (le magasin sqlite partagé avec l'agent)")
        n = asyncio.run(app.state.relire())
        print(f"relecture : {n} tâche(s) mise(s) à jour")
        return
    mode = "BROUILLON, rien ne part, tout dans " + str(config.sorties) if config.brouillon else "RÉEL, les mails partent"
    print(f"PharmA2A ({config.fournisseur}) sur http://{config.hote}:{config.port}  [{mode}]")
    uvicorn.run(app, host=config.hote, port=config.port, log_level="info")


if __name__ == "__main__":
    main()

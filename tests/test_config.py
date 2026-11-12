"""La configuration : le .env.example se charge tel quel, une variable laissée vide vaut une
variable absente."""
import pathlib
import re
from pharma2a.config import Config

RACINE = pathlib.Path(__file__).resolve().parent.parent


def test_le_env_d_exemple_charge_tel_quel_ne_change_aucun_defaut():
    noms = re.findall(r"^(PHARMA2A_[A-Z_]+)=", (RACINE / ".env.example").read_text(encoding="utf-8"), re.M)
    assert "PHARMA2A_PORT" in noms and "PHARMA2A_SMTP_PORT" in noms
    vide = {n: "" for n in noms}
    assert Config.depuis_env(vide) == Config.depuis_env({})
    c = Config.depuis_env({**vide, "PHARMA2A_JETON": "un-jeton-de-test"})
    assert c.verifier() == [] and (c.port, c.smtp_port, c.backend) == (8765, 587, "mail")

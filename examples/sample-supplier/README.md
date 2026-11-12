# Sample supplier

The smallest supplier agent that speaks PharmA2A: about a hundred lines.

It receives an order over A2A, checks it against the dictionary, and answers at once with an
acknowledgement, line by line, from a small stock: what ships, what follows as a back order,
what is out of stock. An order outside the dictionary is rejected with the reason.

It is a template, not a product. A lab or a software vendor plugs its own order system
(ERP, warehouse) into `accuse()`. To stay on one page it leaves out what an agent in service
must add: authentication, one token per pharmacy, and idempotence, the same reference never
ordered twice (`SPECIFICATION.md`, section 8). It refuses a proposal, the order before the
order, instead of answering it.

```bash
pip install -e .
python examples/sample-supplier/agent.py                     # http://127.0.0.1:8766
python -m pharma2a.officine --agent http://127.0.0.1:8766 --attendre 2    # the demo pharmacy orders from it
```

For a supplier who only has a mailbox today, see the supplier email bridge (the `pharma2a` package).

---

*Version française.* Le plus petit agent fournisseur qui parle PharmA2A, environ cent
lignes. Il reçoit une commande, la vérifie contre le dictionnaire et répond aussitôt par un accusé
de réception ligne par ligne. C'est un modèle : un laboratoire ou un éditeur y branche son propre
système de commandes et ajoute l'authentification.

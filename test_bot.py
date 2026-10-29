import contextlib
import os
import tempfile

os.environ["TELEGRAM_TOKEN"] = "x"

import bot  # noqa: E402
from fetcher import Anexo, NoEncontrado  # noqa: E402

bot.PAUSE = (0, 0)
bot.ALLOWED = {1}
sent = []
bot.send = lambda chat, text: sent.append(text)

site = {}          # numero -> lista de Anexo (el "sitio" simulado)
broken = set()     # numeros que fallan con error


@contextlib.contextmanager
def fake_fetcher(debug=False):
    def fetch(ref):
        if ref in broken:
            raise RuntimeError("el sitio no responde")
        if ref not in site:
            raise NoEncontrado(ref)
        return list(site[ref])
    yield fetch


bot.open_fetcher = fake_fetcher

path = os.path.join(tempfile.mkdtemp(), "state.json")
store = bot.Store(path)
REF = "IO-89-L47-830124970-N-14-2026"
site[REF] = [Anexo("a1", "[Anexos] Convocatoria.pdf", "http://x/p"), Anexo("a2", "[Anexos] Anexo 1.docx", "http://x/p")]

# 1) agregar solo con el numero; primera lectura = base sin spam
bot.handle_message(store, 1, REF.lower())
assert store.data["subs"][0]["ref"] == REF and store.force
bot.run_cycle(store)
assert any("Base lista: 2" in s for s in sent), sent
n = len(sent)
bot.run_cycle(store)
assert len(sent) == n, "sin cambios no debe avisar"

# 2) aparece un acta nueva
site[REF].append(Anexo("a3", "[Actas] Junta de aclaraciones", "http://x/p", "29/10/2026"))
bot.run_cycle(store)
assert "Junta de aclaraciones" in sent[-1] and "Convocatoria" not in sent[-1], sent[-1]
bot.handle_message(store, 1, "/lista")
assert "3 anexos/actas" in sent[-1], sent[-1]

# 3) persistencia y escritura solo si hay cambios
store.save()
m1 = os.path.getmtime(path)
store2 = bot.Store(path)
assert store2.data == store.data
store2.save()
assert os.path.getmtime(path) == m1

# 4) numero que aun no existe: avisa una sola vez
REF2 = "LA-06-L47-830124970-N-1-2026"
bot.handle_message(store, 1, f"/agregar {REF2} pruebas")
bot.run_cycle(store)
assert sum("Aun no encuentro" in s for s in sent) == 1
bot.run_cycle(store)
assert sum("Aun no encuentro" in s for s in sent) == 1
site[REF2] = [Anexo("b1", "[Anexos] Bases", "http://x/q")]
bot.run_cycle(store)
assert any("Base lista: 1" in s for s in sent)

# 5) fallas repetidas: alerta una vez
broken.add(REF)
for _ in range(bot.ALERT_AFTER_FAILURES + 2):
    bot.run_cycle(store)
assert sum("No puedo leer" in s for s in sent) == 1
broken.clear()
bot.run_cycle(store)
assert store.data["fails"][REF] == 0

# 6) quitar por numero de lista y por alias
bot.handle_message(store, 1, "/quitar pruebas")
assert all(s["ref"] != REF2 for s in store.data["subs"]) and REF2 not in store.data["docs"]
bot.handle_message(store, 1, "/quitar 1")
assert not store.data["subs"] and not store.data["docs"]

# 7) numero invalido y chat no autorizado
bot.handle_message(store, 1, "/agregar hola")
assert "tal como aparece" in sent[-1]
bot.handle_message(store, 2, "/lista")
assert sent[-1] == "No autorizado."

# 8) offset de Telegram
bot.tg = lambda method, **kw: {"result": [{"update_id": 7, "message": {"chat": {"id": 1}, "text": "/lista"}}]}
bot.process_updates(store, 0)
assert store.data["offset"] == 8

print("OK: todas las pruebas pasaron")

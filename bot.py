#!/usr/bin/env python3
"""
Vigilante de anexos/actas de ComprasMX, controlado por Telegram.

Modos:
  python bot.py once         GitHub Actions: atiende mensajes pendientes, revisa todo y termina
  python bot.py loop         Servidor: atiende mensajes al instante y revisa cada POLL_SECONDS
  python bot.py probe <num>  Prueba la lectura de un procedimiento (calibracion; guarda capturas)

Comandos en Telegram: /agregar <numero> [alias], /quitar, /lista, /revisar, /ayuda
Solo depende de `requests` (y `playwright` para leer ComprasMX).
"""
import contextlib
import json
import os
import random
import re
import sys
import time

import requests

from fetcher import Anexo, Browser, NoEncontrado  # noqa: F401

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x}
STATE_PATH = os.environ.get("STATE_PATH", "state/state.json")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))              # solo modo loop
NIGHT_POLL_SECONDS = int(os.environ.get("NIGHT_POLL_SECONDS", "900"))  # solo modo loop
ALERT_AFTER_FAILURES = int(os.environ.get("ALERT_AFTER_FAILURES", "6"))
PAUSE = (float(os.environ.get("PAUSE_MIN", "2")), float(os.environ.get("PAUSE_MAX", "5")))
SEND_FAILURES = 0


# --------------------------------------------------------------------------- #
# Estado (JSON). Solo se reescribe si cambio algo.
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self, path: str):
        self.path = path
        self.force = False  # pedir una revision inmediata (modo loop)
        self.data = {"offset": 0, "subs": [], "docs": {}, "fails": {}, "seen": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        self.data.setdefault("seen", {})
        self._saved = self._dump()

    def _dump(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, indent=1, sort_keys=True)

    def save(self):
        s = self._dump()
        if s == self._saved:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(s)
        os.replace(tmp, self.path)
        self._saved = s


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def tg(method: str, **params):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params,
                      timeout=(10, params.get("timeout", 0) + 30))
    r.raise_for_status()
    return r.json()


def send(chat_id: int, text: str):
    global SEND_FAILURES
    try:
        tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:  # noqa: BLE001
        SEND_FAILURES += 1
        print("sendMessage fallo:", e)


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #
def normalize_ref(raw: str) -> str:
    return raw.strip().upper()


def valid_ref(ref: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9./-]{8,60}", ref)) and "-" in ref


def _find(store: Store, chat_id: int, token: str):
    mine = [s for s in store.data["subs"] if s["chat"] == chat_id]
    if token.isdigit() and 1 <= int(token) <= len(mine):
        return mine[int(token) - 1]
    ref = normalize_ref(token)
    for s in mine:
        if s["ref"] == ref or (s["alias"] and s["alias"].lower() == token.lower()):
            return s
    return None


def _ultima(store: Store, ref: str) -> str:
    v = store.data["seen"].get(ref)
    if not v:
        return "aun no se ha revisado"
    mins = max(0, int((time.time() - v["t"]) / 60))
    hace = f"{mins} min" if mins < 120 else f"{mins // 60} h"
    return f"revisado hace {hace}" + ("" if v["ok"] else f" - con problema: {v['err']}")


def _visto(store: Store, ref: str, ok: bool, err: str = ""):
    store.data["seen"][ref] = {"t": int(time.time()), "ok": ok, "err": err[:80]}


def handle_message(store: Store, chat_id: int, text: str):
    text = (text or "").strip()
    if not text:
        return
    if chat_id not in ALLOWED:
        send(chat_id, "No autorizado.")
        return

    cmd, _, rest = text.partition(" ")
    cmd = cmd.lower().split("@")[0]
    rest = rest.strip()
    es_numero = not text.startswith("/") and " " not in text and valid_ref(normalize_ref(text))

    if cmd in ("/start", "/ayuda", "/help"):
        send(chat_id,
             "<b>Vigilante de anexos</b>\n"
             "/agregar &lt;numero de procedimiento&gt; [alias]\n"
             "/quitar &lt;# de la lista, alias o numero&gt;\n"
             "/lista\n/revisar\n\n"
             "Tambien puedes mandar solo el numero, por ejemplo IO-89-L47-830124970-N-14-2026")
    elif cmd == "/agregar" or es_numero:
        parts = (rest if cmd == "/agregar" else text).split(None, 1)
        if not parts:
            send(chat_id, "Uso: /agregar &lt;numero de procedimiento&gt; [alias]")
            return
        ref = normalize_ref(parts[0])
        alias = parts[1].strip() if len(parts) > 1 else ""
        if not valid_ref(ref):
            send(chat_id, "Mandame el numero tal como aparece en ComprasMX, "
                          "por ejemplo IO-89-L47-830124970-N-14-2026")
            return
        if _find(store, chat_id, ref):
            send(chat_id, "Ya estaba en tu lista.")
            return
        store.data["subs"].append({"chat": chat_id, "ref": ref, "alias": alias, "base": False, "warned": False})
        store.force = True
        send(chat_id, f"Agregado: <code>{esc(ref)}</code> {esc(alias)}\n"
                      "Tomo como base los anexos y actas actuales; solo te aviso de los nuevos.")
    elif cmd == "/quitar":
        s = _find(store, chat_id, rest) if rest else None
        if not s:
            send(chat_id, "No lo encontre. Usa /lista y mandame el numero de la lista, el alias o el folio.")
            return
        store.data["subs"].remove(s)
        if not any(x["ref"] == s["ref"] for x in store.data["subs"]):
            store.data["docs"].pop(s["ref"], None)
            store.data["fails"].pop(s["ref"], None)
            store.data["seen"].pop(s["ref"], None)
        send(chat_id, f"Quitado: <code>{esc(s['ref'])}</code>")
    elif cmd == "/lista":
        mine = [s for s in store.data["subs"] if s["chat"] == chat_id]
        if not mine:
            send(chat_id, "No vigilas ningun procedimiento. Usa /agregar.")
            return
        lines = []
        for i, s in enumerate(mine, 1):
            n = len(store.data["docs"].get(s["ref"], {}))
            estado = f"{n} anexos/actas" if s["base"] else "pendiente de primera lectura"
            lines.append(f"{i}. <code>{esc(s['ref'])}</code> {esc(s['alias'])} ({estado})\n    {esc(_ultima(store, s['ref']))}")
        send(chat_id, "\n".join(lines))
    elif cmd == "/revisar":
        store.force = True
        send(chat_id, "Revisando ahora...")
    else:
        send(chat_id, "No entendi. /ayuda")


def process_updates(store: Store, timeout: int):
    res = tg("getUpdates", offset=store.data["offset"], timeout=timeout, allowed_updates=["message"])
    n = 0
    for u in res.get("result", []):
        store.data["offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if "text" in m:
            n += 1
            print(f"mensaje de chat {m['chat']['id']}: {m['text'][:60]!r}")
            handle_message(store, m["chat"]["id"], m["text"])
    store.save()
    return n


# --------------------------------------------------------------------------- #
# Revision
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def open_fetcher(debug: bool = False):
    with Browser(debug=debug) as fetch:
        yield fetch


def interval_now() -> int:
    return POLL_SECONDS if 7 <= time.localtime().tm_hour < 21 else NIGHT_POLL_SECONDS


def check_ref(store: Store, ref: str, fetch):
    subs = [s for s in store.data["subs"] if s["ref"] == ref]
    fails = store.data["fails"]
    try:
        anexos = fetch(ref)
    except NoEncontrado:
        fails[ref] = fails.get(ref, 0) + 1
        _visto(store, ref, False, "no aparece en la consulta publica")
        for s in subs:
            if not s["base"] and not s["warned"]:
                s["warned"] = True
                send(s["chat"], f"Aun no encuentro <code>{esc(ref)}</code> en ComprasMX "
                                "(puede no estar publicado todavia). Sigo intentando en cada revision.")
            elif s["base"] and fails[ref] == ALERT_AFTER_FAILURES:
                send(s["chat"], f"<code>{esc(ref)}</code> ya no aparece en la consulta publica.")
        return
    except Exception as e:  # noqa: BLE001
        fails[ref] = fails.get(ref, 0) + 1
        print(f"[{ref}] error ({fails[ref]}): {e}")
        _visto(store, ref, False, str(e))
        if fails[ref] == ALERT_AFTER_FAILURES:
            for s in subs:
                send(s["chat"], f"No puedo leer <code>{esc(ref)}</code> desde hace varias revisiones: "
                                f"{esc(str(e))[:200]}")
        return

    known = store.data["docs"].setdefault(ref, {})
    if not anexos and known:
        # Antes tenia anexos y ahora ninguno: casi seguro la pagina cargo incompleta. No tocar nada.
        fails[ref] = fails.get(ref, 0) + 1
        print(f"[{ref}] lectura vacia sospechosa (antes habia {len(known)}); se ignora")
        _visto(store, ref, False, "lectura vacia, se ignoro")
        return
    fails[ref] = 0
    _visto(store, ref, True)
    new = [a for a in anexos if a.id not in known]
    antes: dict[str, int] = {}  # total de archivos previo de cada fila, para mostrar "antes N"
    for d in known.values():
        if d.get("g"):
            antes[d["g"]] = max(antes.get(d["g"], 0), d.get("t", 0))
    for a in new:
        known[a.id] = {"n": a.nombre, "f": a.fecha, "g": a.grupo, "t": a.total}
    for s in subs:
        if s["base"]:
            if new:
                alias = f" ({esc(s['alias'])})" if s["alias"] else ""
                body = "\n".join(
                    f"• {esc(a.nombre)}"
                    + (f" — {esc(a.fecha)}" if a.fecha else "")
                    + (f" (antes: {antes[a.grupo]})" if a.grupo in antes else " (nuevo)")
                    for a in new
                )
                link = f"\n{esc(new[0].url)}" if new[0].url else ""
                send(s["chat"], f"<b>Nuevo en</b> <code>{esc(ref)}</code>{alias}\n{body}{link}")
        else:
            s["base"] = True
            resumen = "\n".join(f"• {esc(a.nombre)}" for a in anexos[:25]) + ("\n..." if len(anexos) > 25 else "")
            send(s["chat"], f"Base lista: {len(known)} anexos/actas actuales en <code>{esc(ref)}</code>.\n"
                            f"Esto ya existia, solo aviso de lo que aparezca despues:\n{resumen}")


def run_cycle(store: Store):
    refs = sorted({s["ref"] for s in store.data["subs"]})
    if not refs:
        return
    with open_fetcher() as fetch:
        for i, ref in enumerate(refs):
            check_ref(store, ref, fetch)
            store.save()
            if i < len(refs) - 1:
                time.sleep(random.uniform(*PAUSE))


SCHEMA = 2   # 2 = lee todas las paginas de ANEXOS e incluye el Num. de fila en la identidad


def migrate(store: Store):
    """Si cambio la forma de leer, las fotos iniciales viejas ya no sirven: se vuelven a tomar (una vez)."""
    if store.data.get("v") == SCHEMA:
        return
    primera_vez = "v" not in store.data and not store.data["subs"]
    store.data["v"] = SCHEMA
    if not primera_vez and store.data["subs"]:
        store.data["docs"] = {}
        for s in store.data["subs"]:
            s["base"] = False
            s["warned"] = False
        for chat in sorted({s["chat"] for s in store.data["subs"]}):
            send(chat, "Actualice el lector: ahora leo <b>todas las paginas</b> de la tabla de anexos "
                       "(antes solo la primera). Vuelvo a tomar la foto inicial de tus procedimientos; "
                       "te la mando enseguida.")
    store.save()


# --------------------------------------------------------------------------- #
# Modos
# --------------------------------------------------------------------------- #
def once(store: Store):
    problema = False
    try:
        n = process_updates(store, 0)
        print(f"[once] mensajes nuevos: {n} | procedimientos vigilados: {len(store.data['subs'])}")
    except Exception as e:  # noqa: BLE001
        print("ERROR de Telegram (getUpdates):", e)
        problema = True
    run_cycle(store)
    store.save()
    if problema or SEND_FAILURES:
        # Que la corrida salga en rojo para que el problema se vea, en vez de fallar en silencio.
        sys.exit("Hubo errores con Telegram: revisa TELEGRAM_TOKEN, ALLOWED_CHAT_IDS o si hay un webhook activo.")


def loop(store: Store):
    print("Bot en marcha.")
    nxt = 0.0
    while True:
        try:
            process_updates(store, 25)
        except Exception as e:  # noqa: BLE001
            print("telegram:", e)
            time.sleep(5)
        if store.force or time.time() >= nxt:
            store.force = False
            try:
                run_cycle(store)
            except Exception as e:  # noqa: BLE001
                print("ciclo:", e)
            nxt = time.time() + interval_now() * random.uniform(0.9, 1.2)


def probe(refs: list[str]) -> int:
    refs = [normalize_ref(r) for r in refs if r.strip()]
    if not refs:
        print("Uso: python bot.py probe <numero> [<numero> ...]")
        return 2
    codigo = 0
    with open_fetcher(debug=True) as fetch:
        for ref in refs:
            print(f"=== {ref}")
            try:
                docs = fetch(ref)
            except NoEncontrado:
                print(f"NO ENCONTRADO: {ref} no aparece en la consulta publica (revisa capturas en debug/)")
                codigo = 1
                continue
            except Exception as e:  # noqa: BLE001
                print(f"ERROR leyendo {ref}: {e}")
                codigo = 1
                continue
            print(f"{len(docs)} anexos/actas en {ref}:")
            for d in docs:
                print(f"  - {d.nombre} | {d.fecha}")
    return codigo


def main(argv) -> int:
    mode = argv[1] if len(argv) > 1 else "once"
    if mode == "probe":
        return probe(argv[2:])
    if not TOKEN or not ALLOWED:
        sys.exit("Faltan TELEGRAM_TOKEN y/o ALLOWED_CHAT_IDS")
    store = Store(STATE_PATH)
    migrate(store)
    if mode == "once":
        once(store)
    elif mode == "loop":
        loop(store)
    else:
        sys.exit("Modo desconocido. Usa: once | loop | probe <numero>")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

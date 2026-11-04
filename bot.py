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
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import cronograma as cr
import licitacion as lic
import ticktick as tk
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
        self.avisar_fin = None  # chat al que avisar cuando termine una revision pedida con /revisar
        self.data = {"offset": 0, "subs": [], "docs": {}, "fails": {}, "seen": {}, "crono": {}, "datos": {}, "cierre": {}, "tareas": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        self.data.setdefault("seen", {})
        self.data.setdefault("crono", {})
        self.data.setdefault("datos", {})
        self.data.setdefault("cierre", {})
        self.data.setdefault("tareas", {})
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


def ahora_mx() -> datetime:
    """Hora actual en Ciudad de Mexico, sin zona (igual que las fechas que muestra ComprasMX)."""
    return datetime.now(ZoneInfo("America/Mexico_City")).replace(tzinfo=None)


def _enviar_por_partes(chat_id: int, bloques: list[str], limite: int = 3500):
    """Telegram corta los mensajes de mas de 4096 caracteres: se manda en varias partes."""
    actual = ""
    for b in bloques:
        if actual and len(actual) + len(b) + 2 > limite:
            send(chat_id, actual)
            actual = ""
        actual += ("\n\n" if actual else "") + b
    if actual:
        send(chat_id, actual)


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
    cmd, _, rest = text.partition(" ")
    cmd = cmd.lower().split("@")[0]
    rest = rest.strip()
    if cmd == "/id":   # sirve en cualquier chat (incluso sin autorizar): da el numero que va en ALLOWED_CHAT_IDS
        send(chat_id, f"El ID de este chat es <code>{chat_id}</code>")
        return
    es_numero = not text.startswith("/") and " " not in text and valid_ref(normalize_ref(text))
    en_grupo = chat_id < 0
    if chat_id not in ALLOWED:
        if not en_grupo:   # en un grupo no autorizado se calla, para no contestar a cada mensaje de la gente
            send(chat_id, "No autorizado.")
        return
    if en_grupo and not (text.startswith("/") or es_numero):
        return   # en grupos solo reacciona a comandos y folios; la charla normal se ignora

    if cmd in ("/start", "/ayuda", "/help"):
        send(chat_id,
             "<b>Vigilante de anexos</b>\n"
             "/agregar &lt;numero de procedimiento&gt; [alias]\n"
             "/quitar &lt;# de la lista, alias o numero&gt;\n"
             "/lista\n/fechas [# de la lista]\n/plan [# de la lista]\n/revisar\n\n"
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
        store.data["subs"].append({"chat": chat_id, "ref": ref, "alias": alias, "base": False, "warned": False,
                                    "tarea": bool(os.environ.get("TICKTICK_TOKEN"))})
        store.force = True
        extra = ("\nEn cuanto lea su cronograma creo la tarea en TickTick (lista Equipo Médico)."
                 if os.environ.get("TICKTICK_TOKEN") else "")
        send(chat_id, f"Agregado: <code>{esc(ref)}</code> {esc(alias)}\n"
                      f"Tomo como base los anexos y actas actuales; solo te aviso de los nuevos.{extra}")
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
            store.data["crono"].pop(s["ref"], None)
            store.data["datos"].pop(s["ref"], None)
            store.data["cierre"].pop(s["ref"], None)
            tarea = store.data["tareas"].pop(s["ref"], None)
        else:
            tarea = None
        send(chat_id, f"Quitado: <code>{esc(s['ref'])}</code>"
                      + ("\nLa tarea de TickTick se queda como esta; ya no actualizo sus fechas." if tarea else ""))
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
    elif cmd == "/fechas":
        mine = [s for s in store.data["subs"] if s["chat"] == chat_id]
        if not mine:
            send(chat_id, "No vigilas ningun procedimiento. Usa /agregar.")
            return
        if rest:  # cronograma completo de uno
            s = _find(store, chat_id, rest)
            c = store.data["crono"].get(s["ref"]) if s else None
            if not s:
                send(chat_id, "No lo encontre. Usa /lista y mandame el numero de la lista, el alias o el folio.")
            elif not c:
                send(chat_id, "Todavia no tengo el cronograma de ese procedimiento (se lee en la siguiente revision).")
            else:
                lineas = [f"• {esc(k)}: {esc(cr.fmt_fecha(v) if cr.parse_fecha(v) else v)}" for k, v in c.items()]
                send(chat_id, f"<b>Cronograma</b> <code>{esc(s['ref'])}</code>\n" + "\n".join(lineas))
            return
        bloques = []
        for i, s in enumerate(mine, 1):
            c = store.data["crono"].get(s["ref"])
            filas = cr.resumen(c) if c else []
            cuerpo = "\n".join(f"   {esc(t)}: {esc(v)}" for t, v in filas) or "   (aun sin leer)"
            bloques.append(f"{i}. <code>{esc(s['ref'])}</code> {esc(s['alias'])}\n{cuerpo}")
        _enviar_por_partes(chat_id, bloques)
    elif cmd == "/plan":
        mine = [s for s in store.data["subs"] if s["chat"] == chat_id]
        if not mine:
            send(chat_id, "No vigilas ningun procedimiento. Usa /agregar.")
            return
        s = _find(store, chat_id, rest) if rest else (mine[0] if len(mine) == 1 else None)
        if not s:
            send(chat_id, "Dime cual: /plan <# de la lista, alias o folio>. Usa /lista para ver los numeros.")
            return
        c = store.data["crono"].get(s["ref"])
        if not c:
            send(chat_id, "Todavia no tengo el cronograma de ese procedimiento (se lee en la siguiente revision).")
            return
        plan = lic.calcular(s["ref"], c, store.data["datos"].get(s["ref"], {}), ahora_mx(),
                            cierre_ja=store.data["cierre"].get(s["ref"], False))
        send(chat_id, "<b>Asi quedaria la tarea en TickTick</b> (vista previa, aun no escribo nada)\n\n"
                      + esc(lic.texto_plan(plan)))
    elif cmd == "/revisar":
        store.force = True
        store.avisar_fin = chat_id
        send(chat_id, "Revisando ahora... (te aviso cuando termine; puede tardar un minuto por procedimiento)")
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


def _car(store: Store, ref: str) -> str:
    c = lic.caracter(ref, store.data["datos"].get(ref, {}))
    return f" · {esc(c)}" if c else ""


def _revisar_cronograma(store: Store, ref: str, ahora, activos: list):
    """Compara el cronograma leido con el guardado y avisa si cambio una fecha vigilada."""
    if not cr.es_lectura_valida(ahora):
        return  # pagina a medio cargar o sin cronograma: no tocar nada
    antes = store.data["crono"].get(ref)
    if antes:  # primera lectura = solo se guarda, sin aviso
        for nombre, a, b in cr.cambios(antes, ahora):
            for s in activos:
                alias = f" ({esc(s['alias'])})" if s["alias"] else ""
                send(s["chat"], f"<b>Cambio de fecha</b> en <code>{esc(ref)}</code>{alias}{_car(store, ref)}\n"
                                f"{esc(cr.describir_cambio(nombre, a, b))}")
    store.data["crono"][ref] = cr.mezclar(antes, ahora)


def _sync_tarea(store: Store, ref: str):
    """Crea (o adopta) la tarea de TickTick de un procedimiento y mantiene sus fechas al dia.
    Solo actua si el procedimiento se agrego con /agregar con el token puesto o ya tiene tarea."""
    subs = [s for s in store.data["subs"] if s["ref"] == ref]
    info = store.data["tareas"].get(ref)
    if not os.environ.get("TICKTICK_TOKEN") or not subs or not (info or any(s.get("tarea") for s in subs)):
        return
    if info and info.get("perdida"):
        return
    crono, datos = store.data["crono"].get(ref), store.data["datos"].get(ref)
    if not crono or not datos or not cr.es_lectura_valida(crono):
        return  # aun no leo lo necesario
    chat = subs[0]["chat"]
    plan = lic.calcular(ref, crono, datos, ahora_mx(), cierre_ja=False)
    try:
        if not info:
            dest = tk.proyecto_destino()
            for t in tk.tareas_de_proyecto(dest["id"]):
                if tk._norm(t.get("title")) == tk._norm(plan.titulo) and t.get("status", 0) == 0:
                    info = store.data["tareas"][ref] = {"id": t["id"], "project": dest["id"]}
                    send(chat, f"Ya existia la tarea <b>{esc(plan.titulo)}</b> en TickTick: la dejo como esta "
                               "y solo mantengo sus fechas y su etiqueta de estatus.")
                    break   # sigue abajo: se le pone la etiqueta de estatus y se ajustan fechas
            else:
                desc = f"{ref}\nCarácter: {plan.caracter}" if plan.caracter else ref
                nueva = tk.crear(tk.tarea_nueva(plan, dest["id"], descripcion=desc))
                store.data["tareas"][ref] = {"id": nueva["id"], "project": dest["id"], "estatus": plan.estatus}
                send(chat, f"<b>Tarea creada en TickTick</b>\n{esc(plan.titulo)} · {esc(plan.caracter)}"
                           + (f" · {esc(plan.estatus)}" if plan.estatus else "") + f"\nLista: {esc(plan.lista)}")
                return
        try:
            t = tk.obtener(info["project"], info["id"])
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (404, 400):
                info["perdida"] = True
                send(chat, f"No encuentro la tarea de <code>{esc(ref)}</code> en TickTick (¿la borraste?). "
                           "Ya no la actualizo.")
                return
            raise
        if t.get("status", 0) != 0:
            return  # tarea completada: no se toca
        t2, cambios = tk.aplicar_plan(t, plan, estatus_anterior=info.get("estatus", ""))
        if cambios:
            tk.actualizar(t2)
            print(f"[{ref}] tarea actualizada: {', '.join(cambios)}")
        if plan.estatus:
            store.data["tareas"][ref]["estatus"] = plan.estatus
        store.data["tareas"][ref].pop("err", None)
    except Exception as e:  # noqa: BLE001
        print(f"[{ref}] TickTick error: {e}")
        inf = store.data["tareas"].setdefault(ref, {}) if info else None
        if inf is not None:
            inf["err"] = inf.get("err", 0) + 1
        if not info and not store.data.setdefault("tk_err", {}).get(ref):
            store.data["tk_err"][ref] = True
            send(chat, f"No pude crear la tarea de <code>{esc(ref)}</code> en TickTick: {esc(str(e))[:150]}. "
                       "Sigo intentando en cada revision.")


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

    _revisar_cronograma(store, ref, getattr(anexos, "cronograma", None), [s for s in subs if s["base"]])
    datos = lic.extraer_datos(getattr(anexos, "datos", None))
    if datos:
        antes_est = store.data["datos"].get(ref, {}).get("estatus", "")
        store.data["datos"][ref] = {**store.data["datos"].get(ref, {}), **datos}
        nuevo_est = datos.get("estatus", "")
        if antes_est and nuevo_est and tk._norm(antes_est) != tk._norm(nuevo_est):   # la primera lectura es silenciosa
            for s in [s for s in subs if s["base"]]:
                alias = f" ({esc(s['alias'])})" if s["alias"] else ""
                send(s["chat"], f"<b>Cambio de estatus</b> en <code>{esc(ref)}</code>{alias}{_car(store, ref)}\n"
                                f"Antes: {esc(antes_est)}\nAhora: <b>{esc(nuevo_est)}</b>")
    if anexos and any(re.search(r"cierre", a.nombre, re.I) and re.search(r"aclaraci|junta", a.nombre, re.I)
                      for a in anexos):
        store.data["cierre"][ref] = True   # no habra mas cambios en bases ni fechas
    _sync_tarea(store, ref)

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
                send(s["chat"], f"<b>Nuevo en</b> <code>{esc(ref)}</code>{alias}{_car(store, ref)}\n{body}{link}")
        else:
            s["base"] = True
            resumen = "\n".join(f"• {esc(a.nombre)}" for a in anexos[:25]) + ("\n..." if len(anexos) > 25 else "")
            fechas = cr.resumen(store.data["crono"].get(ref) or {})
            lin_f = ("\n\n<b>Fechas</b>\n" + "\n".join(f"{esc(t)}: {esc(v)}" for t, v in fechas)) if fechas else ""
            send(s["chat"], f"Base lista: {len(known)} anexos/actas actuales en <code>{esc(ref)}</code>.\n"
                            f"Esto ya existia, solo aviso de lo que aparezca despues:\n{resumen}{lin_f}")


def run_cycle(store: Store, between=None):
    """between: funcion que se llama despues de cada procedimiento (el modo loop la usa para seguir atendiendo Telegram)."""
    refs = sorted({s["ref"] for s in store.data["subs"]})
    if not refs:
        return
    with open_fetcher() as fetch:
        for i, ref in enumerate(refs):
            check_ref(store, ref, fetch)
            store.save()
            if between:
                between()
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
            t0 = time.time()

            def atender():   # sigue contestando mensajes mientras lee los procedimientos
                try:
                    process_updates(store, 0)
                except Exception as e:  # noqa: BLE001
                    print("telegram (durante la revision):", e)

            try:
                run_cycle(store, between=atender)
            except Exception as e:  # noqa: BLE001
                print("ciclo:", e)
            if store.avisar_fin and not store.force:
                send(store.avisar_fin, f"Revision terminada ({int(time.time() - t0)} s). "
                                       f"Procedimientos vigilados: {len(store.data['subs'])}.")
                store.avisar_fin = None
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
            crono = getattr(docs, "cronograma", None)
            if crono:
                print("  CRONOGRAMA DE EVENTOS:")
                for k, v in crono.items():
                    print(f"    {k}: {v}")
            else:
                print("  (no pude leer el cronograma de eventos)")
            datos = lic.extraer_datos(getattr(docs, "datos", None))
            print(f"  DATOS: {datos}")
            if crono:
                plan = lic.calcular(ref, crono, datos, ahora_mx())
                print("  PLAN DE LA TAREA:")
                for linea in lic.texto_plan(plan).splitlines():
                    print("    " + linea)
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

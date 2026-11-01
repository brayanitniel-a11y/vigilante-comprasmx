import contextlib
import os
import tempfile

os.environ["TELEGRAM_TOKEN"] = "x"

import bot  # noqa: E402
from fetcher import Anexo, Lectura, NoEncontrado  # noqa: E402
import cronograma as cr  # noqa: E402
import licitacion as lic  # noqa: E402
from datetime import datetime  # noqa: E402

bot.PAUSE = (0, 0)
bot.ALLOWED = {1}
sent = []
bot.send = lambda chat, text: sent.append(text)

site = {}          # numero -> lista de Anexo (el "sitio" simulado)
broken = set()     # numeros que fallan con error
crono_site = {}    # numero -> dict del cronograma (None = la pagina no lo trajo)
datos_site = {}    # numero -> dict de datos generales de la pagina


@contextlib.contextmanager
def fake_fetcher(debug=False):
    def fetch(ref):
        if ref in broken:
            raise RuntimeError("el sitio no responde")
        if ref not in site:
            raise NoEncontrado(ref)
        lec = Lectura(site[ref])
        lec.cronograma = crono_site.get(ref)
        lec.datos = datos_site.get(ref)
        return lec
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

# 9) lectura de la pagina real de ComprasMX (fixture extraido de un procedimiento verdadero)
import json  # noqa: E402
from fetcher import parse_tablas  # noqa: E402

raw = json.load(open(os.path.join(os.path.dirname(__file__), "tests", "fixture_procedimiento.json"), encoding="utf-8"))
docs = parse_tablas(raw, "http://x/p")
assert len(docs) == 6, [d.nombre for d in docs]          # solo la tabla ANEXOS, no partidas ni requerimientos
assert any("ANEXO TÉCNICO" in d.nombre and "(90 archivos)" in d.nombre for d in docs)
assert not any("53101" in d.nombre or "21601" in d.nombre for d in docs)

# si sube el total de archivos de una fila, se avisa con el valor anterior
REF3 = "LA-07-110-007000999-T-730-2026"
site[REF3] = docs
bot.handle_message(store, 1, REF3)
bot.run_cycle(store)
assert any("Base lista: 6" in s for s in sent)
raw2 = json.loads(json.dumps(raw))
for t in raw2["tables"]:
    for row in t["rows"]:
        if t["sec"] == "ANEXOS" and row[1] == "ANEXO TÉCNICO":
            row[3] = "91"
site[REF3] = parse_tablas(raw2, "http://x/p")
bot.run_cycle(store)
assert "ANEXO TÉCNICO" in sent[-1] and "91 archivos" in sent[-1] and "antes: 90" in sent[-1], sent[-1]
bot.run_cycle(store)
assert sum("91 archivos" in s for s in sent) == 1   # no repite el aviso

# 10) fila nueva (acta) + total que sube, en un solo aviso
import copy  # noqa: E402

docs = parse_tablas(raw, "http://x/p")
assert len(docs) == 6, [d.nombre for d in docs]          # solo la tabla ANEXOS: sin partidas ni requerimientos
assert docs[1].nombre == "[ANEXOS] ANEXO TÉCNICO (90 archivos)", docs[1].nombre
assert docs[3].nombre == "[ANEXOS] ANEXOS DE CONVOCATORIA — ANEXOS TECNICOS (85 archivos)", docs[3].nombre
assert [d.total for d in docs] == [1, 90, 1, 85, 3, 9]
assert len({d.id for d in docs}) == 6

# si sube el total de una fila, es "nuevo" con el mismo grupo; si aparece una fila nueva, tambien
raw2 = copy.deepcopy(raw)
for t in raw2["tables"]:
    if t["sec"] == "ANEXOS":
        t["rows"][1][3] = "91"
        t["rows"].append(["7", "ACTA DE JUNTA DE ACLARACIONES", "ACTA", "1", ""])
docs2 = parse_tablas(raw2, "http://x/p")
nuevos = [d for d in docs2 if d.id not in {x.id for x in docs}]
assert len(nuevos) == 2 and nuevos[0].grupo == docs[1].grupo and nuevos[0].total == 91, nuevos

# flujo completo: base con 6 filas, luego sube un total y aparece un acta -> un solo aviso con "antes"
REF4 = "LA-07-110-007000999-T-731-2026"
site[REF4] = docs
bot.handle_message(store, 1, f"/agregar {REF4}")
bot.run_cycle(store)
assert any("Base lista: 6" in s for s in sent), sent[-3:]
site[REF4] = docs2
bot.run_cycle(store)
assert "(antes: 90)" in sent[-1] and "ACTA DE JUNTA" in sent[-1] and "(nuevo)" in sent[-1], sent[-1]
assert "CONVOCATORIA (1" not in sent[-1]

# 11) lectura vacia sospechosa (pagina a medio cargar): no se toca el estado ni se avisa
site[REF4] = []
n_msgs, n_docs = len(sent), len(store.data["docs"][REF4])
bot.run_cycle(store)
assert len(sent) == n_msgs and len(store.data["docs"][REF4]) == n_docs

# 12) filas identicas (mismo tipo, descripcion y total) son filas distintas: se distinguen por Num.
raw3 = {"titles": ["ANEXOS"], "tables": [{
    "sec": "ANEXOS", "ths": ["Núm.", "Tipo de documento", "Descripción", "Total de Archivos", "Acciones"],
    "rows": [["10", "AVISO DE SUSPENSIÓN DE JUNTA DE ACLARACIONES", "AVISO DE SUSPENSIÓN DE JUNTA DE ACLARACIONES", "1", ""],
             ["11", "AVISO DE SUSPENSIÓN DE JUNTA DE ACLARACIONES", "AVISO DE SUSPENSIÓN DE JUNTA DE ACLARACIONES", "1", ""],
             ["16", "ACTA JUNTA DE ACLARACIONES", "ACTA JUNTA DE ACLARACIONES", "1", ""]]}]}
d3 = parse_tablas(raw3, "http://x/p")
assert len(d3) == 3 and len({d.id for d in d3}) == 3, d3

# 13) migracion: al cambiar el esquema se vuelve a tomar la foto inicial y se avisa una vez
st = bot.Store(os.path.join(tempfile.mkdtemp(), "s.json"))
st.data["subs"].append({"chat": 1, "ref": "LA-50-GYR-050GYR003-T-163-2026", "alias": "", "base": True, "warned": False})
st.data["docs"]["LA-50-GYR-050GYR003-T-163-2026"] = {"x": {"n": "a", "f": "", "g": "", "t": 0}}
n0 = len(sent)
bot.migrate(st)
assert st.data["v"] == bot.SCHEMA and st.data["docs"] == {} and st.data["subs"][0]["base"] is False
assert len(sent) == n0 + 1 and "todas las paginas" in sent[-1]
bot.migrate(st)
assert len(sent) == n0 + 1          # solo una vez
st2 = bot.Store(os.path.join(tempfile.mkdtemp(), "nuevo.json"))
bot.migrate(st2)                    # instalacion nueva: no manda nada
assert len(sent) == n0 + 1

# 14) cronograma: lectura de la pagina real
pares = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "fixture_cronograma.json"), encoding="utf-8"))
C = cr.parse_pairs(pares)
assert C["Fecha y hora de presentación y apertura de proposiciones"] == "06/11/2026 11:00"
assert "Lugar del acto del Fallo" in C and len(C) == 13
assert cr.fmt_fecha("06/11/2026 11:00") == "viernes 06/11/2026 11:00"
assert cr.fmt_fecha("18/11/2026") == "miércoles 18/11/2026"
assert not cr.es_lectura_valida({}) and not cr.es_lectura_valida({"Fecha y hora de x": ""}) and cr.es_lectura_valida(C)
assert cr.resumen(C)[2] == ("Presentación y apertura", "viernes 06/11/2026 11:00")

REF5 = "LA-50-GYR-050GYR003-T-163-2026"
APER = "Fecha y hora de presentación y apertura de proposiciones"
site[REF5] = [Anexo("z1", "[ANEXOS] CONVOCATORIA (1 archivos)", "http://x/p")]
crono_site[REF5] = dict(C)
bot.handle_message(store, 1, f"/agregar {REF5} T-163")
bot.run_cycle(store)
assert store.data["crono"][REF5][APER] == "06/11/2026 11:00"
assert any("Base lista" in s and "Presentación y apertura: viernes 06/11/2026 11:00" in s for s in sent)
n = len(sent)
bot.run_cycle(store)
assert len(sent) == n                                   # sin cambios: ningun aviso

# 15) diferimiento: cambia la fecha de apertura -> un aviso claro, una sola vez
crono_site[REF5] = {**C, APER: "13/11/2026 09:30"}
bot.run_cycle(store)
msg = sent[-1]
assert "Cambio de fecha" in msg and "(T-163)" in msg, msg
assert "del viernes 06/11/2026 11:00 al viernes 13/11/2026 09:30" in msg and "Diferida 7 días" in msg, msg
bot.run_cycle(store)
assert sum("Cambio de fecha" in s for s in sent) == 1
assert store.data["crono"][REF5][APER] == "13/11/2026 09:30"

# 16) adelantarla, o solo cambiar la hora
crono_site[REF5] = {**C, APER: "12/11/2026 09:30"}
bot.run_cycle(store)
assert "Adelantada 1 día." in sent[-1], sent[-1]
crono_site[REF5] = {**C, APER: "12/11/2026 12:00"}
bot.run_cycle(store)
assert "mismo día; cambia la hora" in sent[-1], sent[-1]

# 17) ahora se vigilan todas las fechas importantes (junta, fallo, limite, visita); lo demas se guarda sin avisar
JA_L = "Fecha y hora de junta de aclaraciones"
NF_L = "Fecha y hora del acto del Fallo"
n = len(sent)
crono_site[REF5] = {**C, APER: "12/11/2026 12:00", JA_L: "25/09/2026 10:00"}
bot.run_cycle(store)
assert len(sent) == n + 1 and "fecha de la junta de aclaraciones, del jueves 24/09/2026 12:00 al viernes 25/09/2026 10:00" in sent[-1], sent[-1]
crono_site[REF5] = {**C, APER: "12/11/2026 12:00", JA_L: "25/09/2026 10:00", NF_L: "20/11/2026 16:00"}
bot.run_cycle(store)
assert "fecha del acto del fallo, del viernes 13/11/2026 16:00 al viernes 20/11/2026 16:00" in sent[-1] and "Diferida 7 días" in sent[-1], sent[-1]
n = len(sent)
crono_site[REF5] = {**C, APER: "12/11/2026 12:00", JA_L: "25/09/2026 10:00", NF_L: "20/11/2026 16:00",
                    "Lugar del acto del Fallo": "OTRO LUGAR", "Fecha estimada del inicio del contrato": "01/12/2026"}
bot.run_cycle(store)
assert len(sent) == n                                   # lugares y fecha de contrato no avisan

# 18) lectura incompleta o sin cronograma: no avisa ni borra lo guardado
for malo in (None, {}, {k: "" for k in C}, {APER: ""}):
    crono_site[REF5] = malo
    bot.run_cycle(store)
    assert len(sent) == n and store.data["crono"][REF5][APER] == "12/11/2026 12:00"

# 19) procedimiento que ya vigilabas antes de esta funcion: la primera lectura solo guarda, sin aviso
REF6 = "LA-50-GYR-050GYR004-T-164-2026"
site[REF6] = [Anexo("w1", "[ANEXOS] CONVOCATORIA (1 archivos)", "http://x/p")]
bot.handle_message(store, 1, f"/agregar {REF6}")
bot.run_cycle(store)                                    # base sin cronograma (version anterior)
assert REF6 not in store.data["crono"]
n = len(sent)
crono_site[REF6] = {**C, APER: "20/11/2026 10:00"}
bot.run_cycle(store)
assert len(sent) == n and store.data["crono"][REF6][APER] == "20/11/2026 10:00"
crono_site[REF6] = {**C, APER: "27/11/2026 10:00"}
bot.run_cycle(store)
assert "Cambio de fecha" in sent[-1] and "REF6" not in sent[-1] and REF6 in sent[-1]

# 20) /fechas: resumen de todos y cronograma completo de uno
bot.handle_message(store, 1, "/fechas")
assert "T-163" in sent[-1] and "Presentación y apertura" in sent[-1], sent[-1]
bot.handle_message(store, 1, "/fechas T-163")
assert "Lugar del acto del Fallo" in sent[-1] and "jueves 12/11/2026 12:00" in sent[-1], sent[-1]
bot.handle_message(store, 1, f"/quitar T-163")
assert REF5 not in store.data["crono"]
big = [f"{i}. bloque " + "x" * 400 for i in range(20)]
sent_antes = len(sent)
bot._enviar_por_partes(1, big)
assert len(sent) - sent_antes >= 3 and all(len(m) <= 4096 for m in sent[sent_antes:])

# 21) nombre corto, identificador de lugar y titulo
assert lic.nombre_corto("LA-50-GYR-050GYR003-T-163-2026") == "LA-T-163-2026"
assert lic.nombre_corto("IO-89-L47-830124970-N-14-2026") == "IO-N-14-2026"
assert lic.nombre_corto("raro") == "RARO"
D163 = {"dependencia": "050GYR - INSTITUTO MEXICANO DEL SEGURO SOCIAL", "ramo": "50 - INSTITUTO MEXICANO DEL SEGURO SOCIAL",
        "entidad": "BAJA CALIFORNIA"}
D730 = {"dependencia": "007000 - SECRETARIA DE LA DEFENSA NACIONAL", "ramo": "07 - DEFENSA NACIONAL", "entidad": "CIUDAD DE MÉXICO"}
assert lic.titulo("LA-50-GYR-050GYR003-T-163-2026", D163) == "LICITACIÓN LA-T-163-2026 Baja California"
assert lic.titulo("LA-07-110-007000999-T-730-2026", D730) == "LICITACIÓN LA-T-730-2026 SEDENA"   # SEDENA gana sobre la entidad
assert lic.identificador({"ramo": "50 - IMSS", "entidad": "CIUDAD DE MÉXICO"}) == "CDMX"
assert lic.identificador({"entidad": "MÉXICO"}) == "EDOMEX" and lic.identificador({"entidad": "ESTADO DE MÉXICO"}) == "EDOMEX"
assert lic.identificador({"entidad": "COAHUILA DE ZARAGOZA"}) == "Coahuila de Zaragoza"
assert lic.identificador({"entidad": "NUEVO LEÓN"}) == "Nuevo León" and lic.identificador({}) == ""
assert lic.titulo("LA-50-GYR-050GYR003-T-163-2026", {}) == "LICITACIÓN LA-T-163-2026"
assert lic.titulo("IO-89-L47-830124970-N-14-2026", {"entidad": "HIDALGO"}) == "INVITACIÓN IO-N-14-2026 Hidalgo"
dd = lic.desambiguar({"LA-50-GYR-050GYR003-T-163-2026": "LICITACIÓN LA-T-163-2026 Hidalgo",
                      "LA-12-AAA-012AAA001-T-163-2026": "LICITACIÓN LA-T-163-2026 Hidalgo", "X": "otro"})
assert dd["LA-50-GYR-050GYR003-T-163-2026"].endswith("(050GYR003)") and dd["X"] == "otro"

# 22) extraer datos generales de la pagina
todos = cr.parse_pairs([["Estatus del procedimiento de contratación:", "EN ACLARACIONES"], ["Ramo:", "07 - DEFENSA NACIONAL"],
                        ["Entidad Federativa donde se llevará a cabo la contratación:", "CIUDAD DE MÉXICO"],
                        ["Carácter:", "INTERNACIONAL BAJO LA COBERTURA DE TRATADOS"], ["Código del expediente:", "E-2026-1"]])
dx = lic.extraer_datos(todos)
assert dx == {"estatus": "EN ACLARACIONES", "ramo": "07 - DEFENSA NACIONAL", "entidad": "CIUDAD DE MÉXICO",
              "caracter": "INTERNACIONAL BAJO LA COBERTURA DE TRATADOS"}, dx

# 23) plan de la tarea con el ejemplo de la captura del usuario (JA 24 nov 9:00, PP 9 dic 9:00, NF 29 dic 12:00)
CE = {"Fecha y hora de junta de aclaraciones": "24/11/2026 09:00",
      "Fecha y hora límite para envío de aclaraciones a través de Compras MX": "23/11/2026 09:00",
      "Fecha y hora de presentación y apertura de proposiciones": "09/12/2026 09:00",
      "Fecha y hora del acto del Fallo": "29/12/2026 12:00"}
R = "LA-50-GYR-050GYR003-T-163-2026"
pl = lic.calcular(R, CE, D163, datetime(2026, 10, 31, 12, 0))
por = {i.titulo: i for i in pl.items}
assert [i.titulo for i in pl.items][:8] == ["Solicitar Marcas y Modelos", "Solicitar Referencias", "Solicitar Apoyos", "Elaborar Preguntas",
                                            "¿Visita?", "Limite para envío de Preguntas", "EVENTO: JA", "Acta de cierre de JA"]
assert len(pl.items) == 16 and pl.items[-1].titulo == "Evento: NF"
assert por["Limite para envío de Preguntas"].fecha == datetime(2026, 11, 23, 8, 0)         # JA - 25 h
assert por["Elaborar Preguntas"].fecha.isoformat() == "2026-11-22" and por["Elaborar Preguntas"].todo_el_dia
assert por["EVENTO: JA"].fecha == datetime(2026, 11, 24, 9, 0) and por["Evento: PP"].fecha == datetime(2026, 12, 9, 9, 0)
assert por["Evento: NF"].fecha == datetime(2026, 12, 29, 12, 0) and por["¿Visita?"].fecha is None
assert por["Solicitar Apoyos"].fecha is None and por["Acta de cierre de JA"].fecha is None and not por["Acta de cierre de JA"].hecho
assert pl.fecha_tarea == datetime(2026, 12, 9, 9, 0) and pl.etiquetas == ["LICITACIONES"] and pl.lista == "Equipo Medico"
assert pl.titulo == "LICITACIÓN LA-T-163-2026 Baja California" and not pl.avisos

# la fecha de la tarea cambia a NF + FALLO cuando se cumple la hora de PP
antes_pp = lic.calcular(R, CE, D163, datetime(2026, 12, 9, 8, 59))
assert antes_pp.fecha_tarea == datetime(2026, 12, 9, 9, 0) and antes_pp.etiquetas == ["LICITACIONES"]
despues = lic.calcular(R, CE, D163, datetime(2026, 12, 9, 9, 0))
assert despues.fecha_tarea == datetime(2026, 12, 29, 12, 0) and despues.etiquetas == ["LICITACIONES", "FALLO"]
# si difieren la PP despues, el plan se recalcula solo (la tarea vuelve a PP y se quita FALLO)
dif = lic.calcular(R, {**CE, "Fecha y hora de presentación y apertura de proposiciones": "16/12/2026 09:00"}, D163, datetime(2026, 12, 9, 10, 0))
assert dif.fecha_tarea == datetime(2026, 12, 16, 9, 0) and dif.etiquetas == ["LICITACIONES"]

# limite: si ComprasMX trae uno mas temprano que 24 h antes, manda ese menos 1 h
temprano = lic.calcular(R, {**CE, "Fecha y hora límite para envío de aclaraciones a través de Compras MX": "22/11/2026 09:00"}, D163, datetime(2026, 10, 31))
pt = {i.titulo: i for i in temprano.items}
assert pt["Limite para envío de Preguntas"].fecha == datetime(2026, 11, 22, 8, 0) and pt["Elaborar Preguntas"].fecha.isoformat() == "2026-11-21"
# sin limite propio de ComprasMX: solo JA - 25 h
solo_ja = lic.calcular(R, {k: v for k, v in CE.items() if "límite" not in k}, D163, datetime(2026, 10, 31))
assert {i.titulo: i for i in solo_ja.items}["Limite para envío de Preguntas"].fecha == datetime(2026, 11, 23, 8, 0)
# visita con fecha
vis = lic.calcular(R, {**CE, "Fecha y hora de la visita a las instalaciones": "18/11/2026 10:00"}, D163, datetime(2026, 10, 31))
assert {i.titulo: i for i in vis.items}["¿Visita?"].fecha == datetime(2026, 11, 18, 10, 0)
# faltan fechas: avisa y deja ese item sin fecha
falta = lic.calcular(R, {"Fecha y hora de publicación": "01/10/2026 10:00"}, D163, datetime(2026, 10, 31))
assert len(falta.avisos) == 3 and {i.titulo: i for i in falta.items}["EVENTO: JA"].fecha is None and falta.fecha_tarea is None
# acta de cierre marcada
assert {i.titulo: i for i in lic.calcular(R, CE, D163, datetime(2026, 10, 31), cierre_ja=True).items}["Acta de cierre de JA"].hecho
txt = lic.texto_plan(pl)
assert "LICITACIÓN LA-T-163-2026 Baja California" in txt and "☐ Limite para envío de Preguntas — lunes 23/11/2026 08:00" in txt, txt
assert "☐ Elaborar Preguntas — domingo 22/11/2026 (sin hora)" in txt and "Fecha de la tarea: miércoles 09/12/2026 09:00" in txt, txt

# 24) /plan en el bot: lee datos y cronograma guardados, calcula con la hora de Mexico
bot.ahora_mx = lambda: datetime(2026, 10, 31, 12, 0)
datos_site[REF6] = {"Estatus del procedimiento de contratación:": "EN ACLARACIONES", "Ramo": "50 - IMSS",
                    "Dependencia o Entidad": "050GYR - IMSS", "Entidad Federativa donde se llevará a cabo la contratación": "HIDALGO"}
datos_site[REF6] = cr.parse_pairs([[k, v] for k, v in datos_site[REF6].items()])
crono_site[REF6] = dict(CE)
bot.run_cycle(store)
assert store.data["datos"][REF6]["entidad"] == "HIDALGO" and store.data["datos"][REF6]["estatus"] == "EN ACLARACIONES"
bot.handle_message(store, 1, "/plan")        # varios vigilados -> pide cual
assert "Dime cual" in sent[-1]
bot.handle_message(store, 1, f"/plan {REF6}")
assert "Asi quedaria" in sent[-1] and "LICITACIÓN LA-T-164-2026 Hidalgo" in sent[-1] and "☐ EVENTO: JA — martes 24/11/2026 09:00" in sent[-1], sent[-1]
bot.handle_message(store, 1, "/plan 99")
assert "Dime cual" in sent[-1]

# 25) se detecta el acta de cierre de la JA: la casilla queda marcada
assert not store.data["cierre"].get(REF6)
site[REF6].append(Anexo("c1", "[ANEXOS] ACTA DE CIERRE DE JUNTA DE ACLARACIONES (1 archivos)", "http://x/p"))
bot.run_cycle(store)
assert store.data["cierre"][REF6] is True
bot.handle_message(store, 1, f"/plan {REF6}")
assert "☑ Acta de cierre de JA" in sent[-1], sent[-1]
bot.handle_message(store, 1, f"/quitar {REF6}")
assert REF6 not in store.data["datos"] and REF6 not in store.data["cierre"]

# 26) prefijo segun el portal y caracter
assert lic.prefijo("XX-1-N-1-2026", {"tipo": "INVITACIÓN A CUANDO MENOS TRES PERSONAS"}) == "INVITACIÓN"
assert lic.prefijo("LA-1-N-1-2026", {"tipo": "ADJUDICACIÓN DIRECTA"}) == "ADJUDICACIÓN" and lic.prefijo("LA-1-N-1-2026", {}) == "LICITACIÓN"
assert lic.caracter("LA-50-GYR-050GYR003-T-163-2026", D163 | {"caracter": "INTERNACIONAL BAJO LA COBERTURA DE TRATADOS"}) == "Internacional bajo tratados"
assert lic.caracter("LA-72-037-913055954-N-23-2026", {}) == "Nacional" and lic.caracter("LA-72-037-913055954-N-23-2026", {"caracter": "NACIONAL"}) == "Nacional"
assert "Carácter: Internacional bajo tratados" in lic.texto_plan(pl), lic.texto_plan(pl)
print("OK: todas las pruebas pasaron")

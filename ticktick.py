"""Cliente de TickTick (Open API). Solo toca la lista indicada en plantilla.json (carpeta licitaciones / lista Equipo Médico).
Nunca imprime el token. Reglas de oro:
- Al ACTUALIZAR solo se cambian: fecha de la tarea, etiqueta FALLO y las fechas de los puntos con fecha automatica.
  El estado (hecho / pendiente), la descripcion y los demas puntos del usuario no se tocan.
- El punto "Acta de cierre de JA" lo marca el usuario; el bot nunca lo marca ni lo desmarca."""
import os
import sys
import unicodedata
from datetime import date, datetime

import requests

import licitacion as lic

BASE = "https://api.ticktick.com/open/v1"
TZ = "America/Mexico_City"
OFFSET = "-0600"          # Mexico ya no tiene horario de verano


def _h():
    return {"Authorization": f"Bearer {os.environ['TICKTICK_TOKEN'].strip()}", "Content-Type": "application/json"}


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn").strip().lower()


def fmt(d) -> str:
    if not isinstance(d, datetime):
        d = datetime(d.year, d.month, d.day)
    return d.strftime("%Y-%m-%dT%H:%M:%S") + OFFSET


def instante(v):
    """Fecha de TickTick (milisegundos o texto con zona) -> datetime UTC; None si no hay."""
    from datetime import timezone
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc)
    return datetime.strptime(v[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc) - _off(v[19:])


def _off(s: str):
    from datetime import timedelta
    s = s.strip()
    if "+" in s[-5:] or "-" in s[-5:]:
        sg = 1 if s[-5] == "+" else -1
        return sg * timedelta(hours=int(s[-4:-2]), minutes=int(s[-2:]))
    return timedelta(0)


def _mismo(v, d) -> bool:
    return instante(v) == instante(fmt(d))


def _iso(v):
    """Devuelve la fecha en formato de escritura (hora de Mexico), o None."""
    from datetime import timedelta
    i = instante(v)
    return None if i is None else (i - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S") + OFFSET


# ---------- logica pura (con pruebas) ----------
def tarea_nueva(plan: lic.Plan, project_id: str, titulo: str | None = None, descripcion: str = "") -> dict:
    items = []
    for i, it in enumerate(plan.items):
        d = {"title": it.titulo, "status": 1 if it.hecho else 0, "sortOrder": i * 1000, "timeZone": TZ}
        if it.fecha:
            d["startDate"] = fmt(it.fecha)
            d["isAllDay"] = bool(it.todo_el_dia)
        items.append(d)
    t = {"title": titulo or plan.titulo, "projectId": project_id, "timeZone": TZ, "tags": list(plan.etiquetas),
         "content": descripcion, "items": items}
    if plan.fecha_tarea:
        t["dueDate"], t["startDate"], t["isAllDay"] = fmt(plan.fecha_tarea), fmt(plan.fecha_tarea), False
    return t


def aplicar_plan(tarea: dict, plan: lic.Plan, plantilla: dict | None = None) -> tuple[dict, list[str]]:
    """Devuelve (tarea modificada, lista de cambios). No toca estados ni puntos sin fecha automatica."""
    pl = plantilla or lic.cargar_plantilla()
    cambios = []
    t = dict(tarea)
    if plan.fecha_tarea and not _mismo(t.get("dueDate"), plan.fecha_tarea):
        t["dueDate"], t["startDate"] = fmt(plan.fecha_tarea), fmt(plan.fecha_tarea)
        t["isAllDay"] = False
        cambios.append("fecha de la tarea")
    tags = list(t.get("tags") or [])
    fallo = pl["etiqueta_fallo"]
    quiere = fallo in plan.etiquetas
    tiene = any(_norm(x) == _norm(fallo) for x in tags)
    if quiere and not tiene:
        tags.append(fallo)
        cambios.append(f"etiqueta {fallo} agregada")
    elif tiene and not quiere:
        tags = [x for x in tags if _norm(x) != _norm(fallo)]
        cambios.append(f"etiqueta {fallo} quitada")
    t["tags"] = tags
    por_titulo = {_norm(i.titulo): i for i in plan.items if i.fecha}
    nuevos = []
    for it in t.get("items") or []:
        it = dict(it)
        if it.get("startDate") is not None:
            it["startDate"] = _iso(it["startDate"])
        p = por_titulo.get(_norm(it.get("title")))
        if p and not _mismo(it.get("startDate"), p.fecha):
            it["startDate"], it["isAllDay"], it["timeZone"] = fmt(p.fecha), bool(p.todo_el_dia), TZ
            cambios.append(f"{it['title']}")
        nuevos.append(it)
    t["items"] = nuevos
    return t, cambios


# ---------- llamadas a la API ----------
def proyectos():
    r = requests.get(f"{BASE}/project", headers=_h(), timeout=30)
    r.raise_for_status()
    return r.json()


def proyecto_destino(plantilla: dict | None = None) -> dict:
    pl = plantilla or lic.cargar_plantilla()
    hallados = [p for p in proyectos() if _norm(p.get("name")) == _norm(pl["lista"]) and not p.get("closed")]
    if len(hallados) != 1:
        raise RuntimeError(f"Esperaba 1 lista '{pl['lista']}' y encontre {len(hallados)}")
    return hallados[0]


def tareas_de_proyecto(project_id: str) -> list:
    r = requests.get(f"{BASE}/project/{project_id}/data", headers=_h(), timeout=30)
    r.raise_for_status()
    return r.json().get("tasks") or []


def crear(t: dict) -> dict:
    r = requests.post(f"{BASE}/task", headers=_h(), json=t, timeout=30)
    r.raise_for_status()
    return r.json()


def obtener(project_id: str, task_id: str) -> dict:
    r = requests.get(f"{BASE}/project/{project_id}/task/{task_id}", headers=_h(), timeout=30)
    r.raise_for_status()
    return r.json()


def actualizar(t: dict) -> dict:
    r = requests.post(f"{BASE}/task/{t['id']}", headers=_h(), json=t, timeout=30)
    r.raise_for_status()
    return r.json()


def borrar(project_id: str, task_id: str) -> int:
    return requests.delete(f"{BASE}/project/{project_id}/task/{task_id}", headers=_h(), timeout=30).status_code


# ---------- modos de calibracion ----------
def sonda():
    for p in proyectos():
        print(f"  lista: {p.get('name')!r} | carpeta: {p.get('groupId')} | id: {p.get('id')}")
    return 0


def prueba():
    """Crea una tarea de PRUEBA en la lista destino, la lee, cambia fechas, comprueba que respeta el resto y la borra."""
    from datetime import datetime as dt
    crono = {"Fecha y hora de junta de aclaraciones": "24/11/2026 09:00",
             "Fecha y hora de presentación y apertura de proposiciones": "09/12/2026 09:00",
             "Fecha y hora del acto del Fallo": "29/12/2026 12:00"}
    datos = {"tipo": "LICITACIÓN PÚBLICA", "entidad": "HIDALGO", "caracter": "NACIONAL"}
    ref = "LA-72-037-913055954-N-999-2026"
    plan = lic.calcular(ref, crono, datos, dt(2026, 11, 1, 12, 0))
    dest = proyecto_destino()
    print(f"lista destino: {dest['name']} (carpeta {dest.get('groupId')})")
    c = crear(tarea_nueva(plan, dest["id"], titulo="PRUEBA CENTINELA (borrar) " + plan.titulo))
    print("crear -> id", c.get("id"), "| items devueltos:", len(c.get("items") or []), "| tags:", c.get("tags"), "| due:", c.get("dueDate"))
    ok = False
    try:
        t = obtener(dest["id"], c["id"])
        print("leer -> items:", len(t.get("items") or []), "| due:", t.get("dueDate"), "| tags:", t.get("tags"), "| tz:", t.get("timeZone"))
        for it in t["items"]:
            print("   ", it.get("status"), it.get("title"), "|", it.get("startDate"), "|", it.get("isAllDay"))
        # simula que el usuario marco un punto y escribio una descripcion
        t["items"][0]["status"] = 1
        t["content"] = "nota del usuario"
        t = actualizar(t)
        # difieren PP y llega la hora: debe cambiar fecha, quitar/poner FALLO, y respetar lo del usuario
        crono2 = {**crono, "Fecha y hora de presentación y apertura de proposiciones": "16/12/2026 09:00"}
        plan2 = lic.calcular(ref, crono2, datos, dt(2026, 12, 16, 9, 0))
        t2, cambios = aplicar_plan(obtener(dest["id"], c["id"]), plan2)
        print("cambios a aplicar:", cambios)
        r = actualizar(t2)
        f = obtener(dest["id"], c["id"])
        print("final -> items:", len(f.get("items") or []), "| due:", f.get("dueDate"), "| tags:", f.get("tags"),
              "| item0 status:", f["items"][0].get("status"), "| content:", repr(f.get("content")))
        ok = (len(f["items"]) == len(plan.items) and f["items"][0].get("status") == 1 and f.get("content") == "nota del usuario"
              and "FALLO" in (f.get("tags") or []))
    finally:
        print("borrar -> HTTP", borrar(dest["id"], c["id"]))
    print("RESULTADO:", "OK" if ok else "REVISAR")
    return 0 if ok else 1


if __name__ == "__main__":
    if not os.environ.get("TICKTICK_TOKEN"):
        print("Falta el secreto TICKTICK_TOKEN")
        sys.exit(1)
    modo = sys.argv[1] if len(sys.argv) > 1 else "sonda"
    try:
        sys.exit({"sonda": sonda, "prueba": prueba}[modo]())
    except requests.HTTPError as e:
        print("HTTP", e.response.status_code, e.response.text[:300])
        sys.exit(1)

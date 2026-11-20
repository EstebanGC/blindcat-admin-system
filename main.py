import os
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import date

app = FastAPI(title="BlindCat - Trazabilidad y CRUD")

DATABASE_URL = os.getenv("DATABASE_URL")
BASE_DIR = Path(__file__).resolve().parent

# AJUSTA AQUÍ TUS CREDENCIALES DE POSTGRESQL (respaldo local)
DB_CONFIG = {
    "dbname": os.getenv("DB_NAME", "blindcat_dml"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "whatever"),
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5433"),
}

def get_conn():
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)

def generar_codigo_lote(cur) -> str:
    """Usa seq_codigo_lote si existe; si no, calcula el siguiente correlativo del año."""
    cur.execute("""
        SELECT EXISTS (
            SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'S' AND c.relname = 'seq_codigo_lote' AND n.nspname = 'public'
        ) AS existe_seq;
    """)
    existe_seq = cur.fetchone()["existe_seq"]
    anio = date.today().year

    if existe_seq:
        cur.execute("SELECT nextval('public.seq_codigo_lote') AS num;")
        num = cur.fetchone()["num"]
    else:
        cur.execute("""
            SELECT COALESCE(
                MAX(CAST(split_part(codigo_trazabilidad, '-', 3) AS INTEGER)), 0
            ) + 1 AS num
            FROM public.lote
            WHERE codigo_trazabilidad LIKE %s;
        """, (f"LT-{anio}-%",))
        num = cur.fetchone()["num"]

    return f"LT-{anio}-{int(num):05d}"

# Modelos Pydantic
class LoteCreate(BaseModel):
    codigo_trazabilidad: str | None = None
    id_variedad: int
    id_invernadero: int
    id_licencia: int
    cantidad_plantas: int
    fecha_inicio: date
    fecha_cosecha: date | None = None
    cedula_cosechador: str | None = None

class LoteUpdate(BaseModel):
    estado_actual: str
    peso_humedo_kg: float | None = None
    peso_seco_kg: float | None = None
    fecha_cosecha: date | None = None
    cedula_cosechador: str | None = None

@app.get("/", response_class=HTMLResponse)
def home():
    html_path = BASE_DIR / "index.html"
    return html_path.read_text(encoding="utf-8")

# 1. READ: Consultar lotes con JOINs a variedad, invernadero y cosechador
@app.get("/api/lotes")
def listar_lotes():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT l.codigo_trazabilidad,
                       v.nombre AS variedad,
                       i.nombre_modulo AS invernadero,
                       l.cantidad_plantas,
                       l.fecha_inicio,
                       l.fecha_cosecha,
                       COALESCE(e.nombre_completo, l.cedula_cosechador) AS cosechador,
                       l.estado_actual,
                       l.peso_humedo_kg,
                       l.peso_seco_kg
                FROM public.lote l
                JOIN public.variedad v USING (id_variedad)
                JOIN public.invernadero i USING (id_invernadero)
                LEFT JOIN public.empleado e ON e.cedula = l.cedula_cosechador
                ORDER BY l.codigo_trazabilidad DESC;
            """)
            return cur.fetchall()
    finally:
        conn.close()

# 2. CREATE: Crear nuevo lote (incluye datos opcionales de cosecha)
@app.post("/api/lotes")
def crear_lote(data: LoteCreate):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            codigo = (data.codigo_trazabilidad or "").strip() or generar_codigo_lote(cur)
            cur.execute("""
                INSERT INTO public.lote (
                    codigo_trazabilidad, id_variedad, id_invernadero, id_licencia,
                    cantidad_plantas, fecha_inicio, fecha_cosecha,
                    cedula_cosechador, id_rol_cosechador
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 2)
                RETURNING codigo_trazabilidad, estado_actual, fecha_cosecha, cedula_cosechador;
            """, (
                codigo, data.id_variedad, data.id_invernadero, data.id_licencia,
                data.cantidad_plantas, data.fecha_inicio, data.fecha_cosecha,
                data.cedula_cosechador
            ))
            nuevo = cur.fetchone()
            conn.commit()
            return {"mensaje": "Lote creado exitosamente", "lote": nuevo}
    except psycopg2.Error as e:
        conn.rollback()
        msg = e.pgerror.splitlines()[0] if e.pgerror else str(e)
        raise HTTPException(status_code=400, detail=msg)
    finally:
        conn.close()

# 3. UPDATE: Actualizar estado, pesos y datos de cosecha del lote
@app.put("/api/lotes/{codigo}")
def actualizar_lote(codigo: str, data: LoteUpdate):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE public.lote
                SET estado_actual = %s,
                    peso_humedo_kg = COALESCE(%s, peso_humedo_kg),
                    peso_seco_kg = COALESCE(%s, peso_seco_kg),
                    fecha_cosecha = COALESCE(%s, fecha_cosecha),
                    cedula_cosechador = COALESCE(%s, cedula_cosechador),
                    id_rol_cosechador = COALESCE(id_rol_cosechador, 2)
                WHERE codigo_trazabilidad = %s
                RETURNING codigo_trazabilidad, estado_actual, peso_humedo_kg, peso_seco_kg, fecha_cosecha, cedula_cosechador;
            """, (
                data.estado_actual, data.peso_humedo_kg, data.peso_seco_kg,
                data.fecha_cosecha, data.cedula_cosechador, codigo
            ))
            actualizado = cur.fetchone()
            if not actualizado:
                raise HTTPException(status_code=404, detail="Lote no encontrado")
            conn.commit()
            return {"mensaje": "Lote actualizado", "lote": actualizado}
    except psycopg2.Error as e:
        conn.rollback()
        msg = e.pgerror.splitlines()[0] if e.pgerror else str(e)
        raise HTTPException(status_code=400, detail=msg)
    finally:
        conn.close()

# 4. DELETE / SOFT DELETE:
# A) Soft delete de empleado (activo = FALSE)
@app.patch("/api/empleados/{cedula}/desactivar")
def soft_delete_empleado(cedula: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE public.empleado SET activo = FALSE
                WHERE cedula = %s RETURNING cedula, nombre_completo, activo;
            """, (cedula,))
            emp = cur.fetchone()
            if not emp:
                raise HTTPException(status_code=404, detail="Empleado no encontrado")
            conn.commit()
            return {"mensaje": "Soft Delete aplicado correctamente", "empleado": emp}
    except psycopg2.Error as e:
        conn.rollback()
        msg = e.pgerror.splitlines()[0] if e.pgerror else str(e)
        raise HTTPException(status_code=400, detail=msg)
    finally:
        conn.close()

# B) Prueba de integridad / inmutabilidad en registro_custodia
@app.delete("/api/custodia/{id_custodia}")
def eliminar_custodia(id_custodia: int):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM public.registro_custodia WHERE id_custodia = %s;", (id_custodia,))
            conn.commit()
            return {"mensaje": "Registro eliminado"}
    except psycopg2.Error as e:
        conn.rollback()
        msg = e.pgerror.splitlines()[0] if e.pgerror else str(e)
        raise HTTPException(status_code=400, detail=msg)
    finally:
        conn.close()
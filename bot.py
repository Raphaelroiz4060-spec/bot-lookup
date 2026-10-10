# bot.py
# pip install discord.py aiosqlite pandas openpyxl

import discord
from discord.ext import commands
import aiosqlite
import pandas as pd
import io
import os
import json
from datetime import datetime

TOKEN = "MTU1Nzc5NzcyODI5MjA0NDkyMA.G_O5w9.sOndjNvAfaip6M0Ylfy_ZjoJb58NCQ76t08iuk"
DB_PATH = "search.db"
ALLOWED_EXTENSIONS = {".csv", ".json", ".txt", ".xlsx"}

COLOR_SUCCESS = 0x3FB950
COLOR_ERROR   = 0xF85149
COLOR_INFO    = 0x58A6FF
COLOR_PURPLE  = 0x8B5CF6

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ── Clean valeur ──────────────────────────────────────────
def clean_value(v) -> str:
    if v is None:
        return ""
    v = str(v).strip()
    if v in ("nan", "N/A", "null", "None", ""):
        return ""
    return v

# ── Init DB ───────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS datasets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                uploaded_by TEXT NOT NULL,
                row_count INTEGER,
                columns TEXT,
                uploaded_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dataset_id INTEGER NOT NULL,
                data TEXT NOT NULL,
                FOREIGN KEY (dataset_id) REFERENCES datasets(id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_records_data ON records(data)")
        await db.commit()

# ── Ingestion via Discord upload ──────────────────────────
async def ingest_file(file_bytes: bytes, filename: str, uploader_id: int) -> dict:
    ext = os.path.splitext(filename)[1].lower()
    df = None

    if ext == ".csv":
        for enc in ["utf-8", "latin-1", "cp1252"]:
            try:
                df = pd.read_csv(io.BytesIO(file_bytes), dtype=str,
                                 on_bad_lines="skip", encoding=enc)
                break
            except Exception:
                continue

    elif ext == ".json":
        try:
            df = pd.read_json(io.BytesIO(file_bytes), dtype=str)
        except Exception:
            lines = file_bytes.decode("utf-8", errors="ignore").splitlines()
            json_rows = [json.loads(l) for l in lines if l.strip()]
            df = pd.DataFrame(json_rows)

    elif ext == ".xlsx":
        df = pd.read_excel(io.BytesIO(file_bytes), dtype=str)

    elif ext == ".txt":
        try:
            lines = file_bytes.decode("utf-8", errors="ignore").splitlines()
            json_rows = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                json_rows.append(json.loads(line))
            if json_rows:
                df = pd.DataFrame(json_rows)
        except Exception:
            df = None

        if df is None:
            for sep in [",", ";", "\t", "|"]:
                for enc in ["utf-8", "latin-1", "cp1252"]:
                    try:
                        df = pd.read_csv(io.BytesIO(file_bytes), sep=sep,
                                        dtype=str, on_bad_lines="skip", encoding=enc)
                        if len(df.columns) > 1:
                            break
                    except Exception:
                        continue
                if df is not None and len(df.columns) > 1:
                    break

        if df is None or len(df.columns) == 1:
            lines = file_bytes.decode("utf-8", errors="ignore").splitlines()
            df = pd.DataFrame(lines, columns=["value"])

    if df is None:
        raise ValueError(f"Format non supporté : {ext}")

    df = df.fillna("")
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()

    columns = list(df.columns)
    rows = df.to_dict(orient="records")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM datasets WHERE name = ?", (filename,)) as cur:
            existing = await cur.fetchone()
        if existing:
            await db.execute("DELETE FROM records WHERE dataset_id = ?", (existing[0],))
            await db.execute("DELETE FROM datasets WHERE id = ?", (existing[0],))

        await db.execute("""
            INSERT INTO datasets (name, uploaded_by, row_count, columns, uploaded_at)
            VALUES (?, ?, ?, ?, ?)
        """, (filename, str(uploader_id), len(rows), ",".join(columns), datetime.utcnow().isoformat()))

        async with db.execute("SELECT id FROM datasets WHERE name = ?", (filename,)) as cur:
            dataset_id = (await cur.fetchone())[0]

        batch = [(dataset_id, json.dumps(row, ensure_ascii=False)) for row in rows]
        await db.executemany("INSERT INTO records (dataset_id, data) VALUES (?, ?)", batch)
        await db.commit()

    return {"filename": filename, "rows": len(rows), "columns": columns}

# ── Recherche insensible à la casse ───────────────────────
async def search_records(query: str, dataset_name: str = None, limit: int = 20):
    terms = query.strip().split()
    async with aiosqlite.connect(DB_PATH) as db:
        dataset_filter = ""
        if dataset_name:
            async with db.execute("SELECT id FROM datasets WHERE name = ?", (dataset_name,)) as cur:
                row = await cur.fetchone()
            if row:
                dataset_filter = f"AND r.dataset_id = {row[0]}"

        where_clauses = " AND ".join(["LOWER(r.data) LIKE ?" for _ in terms])
        params = [f"%{t.lower()}%" for t in terms] + [limit]

        async with db.execute(f"""
            SELECT d.name, r.data FROM records r
            JOIN datasets d ON r.dataset_id = d.id
            WHERE {where_clauses} {dataset_filter} LIMIT ?
        """, params) as cur:
            rows = await cur.fetchall()

    results = []
    for r in rows:
        dsname, data_str = r
        try:
            record = json.loads(data_str)
            record = {str(k): str(v) for k, v in record.items()}
        except Exception:
            continue
        results.append({"dataset": dsname, "record": record})
    return results

async def list_datasets():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT name, uploaded_by, row_count, columns, uploaded_at
            FROM datasets ORDER BY uploaded_at DESC
        """) as cur:
            return await cur.fetchall()

async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*), SUM(row_count) FROM datasets") as cur:
            return await cur.fetchone()

# ── Labels ────────────────────────────────────────────────
LABEL_MAP = {
    "prenom": "Prénom", "prénom": "Prénom", "firstname": "Prénom", "first_name": "Prénom",
    "nom": "Nom", "name": "Nom", "lastname": "Nom", "last_name": "Nom",
    "civilite": "Civilité", "civilité": "Civilité", "genre": "Genre",
    "ddn": "Date naissance", "date_naissance": "Date naissance",
    "date": "Date", "naissance": "Naissance", "annee": "Année", "age": "Âge",
    "email": "Email", "mail": "Email",
    "telephone": "Téléphone", "téléphone": "Téléphone", "tel": "Téléphone",
    "phone": "Téléphone", "mobile": "Mobile", "portable": "Mobile",
    "adresse": "Adresse", "address": "Adresse", "address1": "Adresse",
    "address2": "Adresse 2", "rue": "Rue", "street": "Rue",
    "ville": "Ville", "city": "Ville",
    "code_postal": "Code postal", "postcode": "Code postal", "cp": "Code postal",
    "pays": "Pays", "country": "Pays", "region": "Région",
}

IDENTITY_KEYS = ["prenom", "prénom", "firstname", "first_name", "nom", "name",
                 "lastname", "last_name", "civilite", "civilité", "genre",
                 "ddn", "date_naissance", "date", "naissance", "annee", "age"]
CONTACT_KEYS  = ["email", "mail", "telephone", "téléphone", "tel", "phone",
                 "mobile", "portable", "fax"]
ADDRESS_KEYS  = ["adresse", "address", "address1", "address2", "rue", "street",
                 "ville", "city", "code_postal", "postcode", "postal", "cp",
                 "pays", "country", "region", "departement"]

def fmt_section(data: dict) -> str:
    lines = []
    for k, v in data.items():
        label = LABEL_MAP.get(k.lower().replace(" ", "_"), k.title())
        lines.append(f"`{label:<16}` **{v}**")
    return "\n\n".join(lines)

# ── Build embed ───────────────────────────────────────────
def build_result_embed(res: dict, page: int, total: int, query: str) -> discord.Embed:
    record = res["record"]
    embed = discord.Embed(color=COLOR_PURPLE)
    embed.set_author(name=f"🔍  RÉSULTAT  {page + 1} / {total}")

    identity = {}
    contact  = {}
    address  = {}
    other    = {}

    for k, v in record.items():
        cleaned = clean_value(v)
        if not cleaned:
            continue
        kl = k.lower().replace(" ", "_")
        if any(f in kl for f in IDENTITY_KEYS):
            identity[k] = cleaned
        elif any(f in kl for f in CONTACT_KEYS):
            contact[k] = cleaned
        elif any(f in kl for f in ADDRESS_KEYS):
            address[k] = cleaned
        else:
            other[k] = cleaned

    if identity:
        embed.add_field(name="👤  IDENTITÉ", value=fmt_section(identity), inline=False)
        embed.add_field(name="\u200b", value="\u200b", inline=False)

    if contact:
        embed.add_field(name="📞  CONTACT", value=fmt_section(contact), inline=False)
        embed.add_field(name="\u200b", value="\u200b", inline=False)

    if address:
        embed.add_field(name="🏠  ADRESSE", value=fmt_section(address), inline=False)
        embed.add_field(name="\u200b", value="\u200b", inline=False)

    if other:
        truncated = dict(list(other.items())[:5])
        embed.add_field(name="📋  AUTRES", value=fmt_section(truncated), inline=False)

    embed.set_footer(
        text=f"📁 {res['dataset']}  •  Requête : {query}  •  {total} résultat(s)"
    )
    return embed

# ── Result View ───────────────────────────────────────────
class ResultView(discord.ui.View):
    def __init__(self, results: list, query: str):
        super().__init__(timeout=180)
        self.results = results
        self.query = query
        self.page = 0

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary, custom_id="prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(
            embed=build_result_embed(self.results[self.page], self.page, len(self.results), self.query),
            view=self
        )

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary, custom_id="next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < len(self.results) - 1:
            self.page += 1
        await interaction.response.edit_message(
            embed=build_result_embed(self.results[self.page], self.page, len(self.results), self.query),
            view=self
        )

    @discord.ui.button(label="🗑️ Fermer", style=discord.ButtonStyle.danger, custom_id="close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.message.delete()

# ── Panel View ────────────────────────────────────────────
class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔍  SEARCH", style=discord.ButtonStyle.danger,
                       custom_id="panel_search")
    async def search_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SearchModal())

    @discord.ui.button(label="📂  BASES", style=discord.ButtonStyle.primary,
                       custom_id="panel_db")
    async def db_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        rows = await list_datasets()
        if not rows:
            await interaction.response.send_message(
                embed=discord.Embed(description="❌ Aucune base chargée.", color=COLOR_ERROR),
                ephemeral=True
            )
            return
        embed = discord.Embed(color=COLOR_INFO)
        embed.set_author(name="📂  BASES DE DONNÉES")
        total_entries = 0
        for row in rows:
            name, uploader, count, cols, uploaded_at = row
            count = count or 0
            total_entries += count
            ts = int(datetime.fromisoformat(uploaded_at).timestamp())
            embed.add_field(
                name=f"📄  {name}",
                value=(
                    f"`Entrées  ` **{count:,}**\n\n"
                    f"`Colonnes ` {cols[:60]}\n\n"
                    f"`Ajouté   ` <t:{ts}:R>"
                ),
                inline=False
            )
        embed.set_footer(text=f"Total : {total_entries:,} entrées  •  {len(rows)} base(s)")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="📊  STATS", style=discord.ButtonStyle.success,
                       custom_id="panel_stats")
    async def stats_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        nb_db, total = await get_stats()
        embed = discord.Embed(color=COLOR_SUCCESS)
        embed.set_author(name="📊  STATISTIQUES")
        embed.add_field(name="`Bases chargées`", value=f"**{nb_db or 0}**",   inline=True)
        embed.add_field(name="`Total entrées`",  value=f"**{total or 0:,}**", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ── Search Modal ──────────────────────────────────────────
class SearchModal(discord.ui.Modal, title="🔍  Recherche"):
    query = discord.ui.TextInput(
        label="Nom / Prénom / Email / Ville...",
        placeholder="Ex: Jean Dupont",
        min_length=2,
        max_length=100
    )
    database = discord.ui.TextInput(
        label="Base spécifique (vide = toutes)",
        placeholder="Ex: fichier.csv",
        required=False,
        max_length=100
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        q = self.query.value.strip()
        db_filter = self.database.value.strip() or None
        results = await search_records(q, db_filter, limit=20)
        if not results:
            await interaction.followup.send(
                embed=discord.Embed(
                    description=f"❌  Aucun résultat pour **{q}**",
                    color=COLOR_ERROR
                ),
                ephemeral=True
            )
            return
        view = ResultView(results, q)
        await interaction.followup.send(
            embed=build_result_embed(results[0], 0, len(results), q),
            view=view,
            ephemeral=True
        )

# ── Events ────────────────────────────────────────────────
@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()
    bot.add_view(PanelView())
    print(f"[+] {bot.user} prêt")

# ── Commandes ─────────────────────────────────────────────
@bot.command(name="panel")
async def panel_cmd(ctx):
    stats = await get_stats()
    nb_db = stats[0] or 0
    total = stats[1] or 0
    embed = discord.Embed(color=COLOR_PURPLE)
    embed.set_author(name="⚡  SEARCH PANEL")
    embed.description = (
        f"**🔍 SEARCH** — Recherche par nom, prénom, email, ville...\n\n"
        f"**📂 BASES** — Voir les bases disponibles\n\n"
        f"**📊 STATS** — Statistiques globales\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"`Bases actives` **{nb_db}**\n\n"
        f"`Total entrées` **{total:,}**"
    )
    embed.set_footer(text="Résultats instantanés  •  Données privées")
    await ctx.send(embed=embed, view=PanelView())

@bot.command(name="upload")
async def upload_cmd(ctx):
    if not ctx.message.attachments:
        await ctx.send("❌ Attache un fichier CSV/JSON/XLSX/TXT.")
        return
    for attachment in ctx.message.attachments:
        ext = os.path.splitext(attachment.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            await ctx.send(f"❌ Format non supporté : `{ext}`")
            continue
        msg = await ctx.send(f"⏳ Ingestion de `{attachment.filename}`...")
        try:
            file_bytes = await attachment.read()
            info = await ingest_file(file_bytes, attachment.filename, ctx.author.id)
            embed = discord.Embed(color=COLOR_SUCCESS)
            embed.set_author(name="✅  Base chargée")
            embed.add_field(name="`Fichier`",  value=info['filename'],           inline=False)
            embed.add_field(name="`Entrées`",  value=f"**{info['rows']:,}**",    inline=True)
            embed.add_field(name="`Colonnes`", value=', '.join(info['columns']), inline=False)
            await msg.edit(content=None, embed=embed)
        except Exception as e:
            await msg.edit(content=f"❌ Erreur : {e}")

@bot.command(name="search")
async def search_cmd(ctx, *, query: str):
    db_filter = None
    if "--db" in query:
        parts = query.split("--db")
        query = parts[0].strip()
        db_filter = parts[1].strip()
    results = await search_records(query, db_filter, limit=20)
    if not results:
        await ctx.send(embed=discord.Embed(
            description=f"❌ Aucun résultat pour **{query}**",
            color=COLOR_ERROR
        ))
        return
    view = ResultView(results, query)
    await ctx.send(
        embed=build_result_embed(results[0], 0, len(results), query),
        view=view
    )

@bot.command(name="deldb")
async def deldb_cmd(ctx, *, filename: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM datasets WHERE name = ?", (filename,)) as cur:
            row = await cur.fetchone()
        if not row:
            await ctx.send(f"❌ Base `{filename}` introuvable.")
            return
        await db.execute("DELETE FROM records WHERE dataset_id = ?", (row[0],))
        await db.execute("DELETE FROM datasets WHERE id = ?", (row[0],))
        await db.commit()
    await ctx.send(f"🗑️ Base `{filename}` supprimée.")

bot.run("MTU1Nzc5NzcyODI5MjA0NDkyMA.G_O5w9.sOndjNvAfaip6M0Ylfy_ZjoJb58NCQ76t08iuk")
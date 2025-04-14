import os
import sys
import json
import time
import threading
import webbrowser
import unicodedata
import tkinter as tk
import firebase_admin
from tkinter import ttk
from pathlib import Path
from PIL import Image, ImageTk 
from datetime import datetime, timedelta
from tkinter import messagebox, scrolledtext
from firebase_admin import credentials, firestore

construcoes_cache = {}
ultimo_cargo = []
ultimo_cargo_timestamp = 0
ultima_entrega_realizada = False
ultima_morte_processada = None
ultima_undocked_processada = None
ultima_posicao_log = 0
ultima_sessao_jogo = None


#  pyinstaller --onefile --windowed --add-data "serviceAccountKey.json;." EDTMS.py

# pyinstaller --onefile --windowed --add-data "serviceAccountKey.json;." --add-data "edtms_logo.png;." --icon=EDTMS.ico EDTMS.py

# Caminho padrão do log
LOG_DIR = Path(os.environ["USERPROFILE"]) / "Saved Games" / "Frontier Developments" / "Elite Dangerous"

# Firebase com compatibilidade para PyInstaller (empacotado)
def get_embedded_service_account():
    if hasattr(sys, "_MEIPASS"):  # PyInstaller executável
        path = os.path.join(sys._MEIPASS, "serviceAccountKey.json")
    else:  # Rodando direto no Python
        path = "serviceAccountKey.json"
    return credentials.Certificate(path)

# Inicializa Firebase
cred = get_embedded_service_account()
firebase_admin.initialize_app(cred)
db = firestore.client()

def obter_log_mais_recente():
    arquivos = list(LOG_DIR.glob("Journal*.log"))
    arquivos.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return arquivos[0] if arquivos else None

def limpar_nome_estacao(nome_completo):
    prefixos = [
        "Orbital Construction Site: ",
        "Planetary Construction Site: ",  # <- Adicionado aqui
        "Construction Site: ",
        "Station: ",
        "Outpost: "
    ]
    for prefixo in prefixos:
        if nome_completo.startswith(prefixo):
            return nome_completo.replace(prefixo, "").strip()
    return nome_completo.strip()



def gerar_id(nome):
    nome_normalized = unicodedata.normalize('NFKD', nome)
    nome_sem_acentos = ''.join([c for c in nome_normalized if not unicodedata.combining(c)])
    nome_limpo = nome_sem_acentos.lower().strip()
    nome_limpo = nome_limpo.replace(' ', '_').replace('-', '_')
    nome_limpo = ''.join([c if c.isalnum() or c == '_' else '' for c in nome_limpo])
    nome_limpo = '_'.join(filter(None, nome_limpo.split('_')))
    return nome_limpo

def processar_log(caminho_log):
    eventos = []
    with open(caminho_log, encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if not linha:
                continue
            try:
                eventos.append(json.loads(linha))
            except json.JSONDecodeError:
                continue

    eventos_docked = [e for e in eventos if e.get("event") == "Docked"]
    if not eventos_docked:
        return "", "", []

    ultimo_docked = sorted(eventos_docked, key=lambda e: e["timestamp"], reverse=True)[0]
    nome_estacao = ultimo_docked.get("StationName", "")
    tipo_estacao = ultimo_docked.get("StationType", "")
    market_id = ultimo_docked.get("MarketID", None)

    eventos_construction = [
        e for e in eventos
        if e.get("event") == "ColonisationConstructionDepot" and e.get("MarketID") == market_id
    ]
    if not eventos_construction:
        return limpar_nome_estacao(nome_estacao), tipo_estacao, []

    evento_construction = sorted(eventos_construction, key=lambda e: e["timestamp"], reverse=True)[0]
    recursos = evento_construction.get("ResourcesRequired", [])

    materiais = []
    for recurso in recursos:
        nome = recurso.get("Name_Localised") or recurso.get("Name")
        total = recurso.get("TotalRequired")
        if total is None:
            total = recurso.get("RequiredAmount", 0)
        restante = recurso.get("RequiredAmount", 0) - recurso.get("ProvidedAmount", 0)

        materiais.append({
            "id": gerar_id(nome),
            "material": nome,
            "quantidade": total,
            "restante": restante
        })

    return limpar_nome_estacao(nome_estacao), tipo_estacao, materiais

def atualizar_firestore(nome_estacao, materiais_script):
    colecao = db.collection("inventories")
    docs = colecao.where("name", "==", nome_estacao).get()

    if not docs:
        log_text.insert(tk.END, f"Estação '{nome_estacao}' não encontrada no Firestore.\n")
        return

    doc_ref = docs[0].reference
    doc_data = docs[0].to_dict()
    itens_atual = doc_data.get("items", [])

    # Cria dicionário de itens do Firestore com IDs gerados a partir do nome do material
    itens_por_id = {}
    for item in itens_atual:
        material_name = item.get("material", "")
        generated_id = gerar_id(material_name)
        itens_por_id[generated_id] = {
            "id": generated_id,
            "material": material_name,
            "quantidade": item.get("quantidade", 0),
            "restante": item.get("restante", 0),
        }

    novos_itens = []

    for material in materiais_script:
        nome = material["material"]
        novo_id = gerar_id(nome)
        quantidade_script = material["quantidade"]
        restante_script = material["restante"]

        if novo_id in itens_por_id:
            existente = itens_por_id[novo_id]
            quantidade_existente = existente["quantidade"]
            restante_existente = existente["restante"]

            # Atualiza somente se a quantidade do script for maior
            if quantidade_script > quantidade_existente:
                itens_por_id[novo_id] = {
                    "id": novo_id,
                    "material": nome,
                    "quantidade": quantidade_script,
                    "restante": restante_script,
                }
            # Mantém o restante atual se a quantidade for igual ou menor
            elif quantidade_script == quantidade_existente and restante_script < restante_existente:
                itens_por_id[novo_id]["restante"] = restante_script
        else:
            itens_por_id[novo_id] = {
                "id": novo_id,
                "material": nome,
                "quantidade": quantidade_script,
                "restante": restante_script,
            }

    novos_itens = list(itens_por_id.values())

    # Atualiza Firestore
    doc_ref.update({
        "items": novos_itens,
        "updatedAt": firestore.SERVER_TIMESTAMP
    })

    log_text.insert(tk.END, f"Estação '{nome_estacao}' sincronizada com {len(novos_itens)} materiais.\n")
    log_text.see(tk.END)


def loop_verificacao():
    threading.Thread(target=processar_carga, daemon=True).start()

    ultimo_dock_log = "dock_status.txt"
    ultima_estacao = ""
    ultimo_estado_materiais = {}

    while True:
        log_path = obter_log_mais_recente()
        if not log_path:
            time.sleep(5)
            continue

        nome_estacao, tipo_estacao, materiais = processar_log(log_path)

        if nome_estacao:
            if nome_estacao != ultima_estacao:
                ultima_estacao = nome_estacao
                ultimo_estado_materiais = {}  # Zera o estado quando troca de estação

                with open(ultimo_dock_log, "w", encoding="utf-8") as f:
                    f.write(f"Docked em {nome_estacao} ({tipo_estacao})\n")

                log_text.delete("1.0", tk.END)
                log_text.insert(tk.END, f"[INFO] Atracado em: {nome_estacao} ({tipo_estacao})\n")
                log_text.see(tk.END)

            if materiais:
                # Verifica se houve mudança nos materiais antes de atualizar
                materiais_dict = {mat["id"]: mat["restante"] for mat in materiais}
                if materiais_dict != ultimo_estado_materiais:
                    atualizar_firestore(nome_estacao, materiais)
                    ultimo_estado_materiais = materiais_dict

        time.sleep(5)

def normalizar_nome(nome):
    nome = nome.lower()
    substituicoes = {
        'í': 'i', 'ó': 'o', 'ã': 'a', 'á': 'a', 
        'é': 'e', 'ê': 'e', 'ç': 'c', 'ú': 'u',
        'construction': '', 'materials': '', ' ': '_',
        '-': '', "'": "", ":": "", "(": "", ")": ""
    }
    for k, v in substituicoes.items():
        nome = nome.replace(k, v)
    return nome.rstrip('s').strip('_')

from datetime import datetime, timedelta

ultima_morte_detectada = None
ultima_resurreicao_processada = None

def processar_carga():
    global ultimo_cargo, ultimo_cargo_timestamp, ultima_entrega_realizada
    global ultima_morte_detectada, ultima_resurreicao_processada

    cargo_path = LOG_DIR / "Cargo.json"
    log_path = obter_log_mais_recente()

    while True:
        try:
            # Verifica morte e ressurreição
            if log_path and log_path.exists():
                with open(log_path, encoding="utf-8") as f:
                    linhas = f.readlines()
                    eventos = [json.loads(linha) for linha in linhas if '"event":"Died"' in linha or '"event":"Resurrect"' in linha]

                for evento in eventos:
                    ts = datetime.strptime(evento["timestamp"], "%Y-%m-%dT%H:%M:%SZ")

                    if evento.get("event") == "Died":
                        if not ultima_morte_detectada or ts > ultima_morte_detectada:
                            ultima_morte_detectada = ts

                    elif evento.get("event") == "Resurrect":
                        if (
                            ultima_morte_detectada
                            and (not ultima_resurreicao_processada or ts > ultima_resurreicao_processada)
                            and ts - ultima_morte_detectada <= timedelta(seconds=60)
                        ):
                            log_text.insert(tk.END, f"[💀] Morte confirmada por Resurrect. Revertendo entrega...\n")
                            ultima_resurreicao_processada = ts

                            if ultima_entrega_realizada and ultimo_cargo:
                                construcao_nome = construcoes_var.get()
                                if construcao_nome and construcao_nome in construcoes_cache:
                                    construcao = construcoes_cache[construcao_nome]
                                    doc_ref = db.collection("inventories").document(construcao["doc_id"])
                                    doc = doc_ref.get()
                                    if doc.exists:
                                        materiais = doc.to_dict().get("items", [])
                                        revertidos = 0

                                        for item in ultimo_cargo:
                                            nome_raw = item.get("Name_Localised") or item.get("Name")
                                            nome_id = gerar_id(nome_raw)
                                            quantidade = item.get("Count", 0)

                                            for mat in materiais:
                                                if gerar_id(mat["material"]) == nome_id:
                                                    mat["restante"] += quantidade
                                                    log_text.insert(tk.END, 
                                                        f"↩ {mat['material']}: Revertido +{quantidade} após morte\n"
                                                    )
                                                    revertidos += 1

                                        if revertidos > 0:
                                            doc_ref.update({"items": materiais})
                                            log_text.insert(tk.END, f"♻️ {revertidos} materiais devolvidos na construção {construcao_nome}\n")
                                        else:
                                            log_text.insert(tk.END, "⚠️ Nenhum material correspondente para reverter\n")
                                else:
                                    log_text.insert(tk.END, "⚠️ Nenhuma construção válida selecionada para reverter\n")

                            ultimo_cargo = []
                            ultima_entrega_realizada = False
                            time.sleep(5)
                            break

            # Verifica se arquivo de carga existe
            if not cargo_path.exists():
                time.sleep(5)
                continue

            stat = os.stat(cargo_path)
            if stat.st_mtime <= ultimo_cargo_timestamp:
                time.sleep(2)
                continue

            with open(cargo_path, encoding="utf-8") as f:
                cargo_data = json.load(f)

            novo_cargo = cargo_data.get("Inventory", [])
            ultimo_cargo_timestamp = stat.st_mtime

            nomes_carga = [i['Name'] for i in novo_cargo]
            log_text.insert(tk.END, f"\n[DEBUG] Carga detectada: {nomes_carga}\n")

            if not novo_cargo:
                ultima_entrega_realizada = False
                ultimo_cargo = []
                time.sleep(2)
                continue

            if ultima_entrega_realizada:
                time.sleep(2)
                continue

            # Verifica alterações na carga
            delta = {}
            for item in novo_cargo:
                nome_bruto = item.get("Name_Localised") or item.get("Name")
                nome_normalizado = normalizar_nome(nome_bruto)
                qtd = item["Count"]

                item_anterior = next((i for i in ultimo_cargo if normalizar_nome(i.get("Name")) == nome_normalizado), None)
                if not item_anterior or qtd > item_anterior["Count"]:
                    delta[nome_normalizado] = qtd - (item_anterior["Count"] if item_anterior else 0)
                    log_text.insert(tk.END, f"[DEBUG] Delta {nome_normalizado}: +{delta[nome_normalizado]}\n")

            if delta:
                construcao_nome = construcoes_var.get()
                if not construcao_nome:
                    log_text.insert(tk.END, "⚠️ Nenhuma construção selecionada!\n")
                    continue

                construcao = construcoes_cache.get(construcao_nome)
                if not construcao:
                    log_text.insert(tk.END, f"⚠️ Construção '{construcao_nome}' não encontrada no cache\n")
                    continue

                doc_ref = db.collection("inventories").document(construcao["doc_id"])
                doc = doc_ref.get()
                if not doc.exists:
                    log_text.insert(tk.END, f"⚠️ Documento {construcao['doc_id']} não existe\n")
                    continue

                materiais = doc.to_dict().get("items", [])
                atualizados = 0

                for mat in materiais:
                    mat_nome_normalizado = normalizar_nome(mat["material"])
                    if mat_nome_normalizado in delta:
                        novo_restante = max(0, mat["restante"] - delta[mat_nome_normalizado])
                        log_text.insert(tk.END,
                            f"✎ {mat['material']}: {mat['restante']} → {novo_restante} "
                            f"(Deduzido: {delta[mat_nome_normalizado]})\n"
                        )
                        mat["restante"] = novo_restante
                        atualizados += 1

                if atualizados > 0:
                    doc_ref.update({"items": materiais})
                    log_text.insert(tk.END, f"✅ {atualizados} materiais atualizados na construção {construcao_nome}\n")
                    ultima_entrega_realizada = True

            # Atualiza último cargo, mas só processa na saída
            ultimo_cargo = novo_cargo

            # Verifica evento Undocked para iniciar entrega
            with open(log_path, encoding="utf-8") as f:
                eventos_log = [json.loads(l) for l in f if '"event":"Undocked"' in l]

            if eventos_log:
                undocked_event = sorted(eventos_log, key=lambda e: e["timestamp"], reverse=True)[0]
                ts_undocked = datetime.strptime(undocked_event["timestamp"], "%Y-%m-%dT%H:%M:%SZ")
                if datetime.utcnow() - ts_undocked <= timedelta(seconds=30):  # Considera recente

                    if ultimo_cargo and not ultima_entrega_realizada:
                        log_text.insert(tk.END, "[🚀] Saída detectada. Iniciando entrega...\n")

                        construcao_nome = construcoes_var.get()
                        if not construcao_nome:
                            log_text.insert(tk.END, "⚠️ Nenhuma construção selecionada!\n")
                        else:
                            construcao = construcoes_cache.get(construcao_nome)
                            if not construcao:
                                log_text.insert(tk.END, f"⚠️ Construção '{construcao_nome}' não encontrada no cache\n")
                            else:
                                doc_ref = db.collection("inventories").document(construcao["doc_id"])
                                doc = doc_ref.get()
                                if doc.exists:
                                    materiais = doc.to_dict().get("items", [])
                                    atualizados = 0

                                    for item in ultimo_cargo:
                                        nome_bruto = item.get("Name_Localised") or item.get("Name")
                                        nome_id = gerar_id(nome_bruto)
                                        qtd = item.get("Count", 0)

                                        for mat in materiais:
                                            if gerar_id(mat["material"]) == nome_id:
                                                novo_restante = max(0, mat["restante"] - qtd)
                                                log_text.insert(tk.END,
                                                    f"✎ {mat['material']}: {mat['restante']} → {novo_restante} (Deduzido: {qtd})\n"
                                                )
                                                mat["restante"] = novo_restante
                                                atualizados += 1

                                    if atualizados > 0:
                                        doc_ref.update({"items": materiais})
                                        log_text.insert(tk.END, f"✅ {atualizados} materiais atualizados na construção {construcao_nome}\n")
                                        ultima_entrega_realizada = True
                                else:
                                    log_text.insert(tk.END, f"⚠️ Documento {construcao['doc_id']} não existe\n")


        except Exception as e:
            log_text.insert(tk.END, f"🔥 Erro crítico: {str(e)}\n")
            import traceback
            traceback.print_exc()

        time.sleep(5)

def carregar_construcoes():
    uid = uid_entry.get().strip()
    if not uid:
        messagebox.showerror("Erro", "Digite seu UID primeiro!")
        return
    
    try:
        construcoes_ref = db.collection("inventories")
        query = construcoes_ref.where("collaborators", "array_contains", uid).get()
        
        construcoes = []
        for doc in query:
            data = doc.to_dict()
            construcoes.append({
                "doc_id": doc.id,  # ← Armazena o ID do documento
                "nome": data.get("name", "Sem nome"),
                "dados": data
            })
        
        construcoes_dropdown["values"] = [c["nome"] for c in construcoes]
        construcoes_dropdown.set("")
        
        global construcoes_cache
        construcoes_cache = {c["nome"]: c for c in construcoes}
        
        log_text.insert(tk.END, f"Carregadas {len(construcoes)} construções\n")
        
    except Exception as e:
        log_text.insert(tk.END, f"Erro ao carregar: {str(e)}\n")

def obter_materiais_da_construcao(nome_construcao):
    if not nome_construcao or not construcoes_cache.get(nome_construcao):
        return None
    return construcoes_cache[nome_construcao]["dados"]["items"]

def iniciar_loop():
    threading.Thread(target=loop_verificacao, daemon=True).start()

# Interface Gráfica Tkinter - Design Atualizado
janela = tk.Tk()
janela.title("")
janela.geometry("1000x500")  # Altura um pouco maior
janela.configure(bg="#1a1a1a")
janela.overrideredirect(True)

# Cores e Fontes
COR_DE_FUNDO = "#1a1a1a"
COR_TEXTO = "#cccccc"
COR_DESTAQUE = "#ff9900"
FONTE_TITULO = ("Arial", 12, "bold")
FONTE_TEXTO = ("Arial", 9)

# Frame Principal
frame_principal = tk.Frame(janela, bg=COR_DE_FUNDO)
frame_principal.pack(fill="both", expand=True, padx=20, pady=15)
auth_frame = tk.Frame(frame_principal, bg=COR_DE_FUNDO)
auth_frame.pack(fill="x", pady=(0, 10))

# Campo UID
tk.Label(auth_frame, text="Seu UID:", bg=COR_DE_FUNDO, fg=COR_TEXTO).pack(side="left", padx=(0, 5))
uid_entry = tk.Entry(auth_frame, bg="#2a2a2a", fg=COR_TEXTO, width=30)
uid_entry.pack(side="left", fill="x", expand=True)

# Botão de carregar construções
load_btn = tk.Button(auth_frame, text="Carregar Construções", command=lambda: carregar_construcoes(),
                    bg=COR_DESTAQUE, fg="#1a1a1a", relief="flat")
load_btn.pack(side="left", padx=(10, 0))

# Dropdown de construções
tk.Label(auth_frame, text="Construção:", bg=COR_DE_FUNDO, fg=COR_TEXTO).pack(side="left", padx=(10, 5))
construcoes_var = tk.StringVar()
construcoes_dropdown = ttk.Combobox(auth_frame, textvariable=construcoes_var, state="readonly", background="#2a2a2a", foreground=COR_TEXTO)
construcoes_dropdown.pack(side="left", fill="x", expand=True)

# --- Área do Cabeçalho (Arrastável) ---
header_frame = tk.Frame(frame_principal, bg=COR_DE_FUNDO)
header_frame.pack(side="top", fill="x", pady=(0, 15))

# Título Centralizado
cabecalho = tk.Label(
    header_frame,
    text="ELITE DANGEROUS CONSTRUCTION SYNC",
    font=FONTE_TITULO,
    fg=COR_DESTAQUE,
    bg=COR_DE_FUNDO
)
cabecalho.pack(side="top", pady=(0, 5))

def get_resource_path(relative_path):
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

def abrir_site(event):
    webbrowser.open("https://edtms.squareweb.app")
    status_bar.config(text="Redirecionando para o site EDTMS...")

try:
    logo_path = get_resource_path("edtms_logo.png")
    logo_img = Image.open(logo_path)
    logo_img = logo_img.resize((200, 60), Image.LANCZOS)
    logo_tk = ImageTk.PhotoImage(logo_img)
    
    # Cria um frame para o efeito de hover
    logo_frame = tk.Frame(
        header_frame,
        bg=COR_DE_FUNDO,
        highlightbackground=COR_DE_FUNDO,
        highlightthickness=2
    )
    logo_frame.pack(pady=5)
    
    logo_label = tk.Label(
        logo_frame,
        image=logo_tk,
        bg=COR_DE_FUNDO,
        cursor="hand2"
    )
    logo_label.image = logo_tk
    logo_label.pack()
    
    # Efeitos Interativos
    def hover_enter(e):
        logo_frame.config(highlightbackground=COR_DESTAQUE)
        logo_label.config(bg="#252525")
        
    def hover_leave(e):
        logo_frame.config(highlightbackground=COR_DE_FUNDO)
        logo_label.config(bg=COR_DE_FUNDO)
    
    # Bind dos eventos
    logo_label.bind("<Enter>", hover_enter)
    logo_label.bind("<Leave>", hover_leave)
    logo_label.bind("<Button-1>", abrir_site)
    
except Exception as e:
    print(f"Erro ao carregar logo: {str(e)}")

# --- Botão Fechar ---
btn_fechar = tk.Label(
    janela,
    text="✕",
    font=("Arial", 14),
    fg=COR_TEXTO,
    bg=COR_DE_FUNDO,
    cursor="hand2"
)
btn_fechar.place(x=465, y=5)  # Posição ajustada
btn_fechar.bind("<Button-1>", lambda e: janela.destroy())

# --- Área de Log Estilizada ---
log_frame = tk.Frame(frame_principal, bg=COR_DE_FUNDO)
log_frame.pack(fill="both", expand=True)

log_text = scrolledtext.ScrolledText(
    log_frame,
    wrap=tk.WORD,
    width=60,
    height=12,
    bg="#2a2a2a",
    fg=COR_TEXTO,
    insertbackground=COR_DESTAQUE,
    selectbackground=COR_DESTAQUE,
    font=FONTE_TEXTO,
    relief="flat",
    highlightthickness=0
)
log_text.pack(fill="both", expand=True)

# Personalização da Scrollbar
log_text.vbar.config(
    troughcolor="#2a2a2a",
    bg="#404040",
    activebackground=COR_DESTAQUE
)

# --- Barra de Status Premium ---
status_bar = tk.Label(
    janela,
    text="🟢 PRONTO PARA SINCRONIZAR | EDTMS v1.0",
    bg=COR_DESTAQUE,
    fg="#1a1a1a",
    font=("Consolas", 9, "bold"),
    height=2,
    anchor="center",
    padx=10
)
status_bar.pack(side="bottom", fill="x")

# --- Sistema de Arraste ---
def start_drag(event):
    janela._offset_x = event.x
    janela._offset_y = event.y

def do_drag(event):
    x = janela.winfo_pointerx() - janela._offset_x
    y = janela.winfo_pointery() - janela._offset_y
    janela.geometry(f"+{x}+{y}")

# Permite arrastar por qualquer parte do cabeçalho
header_frame.bind("<Button-1>", start_drag)
header_frame.bind("<B1-Motion>", do_drag)

# Mensagem Inicial
log_text.tag_config("success", foreground="#00ff00")
log_text.insert(tk.END, "[🛰️ SISTEMA] ", "success")
log_text.insert(tk.END, "Conectado aos servidores da Federação!\n")

iniciar_loop()
janela.mainloop()
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
leitura_carga_permitida = False
ultimos_abandonos = set()  # ← Agora fora da função, persistente
verificacao_em_andamento = False
verificacao_thread = None
ultimo_undocked_ts = None

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

def verificar_abandono_ou_morte(materiais):
    global ultima_morte_detectada, ultimos_abandonos, ultimo_undocked_ts

    log_path = obter_log_mais_recente()
    if not log_path or not log_path.exists():
        return

    morte_processada = False

    while True:
        try:
            with open(log_path, encoding="utf-8") as f:
                linhas = f.readlines()
            eventos = [json.loads(l) for l in linhas if l.strip().startswith('{')]

            for evento in eventos:
                tipo = evento.get("event")
                ts = datetime.strptime(evento["timestamp"], "%Y-%m-%dT%H:%M:%SZ")

                # Evita eventos anteriores ao último undocked
                if ultimo_undocked_ts and ts <= ultimo_undocked_ts:
                    continue

                # Ejeção de carga não abandonada
                if tipo == "EjectCargo":
                    nome = evento.get("Type_Localised") or evento.get("Type")
                    qtd = evento.get("Count", 0)
                    mat_id = gerar_id(nome)
                    evento_id = f"{mat_id}|{qtd}|{ts.isoformat()}"

                    if evento_id not in ultimos_abandonos:
                        ultimos_abandonos.add(evento_id)
                        abandono_para_firebase(nome, qtd)

                # Morte
                elif tipo == "Died" and not morte_processada:
                    morte_processada = True
                    for mat in materiais:
                        abandono_para_firebase(mat["material"], mat["quantidade"])
                    log_text.insert(tk.END, f"♻️ {len(materiais)} materiais devolvidos após morte/abandono\n")

                # Parar monitoramento se atracar de novo
                elif tipo == "Docked":
                    return

            # Limpa eventos muito antigos do set para evitar crescimento
            ultimos_abandonos = {
                eid for eid in ultimos_abandonos
                if datetime.fromisoformat(eid.rsplit("|", 1)[-1]) > datetime.utcnow() - timedelta(minutes=5)
            }

        except Exception as e:
            log_text.insert(tk.END, f"[ERRO VERIFICAÇÃO] {str(e)}\n")
            import traceback
            traceback.print_exc()

        time.sleep(1)

def processar_carga():
    global ultimo_cargo, ultimo_cargo_timestamp, ultima_entrega_realizada, verificacao_thread
    global ultima_morte_detectada, ultima_resurreicao_processada, verificacao_thread, ultimo_undocked_ts

    log_path = obter_log_mais_recente()
    if not log_path or not log_path.exists():
        log_text.insert(tk.END, "[ERRO] Log não encontrado.\n")
        return

    log_text.insert(tk.END, "[🛰️] Monitorando log para eventos de compra e entrega...\n")

    eventos_processados = set()
    materiais_entregues = []

    while True:
        try:
            with open(log_path, encoding="utf-8") as f:
                linhas = f.readlines()

            eventos = [json.loads(l) for l in linhas if l.strip().startswith('{')]
            eventos = sorted(eventos, key=lambda e: e["timestamp"])  # Ordenar por tempo

            # ⚠️ Ignora eventos mais antigos que 60s para evitar leitura de logs antigos
            agora = datetime.utcnow()
            eventos = [e for e in eventos if (agora - datetime.strptime(e["timestamp"], "%Y-%m-%dT%H:%M:%SZ")).total_seconds() <= 60]


            for evento in eventos:
                tipo = evento.get("event")
                timestamp = evento.get("timestamp")
                evento_id = f"{tipo}-{timestamp}"

                # Ignora eventos já processados
                if evento_id in eventos_processados:
                    continue

                # 1. Evento de DOCKED → ponto de compra
                if tipo == "Docked":
                    ultima_entrega_realizada = False
                    log_text.insert(tk.END, "[INFO] Atracado. Aguardando compras...\n")

                # 2. MARKETBUY (Compras na estação)
                elif tipo == "MarketBuy":
                    if ultima_entrega_realizada:
                        continue
                    nome = evento.get("Type_Localised") or evento.get("Type")
                    qtd = evento.get("Count", 0)
                    if nome and qtd:
                        materiais_entregues.append({
                            "id": gerar_id(nome),
                            "material": nome,
                            "quantidade": qtd
                        })
                        log_text.insert(tk.END, f"[💰] Compra detectada: {nome} x{qtd}\n")

                # 3. UNDОCKED → iniciar verificação de morte/abandono
                elif tipo == "Undocked":
                    ts_undocked = datetime.strptime(evento["timestamp"], "%Y-%m-%dT%H:%M:%SZ")
                    if not ultimo_undocked_ts or ts_undocked > ultimo_undocked_ts:
                        ultimo_undocked_ts = ts_undocked
                        log_text.insert(tk.END, "[🚀] Undocked detectado. Iniciando verificação de carga...\n")
                        
                        def iniciar_verificacao():
                            global verificacao_em_andamento
                            verificacao_em_andamento = True
                            verificar_abandono_ou_morte(materiais_entregues.copy())
                            verificacao_em_andamento = False

                        # Interrompe o loop anterior se ainda estiver rodando (não temos cancelamento, então só permite um)
                        if not verificacao_em_andamento:
                            global verificacao_thread
                            verificacao_thread = threading.Thread(target=iniciar_verificacao, daemon=True)
                            verificacao_thread.start()
                            ultima_entrega_realizada = True
                            materiais_entregues.clear()


                eventos_processados.add(evento_id)

        except Exception as e:
            log_text.insert(tk.END, f"[ERRO] {str(e)}\n")
            import traceback
            traceback.print_exc()

        time.sleep(1)

def abandono_para_firebase(nome, qtd):
    construcao_nome = construcoes_var.get()
    if not construcao_nome or construcao_nome not in construcoes_cache:
        log_text.insert(tk.END, "⚠️ Nenhuma construção válida para reverter.\n")
        return

    doc_ref = db.collection("inventories").document(construcoes_cache[construcao_nome]["doc_id"])
    doc = doc_ref.get()
    if not doc.exists:
        return

    materiais = doc.to_dict().get("items", [])
    for mat in materiais:
        if gerar_id(mat["material"]) == gerar_id(nome):
            mat["restante"] += qtd
            log_text.insert(tk.END, f"↩ {mat['material']}: +{qtd} (Abandono)\n")
            break

    doc_ref.update({"items": materiais})


def loop_verificacao():
    ultimo_dock_log = "dock_status.txt"
    ultima_estacao = ""
    ultimo_estado_materiais = {}

    while True:
        log_path = obter_log_mais_recente()
        if not log_path:
            time.sleep(5)
            continue

        threading.Thread(target=processar_carga, daemon=True).start()
        nome_estacao, tipo_estacao, materiais = processar_log(log_path)

        if nome_estacao:
            if nome_estacao != ultima_estacao:
                ultima_estacao = nome_estacao
                ultimo_estado_materiais = {}  # Zera o estado quando troca de estação
                leitura_carga_permitida = False

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
janela.geometry("500x400")
janela.configure(bg="#1a1a1a")
janela.overrideredirect(True)

# Cores e Fontes
COR_DE_FUNDO = "#1a1a1a"
COR_TEXTO = "#cccccc"
COR_DESTAQUE = "#ff9900"
FONTE_TITULO = ("Arial", 12, "bold")
FONTE_TEXTO = ("Arial", 9)

# Estilo ttk para bordas arredondadas nos botões
style = ttk.Style()
style.theme_use("default")
style.configure("Rounded.TButton", padding=6, relief="flat", background=COR_DESTAQUE, foreground="#1a1a1a", font=("Arial", 9, "bold"))
style.map("Rounded.TButton",
    background=[("active", "#ffaa33")],
    foreground=[("disabled", "#888888")]
)

# Frame Principal
frame_principal = tk.Frame(janela, bg=COR_DE_FUNDO)
frame_principal.pack(fill="both", expand=True, padx=10, pady=10)

# Top Frame (UID, Construção e Botões)
top_frame = tk.Frame(frame_principal, bg=COR_DE_FUNDO)
top_frame.pack(fill="x", pady=(0, 5))

tk.Label(top_frame, text="UID:", bg=COR_DE_FUNDO, fg=COR_TEXTO).grid(row=0, column=0, sticky="w", padx=5)
uid_entry = tk.Entry(top_frame, bg="#2a2a2a", fg=COR_TEXTO, width=25)
uid_entry.grid(row=0, column=1, sticky="ew", padx=5)

load_btn = ttk.Button(top_frame, text="Carregar", style="Rounded.TButton", command=carregar_construcoes)
load_btn.grid(row=0, column=2, padx=(5, 0))

# Botão Fechar ao lado do "Carregar"
btn_fechar = tk.Label(
    top_frame,
    text="✕",
    font=("Arial", 12),
    fg=COR_TEXTO,
    bg=COR_DE_FUNDO,
    cursor="hand2"
)
btn_fechar.grid(row=0, column=3, padx=(10, 5))
btn_fechar.bind("<Button-1>", lambda e: janela.destroy())

# Dropdown de Construções
tk.Label(top_frame, text="Construção:", bg=COR_DE_FUNDO, fg=COR_TEXTO).grid(row=1, column=0, sticky="w", padx=5, pady=(5, 0))
construcoes_var = tk.StringVar()
construcoes_dropdown = ttk.Combobox(top_frame, textvariable=construcoes_var, state="readonly", width=32)
construcoes_dropdown.grid(row=1, column=1, columnspan=3, sticky="ew", padx=5, pady=(5, 0))

top_frame.grid_columnconfigure(1, weight=1)

# Cabeçalho
header_frame = tk.Frame(frame_principal, bg=COR_DE_FUNDO)
header_frame.pack(side="top", fill="x", pady=(0, 10))

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

    logo_frame = tk.Frame(header_frame, bg=COR_DE_FUNDO, highlightbackground=COR_DE_FUNDO, highlightthickness=2)
    logo_frame.pack(pady=5)

    logo_label = tk.Label(logo_frame, image=logo_tk, bg=COR_DE_FUNDO, cursor="hand2")
    logo_label.image = logo_tk
    logo_label.pack()

    def hover_enter(e):
        logo_frame.config(highlightbackground=COR_DESTAQUE)
        logo_label.config(bg="#252525")

    def hover_leave(e):
        logo_frame.config(highlightbackground=COR_DE_FUNDO)
        logo_label.config(bg=COR_DE_FUNDO)

    logo_label.bind("<Enter>", hover_enter)
    logo_label.bind("<Leave>", hover_leave)
    logo_label.bind("<Button-1>", abrir_site)

except Exception as e:
    print(f"Erro ao carregar logo: {str(e)}")

# Área de Log
log_frame = tk.Frame(frame_principal, bg=COR_DE_FUNDO)
log_frame.pack(fill="both", expand=True)

log_text = scrolledtext.ScrolledText(
    log_frame,
    wrap=tk.WORD,
    width=60,
    height=10,
    bg="#2a2a2a",
    fg=COR_TEXTO,
    insertbackground=COR_DESTAQUE,
    selectbackground=COR_DESTAQUE,
    font=FONTE_TEXTO,
    relief="flat",
    highlightthickness=0
)
log_text.pack(fill="both", expand=True)

log_text.vbar.config(
    troughcolor="#2a2a2a",
    bg="#404040",
    activebackground=COR_DESTAQUE
)

# Barra de Status
status_bar = tk.Label(
    janela,
    text="🟢 PRONTO PARA SINCRONIZAR | EDTMS v1.0",
    bg=COR_DESTAQUE,
    fg="#1a1a1a",
    font=("Consolas", 10, "bold"),
    height=2,
    anchor="center",
    padx=10
)
status_bar.pack(side="bottom", fill="x")

# Sistema de Arraste
def start_drag(event):
    janela._offset_x = event.x
    janela._offset_y = event.y

def do_drag(event):
    x = janela.winfo_pointerx() - janela._offset_x
    y = janela.winfo_pointery() - janela._offset_y
    janela.geometry(f"+{x}+{y}")

header_frame.bind("<Button-1>", start_drag)
header_frame.bind("<B1-Motion>", do_drag)

# Mensagem Inicial
log_text.tag_config("success", foreground="#00ff00")
log_text.insert(tk.END, "[🛰️ SISTEMA] ", "success")
log_text.insert(tk.END, "Conectado aos servidores da Federação!\n")

# Iniciar sistema
iniciar_loop()
janela.mainloop()
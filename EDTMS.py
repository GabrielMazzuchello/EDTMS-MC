import os
import sys
import json
import time
import threading
import webbrowser
import unicodedata
import tkinter as tk
from PIL import Image, ImageTk 
from tkinter import messagebox, scrolledtext
import firebase_admin
from firebase_admin import credentials, firestore
from pathlib import Path

#  pyinstaller --onefile --windowed --icon=meu_icone.ico EDTMS.py

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


def iniciar_loop():
    threading.Thread(target=loop_verificacao, daemon=True).start()

# Interface Gráfica Tkinter - Design Atualizado
janela = tk.Tk()
janela.title("")
janela.geometry("500x400")  # Altura um pouco maior
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

def abrir_site(event):
    webbrowser.open("https://edtms.squareweb.app")
    status_bar.config(text="Redirecionando para o site EDTMS...")

try:
    logo_img = Image.open("edtms_logo.png")
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
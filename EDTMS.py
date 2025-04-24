import os
import sys
import json
import time
import threading
import webbrowser
import unicodedata
import firebase_admin
from tkinter import ttk
from pathlib import Path
from PIL import Image, ImageTk 
from datetime import datetime, timedelta
from firebase_admin import credentials, firestore
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QCursor, QIcon
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QLineEdit, QPushButton,
    QComboBox, QPlainTextEdit, QVBoxLayout, QWidget, QMessageBox
)

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
processar_carga_em_execucao = False

# pyinstaller --onefile --windowed --icon=EDTMS.ico --add-data "EDTMS.ico;." --add-data "edtms_logo.png;." --add-data "serviceAccountKey.json;." EDTMS.py

# Caminho padrão do log
LOG_DIR = Path(os.environ["USERPROFILE"]) / "Saved Games" / "Frontier Developments" / "Elite Dangerous"

# Firebase com compatibilidade para PyInstaller (empacotado)
def get_embedded_service_account():
    if hasattr(sys, "_MEIPASS"):  # PyInstaller executável
        path = os.path.join(sys._MEIPASS, "serviceAccountKey.json")
    else:  # Rodando direto no Python'
        path = "serviceAccountKey.json"
    return credentials.Certificate(path)

def get_resource_path(relative_path):
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

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
        window.log_box.appendPlainText(f"Estação '{nome_estacao}' não encontrada no Firestore.")
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

    window.log_box.appendPlainText(f"Estação '{nome_estacao}' sincronizada com {len(novos_itens)} materiais.")
    window.log_box.verticalScrollBar().setValue(window.log_box.verticalScrollBar().maximum())

def verificar_abandono_ou_morte(materiais):
    global ultima_morte_detectada, ultimos_abandonos, ultimo_undocked_ts

    log_path = obter_log_mais_recente()
    if not log_path or not log_path.exists():
        return

    morte_processada = False

    while True:
        try:
            eventos = []
            linhas_invalidas = 0
            with open(log_path, encoding="utf-8") as f:
                for l in f:
                    l = l.strip()
                    if not l:
                        continue
                    try:
                        eventos.append(json.loads(l))
                    except json.JSONDecodeError:
                        linhas_invalidas += 1

            if linhas_invalidas > 0:
                window.log_box.appendPlainText(f"[INFO] {linhas_invalidas} linhas de log ignoradas (JSON inválido)")


            for evento in eventos:
                tipo = evento.get("event")
                ts = datetime.strptime(evento["timestamp"], "%Y-%m-%dT%H:%M:%SZ")

                # Ignorar eventos antigos ou antes do undocked
                if ultimo_undocked_ts and ts <= ultimo_undocked_ts:
                    continue

                # Abandono manual (EjectCargo)
                if tipo == "EjectCargo":
                    nome = evento.get("Type_Localised") or evento.get("Type")
                    qtd = evento.get("Count", 0)
                    mat_id = gerar_id(nome)
                    evento_id = f"{mat_id}|{qtd}|{ts.isoformat()}|eject"

                    if evento_id not in ultimos_abandonos:
                        ultimos_abandonos.add(evento_id)
                        abandono_para_firebase(nome, qtd)
                
                # Venda no mercado (MarketSell)
                elif tipo == "MarketSell":
                    nome = evento.get("Type_Localised") or evento.get("Type")
                    qtd = evento.get("Count", 0)
                    mat_id = gerar_id(nome)
                    evento_id = f"{mat_id}|{qtd}|{ts.isoformat()}|marketsell"

                    if evento_id not in ultimos_abandonos:
                        ultimos_abandonos.add(evento_id)
                        abandono_para_firebase(nome, qtd)
                        window.log_box.appendPlainText(f"💸 Venda detectada: {nome} x{qtd} (Revertido)")

                # Transferência via porta-frotas
                elif tipo == "CargoTransfer":
                    for transf in evento.get("Transfers", []):
                        direcao = transf.get("Direction")
                        nome = transf.get("Type_Localised") or transf.get("Type")
                        qtd = transf.get("Count", 0)
                        mat_id = gerar_id(nome)
                        evento_id = f"{mat_id}|{qtd}|{ts.isoformat()}|{direcao}"

                        if evento_id in ultimos_abandonos:
                            continue
                        ultimos_abandonos.add(evento_id)

                        if direcao == "tocarrier":
                            abandono_para_firebase(nome, qtd)
                        elif direcao == "toship":
                            subtrair_do_firestore(nome, qtd)
                            window.log_box.appendPlainText(f"[💰] Transferência do porta-frotas detectada: {nome} x{qtd} (Comprado)")

                # Morte
                elif tipo == "Died" and not morte_processada:
                    morte_processada = True
                    for mat in materiais:
                        abandono_para_firebase(mat["material"], mat["quantidade"])
                    window.log_box.appendPlainText(f"♻️ {len(materiais)} materiais devolvidos após morte/abandono")

                # Fim do monitoramento
                elif tipo == "Docked":
                    return

            # Limpeza de eventos antigos (5min)
            ultimos_abandonos = {
                eid for eid in ultimos_abandonos
                if datetime.fromisoformat(eid.split("|")[2]) > datetime.utcnow() - timedelta(minutes=5)
            }

        except Exception as e:
            window.log_box.appendPlainText(f"[ERRO VERIFICAÇÃO] {str(e)}")
            import traceback
            traceback.print_exc()

        time.sleep(1)

def processar_carga():
    global ultimo_cargo, ultimo_cargo_timestamp, ultima_entrega_realizada, verificacao_thread, processar_carga_em_execucao
    global ultima_morte_detectada, ultima_resurreicao_processada, verificacao_thread, ultimo_undocked_ts
    if processar_carga_em_execucao:
        return
    processar_carga_em_execucao = True

    log_path = obter_log_mais_recente()
    if not log_path or not log_path.exists():
        window.log_box.appendPlainText("[ERRO] Log não encontrado.")
        return

    window.log_box.appendPlainText("[🛰️] Monitorando log para eventos de compra e entrega...")

    eventos_processados = set()
    materiais_entregues = []

    while True:
        try:
            with open(log_path, encoding="utf-8") as f:
                linhas = f.readlines()

            eventos = []
            linhas_invalidas = 0
            with open(log_path, encoding="utf-8") as f:
                for l in f:
                    l = l.strip()
                    if not l:
                        continue
                    try:
                        eventos.append(json.loads(l))
                    except json.JSONDecodeError:
                        linhas_invalidas += 1

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
                    window.log_box.appendPlainText("[INFO] Atracado. Aguardando compras...")
                    eventos_processados.add(evento_id)

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
                        window.log_box.appendPlainText(f"[💰] Compra detectada: {nome} x{qtd}")
                        subtrair_do_firestore(nome, qtd)  # 👈 subtrai diretamente após compra
                        eventos_processados.add(evento_id)


                # 3. UNDОCKED → iniciar verificação de morte/abandono
                elif tipo == "Undocked":
                    ts_undocked = datetime.strptime(evento["timestamp"], "%Y-%m-%dT%H:%M:%SZ")
                    if not ultimo_undocked_ts or ts_undocked > ultimo_undocked_ts:
                        ultimo_undocked_ts = ts_undocked
                        window.log_box.appendPlainText("[🚀] Undocked detectado. Iniciando verificação de carga...")
                        
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
            window.log_box.appendPlainText(f"[ERRO] {str(e)}")
            import traceback
            traceback.print_exc()
        time.sleep(1)
    processar_carga_em_execucao = False # coloquei para dentro do while TAB remover se der algum problema (removi e funcionou)


def abandono_para_firebase(nome, qtd):
    construcao_nome = window.construcoes_dropdown.currentText()
    if not construcao_nome or construcao_nome not in construcoes_cache:
        window.log_box.appendPlainText("⚠️ Nenhuma construção válida para reverter.")
        return

    doc_ref = db.collection("inventories").document(construcoes_cache[construcao_nome]["doc_id"])
    doc = doc_ref.get()
    if not doc.exists:
        return

    materiais = doc.to_dict().get("items", [])
    for mat in materiais:
        if gerar_id(mat["material"]) == gerar_id(nome):
            mat["restante"] += qtd
            window.log_box.appendPlainText(f"↩ {mat['material']}: +{qtd} (Abandono)")
            break

    doc_ref.update({"items": materiais})

def subtrair_do_firestore(nome, qtd):
    construcao_nome = window.construcoes_dropdown.currentText()
    if not construcao_nome or construcao_nome not in construcoes_cache:
        window.log_box.appendPlainText("⚠️ Nenhuma construção válida para subtrair.")
        return

    doc_ref = db.collection("inventories").document(construcoes_cache[construcao_nome]["doc_id"])
    doc = doc_ref.get()
    if not doc.exists:
        return

    materiais = doc.to_dict().get("items", [])
    for mat in materiais:
        if gerar_id(mat["material"]) == gerar_id(nome):
            novo_valor = max(0, mat["restante"] - qtd)
            window.log_box.appendPlainText(f"✎ {mat['material']}: {mat['restante']} → {novo_valor} (Comprado)")
            mat["restante"] = novo_valor
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

        if not processar_carga_em_execucao:
            threading.Thread(target=processar_carga, daemon=True).start()

        nome_estacao, tipo_estacao, materiais = processar_log(log_path)

        if nome_estacao:
            if nome_estacao != ultima_estacao:
                ultima_estacao = nome_estacao
                ultimo_estado_materiais = {}  # Zera o estado quando troca de estação

                with open(ultimo_dock_log, "w", encoding="utf-8") as f:
                    f.write(f"Docked em {nome_estacao} ({tipo_estacao})")

                window.log_box.clear()
                window.log_box.appendPlainText(f"[INFO] Atracado em: {nome_estacao} ({tipo_estacao})")

            if materiais:
                # Verifica se houve mudança nos materiais antes de atualizar
                materiais_dict = {mat["id"]: mat["restante"] for mat in materiais}
                if materiais_dict != ultimo_estado_materiais:
                    atualizar_firestore(nome_estacao, materiais)
                    ultimo_estado_materiais = materiais_dict

        time.sleep(5)

# def normalizar_nome(nome):
#     nome = nome.lower()
#     substituicoes = {
#         'í': 'i', 'ó': 'o', 'ã': 'a', 'á': 'a', 
#         'é': 'e', 'ê': 'e', 'ç': 'c', 'ú': 'u',
#         'construction': '', 'materials': '', ' ': '_',
#         '-': '', "'": "", ":": "", "(": "", ")": ""
#     }
#     for k, v in substituicoes.items():
#         nome = nome.replace(k, v)
#     return nome.rstrip('s').strip('_')

from datetime import datetime, timedelta

ultima_morte_detectada = None
ultima_resurreicao_processada = None

def obter_materiais_da_construcao(nome_construcao):
    if not nome_construcao or not construcoes_cache.get(nome_construcao):
        return None
    return construcoes_cache[nome_construcao]["dados"]["items"]

def iniciar_loop():
    threading.Thread(target=loop_verificacao, daemon=True).start()





class EDTMSWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EDTMS")
        self.setWindowIcon(QIcon(get_resource_path("EDTMS.ico")))
        self.setGeometry(100, 100, 450, 425)
        
        # Configuração da interface
        self.setup_ui()
        
    def setup_ui(self):
        # Widgets principais
        self.uid_input = QLineEdit()
        self.uid_input.setPlaceholderText("Digite seu UID")
        self.construcoes_dropdown = QComboBox()  
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)

        # Botão
        carregar_btn = QPushButton("Carregar")
        carregar_btn.clicked.connect(self.carregar_construcoes)

        # Configuração da logo
        self.setup_logo()

        # Layout principal
        layout = QVBoxLayout()
        layout.addWidget(self.logo_label, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.uid_input)
        layout.addWidget(carregar_btn)
        layout.addWidget(self.construcoes_dropdown)
        layout.addWidget(self.log_box)

        # Widget central
        central_widget = QWidget()
        central_widget.setLayout(layout)
        self.setCentralWidget(central_widget)

    def setup_logo(self):
        """Configura a logo clicável"""
        self.logo_label = QLabel()
        
        try:
            pixmap = QPixmap(get_resource_path("edtms_logo.png"))
            if pixmap.isNull():
                raise FileNotFoundError
                
            # Redimensiona mantendo aspect ratio
            self.logo_label.setPixmap(pixmap.scaled(
                200, 60, 
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            ))

            self.logo_label.setStyleSheet("""
                QLabel {
                    background-color: transparent;
                    border-radius: 15px;
                    border: 2px solid #FFA726;                      
                }
                QLabel:hover {
                    border: 2px solid #F57C00;
                    background-color: rgba(52, 152, 219, 0.1);
                }
            """)
        except Exception:
            # Fallback: cria uma logo simples se o arquivo não for encontrado
            self.logo_label.setText("EDTMS Logo")
            self.logo_label.setStyleSheet("""
                QLabel {
                    background-color: #2c3e50;
                    color: white;
                    font-weight: bold;
                    font-size: 18px;
                    padding: 10px;
                    min-width: 200px;
                    min-height: 60px;
                    text-align: center;
                }
            """)
        
        # Tornar clicável
        self.logo_label.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.logo_label.mousePressEvent = self.abrir_site_edtms

    def abrir_site_edtms(self, event):
        """Abre o site da EDTMS no navegador padrão"""
        webbrowser.open("https://edtms.squareweb.app")

    def carregar_construcoes(self):
        uid = self.uid_input.text().strip()
        if not uid:
            QMessageBox.critical(self, "Erro", "Digite seu UID primeiro!")
            return

        try:
            construcoes_ref = db.collection("inventories")
            query = construcoes_ref.where("collaborators", "array_contains", uid).get()

            construcoes = []
            for doc in query:
                data = doc.to_dict()
                construcoes.append({
                    "doc_id": doc.id,
                    "nome": data.get("name", "Sem nome"),
                    "dados": data
                })

            self.construcoes_dropdown.clear()
            self.construcoes_dropdown.addItems([c["nome"] for c in construcoes])

            global construcoes_cache
            construcoes_cache = {c["nome"]: c for c in construcoes}

            self.log_box.appendPlainText(f"Carregadas {len(construcoes)} construções")
            threading.Thread(target=processar_carga, daemon=True).start()

        except Exception as e:
            self.log_box.appendPlainText(f"Erro ao carregar: {str(e)}")
    

def iniciar_loop():
    threading.Thread(target=loop_verificacao, daemon=True).start()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = EDTMSWindow()
    window.show()
    iniciar_loop()
    sys.exit(app.exec())
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
ultima_undocked_processada = None
ultima_posicao_log = 0
ultima_sessao_jogo = None
leitura_carga_permitida = False
ultimos_abandonos = set()  # ← Agora fora da função, persistente
verificacao_em_andamento = False
verificacao_thread = None
ultimo_undocked_ts = None
processar_carga_em_execucao = False
processar_carga_thread = None

# 

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

def processar_carga():
    global ultimo_cargo, ultimo_cargo_timestamp, ultima_entrega_realizada
    global verificacao_thread, processar_carga_em_execucao
    global ultima_morte_detectada, ultima_resurreicao_processada
    global ultimo_undocked_ts, ultima_posicao_log

    if processar_carga_em_execucao:
        return

    processar_carga_em_execucao = True

    log_path = obter_log_mais_recente()
    if not log_path or not log_path.exists():
        log_text.insert(tk.END, "[ERRO] Log não encontrado.\n")
        processar_carga_em_execucao = False
        return

    log_text.insert(tk.END, "[🛰️] Monitorando log para eventos de carga...\n")
    log_text.see(tk.END)

    eventos_processados = set()

    # Inventário REAL atual da nave
    # Estrutura:
    # {
    #   "aluminio": {"material": "Alumínio", "quantidade": 40}
    # }
    inventario_atual = {}

    # Começa lendo apenas eventos novos, para não reprocessar log antigo
    try:
        ultima_posicao_log = log_path.stat().st_size
    except Exception:
        ultima_posicao_log = 0

    def adicionar_inventario(nome, qtd, excedente=0):
        if not nome or qtd <= 0 and excedente <= 0:
            return

        mat_id = gerar_id(nome)

        if mat_id not in inventario_atual:
            inventario_atual[mat_id] = {
                "material": nome,
                "quantidade": 0,
                "excedente": 0
            }

        inventario_atual[mat_id]["quantidade"] += qtd
        inventario_atual[mat_id]["excedente"] += excedente

    def remover_inventario(nome, qtd):
        if not nome or qtd <= 0:
            return 0, 0

        mat_id = gerar_id(nome)

        if mat_id not in inventario_atual:
            return 0, qtd

        item = inventario_atual[mat_id]

        excedente_atual = item.get("excedente", 0)
        valido_atual = item.get("quantidade", 0)

        # Remove primeiro do excedente, porque excedente NÃO deve voltar ao site
        removido_excedente = min(qtd, excedente_atual)
        restante_para_remover = qtd - removido_excedente

        # Só o que sair da quantidade válida deve voltar ao Firebase
        removido_valido = min(restante_para_remover, valido_atual)

        item["excedente"] = excedente_atual - removido_excedente
        item["quantidade"] = valido_atual - removido_valido

        if item["quantidade"] <= 0 and item["excedente"] <= 0:
            del inventario_atual[mat_id]

        return removido_valido, removido_excedente

    def mostrar_inventario():
        if not inventario_atual:
            log_text.insert(tk.END, "[📦] Carga atual: vazia\n")
            return

        log_text.insert(tk.END, "\n[📦] Carga atual:\n")

        for item in inventario_atual.values():
            material = item["material"]
            qtd = item.get("quantidade", 0)
            excedente = item.get("excedente", 0)

            if excedente > 0:
                log_text.insert(
                    tk.END,
                    f"   • {material} x{qtd} | excedente x{excedente}\n"
                )
            else:
                log_text.insert(
                    tk.END,
                    f"   • {material} x{qtd}\n"
                )

        log_text.insert(tk.END, "\n")
        log_text.see(tk.END)

    def comprar_material_controlado(nome, qtd):
        construcao_nome = construcoes_var.get()

        if not construcao_nome or construcao_nome not in construcoes_cache:
            log_text.insert(tk.END, "⚠️ Nenhuma construção válida selecionada.\n")
            return 0, qtd

        doc_ref = db.collection("inventories").document(construcoes_cache[construcao_nome]["doc_id"])
        doc = doc_ref.get()

        if not doc.exists:
            log_text.insert(tk.END, "⚠️ Construção não encontrada no Firestore.\n")
            return 0, qtd

        materiais = doc.to_dict().get("items", [])

        for mat in materiais:
            if gerar_id(mat["material"]) == gerar_id(nome):
                restante = mat.get("restante", 0)

                if restante <= 0:
                    log_text.insert(
                        tk.END,
                        f"[EXCEDENTE] {nome} já está zerado no site. Compra ignorada: x{qtd}\n",
                        "erro"
                    )
                    log_text.tag_config("erro", foreground="#ff4444")
                    return 0, qtd

                qtd_valida = min(qtd, restante)
                qtd_excedente = max(0, qtd - restante)

                mat["restante"] = max(0, restante - qtd_valida)
                doc_ref.update({"items": materiais})

                if qtd_excedente > 0:
                    log_text.insert(
                        tk.END,
                        f"[EXCEDENTE] {nome}: comprou x{qtd}, mas só precisava de x{qtd_valida}. Ignorado: x{qtd_excedente}\n",
                        "erro"
                    )
                    log_text.tag_config("erro", foreground="#ff4444")

                return qtd_valida, qtd_excedente

        log_text.insert(
            tk.END,
            f"[EXCEDENTE] {nome} não existe nessa construção. Compra ignorada: x{qtd}\n",
            "erro"
        )
        log_text.tag_config("erro", foreground="#ff4444")
        return 0, qtd

    while True:
        try:
            log_atual = obter_log_mais_recente()

            if not log_atual or not log_atual.exists():
                time.sleep(1)
                continue

            # Se o jogo criou/trocou de arquivo Journal, muda para o novo
            if log_atual != log_path:
                log_path = log_atual
                ultima_posicao_log = 0
                eventos_processados.clear()
                log_text.insert(tk.END, f"[INFO] Novo arquivo de log detectado: {log_path.name}\n")

            eventos = []

            with open(log_path, encoding="utf-8") as f:
                f.seek(ultima_posicao_log)

                for linha in f:
                    linha = linha.strip()
                    if not linha:
                        continue

                    try:
                        eventos.append(json.loads(linha))
                    except json.JSONDecodeError:
                        log_text.insert(tk.END, "[INFO] Linha de log ignorada: JSON inválido\n")

                ultima_posicao_log = f.tell()

            eventos = sorted(eventos, key=lambda e: e.get("timestamp", ""))

            for evento in eventos:
                tipo = evento.get("event")
                timestamp = evento.get("timestamp")

                if not tipo or not timestamp:
                    continue

                evento_id = json.dumps(evento, sort_keys=True, ensure_ascii=False)

                if evento_id in eventos_processados:
                    continue

                # Atracou
                if tipo == "Docked":
                    ultima_entrega_realizada = False
                    log_text.insert(tk.END, "[INFO] Atracado. Aguardando eventos de carga...\n")
                    eventos_processados.add(evento_id)

                # Compra na estação
                elif tipo == "MarketBuy":
                    nome = evento.get("Type_Localised") or evento.get("Type")
                    qtd = evento.get("Count", 0)

                    if nome and qtd:
                        qtd_valida, qtd_excedente = comprar_material_controlado(nome, qtd)

                        adicionar_inventario(nome, qtd_valida, qtd_excedente)

                        log_text.insert(
                            tk.END,
                            f"[💰] Compra detectada: {nome} x{qtd} | válido: x{qtd_valida} | excedente: x{qtd_excedente}\n"
                        )

                        mostrar_inventario()

                    eventos_processados.add(evento_id)

                # Venda na estação
                elif tipo == "MarketSell":
                    nome = evento.get("Type_Localised") or evento.get("Type")
                    qtd = evento.get("Count", 0)

                    if nome and qtd:
                        qtd_valida, qtd_excedente = remover_inventario(nome, qtd)

                        if qtd_valida > 0:
                            abandono_para_firebase(nome, qtd_valida)

                        log_text.insert(
                            tk.END,
                            f"[💸] Venda detectada: {nome} x{qtd} | revertido: x{qtd_valida} | excedente ignorado: x{qtd_excedente}\n"
                        )
                        mostrar_inventario()

                    eventos_processados.add(evento_id)

                # Abandono/ejeção de carga
                elif tipo == "EjectCargo":
                    nome = evento.get("Type_Localised") or evento.get("Type")
                    qtd = evento.get("Count", 0)

                    if nome and qtd:
                        qtd_valida, qtd_excedente = remover_inventario(nome, qtd)

                        if qtd_valida > 0:
                            abandono_para_firebase(nome, qtd_valida)

                        log_text.insert(
                            tk.END,
                            f"[⚠️] Carga abandonada: {nome} x{qtd} | revertido: x{qtd_valida} | excedente ignorado: x{qtd_excedente}\n"
                        )
                        mostrar_inventario()

                    eventos_processados.add(evento_id)

                # Transferência com porta-frotas
                elif tipo == "CargoTransfer":
                    for transf in evento.get("Transfers", []):
                        direcao = transf.get("Direction")
                        nome = transf.get("Type_Localised") or transf.get("Type")
                        qtd = transf.get("Count", 0)

                        if not nome or not qtd:
                            continue

                        if direcao == "tocarrier":
                            qtd_valida, qtd_excedente = remover_inventario(nome, qtd)

                            if qtd_valida > 0:
                                abandono_para_firebase(nome, qtd_valida)

                            log_text.insert(
                                tk.END,
                                f"[🚚] Transferido para o porta-frotas: {nome} x{qtd} | revertido: x{qtd_valida} | excedente ignorado: x{qtd_excedente}\n"
                            )

                        elif direcao == "toship":
                            subtrair_do_firestore(nome, qtd)
                            adicionar_inventario(nome, qtd)

                            log_text.insert(
                                tk.END,
                                f"[💰] Transferido do porta-frotas para a nave: {nome} x{qtd}\n"
                            )

                    mostrar_inventario()
                    eventos_processados.add(evento_id)

                # Entrega real para estação de construção
                elif tipo == "ColonisationContribution":
                    contribuicoes = evento.get("Contributions", [])

                    for item in contribuicoes:
                        nome = item.get("Name_Localised") or item.get("Name")
                        qtd = item.get("Amount", 0)

                        if nome and qtd:
                            remover_inventario(nome, qtd)
                            log_text.insert(tk.END, f"[📦] Entregue à construção: {nome} x{qtd}\n")

                    mostrar_inventario()
                    eventos_processados.add(evento_id)

                # Morte
                elif tipo == "Died":
                    if inventario_atual:
                        for item in list(inventario_atual.values()):
                            nome = item["material"]
                            qtd = item["quantidade"]

                            if qtd > 0:
                                abandono_para_firebase(nome, qtd)
                                log_text.insert(tk.END, f"↩ {nome}: +{qtd} (Morte)\n")

                        inventario_atual.clear()
                        log_text.insert(tk.END, "♻️ Carga atual devolvida após morte\n")
                    else:
                        log_text.insert(tk.END, "♻️ Morte detectada, mas inventário estava vazio\n")

                    ultima_entrega_realizada = False
                    eventos_processados.add(evento_id)

                # Saiu da estação
                elif tipo == "Undocked":
                    try:
                        ts_undocked = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")

                        if not ultimo_undocked_ts or ts_undocked > ultimo_undocked_ts:
                            ultimo_undocked_ts = ts_undocked
                            log_text.insert(tk.END, "[🚀] Undocked detectado.\n")

                    except ValueError:
                        log_text.insert(tk.END, "[ERRO] Timestamp inválido em Undocked\n")

                    eventos_processados.add(evento_id)

                log_text.see(tk.END)

            # Evita crescimento infinito do set
            if len(eventos_processados) > 2000:
                eventos_processados.clear()

        except FileNotFoundError:
            log_text.insert(tk.END, "[ERRO] Arquivo de log não encontrado durante leitura.\n")

        except KeyError as e:
            log_text.insert(tk.END, f"[ERRO] Campo ausente no evento: {str(e)}\n")

        except ValueError as e:
            log_text.insert(tk.END, f"[ERRO] Problema ao converter dados: {str(e)}\n")

        except Exception as e:
            log_text.insert(tk.END, f"[ERRO DESCONHECIDO] {str(e)}\n")
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
            # log_text.insert(tk.END, f"↩ {mat['material']}: +{qtd} (Abandono)\n")
            break

    doc_ref.update({"items": materiais})

def subtrair_do_firestore(nome, qtd):
    construcao_nome = construcoes_var.get()
    if not construcao_nome or construcao_nome not in construcoes_cache:
        log_text.insert(tk.END, "⚠️ Nenhuma construção válida para subtrair.\n")
        return

    doc_ref = db.collection("inventories").document(construcoes_cache[construcao_nome]["doc_id"])
    doc = doc_ref.get()
    if not doc.exists:
        return

    materiais = doc.to_dict().get("items", [])
    for mat in materiais:
        if gerar_id(mat["material"]) == gerar_id(nome):
            novo_valor = max(0, mat["restante"] - qtd)
            log_text.insert(tk.END, f"✎ {mat['material']}: {mat['restante']} → {novo_valor} (Comprado)\n")
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

        global processar_carga_thread

        if processar_carga_thread is None or not processar_carga_thread.is_alive():
            processar_carga_thread = threading.Thread(target=processar_carga, daemon=True)
            processar_carga_thread.start()
        nome_estacao, tipo_estacao, materiais = processar_log(log_path)

        if nome_estacao:
            if nome_estacao != ultima_estacao:
                ultima_estacao = nome_estacao
                ultimo_estado_materiais = {}  # Zera o estado quando troca de estação
                global leitura_carga_permitida
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

def get_resource_path(relative_path):
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

# Interface Gráfica Tkinter - Design Atualizado
janela = tk.Tk()
janela.iconbitmap(get_resource_path("EDTMS.ico"))
janela.title("EDTMS")
janela.geometry("620x620")
janela.minsize(560, 500)
janela.resizable(True, True)
janela.configure(bg="#1a1a1a")
janela.overrideredirect(False)

# Cores e Fontes
COR_DE_FUNDO = "#1a1a1a"
COR_TEXTO = "#cccccc"
COR_DESTAQUE = "#ff9900"
FONTE_TITULO = ("Arial", 12, "bold")
FONTE_TEXTO = ("Arial", 9)

# Estilo ttk para bordas arredondadas nos botões
style = ttk.Style()
style.theme_use("default")
style.configure(
    "Rounded.TButton",
    padding=(12, 8),
    relief="flat",
    background=COR_DESTAQUE,
    foreground="#1a1a1a",
    font=("Arial", 9, "bold"),
    borderwidth=0
)

style.map(
    "Rounded.TButton",
    background=[
        ("active", "#ffaa33"),
        ("pressed", "#cc7a00"),
        ("disabled", "#555555")
    ],
    foreground=[
        ("disabled", "#999999")
    ]
)

# Frame Principal
frame_principal = tk.Frame(janela, bg=COR_DE_FUNDO)
frame_principal.pack(fill="both", expand=True, padx=10, pady=10)

# Top Frame (UID, Construção e Botões)
top_frame = tk.Frame(frame_principal, bg=COR_DE_FUNDO)
top_frame.pack(fill="x", pady=(0, 5))

tk.Label(top_frame, text="UID:", bg=COR_DE_FUNDO, fg=COR_TEXTO).grid(row=0, column=0, sticky="w", padx=5)
uid_entry = tk.Entry(
    top_frame,
    bg="#2a2a2a",
    fg=COR_TEXTO,
    insertbackground=COR_DESTAQUE,
    width=32,
    relief="flat",
    font=("Consolas", 10)
)
uid_entry.grid(row=0, column=1, sticky="ew", padx=5)

load_btn = ttk.Button(top_frame, text="Carregar", style="Rounded.TButton", command=carregar_construcoes)
load_btn.grid(row=0, column=2, padx=(5, 0))

# Dropdown de Construções
tk.Label(top_frame, text="Construção:", bg=COR_DE_FUNDO, fg=COR_TEXTO).grid(row=1, column=0, sticky="w", padx=5, pady=(5, 0))
construcoes_var = tk.StringVar()
construcoes_dropdown = ttk.Combobox(
    top_frame,
    textvariable=construcoes_var,
    state="readonly",
    width=40
)
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

# cor da barra lateral no windows 11
import ctypes
HWND = ctypes.windll.user32.GetParent(janela.winfo_id())
DWMWA_BORDER_COLOR = 34

ctypes.windll.dwmapi.DwmSetWindowAttribute(
    HWND,
    DWMWA_BORDER_COLOR,
    ctypes.byref(ctypes.c_int(0x00222222)),
    ctypes.sizeof(ctypes.c_int)
)

# Área de Log
log_frame = tk.Frame(frame_principal, bg=COR_DE_FUNDO)
log_frame.pack(fill="both", expand=True)

log_text = scrolledtext.ScrolledText(
    log_frame,
    wrap=tk.WORD,
    width=70,
    height=18,
    bg="#101010",
    fg=COR_TEXTO,
    insertbackground=COR_DESTAQUE,
    selectbackground=COR_DESTAQUE,
    font=("Consolas", 9),
    relief="flat",
    padx=10,
    pady=10,
    highlightthickness=1,
    highlightbackground="#333333",
    highlightcolor=COR_DESTAQUE
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
    text="🟢 PRONTO PARA SINCRONIZAR | EDTMS v1.1",
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

def aplicar_arraste(widget):
    widget.bind("<Button-1>", start_drag)
    widget.bind("<B1-Motion>", do_drag)

header_frame.bind("<Button-1>", start_drag)
header_frame.bind("<B1-Motion>", do_drag)

aplicar_arraste(frame_principal)
aplicar_arraste(header_frame)
aplicar_arraste(cabecalho)

try:
    aplicar_arraste(logo_frame)
    aplicar_arraste(logo_label)
except:
    pass

# Mensagem Inicial
log_text.tag_config("success", foreground="#00ff00")
log_text.insert(tk.END, "[🛰️ SISTEMA] ", "success")
log_text.insert(tk.END, "Conectado aos servidores da Federação!\n")

# Iniciar sistema
iniciar_loop()
janela.mainloop()
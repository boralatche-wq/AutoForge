"""
app.py — AutoForge (motor genérico e configurável de normalização de dados)

Como usar:
    pip install -r requirements.txt
    python app.py
    -> abre automaticamente http://127.0.0.1:5050 no navegador

Estrutura:
    app.py            este arquivo (servidor local + rotas)
    engine.py          motor genérico de extração/normalização (regras em JSON)
    rules.json         regras configuráveis (editáveis pela interface)
    workspace/          pasta de trabalho: arquivos enviados e os CSVs de
                        saída de cada regra (um por regra, sem consolidação)
      rejeitados/       arquivos cujo nome não casa com nenhuma regra (automático)
      apagar/           arquivos já processados, listados em ja_processados.txt
                        (automático)
"""

import os
import io
import shutil
import threading
import webbrowser
import zipfile

import pandas as pd
from flask import Flask, request, jsonify, render_template, send_file, abort

import engine

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.path.join(APP_DIR, "workspace")
RULES_PATH = os.path.join(APP_DIR, "rules.json")
LOG_FILENAME = "ja_processados.txt"

os.makedirs(WORKSPACE_DIR, exist_ok=True)
engine.garantir_subpastas(WORKSPACE_DIR)  # workspace/rejeitados e workspace/apagar
if not os.path.exists(RULES_PATH):
    shutil.copy(os.path.join(APP_DIR, "rules_default.json"), RULES_PATH)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200MB por upload
app.json.ensure_ascii = False  # mostra "ç"/"ã" direto em vez de \u00e7/\u00e3


def _regras_flat(rules_config):
    """Achata grupos em uma lista simples, mantendo referência ao grupo."""
    flat = []
    for item in rules_config.get("regras", []):
        if item.get("tipo") == "grupo":
            for sub in item["regras"]:
                flat.append({"grupo": item["nome"], **sub})
        else:
            flat.append({"grupo": None, **item})
    return flat


def _nomes_saida_csv(rules_config):
    """Todos os nomes de 'saida_csv' configurados nas regras (ativas ou
    não) — usado para não misturar arquivos de saída com uploads do usuário,
    e para validar downloads."""
    nomes = set()
    for item in rules_config.get("regras", []):
        alvo = item["regras"] if item.get("tipo") == "grupo" else [item]
        for r in alvo:
            if r.get("saida_csv"):
                nomes.add(r["saida_csv"])
    return nomes


def _estado_arquivos(rules_config=None):
    """Faz a triagem automática e devolve as 3 listas mostradas na tela:
    fila (raiz), rejeitados e já processados (a apagar)."""
    rules_config = rules_config or engine.carregar_regras(RULES_PATH)
    movidos = engine.organizar_workspace(rules_config, WORKSPACE_DIR, LOG_FILENAME)
    nomes_saida = _nomes_saida_csv(rules_config)
    fila = sorted(
        f for f in os.listdir(WORKSPACE_DIR)
        if os.path.isfile(os.path.join(WORKSPACE_DIR, f))
        and f != LOG_FILENAME
        and f not in nomes_saida
    )
    return {
        "arquivos": fila,
        "rejeitados": engine.listar_subpasta(WORKSPACE_DIR, engine.PASTA_REJEITADOS),
        "apagar": engine.listar_subpasta(WORKSPACE_DIR, engine.PASTA_APAGAR),
        "movidos": movidos,
    }


def _preview_arquivo(caminho, nome):
    try:
        df = pd.read_csv(caminho, sep=";", encoding="utf-8-sig")
    except Exception:
        return {"nome": nome, "colunas": [], "linhas": [], "total": 0, "erro": True}
    return {
        "nome": nome,
        "colunas": df.columns.tolist(),
        "linhas": df.tail(20).fillna("").values.tolist(),
        "total": len(df),
    }


@app.route("/")
def index():
    rules_config = engine.carregar_regras(RULES_PATH)
    estado = _estado_arquivos(rules_config)
    arquivos_saida = engine.listar_arquivos_saida(rules_config, WORKSPACE_DIR)
    return render_template(
        "index.html",
        regras=_regras_flat(rules_config),
        arquivos=estado["arquivos"],
        rejeitados=estado["rejeitados"],
        apagar=estado["apagar"],
        arquivos_saida=arquivos_saida,
    )


@app.route("/upload", methods=["POST"])
def upload():
    enviados = []
    for f in request.files.getlist("arquivos"):
        if not f.filename:
            continue
        destino = os.path.join(WORKSPACE_DIR, os.path.basename(f.filename))
        f.save(destino)
        enviados.append(os.path.basename(f.filename))
    return jsonify({"ok": True, "arquivos": enviados, "estado": _estado_arquivos()})


@app.route("/api/arquivos", methods=["GET"])
def listar_arquivos():
    return jsonify(_estado_arquivos())


@app.route("/api/arquivos/<path:nome>", methods=["DELETE"])
def remover_arquivo(nome):
    caminho = os.path.join(WORKSPACE_DIR, os.path.basename(nome))
    if os.path.exists(caminho):
        os.remove(caminho)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "erro": "arquivo não encontrado"}), 404


@app.route("/api/rules", methods=["GET"])
def obter_regras():
    return jsonify(engine.carregar_regras(RULES_PATH))


@app.route("/api/rules", methods=["POST"])
def salvar_regras_completo():
    rules_config = request.get_json(force=True)
    engine.salvar_regras(RULES_PATH, rules_config)
    return jsonify({"ok": True})


@app.route("/api/rules/nova", methods=["POST"])
def adicionar_regra():
    nova = request.get_json(force=True)
    rules_config = engine.carregar_regras(RULES_PATH)
    rules_config.setdefault("regras", []).append(nova)
    engine.salvar_regras(RULES_PATH, rules_config)
    return jsonify({"ok": True})


@app.route("/api/rules/<path:nome>/toggle", methods=["POST"])
def alternar_regra(nome):
    rules_config = engine.carregar_regras(RULES_PATH)
    encontrada = False
    for item in rules_config.get("regras", []):
        alvo = item["regras"] if item.get("tipo") == "grupo" else [item]
        for r in alvo:
            if r["nome"] == nome:
                r["ativo"] = not r.get("ativo", True)
                encontrada = True
    engine.salvar_regras(RULES_PATH, rules_config)
    return jsonify({"ok": encontrada})


@app.route("/api/rules/<path:nome>", methods=["PUT"])
def atualizar_regra(nome):
    nova_regra = request.get_json(force=True)
    rules_config = engine.carregar_regras(RULES_PATH)
    encontrada = False
    for item in rules_config.get("regras", []):
        if item.get("tipo") == "grupo":
            for i, r in enumerate(item["regras"]):
                if r["nome"] == nome:
                    item["regras"][i] = nova_regra
                    encontrada = True
        else:
            if item["nome"] == nome:
                for k in list(item.keys()):
                    del item[k]
                item.update(nova_regra)
                encontrada = True
    engine.salvar_regras(RULES_PATH, rules_config)
    return jsonify({"ok": encontrada})


@app.route("/api/rules/<path:nome>", methods=["DELETE"])
def remover_regra(nome):
    rules_config = engine.carregar_regras(RULES_PATH)
    novas = []
    for item in rules_config.get("regras", []):
        if item.get("tipo") == "grupo":
            item["regras"] = [r for r in item["regras"] if r["nome"] != nome]
            if item["regras"]:
                novas.append(item)
        else:
            if item["nome"] != nome:
                novas.append(item)
    rules_config["regras"] = novas
    engine.salvar_regras(RULES_PATH, rules_config)
    return jsonify({"ok": True})


@app.route("/api/processar", methods=["POST"])
def processar():
    corpo = request.get_json(silent=True) or {}
    limpar = bool(corpo.get("limpar", False))
    rules_config = engine.carregar_regras(RULES_PATH)
    sucesso, log, arquivos_saida = engine.executar_tudo(rules_config, WORKSPACE_DIR, limpar=limpar)

    previews = [
        _preview_arquivo(os.path.join(WORKSPACE_DIR, nome), nome)
        for nome in arquivos_saida
    ]

    return jsonify({"ok": sucesso, "log": log, "arquivos": previews,
                    "estado": _estado_arquivos(rules_config)})


@app.route("/api/limpar/rejeitados", methods=["POST"])
def limpar_rejeitados():
    removidos = engine.apagar_pasta(WORKSPACE_DIR, engine.PASTA_REJEITADOS)
    return jsonify({"ok": True, "removidos": removidos, "estado": _estado_arquivos()})


@app.route("/api/limpar/processados", methods=["POST"])
def limpar_processados():
    # faz a triagem antes, para pegar também o que ainda estava na raiz
    _estado_arquivos()
    removidos = engine.apagar_pasta(WORKSPACE_DIR, engine.PASTA_APAGAR)
    return jsonify({"ok": True, "removidos": removidos, "estado": _estado_arquivos()})


@app.route("/download/saida/<path:nome>")
def download_saida(nome):
    """Baixa um dos arquivos de saída das regras — exatamente o nome
    configurado em 'Nome do CSV de saída desta regra', sem nenhum arquivo
    consolidado adicional por cima."""
    rules_config = engine.carregar_regras(RULES_PATH)
    nome = os.path.basename(nome)
    if nome not in _nomes_saida_csv(rules_config):
        abort(404)
    caminho = os.path.join(WORKSPACE_DIR, nome)
    if not os.path.exists(caminho):
        abort(404)
    return send_file(caminho, as_attachment=True, download_name=nome)


@app.route("/download/app-zip")
def download_app_zip():
    """Empacota o app inteiro (código + regras), sem os dados do workspace,
    para o usuário levar/redistribuir o AutoForge."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for nome_arquivo in ["app.py", "engine.py", "rules.json", "rules_default.json",
                              "requirements.txt", "README.md"]:
            caminho = os.path.join(APP_DIR, nome_arquivo)
            if os.path.exists(caminho):
                zf.write(caminho, nome_arquivo)
        for pasta in ["templates", "static"]:
            caminho_pasta = os.path.join(APP_DIR, pasta)
            if os.path.isdir(caminho_pasta):
                for raiz, _, arquivos in os.walk(caminho_pasta):
                    for arq in arquivos:
                        caminho_completo = os.path.join(raiz, arq)
                        rel = os.path.relpath(caminho_completo, APP_DIR)
                        zf.write(caminho_completo, rel)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="AutoForge.zip",
                      mimetype="application/zip")


def _abrir_navegador():
    webbrowser.open("http://127.0.0.1:5050")


if __name__ == "__main__":
    threading.Timer(1.0, _abrir_navegador).start()
    app.run(host="127.0.0.1", port=5050, debug=False)
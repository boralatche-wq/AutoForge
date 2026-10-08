"""
engine.py — Motor genérico de extração e normalização do AutoForge.

Em vez de uma função Python por fundo/arquivo, cada fonte de dados é descrita
por uma REGRA (dicionário / JSON). O motor sabe interpretar regras dos
seguintes tipos:

  - csv        : arquivo CSV/TSV, cabeçalho na primeira linha OU em qualquer
                 lugar do arquivo (modo "buscar_cabecalho")
  - xlsx       : planilha Excel. Cada coluna do mapeamento diz explicitamente
                 de qual ABA ela vem, se está na HORIZONTAL ou VERTICAL, e se
                 é um valor FIXO (um valor só, repetido em todas as linhas)
                 ou não (um valor por registro). O cabeçalho da aba principal
                 pode estar em qualquer lugar da planilha (modo
                 "buscar_cabecalho").
  - pdf        : extração automática (sem regex) dizendo só a orientação da
                 tabela no PDF ("horizontal" ou "vertical"); modo "regex"
                 avançado disponível para casos fora do padrão.

Cada regra também define:
  - mapeamento de colunas de saída (origem pode ser uma coluna lida, um valor
    fixo, um metadado extraído, ou um cálculo simples entre duas colunas)
  - filtro opcional de linhas
  - normalização numérica no padrão brasileiro
  - nome do CSV de saída da regra

O resultado final de cada regra é sempre o seu próprio 'saida_csv' — não há
nenhum arquivo consolidado adicional criado por cima. Se duas regras
apontarem para o mesmo 'saida_csv', os resultados de ambas se acumulam
nesse arquivo (linha a linha, sem deduplicação).
"""

import os
import re
import glob
import json
import fnmatch
import unicodedata

import pandas as pd

try:
    import pdfplumber
except ImportError:  # pdfplumber é opcional até ser realmente necessário
    pdfplumber = None


def _norm(texto):
    """
    Normaliza texto para comparação (maiúsculas + forma Unicode NFC).

    Necessário porque letras acentuadas (ç, ã, â, é...) podem chegar em duas
    formas visualmente idênticas mas byte-a-byte diferentes: composta (NFC —
    como o Excel normalmente grava) ou decomposta (NFD — como alguns
    navegadores/teclados enviam ao digitar). Sem normalizar, "Data Posição"
    digitado na interface pode não bater com "Data Posição" lido do arquivo,
    mesmo parecendo idêntico na tela.
    """
    if texto is None:
        return ""
    return unicodedata.normalize("NFC", str(texto)).strip().upper()


# ============================================================
# Utilitários gerais
# ============================================================

def formatar_brasileiro(valor):
    """Converte qualquer número para padrão BR: 11470.478 -> 11.470,478"""
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return valor
    try:
        from decimal import Decimal

        texto = str(valor).strip()
        texto = texto.replace("R$", "").replace(" ", "")
        if texto == "":
            return texto
        numero = (
            float(texto.replace(".", "").replace(",", "."))
            if texto.count(",") == 1 and texto.count(".") > 0
            else float(texto.replace(",", "."))
        )

        # Usa Decimal(str(numero)) em vez de formatar o float diretamente:
        # str(float) já dá a representação mais curta e fiel (o mesmo que
        # Python mostra no repr), evitando ruído de ponto flutuante do tipo
        # 52849440.33 -> "...3299999982" que apareceria com f"{numero:.10f}".
        d = Decimal(str(numero))
        sinal = "-" if d < 0 else ""
        d = abs(d)
        parte_inteira, _, parte_decimal = format(d, "f").partition(".")
        parte_inteira_formatada = f"{int(parte_inteira):,}".replace(",", ".")
        parte_decimal = parte_decimal.rstrip("0")

        if parte_decimal:
            return f"{sinal}{parte_inteira_formatada},{parte_decimal}"
        return f"{sinal}{parte_inteira_formatada}"
    except Exception:
        return valor


def carregar_processados(caminho_log):
    if os.path.exists(caminho_log):
        with open(caminho_log, "r", encoding="utf-8") as f:
            return set(l.strip() for l in f.readlines() if l.strip())
    return set()


def marcar_processado(caminho_log, arquivo):
    with open(caminho_log, "a", encoding="utf-8") as f:
        f.write(arquivo + "\n")


def listar_candidatos(pasta, padrao):
    return sorted(glob.glob(os.path.join(pasta, padrao)))


# ============================================================
# Resolução de valores de coluna de saída (origem / fixo / calculo / metadado)
# ============================================================

def _resolver_valor_coluna(especificacao, row, metadados):
    """
    especificacao pode ser:
      {"origem": "NomeColuna"}
      {"valor_fixo": "texto ou numero"}
      {"de_metadado": "nome_metadado"}
      {"calculo": "ColA / ColB"}   -> só suporta * / + - simples entre 2 colunas
    """
    if "origem" in especificacao:
        col = especificacao["origem"]
        return row.get(col) if hasattr(row, "get") else row[col]

    if "valor_fixo" in especificacao:
        return especificacao["valor_fixo"]

    if "de_metadado" in especificacao:
        return metadados.get(especificacao["de_metadado"])

    if "calculo" in especificacao:
        expr = especificacao["calculo"]
        m = re.match(r"\s*([\w çãõéíóúâêôàü.%-]+)\s*([+\-*/])\s*([\w çãõéíóúâêôàü.%-]+)\s*", expr, re.IGNORECASE)
        if not m:
            return None
        col_a, op, col_b = m.groups()
        try:
            a = float(row[col_a])
            b = float(row[col_b])
        except Exception:
            return None
        return {"+": a + b, "-": a - b, "*": a * b, "/": a / b if b else None}[op]

    return None


def _buscar_valor_unico_excel(caminho, aba, campo, orientacao=None):
    """Busca um único valor (metadado) numa aba. Se `orientacao` for
    informado ('horizontal' ou 'vertical'), usa exatamente esse layout;
    caso contrário tenta os dois automaticamente."""
    resultado = ler_excel_metadados_auto(caminho, {"aba": aba, "orientacao": orientacao}, [campo])
    return resultado.get(campo)


def ler_excel_vertical(caminho, leitura_cfg):
    """
    Lê uma aba 'virada' (transposta): os RÓTULOS dos campos ficam na
    primeira coluna (de cima pra baixo), e cada COLUNA seguinte é um
    registro do resultado (um valor por linha de rótulo):

        Ordem       88          89          90
        Mnemônico   SNR_A05..   SNR_A04..   SNR_A03..
        Qtde. Total 415.710,90  48.995,77   27.035,89

    vira 3 linhas no resultado (uma para 88, uma para 89, uma para 90),
    cada uma com "Ordem" e "Mnemônico" como colunas.
    """
    aba = leitura_cfg.get("aba")
    df_bruto = pd.read_excel(caminho, sheet_name=aba, header=None)
    if df_bruto.empty or df_bruto.shape[1] < 2:
        return pd.DataFrame()

    rotulos = df_bruto.iloc[:, 0].fillna("").astype(str).str.strip().tolist()
    colunas_dados = df_bruto.iloc[:, 1:]
    registros = colunas_dados.transpose()
    registros.columns = rotulos
    registros = registros.reset_index(drop=True)
    return registros


def _ler_tabela_xlsx(caminho, aba, orientacao):
    """Lê uma aba inteira como tabela (várias linhas), no layout pedido —
    usado para colunas não-fixas que vêm de uma aba diferente da principal."""
    if orientacao == "vertical":
        return ler_excel_vertical(caminho, {"aba": aba})
    return ler_excel_direto(caminho, {"aba": aba})


def aplicar_mapeamento_xlsx(caminho, regra, df_principal, metadados=None):
    """
    Versão do mapeamento usada para regras de Excel: cada coluna do
    mapeamento diz explicitamente de qual ABA ela vem, se está na
    HORIZONTAL ou VERTICAL, e se é um valor FIXO (procurado uma vez e
    repetido em todas as linhas) ou não (lido registro a registro).

      {"aba": "Patrimonio", "orientacao": "horizontal", "origem": "Mnemônico", "fixo": false}
      {"aba": "Fundo",      "orientacao": "vertical",   "origem": "CNPJ",      "fixo": true}

    As três informações são obrigatórias — evita o erro mais comum: esquecer
    de configurar a aba/orientação certa e o dado simplesmente não aparecer.
    Mantém compatibilidade com o formato antigo (valor_fixo / calculo /
    de_metadado + fontes_auxiliares) para regras já existentes.
    """
    metadados = metadados or {}
    mapeamento = regra["colunas_saida"]
    leitura_principal = regra.get("leitura") or {}
    aba_principal = leitura_principal.get("aba")
    orientacao_principal = leitura_principal.get("orientacao", "horizontal")
    saida = pd.DataFrame(index=df_principal.index)
    cache_fixos = {}
    cache_tabelas = {}

    for col_saida, especificacao in mapeamento.items():
        if "valor_fixo" in especificacao:
            saida[col_saida] = especificacao["valor_fixo"]
            continue

        if "de_metadado" in especificacao:
            # compatibilidade com regras antigas que usavam fontes_auxiliares
            saida[col_saida] = metadados.get(especificacao["de_metadado"])
            continue

        if "calculo" in especificacao:
            expr = especificacao["calculo"]
            m = re.match(r"\s*(.+?)\s*([+\-*/])\s*(.+?)\s*$", expr)
            if m:
                col_a, op, col_b = m.groups()
                try:
                    a = pd.to_numeric(df_principal[col_a], errors="coerce")
                    b = pd.to_numeric(df_principal[col_b], errors="coerce")
                    saida[col_saida] = {
                        "+": a + b, "-": a - b, "*": a * b, "/": a / b
                    }[op]
                except Exception:
                    saida[col_saida] = None
            continue

        if "origem" in especificacao:
            origem = especificacao["origem"]

            if "aba" not in especificacao:
                raise ValueError(
                    f"Coluna '{col_saida}': é obrigatório informar de qual aba ela "
                    f"vem ('aba')."
                )
            aba = especificacao["aba"]

            if "orientacao" not in especificacao:
                raise ValueError(
                    f"Coluna '{col_saida}': é obrigatório informar se a aba '{aba}' "
                    f"está na horizontal ou vertical ('orientacao': 'horizontal' ou "
                    f"'vertical')."
                )
            orientacao = especificacao["orientacao"]
            if orientacao not in ("horizontal", "vertical"):
                raise ValueError(
                    f"Coluna '{col_saida}': 'orientacao' precisa ser 'horizontal' "
                    f"ou 'vertical' (veio '{orientacao}')."
                )

            if "fixo" not in especificacao:
                raise ValueError(
                    f"Coluna '{col_saida}': é obrigatório dizer se o valor é fixo "
                    f"('fixo': true — só existe um valor, repetido em todas as "
                    f"linhas) ou não ('fixo': false — um valor por registro)."
                )
            fixo = especificacao["fixo"]

            if fixo:
                chave = (aba, origem, orientacao)
                if chave not in cache_fixos:
                    cache_fixos[chave] = _buscar_valor_unico_excel(caminho, aba, origem, orientacao)
                saida[col_saida] = cache_fixos[chave]

            elif aba == aba_principal and orientacao == orientacao_principal:
                saida[col_saida] = df_principal[origem] if origem in df_principal.columns else None

            else:
                chave_tabela = (aba, orientacao)
                if chave_tabela not in cache_tabelas:
                    cache_tabelas[chave_tabela] = _ler_tabela_xlsx(caminho, aba, orientacao)
                df_outro = cache_tabelas[chave_tabela]

                if origem not in df_outro.columns:
                    saida[col_saida] = None
                    continue

                if len(df_outro) != len(df_principal):
                    raise ValueError(
                        f"Coluna '{col_saida}': a aba '{aba}' tem {len(df_outro)} "
                        f"linha(s), mas a aba principal ('{aba_principal}', "
                        f"configurada em 'leitura.aba') tem {len(df_principal)} "
                        f"linha(s) — não fazem sentido juntos. Isso costuma "
                        f"significar que a aba principal está errada: ela deveria "
                        f"ser a aba com os registros de verdade (uma linha por "
                        f"resultado, ex.: '{aba}'), não uma aba de metadados com "
                        f"um valor só. Troque 'leitura.aba' (hoje "
                        f"'{aba_principal}') pela aba certa, e marque como "
                        f"'fixo': true as colunas que vêm de uma aba de "
                        f"metadados (um valor só, repetido em todas as linhas)."
                    )

                valores = df_outro[origem].reset_index(drop=True)
                saida[col_saida] = valores.values
            continue

    return saida


def aplicar_mapeamento(df, mapeamento, metadados=None):
    """Constrói o DataFrame de saída a partir do mapeamento de colunas da regra
    (usado para regras de CSV e PDF)."""
    metadados = metadados or {}
    saida = pd.DataFrame(index=df.index)
    for col_saida, especificacao in mapeamento.items():
        if "origem" in especificacao:
            col = especificacao["origem"]
            saida[col_saida] = df[col] if col in df.columns else None
        elif "valor_fixo" in especificacao:
            saida[col_saida] = especificacao["valor_fixo"]
        elif "de_metadado" in especificacao:
            saida[col_saida] = metadados.get(especificacao["de_metadado"])
        elif "calculo" in especificacao:
            expr = especificacao["calculo"]
            m = re.match(r"\s*(.+?)\s*([+\-*/])\s*(.+?)\s*$", expr)
            if m:
                col_a, op, col_b = m.groups()
                try:
                    a = pd.to_numeric(df[col_a], errors="coerce")
                    b = pd.to_numeric(df[col_b], errors="coerce")
                    saida[col_saida] = {
                        "+": a + b, "-": a - b, "*": a * b, "/": a / b
                    }[op]
                except Exception:
                    saida[col_saida] = None
    return saida


def aplicar_filtro(df, filtro):
    if not filtro:
        return df
    coluna = filtro.get("coluna")
    if coluna not in df.columns:
        return df
    if "contem" in filtro:
        termo = unicodedata.normalize("NFC", str(filtro["contem"]))
        case = filtro.get("case_sensitive", False)
        coluna_normalizada = df[coluna].astype(str).apply(
            lambda v: unicodedata.normalize("NFC", v)
        )
        return df[coluna_normalizada.str.contains(termo, case=case, na=False)]
    if "igual_a" in filtro:
        alvo = unicodedata.normalize("NFC", str(filtro["igual_a"]))
        coluna_normalizada = df[coluna].astype(str).apply(
            lambda v: unicodedata.normalize("NFC", v)
        )
        return df[coluna_normalizada == alvo]
    return df


def aplicar_normalizacao(df, normalizacao):
    if not normalizacao:
        return df
    for col in normalizacao.get("colunas_numericas_br", []):
        if col in df.columns:
            df[col] = df[col].apply(formatar_brasileiro)
    return df


# ============================================================
# Leitores por tipo de arquivo
# ============================================================

def _localizar_linha_cabecalho(df_bruto, palavras_chave):
    """Procura, em um DataFrame lido sem header, a linha que contém TODAS as
    palavras-chave informadas (case-insensitive). Retorna o índice da linha."""
    for i, row in df_bruto.iterrows():
        valores = row.astype(str).apply(_norm)
        if all(valores.str.contains(re.escape(_norm(p)), na=False).any() for p in palavras_chave):
            return i
    return None


def ler_excel_direto(caminho, leitura_cfg):
    """Cabeçalho já está na primeira linha da aba (comportamento padrão do pandas)."""
    kwargs = {}
    if leitura_cfg.get("aba"):
        kwargs["sheet_name"] = leitura_cfg["aba"]
    if leitura_cfg.get("pular_linhas"):
        kwargs["skiprows"] = leitura_cfg["pular_linhas"]
    df = pd.read_excel(caminho, **kwargs)
    df.columns = df.columns.astype(str).str.strip()
    return df


def ler_excel_busca_cabecalho(caminho, leitura_cfg):
    """
    Para abas onde o cabeçalho não está na linha 0 — pode estar em qualquer
    lugar da planilha. Lê sem header, procura a linha cujo conteúdo bate com
    `palavras_chave_cabecalho`, usa essa linha como nome de colunas e lê
    TODAS as linhas de dados a partir dali, até o fim da aba ou até encontrar
    uma linha totalmente vazia (o que vier primeiro).

    Se você souber exatamente quantas linhas de dados existem, pode informar
    `linhas_dados_apos_cabecalho` para limitar a leitura a esse número; caso
    contrário (o normal), o motor lê até acabar.
    """
    aba = leitura_cfg.get("aba")
    df_bruto = pd.read_excel(caminho, sheet_name=aba, header=None)
    palavras = leitura_cfg.get("palavras_chave_cabecalho", [])
    linha_cab = _localizar_linha_cabecalho(df_bruto, palavras)
    if linha_cab is None:
        raise ValueError(
            f"Cabeçalho não encontrado na aba '{aba}' com palavras-chave {palavras}"
        )
    colunas = df_bruto.iloc[linha_cab].fillna("").astype(str).str.strip().tolist()

    bloco_dados = df_bruto.iloc[linha_cab + 1:]
    n_linhas_dados = leitura_cfg.get("linhas_dados_apos_cabecalho")
    if n_linhas_dados is not None:
        bloco_dados = bloco_dados.iloc[:n_linhas_dados]
    else:
        # lê até a primeira linha totalmente vazia (ou até o fim da aba)
        vazia = bloco_dados.isna().all(axis=1) | (bloco_dados.astype(str).apply(
            lambda col: col.str.strip()).eq("").all(axis=1))
        if vazia.any():
            primeiro_vazio = vazia.idxmax()
            bloco_dados = bloco_dados.loc[:primeiro_vazio].iloc[:-1]

    return pd.DataFrame(bloco_dados.values, columns=colunas)


def ler_csv_busca_cabecalho(caminho, leitura_cfg):
    """
    Equivalente do 'buscar_cabecalho' do Excel, mas para CSV — para arquivos
    onde o cabeçalho não está na primeira linha (pode vir precedido de
    títulos, linhas em branco, metadados, etc.). Lê o arquivo sem assumir
    cabeçalho, procura a linha que bate com `palavras_chave_cabecalho`, usa
    essa linha como nome de colunas e lê todas as linhas de dados a partir
    dali até o fim do arquivo ou até uma linha totalmente vazia.
    """
    encoding = leitura_cfg.get("encoding", "utf-8")
    separador = leitura_cfg.get("separador", ";")
    palavras = leitura_cfg.get("palavras_chave_cabecalho", [])

    df_bruto = pd.read_csv(caminho, sep=separador, encoding=encoding, header=None, dtype=str)
    linha_cab = _localizar_linha_cabecalho(df_bruto, palavras)
    if linha_cab is None:
        raise ValueError(
            f"Cabeçalho não encontrado no CSV com palavras-chave {palavras}"
        )
    colunas = df_bruto.iloc[linha_cab].fillna("").astype(str).str.strip().tolist()

    bloco_dados = df_bruto.iloc[linha_cab + 1:]
    n_linhas_dados = leitura_cfg.get("linhas_dados_apos_cabecalho")
    if n_linhas_dados is not None:
        bloco_dados = bloco_dados.iloc[:n_linhas_dados]
    else:
        vazia = bloco_dados.isna().all(axis=1) | (bloco_dados.astype(str).apply(
            lambda col: col.str.strip()).eq("").all(axis=1))
        if vazia.any():
            primeiro_vazio = vazia.idxmax()
            bloco_dados = bloco_dados.loc[:primeiro_vazio].iloc[:-1]

    return pd.DataFrame(bloco_dados.values, columns=colunas)


def ler_excel_primeira_linha(caminho, leitura_cfg):
    """
    Lê uma aba com cabeçalho normal (linha 0) e devolve os metadados a partir
    da PRIMEIRA linha de dados, conforme `mapa_metadados`:
        {"nome_metadado": "NomeColunaNaAba"}
    """
    aba = leitura_cfg.get("aba")
    df_aux = pd.read_excel(caminho, sheet_name=aba)
    df_aux.columns = df_aux.columns.astype(str).str.strip()
    if df_aux.empty:
        return {}
    primeira = df_aux.iloc[0]
    mapa = leitura_cfg.get("mapa_metadados", {})
    return {nome_meta: primeira.get(col) for nome_meta, col in mapa.items()}


def ler_excel_chave_valor(caminho, leitura_cfg):
    """
    Lê uma aba tipo 'Parâmetros': coluna 0 = nome do campo, coluna 1 = valor.
    Retorna um dicionário {metadado: valor} de acordo com `metadados_cfg`,
    cada um casado por 'chave_contem' (substring, case-insensitive) na coluna 0.
    """
    aba = leitura_cfg.get("aba")
    df_bruto = pd.read_excel(caminho, sheet_name=aba, header=None)
    metadados_cfg = leitura_cfg.get("metadados", {})
    resultado = {}
    for _, row in df_bruto.iterrows():
        chave_texto = _norm(row[0])
        for nome_meta, regra_meta in metadados_cfg.items():
            alvo = _norm(regra_meta.get("chave_contem", ""))
            if alvo and alvo in chave_texto:
                resultado[nome_meta] = row[1]
    return resultado


def _metadados_horizontal(df_bruto, campos_desejados, upper_alvo):
    """Uma linha com todos os campos pedidos, e a linha seguinte com os
    valores nas mesmas posições:
        Fundo                Data Posição
        ANGA_SABEMI_VI_FIDC  10/08/2026
    """
    for i in range(len(df_bruto)):
        linha = df_bruto.iloc[i].fillna("").astype(str)
        upper_linha = [_norm(v) for v in linha.tolist()]
        if all(c in upper_linha for c in upper_alvo) and i + 1 < len(df_bruto):
            linha_valores = df_bruto.iloc[i + 1]
            resultado = {}
            for campo, campo_upper in zip(campos_desejados, upper_alvo):
                idx = upper_linha.index(campo_upper)
                resultado[campo] = linha_valores.iloc[idx] if idx < len(linha_valores) else None
            if any(v not in (None, "") and not pd.isna(v) for v in resultado.values()):
                return resultado
    return {}


def _metadados_vertical(df_bruto, campos_desejados, upper_alvo):
    """Cada linha é [rótulo, valor] nas colunas 0 e 1:
        Fundo         ANGA_SABEMI_VI_FIDC
        Data Posição  10/08/2026
    """
    resultado = {}
    for i in range(len(df_bruto)):
        if df_bruto.shape[1] < 2:
            break
        rotulo = _norm(df_bruto.iloc[i, 0])
        if rotulo in upper_alvo:
            campo_original = campos_desejados[upper_alvo.index(rotulo)]
            resultado[campo_original] = df_bruto.iloc[i, 1]
    return resultado


def ler_excel_metadados_auto(caminho, aux_cfg, campos_desejados):
    """
    Busca metadados (valores fixos, ex.: nome do fundo, CNPJ, data de
    posição) numa aba. Se `aux_cfg["orientacao"]` for informado
    ("horizontal" ou "vertical"), usa exatamente esse layout. Se omitido,
    tenta os dois (horizontal primeiro, depois vertical) — mantido por
    compatibilidade, mas prefira sempre informar a orientação explicitamente.

      horizontal — uma linha com os nomes dos campos, e a linha seguinte com
      os valores, na mesma posição:
          Fundo                Data Posição
          ANGA_SABEMI_VI_FIDC  10/08/2026

      vertical — cada linha é [nome do campo, valor]:
          Fundo         ANGA_SABEMI_VI_FIDC
          Data Posição  10/08/2026
    """
    aba = aux_cfg.get("aba")
    df_bruto = pd.read_excel(caminho, sheet_name=aba, header=None)
    upper_alvo = [_norm(c) for c in campos_desejados]
    if not upper_alvo or df_bruto.empty:
        return {}

    orientacao = aux_cfg.get("orientacao")
    if orientacao == "horizontal":
        return _metadados_horizontal(df_bruto, campos_desejados, upper_alvo)
    if orientacao == "vertical":
        return _metadados_vertical(df_bruto, campos_desejados, upper_alvo)

    resultado = _metadados_horizontal(df_bruto, campos_desejados, upper_alvo)
    if resultado:
        return resultado
    return _metadados_vertical(df_bruto, campos_desejados, upper_alvo)


def ler_csv_generico(caminho, leitura_cfg):
    if leitura_cfg.get("linha_aninhada"):
        return ler_csv_linha_aninhada(caminho, leitura_cfg)

    if leitura_cfg.get("modo") == "buscar_cabecalho":
        return ler_csv_busca_cabecalho(caminho, leitura_cfg)

    kwargs = dict(
        sep=leitura_cfg.get("separador", ";"),
        encoding=leitura_cfg.get("encoding", "utf-8"),
    )
    if leitura_cfg.get("decimal"):
        kwargs["decimal"] = leitura_cfg["decimal"]
    df = pd.read_csv(caminho, **kwargs)
    df.columns = df.columns.astype(str).str.strip()
    return df


def ler_csv_linha_aninhada(caminho, leitura_cfg):
    """
    Lê CSVs em que cada linha de dado foi exportada como um único campo
    'super-citado': a linha inteira vem entre aspas, e as aspas internas de
    cada valor (ex.: R$ 2.465,28) vêm escapadas em dobro.
    Exemplo de linha bruta (aspas duplicadas ao redor de cada valor):
        06/2026,FUNDO X,123,[aspas]R$ 2.465,28[aspas],[aspas]R$ 0,00[aspas]
    Aqui removemos a aspa de envelope e desfazemos o escape, então
    reprocessamos a linha como CSV de verdade.
    """
    import csv as _csv

    encoding = leitura_cfg.get("encoding", "utf-8")
    separador = leitura_cfg.get("separador", ",")

    with open(caminho, "r", encoding=encoding, newline="") as f:
        linhas_bruto = f.readlines()

    if not linhas_bruto:
        return pd.DataFrame()

    cabecalho = next(_csv.reader([linhas_bruto[0]], delimiter=separador))
    cabecalho = [c.strip() for c in cabecalho]

    registros = []
    for linha in linhas_bruto[1:]:
        linha = linha.rstrip("\r\n")
        if not linha.strip():
            continue
        if linha.startswith('"') and linha.endswith('"'):
            linha = linha[1:-1]
        linha = linha.replace('""', '"')
        campos = next(_csv.reader([linha], delimiter=separador, quotechar='"'))
        registros.append(campos)

    df = pd.DataFrame(registros)
    n = len(cabecalho)
    df = df.iloc[:, :n] if df.shape[1] >= n else df
    for i in range(df.shape[1], n):
        df[i] = None
    df.columns = cabecalho
    return df


def _extrair_texto_pdf(caminho):
    if pdfplumber is None:
        raise RuntimeError("pdfplumber não está instalado.")
    texto_completo = ""
    with pdfplumber.open(caminho) as pdf:
        for pagina in pdf.pages:
            texto = pagina.extract_text()
            if texto:
                texto_completo += texto + "\n"
    return texto_completo


def _extrair_metadados_pdf(texto_completo, linhas, metadados_regex):
    metadados = {}
    for nome_meta, regra_meta in (metadados_regex or {}).items():
        if "ancora_apos_linha" in regra_meta:
            ancora = regra_meta["ancora_apos_linha"]
            for i, linha in enumerate(linhas):
                if ancora in linha and i + 1 < len(linhas):
                    metadados[nome_meta] = linhas[i + 1].strip()
                    break
        elif "regex" in regra_meta:
            m = re.search(regra_meta["regex"], texto_completo)
            if m:
                metadados[nome_meta] = m.group(1)
    return metadados


_PADRAO_VALOR_TABELA = re.compile(r'^[\d R$,.%/+\-]+$')


def _parece_valor_de_tabela(texto):
    """Verifica se um valor capturado 'parece' um dado de tabela (número,
    data, moeda, percentual) e não texto solto (ex.: início de um rodapé/
    observação). Usado como trava para parar de puxar linhas fora do padrão."""
    texto = (texto or "").strip()
    if not texto:
        return False
    return bool(_PADRAO_VALOR_TABELA.match(texto))


def _extrair_pdf_horizontal_tabela(caminho, colunas_desejadas):
    """
    Extração estruturada (respeita as bordas/células reais da tabela do
    PDF), usada como método preferido para tabelas com grade. Evita o
    embaralhamento de colunas e o vazamento para texto fora da tabela que
    acontece ao ler o PDF como texto corrido.
    """
    if pdfplumber is None:
        raise RuntimeError("pdfplumber não está instalado.")

    upper_alvo = [_norm(c) for c in colunas_desejadas]
    if not upper_alvo:
        return None

    with pdfplumber.open(caminho) as pdf:
        for pagina in pdf.pages:
            for tabela in pagina.extract_tables():
                for i, linha in enumerate(tabela):
                    celulas = [(c or "").strip() for c in linha]
                    upper_celulas = [_norm(c) for c in celulas]
                    if not all(col in upper_celulas for col in upper_alvo):
                        continue

                    indices = {col: upper_celulas.index(col_upper)
                               for col, col_upper in zip(colunas_desejadas, upper_alvo)}
                    max_idx = max(indices.values())

                    registros = []
                    for linha_dados in tabela[i + 1:]:
                        celulas_dados = [(c or "").strip() for c in linha_dados]
                        if len(celulas_dados) <= max_idx:
                            break
                        valores = {col: celulas_dados[idx] for col, idx in indices.items()}
                        if not all(_parece_valor_de_tabela(v) for v in valores.values()):
                            break
                        registros.append(valores)
                    if registros:
                        return registros
    return None


def _extrair_pdf_horizontal(linhas, colunas_desejadas):
    """
    Cabeçalho e dados lado a lado, ex.:
        DATA COTA
        02/07/2026 1381.7883732
    Procura uma linha que contenha TODOS os nomes de coluna pedidos como
    tokens separados (a linha pode ter outras colunas além dessas — só
    usamos a posição das que interessam). As linhas seguintes que tiverem
    tokens suficientes viram registros, até a tabela acabar (ou até um
    valor sair do padrão esperado — número/data/moeda — sinalizando o fim).
    """
    registros = []
    n = len(linhas)
    upper_alvo = [_norm(c) for c in colunas_desejadas]
    i = 0
    while i < n:
        tokens = linhas[i].split()
        upper_tokens = [_norm(t) for t in tokens]
        if upper_alvo and all(col in upper_tokens for col in upper_alvo):
            indices = {col: upper_tokens.index(col_upper)
                       for col, col_upper in zip(colunas_desejadas, upper_alvo)}
            max_idx = max(indices.values())
            j = i + 1
            while j < n:
                linha_dados = linhas[j].strip()
                if not linha_dados:
                    j += 1
                    continue
                tokens_dados = linha_dados.split()
                if len(tokens_dados) <= max_idx:
                    break
                registro = {col: tokens_dados[idx] for col, idx in indices.items()}
                if not all(_parece_valor_de_tabela(v) for v in registro.values()):
                    break
                registros.append(registro)
                j += 1
            i = j
        else:
            i += 1
    return registros


def _extrair_pdf_vertical(linhas, colunas_desejadas):
    """
    Cada campo em sua própria linha, rótulo seguido do valor, ex.:
        DATA  02/07/2026
        COTA 1381.7883732
    Sempre que todos os campos pedidos forem encontrados, fecha um registro
    e começa a procurar o próximo bloco (permite várias datas no mesmo PDF).
    """
    registros = []
    atual = {}
    upper_para_original = {_norm(c): c for c in colunas_desejadas}

    for linha in linhas:
        limpa = unicodedata.normalize("NFC", linha.strip())
        if not limpa:
            continue
        for col_upper, col_original in upper_para_original.items():
            if col_original in atual:
                continue
            m = re.match(rf'^{re.escape(col_upper)}\b[\s:]*(.+)$', limpa, re.IGNORECASE)
            if m:
                atual[col_original] = m.group(1).strip()
                break
        if len(atual) == len(colunas_desejadas):
            registros.append(atual)
            atual = {}

    return registros


def _colunas_pdf_desejadas(regra):
    """Reúne, na ordem em que aparecem, os nomes de coluna que a regra espera
    encontrar no PDF: toda 'origem' usada em colunas_saida, mais a coluna do
    filtro de linhas, se houver."""
    colunas = []
    for especificacao in regra.get("colunas_saida", {}).values():
        origem = especificacao.get("origem")
        if origem and origem not in colunas:
            colunas.append(origem)
    filtro = regra.get("filtro_linhas")
    if filtro and filtro.get("coluna") and filtro["coluna"] not in colunas:
        colunas.append(filtro["coluna"])
    return colunas


def ler_pdf_auto(caminho, leitura_cfg, colunas_desejadas):
    """
    Modo padrão (sem regex manual): o usuário só informa os nomes das
    colunas (via 'origem' no mapeamento) e a orientação da tabela no PDF
    ('horizontal' ou 'vertical'); o motor localiza o cabeçalho sozinho.

    Para 'horizontal', tenta primeiro a extração estruturada de tabela
    (respeita bordas/células reais do PDF — mais confiável); se o PDF não
    tiver uma tabela com grade reconhecível, cai para a leitura por texto.
    """
    texto_completo = _extrair_texto_pdf(caminho)
    linhas = texto_completo.splitlines()
    metadados = _extrair_metadados_pdf(texto_completo, linhas, leitura_cfg.get("metadados_regex"))

    orientacao = leitura_cfg.get("orientacao", "horizontal")
    if orientacao == "vertical":
        registros = _extrair_pdf_vertical(linhas, colunas_desejadas)
    else:
        registros = _extrair_pdf_horizontal_tabela(caminho, colunas_desejadas)
        if not registros:
            registros = _extrair_pdf_horizontal(linhas, colunas_desejadas)

    df = pd.DataFrame(registros)
    return df, metadados


def ler_pdf_tabela_regex(caminho, leitura_cfg):
    """
    Modo avançado (regex manual), mantido para casos que o modo automático
    não cobre. Extrai texto do PDF inteiro e:
      - captura metadados via regex simples (uma captura cada), configurados
        em `metadados_regex`: {"nome_fundo": {"ancora_apos_linha": "..."},
                                 "data_referencia": {"regex": "..."}}
      - captura linhas de tabela cujas linhas casam com `linha_regex`
        (regex simples que serve de "essa linha é dado válido"), separando
        os valores por espaço e nomeando-os conforme `colunas_tabela`
    """
    texto_completo = _extrair_texto_pdf(caminho)
    linhas = texto_completo.splitlines()
    metadados = _extrair_metadados_pdf(texto_completo, linhas, leitura_cfg.get("metadados_regex"))

    linha_regex = leitura_cfg.get("linha_regex")
    colunas_tabela = leitura_cfg.get("colunas_tabela", [])
    dados = []
    if linha_regex:
        padrao = re.compile(linha_regex)
        for linha in linhas:
            if padrao.match(linha):
                partes = linha.split()
                registro = {}
                for i, nome_col in enumerate(colunas_tabela):
                    registro[nome_col] = partes[i] if i < len(partes) else None
                dados.append(registro)

    df = pd.DataFrame(dados)
    return df, metadados


def _campos_metadado_desejados(regra):
    """Reúne, na ordem em que aparecem, os nomes de metadado ('de_metadado')
    referenciados em colunas_saida e em linha_extra_se_metadado — usados como
    padrão quando uma fonte auxiliar não lista 'campos' explicitamente."""
    campos = []
    for especificacao in regra.get("colunas_saida", {}).values():
        nome = especificacao.get("de_metadado")
        if nome and nome not in campos:
            campos.append(nome)
    extra = regra.get("linha_extra_se_metadado")
    if extra:
        for especificacao in extra.get("colunas", {}).values():
            nome = especificacao.get("de_metadado")
            if nome and nome not in campos:
                campos.append(nome)
        if extra.get("condicao") and extra["condicao"] not in campos:
            campos.append(extra["condicao"])
    return campos


# ============================================================
# Execução de uma regra
# ============================================================

def _ler_fonte(caminho, regra):
    """Lê a fonte principal de dados de acordo com o tipo/modo da regra.
    Retorna (df, metadados)."""
    tipo = regra["tipo_arquivo"]
    leitura = regra.get("leitura", {})
    metadados = {}

    if tipo == "csv":
        df = ler_csv_generico(caminho, leitura)

    elif tipo == "xlsx":
        orientacao = leitura.get("orientacao")
        if orientacao not in ("horizontal", "vertical"):
            raise ValueError(
                "leitura: é obrigatório informar 'orientacao' ('horizontal' ou "
                "'vertical') da aba principal."
            )

        if orientacao == "vertical":
            df = ler_excel_vertical(caminho, leitura)
        else:
            modo = leitura.get("modo", "direto")
            if modo == "direto":
                df = ler_excel_direto(caminho, leitura)
            elif modo == "buscar_cabecalho":
                df = ler_excel_busca_cabecalho(caminho, leitura)
            else:
                raise ValueError(f"Modo de leitura xlsx desconhecido: {modo}")

        # Aba(s) auxiliar(es) de metadados (ex.: "Fundo", "Parâmetros")
        for aux in regra.get("fontes_auxiliares", []):
            modo_aux = aux.get("modo", "auto")
            if modo_aux == "auto":
                campos = aux.get("campos") or _campos_metadado_desejados(regra)
                metadados.update(ler_excel_metadados_auto(caminho, aux, campos))
            elif modo_aux == "chave_valor":
                metadados.update(ler_excel_chave_valor(caminho, aux))
            elif modo_aux == "buscar_cabecalho":
                df_aux = ler_excel_busca_cabecalho(caminho, aux)
                if not df_aux.empty:
                    metadados.update(df_aux.iloc[0].to_dict())
            elif modo_aux == "direto_primeira_linha":
                metadados.update(ler_excel_primeira_linha(caminho, aux))

    elif tipo == "pdf":
        modo = leitura.get("modo", "auto")
        if modo == "regex":
            df, metadados = ler_pdf_tabela_regex(caminho, leitura)
        else:
            colunas_desejadas = _colunas_pdf_desejadas(regra)
            df, metadados = ler_pdf_auto(caminho, leitura, colunas_desejadas)

    else:
        raise ValueError(f"Tipo de arquivo desconhecido: {tipo}")

    return df, metadados


def processar_regra(regra, pasta_trabalho, arquivo_log):
    """
    Executa uma regra: localiza arquivos novos que casam com o padrão,
    lê / mapeia / filtra / normaliza, e grava o CSV de saída da regra
    (acumulando com o que já existia nesse CSV).
    Retorna (dataframe_novo_ou_None, lista_arquivos_processados, mensagens).
    """
    mensagens = []
    processados_antes = carregar_processados(arquivo_log)
    padrao = regra["padrao_nome"]
    candidatos = listar_candidatos(pasta_trabalho, padrao)

    if not candidatos:
        return None, [], [f"[{regra['nome']}] nenhum arquivo casa com '{padrao}'."]

    novos = [c for c in candidatos if os.path.basename(c) not in processados_antes]
    if not novos:
        return None, [], [f"[{regra['nome']}] {len(candidatos)} arquivo(s) encontrados, todos já processados."]

    linhas_saida = []
    processados_agora = []

    for caminho in novos:
        nome_arquivo = os.path.basename(caminho)
        try:
            df_bruto, metadados = _ler_fonte(caminho, regra)

            if df_bruto is None or df_bruto.empty:
                mensagens.append(f"[{regra['nome']}] {nome_arquivo}: sem linhas de dados.")
                continue

            df_bruto = aplicar_filtro(df_bruto, regra.get("filtro_linhas"))
            if regra["tipo_arquivo"] == "xlsx":
                df_saida = aplicar_mapeamento_xlsx(caminho, regra, df_bruto, metadados)
            else:
                df_saida = aplicar_mapeamento(df_bruto, regra["colunas_saida"], metadados)

            # linha extra condicional (ex.: cota "Subordinada" calculada a partir de metadado)
            extra = regra.get("linha_extra_se_metadado")
            if extra and metadados.get(extra["condicao"]) not in (None, ""):
                linha = {}
                for col_saida, especificacao in extra["colunas"].items():
                    linha[col_saida] = _resolver_valor_coluna(especificacao, {}, metadados)
                df_saida = pd.concat([df_saida, pd.DataFrame([linha])], ignore_index=True)

            df_saida = aplicar_normalizacao(df_saida, regra.get("normalizacao"))

            linhas_saida.append(df_saida)
            processados_agora.append(nome_arquivo)
            mensagens.append(f"[{regra['nome']}] {nome_arquivo}: {len(df_saida)} linha(s) extraídas.")

        except Exception as e:
            mensagens.append(f"[{regra['nome']}] ERRO em {nome_arquivo}: {e}")

    for nome_arquivo in processados_agora:
        marcar_processado(arquivo_log, nome_arquivo)

    if not linhas_saida:
        return None, [], mensagens

    df_novo = pd.concat(linhas_saida, ignore_index=True)

    # acumula no CSV de saída da própria regra
    caminho_saida = os.path.join(pasta_trabalho, regra["saida_csv"])
    if os.path.exists(caminho_saida):
        try:
            df_existente = pd.read_csv(caminho_saida, sep=";", encoding="utf-8-sig")
            df_total = pd.concat([df_existente, df_novo], ignore_index=True)
        except Exception:
            df_total = df_novo
    else:
        df_total = df_novo

    df_total.to_csv(caminho_saida, sep=";", index=False, encoding="utf-8-sig")

    return df_novo, processados_agora, mensagens


def processar_grupo(regras_grupo, pasta_trabalho, arquivo_log):
    """
    Um 'grupo' representa fontes alternativas para o mesmo dado final
    (ex.: tenta CSV de fechamento; se esse padrão não existir, usa o Excel
    de composição). A primeira regra do grupo cujo padrão encontrar QUALQUER
    arquivo (novo ou já processado) é a usada — replica o comportamento
    original de fallback.
    """
    for regra in regras_grupo:
        candidatos = listar_candidatos(pasta_trabalho, regra["padrao_nome"])
        if candidatos:
            return processar_regra(regra, pasta_trabalho, arquivo_log)
    return None, [], [f"Nenhuma fonte do grupo encontrou arquivos: "
                       f"{[r['padrao_nome'] for r in regras_grupo]}"]


# ============================================================
# Arquivos de saída / limpeza
# ============================================================

def limpar_arquivos(rules_config, pasta_trabalho, arquivo_log):
    manter_fixo = {"app.py", "engine.py", "rules.json", "ja_processados.txt"}
    todas_regras = []
    for item in rules_config.get("regras", []):
        if item.get("tipo") == "grupo":
            todas_regras.extend(item["regras"])
        else:
            todas_regras.append(item)

    manter_fixo.update(r["saida_csv"] for r in todas_regras)
    padroes_validos = [r["padrao_nome"] for r in todas_regras]
    processados = carregar_processados(arquivo_log)

    removidos = []
    for arquivo in os.listdir(pasta_trabalho):
        if arquivo in manter_fixo or arquivo.endswith(".py") or arquivo.endswith(".json"):
            continue
        caminho = os.path.join(pasta_trabalho, arquivo)
        if not os.path.isfile(caminho):
            continue

        elegivel = any(fnmatch.fnmatch(arquivo, p) for p in padroes_validos)
        if not elegivel:
            try:
                os.remove(caminho)
                removidos.append(f"{arquivo} (não elegível)")
            except Exception:
                pass
            continue

        if arquivo in processados:
            try:
                os.remove(caminho)
                removidos.append(f"{arquivo} (já processado)")
            except Exception:
                pass

    return removidos


def listar_arquivos_saida(rules_config, pasta_trabalho):
    """Lista, sem duplicar, os nomes de 'saida_csv' de todas as regras
    (ativas ou não) que já existem em disco — são os arquivos finais que o
    usuário baixa. Cada regra é dona só do seu próprio 'saida_csv'; não há
    mais nenhum arquivo consolidado adicional criado por cima."""
    vistos = []
    todas_regras = []
    for item in rules_config.get("regras", []):
        if item.get("tipo") == "grupo":
            todas_regras.extend(item["regras"])
        else:
            todas_regras.append(item)

    for regra in todas_regras:
        nome = regra.get("saida_csv")
        if nome and nome not in vistos and os.path.exists(os.path.join(pasta_trabalho, nome)):
            vistos.append(nome)
    return vistos


# ============================================================
# Triagem automática: pastas "rejeitados" e "apagar"
# ============================================================
#
#   workspace/
#     (raiz)       arquivos que casam com alguma regra e ainda NÃO foram
#                  processados — a fila de trabalho
#     rejeitados/  nome fora do padrão de TODAS as regras
#     apagar/      nome já listado em ja_processados.txt
#
# A separação é automática (chamada no upload, ao listar e após processar),
# para a raiz ficar sempre limpa. Nada é apagado sozinho — só pelos botões
# (ou pela opção "limpar ao final" do processamento).

PASTA_REJEITADOS = "rejeitados"
PASTA_APAGAR = "apagar"


def _todas_as_regras(rules_config):
    todas = []
    for item in rules_config.get("regras", []):
        if item.get("tipo") == "grupo":
            todas.extend(item["regras"])
        else:
            todas.append(item)
    return todas


def garantir_subpastas(pasta_trabalho):
    for sub in (PASTA_REJEITADOS, PASTA_APAGAR):
        os.makedirs(os.path.join(pasta_trabalho, sub), exist_ok=True)


def listar_subpasta(pasta_trabalho, subpasta):
    pasta = os.path.join(pasta_trabalho, subpasta)
    if not os.path.isdir(pasta):
        return []
    return sorted(f for f in os.listdir(pasta) if os.path.isfile(os.path.join(pasta, f)))


def organizar_workspace(rules_config, pasta_trabalho, arquivo_log="ja_processados.txt"):
    """
    Separa os arquivos da raiz do workspace:
      - nome em ja_processados.txt            -> apagar/
      - nome que não casa com NENHUM padrão   -> rejeitados/
    Também devolve para a raiz os arquivos de rejeitados/ que passaram a
    casar com algum padrão (ex.: você criou a regra depois do upload).
    Regras inativas contam: o padrão delas continua sendo "reconhecido".
    Retorna {"apagar": [...], "rejeitados": [...], "recuperados": [...]}.
    """
    garantir_subpastas(pasta_trabalho)
    regras = _todas_as_regras(rules_config)
    padroes = [r["padrao_nome"] for r in regras if r.get("padrao_nome")]
    saidas = {r["saida_csv"] for r in regras if r.get("saida_csv")}
    processados = carregar_processados(os.path.join(pasta_trabalho, arquivo_log))

    def casa_padrao(nome):
        return any(fnmatch.fnmatch(nome, p) for p in padroes)

    movidos = {"apagar": [], "rejeitados": [], "recuperados": []}

    # 1) resgata de rejeitados/ o que agora tem regra
    pasta_rej = os.path.join(pasta_trabalho, PASTA_REJEITADOS)
    for nome in listar_subpasta(pasta_trabalho, PASTA_REJEITADOS):
        if nome not in processados and casa_padrao(nome):
            try:
                os.replace(os.path.join(pasta_rej, nome), os.path.join(pasta_trabalho, nome))
                movidos["recuperados"].append(nome)
            except OSError:
                pass

    # 2) tria a raiz
    for nome in sorted(os.listdir(pasta_trabalho)):
        caminho = os.path.join(pasta_trabalho, nome)
        if not os.path.isfile(caminho) or nome == arquivo_log or nome in saidas:
            continue
        if nome in processados:
            destino, chave = PASTA_APAGAR, "apagar"
        elif not casa_padrao(nome):
            destino, chave = PASTA_REJEITADOS, "rejeitados"
        else:
            continue
        try:
            os.replace(caminho, os.path.join(pasta_trabalho, destino, nome))
            movidos[chave].append(nome)
        except OSError:
            pass

    return movidos


def apagar_pasta(pasta_trabalho, subpasta):
    """Apaga todos os arquivos de 'rejeitados' ou 'apagar'. Não mexe em
    ja_processados.txt (ele precisa continuar lembrando o que já foi lido,
    senão um novo upload do mesmo nome seria processado em duplicidade)."""
    if subpasta not in (PASTA_REJEITADOS, PASTA_APAGAR):
        raise ValueError(f"Pasta inválida: {subpasta}")
    pasta = os.path.join(pasta_trabalho, subpasta)
    removidos = []
    for nome in listar_subpasta(pasta_trabalho, subpasta):
        try:
            os.remove(os.path.join(pasta, nome))
            removidos.append(nome)
        except OSError:
            pass
    return removidos


# ============================================================
# Execução completa
# ============================================================

def executar_tudo(rules_config, pasta_trabalho, arquivo_log="ja_processados.txt",
                   limpar=False):
    """
    Processa todas as regras ativas. Cada regra grava (e acumula) só no seu
    próprio 'saida_csv' — não existe mais nenhum arquivo consolidado
    adicional; o arquivo final é sempre o que foi configurado em
    'Nome do CSV de saída desta regra'.

    Retorna (sucesso, log_geral, arquivos_saida) onde arquivos_saida é a
    lista dos nomes de saida_csv que existem em disco após o processamento.
    """
    arquivo_log_caminho = os.path.join(pasta_trabalho, arquivo_log)
    log_geral = []

    for item in rules_config.get("regras", []):
        if not item.get("ativo", True):
            continue
        if item.get("tipo") == "grupo":
            _, _, msgs = processar_grupo(item["regras"], pasta_trabalho, arquivo_log_caminho)
        else:
            _, _, msgs = processar_regra(item, pasta_trabalho, arquivo_log_caminho)
        log_geral.extend(msgs)

    arquivos_saida = listar_arquivos_saida(rules_config, pasta_trabalho)
    sucesso = len(arquivos_saida) > 0

    if not log_geral:
        log_geral.append("Nenhuma regra ativa processou arquivos.")

    # triagem automática: processados -> apagar/, fora do padrão -> rejeitados/
    movidos = organizar_workspace(rules_config, pasta_trabalho, arquivo_log)
    if movidos["apagar"]:
        log_geral.append(
            f"{len(movidos['apagar'])} arquivo(s) já processados movidos para '{PASTA_APAGAR}/'."
        )
    if movidos["rejeitados"]:
        log_geral.append(
            f"{len(movidos['rejeitados'])} arquivo(s) fora do padrão movidos para '{PASTA_REJEITADOS}/'."
        )

    if sucesso and limpar:
        removidos = apagar_pasta(pasta_trabalho, PASTA_APAGAR)
        log_geral.extend(f"Removido: {r} (já processado)" for r in removidos)

    return sucesso, log_geral, arquivos_saida


def carregar_regras(caminho_json):
    with open(caminho_json, "r", encoding="utf-8") as f:
        return json.load(f)


def salvar_regras(caminho_json, rules_config):
    with open(caminho_json, "w", encoding="utf-8") as f:
        json.dump(rules_config, f, ensure_ascii=False, indent=2)
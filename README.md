# AutoForge

Motor genérico e configurável de extração e normalização de dados
(CSV, Excel e PDF). Cada regra tem seu próprio arquivo de saída — não existe
nenhum arquivo consolidado adicional criado por cima: o resultado final é
sempre exatamente o que você configurou em "Nome do CSV de saída desta
regra".

Diferente de uma função Python por fundo/arquivo, aqui cada fonte de dados é
descrita por uma **regra** — um pequeno JSON que diz onde procurar o
arquivo, como ler a tabela, como mapear as colunas de saída e como
filtrar/normalizar os valores. Novas fontes não exigem escrever código:
basta cadastrar uma nova regra pela interface.

## Como rodar

```bash
pip install -r requirements.txt
python app.py
```

Isso abre automaticamente `http://127.0.0.1:5050` no navegador.

## Fluxo de uso

1. **Arquivos** — arraste os CSVs/Excels/PDFs para a zona de upload (ou
   clique para escolher). Eles ficam salvos em `workspace/`.
   A separação é **automática**, para a lista não ficar poluída:
   - **Fila para processar** — arquivos cujo nome casa com alguma regra e
     que ainda não foram lidos.
   - **Rejeitados** (`workspace/rejeitados/`) — nome fora do padrão de
     qualquer regra. Se você criar a regra depois, eles voltam sozinhos
     para a fila.
   - **Já processados** (`workspace/apagar/`) — nomes que constam em
     `ja_processados.txt`.
   Os botões **Apagar rejeitados** e **Apagar já processados** esvaziam
   essas pastas (o `ja_processados.txt` é mantido, para não reprocessar um
   arquivo reenviado). Todas as listas têm barra de rolagem.
2. **Regras** — a lista já vem com as 4 fontes originais recriadas como
   regras genéricas (BACH, SOMACRED II, SOMACRED SABEMI III em PDF, ANGA
   SABEMI VI). Você pode ativar/desativar, editar o JSON de qualquer regra
   inline, excluir, ou clicar em **+ Nova regra** para cadastrar uma fonte
   nova pelo formulário.
3. **Processar** — roda todas as regras ativas sobre os arquivos que ainda
   não foram processados (o app lembra o que já processou, então rodar de
   novo não duplica linhas). Opcionalmente, marque para apagar os arquivos
   de origem já processados ao final.
4. **Arquivos de saída** — um card por regra, cada um com o nome exato
   configurado em "Nome do CSV de saída desta regra", prévia das últimas
   linhas na tela e botão próprio de download. Duas regras que apontam para
   o mesmo nome de saída acumulam no mesmo arquivo (linha a linha, sem
   deduplicação). Você também pode baixar o app inteiro em `.zip`.

## Estrutura dos arquivos

```
app.py             servidor local (Flask) e rotas
engine.py           motor genérico de extração/normalização
rules.json          suas regras (editáveis pela interface ou na mão)
rules_default.json  regras originais de fábrica (referência/backup)
templates/          página HTML
static/             CSS e JS
workspace/           fila de arquivos + o CSV de saída de cada regra
  rejeitados/        arquivos fora do padrão de qualquer regra (automático)
  apagar/            arquivos já processados (automático)
```

## Anatomia de uma regra

```json
{
  "nome": "Fundo XPTO — posição mensal",
  "ativo": true,
  "tipo_arquivo": "xlsx",
  "padrao_nome": "FIDC_XPTO_*.xlsx",
  "leitura": { "modo": "direto", "aba": "Patrimonio", "orientacao": "horizontal" },
  "colunas_saida": {
    "Classe Cota": { "aba": "Patrimonio", "orientacao": "horizontal", "origem": "Classe Cota", "fixo": false },
    "Valor Cota":  { "aba": "Patrimonio", "orientacao": "horizontal", "origem": "Valor Cota",  "fixo": false },
    "Fundo":        { "aba": "Fundo", "orientacao": "horizontal", "origem": "Fundo",        "fixo": true },
    "Data Posição": { "aba": "Fundo", "orientacao": "horizontal", "origem": "Data Posição", "fixo": true }
  },
  "normalizacao": { "colunas_numericas_br": ["Valor Cota"] },
  "saida_csv": "FIDC_XPTO.csv"
}
```

`saida_csv` é sempre o arquivo final — não há nenhuma etapa extra que
combina o resultado de várias regras num único arquivo. Se duas regras
usarem o mesmo `saida_csv`, os resultados de ambas se acumulam nele.

### Tipos de leitura (`tipo_arquivo` / `leitura.modo`)

- `csv` — `separador`, `encoding`, `decimal`. Se o arquivo vier com cada
  linha "super-citada" (aspas duplicadas ao redor de cada valor), marque
  `"linha_aninhada": true`.
- `csv` + `modo: "buscar_cabecalho"` — para quando o cabeçalho **não está
  na primeira linha** do arquivo. Informe `palavras_chave_cabecalho` (ex.:
  `["Ordem", "Mnemônico"]`); o motor procura a linha que contém essas
  palavras em qualquer lugar do arquivo e lê **todos** os dados a partir
  dali, até o fim do arquivo ou até achar uma linha totalmente vazia.
- `xlsx` + `modo: "direto"` — cabeçalho já está na linha 0 da aba
- `xlsx` + `modo: "buscar_cabecalho"` — mesma ideia do CSV, mas para Excel:
  o cabeçalho pode estar **em qualquer lugar da planilha**. Lê todos os
  dados a partir da linha seguinte ao cabeçalho encontrado, até o fim da
  aba ou até uma linha vazia. Para limitar manualmente, informe
  `linhas_dados_apos_cabecalho`.
- `pdf` + `modo: "auto"` (padrão) — **não precisa de regex.** Basta dizer a
  `orientacao` da tabela no PDF ("horizontal": cabeçalho e dados lado a
  lado; "vertical": cada campo em sua própria linha). O motor descobre
  sozinho onde está o rótulo, usando os nomes em `colunas_saida` como
  `origem` — esse nome tem que ser igual, letra por letra, ao rótulo que
  aparece de fato no PDF.
- `pdf` + `modo: "regex"` — modo avançado, para PDFs fora do padrão
  automático (`linha_regex`, `colunas_tabela`, `metadados_regex`).

### Colunas de Excel: aba + orientação + fixo (obrigatório)

Para regras de Excel, **cada coluna em `colunas_saida` diz explicitamente
de qual aba ela vem (`aba`), se essa aba está na horizontal ou vertical
(`orientacao`), e se é um valor fixo (`fixo`)**. As três informações são
obrigatórias de propósito: deixar isso implícito era a causa mais comum de
"o dado esperado não aparece no resultado" ou vem errado/truncado.

- **`fixo: false`** — a aba tem **vários registros** (um por linha).
- **`fixo: true`** — a aba tem **um valor só** que se repete em todas as
  linhas do resultado (ex.: Carteira, CNPJ, Fundo, Data Posição).
- **`orientacao: "horizontal"`** — cabeçalho/rótulos numa linha, dados ao
  lado ou embaixo.
- **`orientacao: "vertical"`** — cabeçalho/rótulos numa coluna, dados na(s)
  coluna(s) ao lado. Para `fixo: false`, vertical também cobre tabelas
  "viradas" (transpostas) — cada rótulo é uma linha, e cada coluna seguinte
  é um registro.
- Uma coluna não-fixa pode vir de uma aba **diferente** da aba principal
  (`leitura.aba`) — o motor lê essa outra aba separadamente e alinha os
  valores pela posição. **Atenção:** a aba principal (`leitura.aba`)
  precisa ser a aba com os registros de verdade — se ela tiver menos linhas
  que uma aba secundária não-fixa, a regra falha com um erro explicando o
  descompasso, em vez de truncar silenciosamente. Se o dado que você quer
  repetir em todas as linhas (ex.: nome do fundo) está numa aba com um
  valor só, marque essa coluna como `"fixo": true` — não tente usá-la como
  aba principal.
- Esquecer `aba`, `orientacao` ou `fixo` numa coluna faz a regra falhar com
  uma mensagem clara no log, em vez de silenciosamente não trazer o dado.

Formatos antigos (`fontes_auxiliares` + `de_metadado`, `valor_fixo`,
`calculo`) continuam funcionando para regras já existentes, mas prefira o
formato `aba`/`orientacao`/`fixo` acima em regras novas de Excel.

Veja `rules_default.json` para exemplos completos e funcionais.
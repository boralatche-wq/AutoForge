// ============================================================
// AutoForge — frontend
// ============================================================

const dropzone = document.getElementById('dropzone');
const inputArquivos = document.getElementById('input-arquivos');
const listaArquivos = document.getElementById('lista-arquivos');

// ---------------- Upload / drag & drop ----------------

dropzone.addEventListener('click', () => inputArquivos.click());

inputArquivos.addEventListener('change', () => {
  enviarArquivos(inputArquivos.files);
});

['dragenter', 'dragover'].forEach(evt =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.add('dragover');
  })
);
['dragleave', 'drop'].forEach(evt =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.remove('dragover');
  })
);
dropzone.addEventListener('drop', (e) => {
  const arquivos = e.dataTransfer.files;
  if (arquivos.length) enviarArquivos(arquivos);
});

async function enviarArquivos(fileList) {
  const formData = new FormData();
  for (const f of fileList) formData.append('arquivos', f);
  const resp = await fetch('/upload', { method: 'POST', body: formData });
  const data = await resp.json();
  if (data.ok) {
    renderEstado(data.estado);
    avisarTriagem(data.estado.movidos);
  }
  inputArquivos.value = '';
}

// ---------------- Listas: fila / rejeitados / já processados ----------------

const listaRejeitados = document.getElementById('lista-rejeitados');
const listaApagar = document.getElementById('lista-apagar');
const btnApagarRejeitados = document.getElementById('btn-apagar-rejeitados');
const btnApagarProcessados = document.getElementById('btn-apagar-processados');
const avisoTriagem = document.getElementById('aviso-triagem');
let avisoTimer = null;

function esc(texto) {
  return String(texto).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function renderEstado(estado) {
  // sem espaços entre os <li>, para o CSS :empty mostrar "nenhum arquivo"
  listaArquivos.innerHTML = estado.arquivos.map(n =>
    `<li data-nome="${esc(n)}"><span class="file-name">${esc(n)}</span>` +
    `<button class="file-remove" data-nome="${esc(n)}" title="Remover">×</button></li>`
  ).join('');
  listaRejeitados.innerHTML = estado.rejeitados.map(n =>
    `<li><span class="file-name">${esc(n)}</span></li>`).join('');
  listaApagar.innerHTML = estado.apagar.map(n =>
    `<li><span class="file-name">${esc(n)}</span></li>`).join('');

  document.getElementById('cnt-arquivos').textContent = estado.arquivos.length;
  document.getElementById('cnt-rejeitados').textContent = estado.rejeitados.length;
  document.getElementById('cnt-apagar').textContent = estado.apagar.length;
  btnApagarRejeitados.disabled = estado.rejeitados.length === 0;
  btnApagarProcessados.disabled = estado.apagar.length === 0;
}

function avisarTriagem(movidos) {
  if (!movidos) return;
  const partes = [];
  if (movidos.rejeitados && movidos.rejeitados.length)
    partes.push(`${movidos.rejeitados.length} arquivo(s) fora do padrão foram para Rejeitados`);
  if (movidos.apagar && movidos.apagar.length)
    partes.push(`${movidos.apagar.length} já processado(s) foram para Já processados`);
  if (movidos.recuperados && movidos.recuperados.length)
    partes.push(`${movidos.recuperados.length} voltaram para a fila (agora há regra para eles)`);
  if (!partes.length) return;
  avisoTriagem.textContent = partes.join(' · ');
  avisoTriagem.classList.remove('hidden');
  clearTimeout(avisoTimer);
  avisoTimer = setTimeout(() => avisoTriagem.classList.add('hidden'), 8000);
}

listaArquivos.addEventListener('click', async (e) => {
  if (!e.target.classList.contains('file-remove')) return;
  const nome = e.target.dataset.nome;
  await fetch(`/api/arquivos/${encodeURIComponent(nome)}`, { method: 'DELETE' });
  const resp = await fetch('/api/arquivos');
  renderEstado(await resp.json());
});

async function apagarPasta(url, mensagem) {
  if (!confirm(mensagem)) return;
  const resp = await fetch(url, { method: 'POST' });
  const data = await resp.json();
  if (data.ok) renderEstado(data.estado);
}

btnApagarRejeitados.addEventListener('click', () => apagarPasta(
  '/api/limpar/rejeitados',
  'Apagar definitivamente todos os arquivos rejeitados?'
));

btnApagarProcessados.addEventListener('click', () => apagarPasta(
  '/api/limpar/processados',
  'Apagar definitivamente todos os arquivos já processados? (os CSVs de saída não são afetados)'
));

// ---------------- Regras: toggle / editar / excluir ----------------

const listaRegras = document.getElementById('lista-regras');

listaRegras.addEventListener('change', async (e) => {
  if (!e.target.classList.contains('rule-toggle')) return;
  const nome = e.target.dataset.nome;
  await fetch(`/api/rules/${encodeURIComponent(nome)}/toggle`, { method: 'POST' });
  e.target.closest('.rule-card').classList.toggle('rule-off');
});

listaRegras.addEventListener('click', async (e) => {
  if (e.target.classList.contains('btn-editar-regra')) {
    const card = e.target.closest('.rule-card');
    const textarea = card.querySelector('.rule-json');
    const jaAberto = !textarea.classList.contains('hidden');
    textarea.classList.toggle('hidden');
    e.target.textContent = jaAberto ? 'editar' : 'salvar';
    if (jaAberto) {
      try {
        const nova = JSON.parse(textarea.value);
        const nome = textarea.dataset.nome;
        await fetch(`/api/rules/${encodeURIComponent(nome)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(nova),
        });
        location.reload();
      } catch (err) {
        alert('JSON inválido: ' + err.message);
        textarea.classList.remove('hidden');
        e.target.textContent = 'salvar';
      }
    }
  }

  if (e.target.classList.contains('btn-excluir-regra')) {
    const nome = e.target.dataset.nome;
    if (!confirm(`Excluir a regra "${nome}"?`)) return;
    await fetch(`/api/rules/${encodeURIComponent(nome)}`, { method: 'DELETE' });
    e.target.closest('.rule-card').remove();
  }
});

// ---------------- Nova regra (form builder) ----------------

const painelNovaRegra = document.getElementById('painel-nova-regra');
const colunasMapeamento = document.getElementById('colunas-mapeamento');
const campoAba = document.getElementById('campo-aba');
const campoOrientacao = document.getElementById('campo-orientacao');
const nrTipo = document.getElementById('nr-tipo');

document.getElementById('btn-nova-regra').addEventListener('click', () => {
  painelNovaRegra.classList.remove('hidden');
  painelNovaRegra.scrollIntoView({ behavior: 'smooth', block: 'start' });
  colunasMapeamento.innerHTML = '';
  adicionarLinhaColuna();
  atualizarCamposPorTipo();
});

document.getElementById('btn-cancelar-regra').addEventListener('click', () => {
  painelNovaRegra.classList.add('hidden');
});

nrTipo.addEventListener('change', () => {
  atualizarCamposPorTipo();
  // reconstrói as linhas de coluna já criadas para refletir o novo tipo
  const quantas = colunasMapeamento.children.length || 1;
  colunasMapeamento.innerHTML = '';
  for (let i = 0; i < quantas; i++) adicionarLinhaColuna();
});

function atualizarCamposPorTipo() {
  const tipo = nrTipo.value;
  campoAba.style.display = tipo === 'xlsx' ? 'flex' : 'none';
  campoOrientacao.style.display = (tipo === 'pdf' || tipo === 'xlsx') ? 'flex' : 'none';
  document.getElementById('hint-pdf-mapeamento').style.display = tipo === 'pdf' ? 'block' : 'none';
  document.getElementById('hint-xlsx-mapeamento').style.display = tipo === 'xlsx' ? 'block' : 'none';
  document.getElementById('subpanel-cabecalho').style.display = tipo === 'pdf' ? 'none' : 'block';
}

document.getElementById('btn-add-coluna').addEventListener('click', adicionarLinhaColuna);

document.getElementById('nr-cabecalho-deslocado').addEventListener('change', (e) => {
  document.getElementById('campos-cabecalho-deslocado').style.display = e.target.checked ? 'grid' : 'none';
});

function adicionarLinhaColuna() {
  const tipo = nrTipo.value;
  const abaPrincipal = document.getElementById('nr-aba').value.trim();
  const orientacaoPrincipal = document.getElementById('nr-orientacao').value;
  const row = document.createElement('div');
  row.className = 'coluna-row' + (tipo === 'xlsx' ? ' coluna-row-xlsx' : '');

  const placeholderOrigem = tipo === 'pdf'
    ? 'rótulo exatamente como aparece no PDF (ex.: DATA, COTA)'
    : 'nome da coluna/rótulo de origem';

  const campoAbaXlsx = tipo === 'xlsx'
    ? `<input class="col-aba" type="text" placeholder="aba (obrigatório)" value="${abaPrincipal}">
       <select class="col-orientacao">
         <option value="horizontal" ${orientacaoPrincipal === 'horizontal' ? 'selected' : ''}>horizontal</option>
         <option value="vertical" ${orientacaoPrincipal === 'vertical' ? 'selected' : ''}>vertical</option>
       </select>
       <label class="col-fixo-label"><input type="checkbox" class="col-fixo"> fixa (só um valor)</label>`
    : '';

  row.innerHTML = `
    <input class="col-nome" type="text" placeholder="Nome da coluna de saída (ex.: Mnemônico)">
    <select class="col-tipo">
      <option value="origem">vem da coluna...</option>
      <option value="valor_fixo">valor fixo</option>
      <option value="calculo">cálculo (A / B, A * B...)</option>
    </select>
    <input class="col-valor" type="text" placeholder="${placeholderOrigem}">
    ${campoAbaXlsx}
    <button class="coluna-remove" title="Remover">×</button>
  `;
  colunasMapeamento.appendChild(row);
  row.querySelector('.coluna-remove').addEventListener('click', () => row.remove());

  // "valor fixo"/"cálculo" não dependem de aba específica do Excel — somem
  // com os campos de aba/orientação/fixo quando o tipo de coluna não é "origem"
  const colTipoSelect = row.querySelector('.col-tipo');
  const campoAbaEl = row.querySelector('.col-aba');
  const campoOrientacaoEl = row.querySelector('.col-orientacao');
  const campoFixoEl = row.querySelector('.col-fixo-label');
  if (colTipoSelect && (campoAbaEl || campoFixoEl)) {
    colTipoSelect.addEventListener('change', () => {
      const mostrar = colTipoSelect.value === 'origem';
      if (campoAbaEl) campoAbaEl.style.display = mostrar ? '' : 'none';
      if (campoOrientacaoEl) campoOrientacaoEl.style.display = mostrar ? '' : 'none';
      if (campoFixoEl) campoFixoEl.style.display = mostrar ? '' : 'none';
    });
  }
}

document.getElementById('btn-salvar-regra').addEventListener('click', async () => {
  const nome = document.getElementById('nr-nome').value.trim();
  const tipo = document.getElementById('nr-tipo').value;
  const padrao = document.getElementById('nr-padrao').value.trim();
  const aba = document.getElementById('nr-aba').value.trim();
  const orientacao = document.getElementById('nr-orientacao').value;
  const saida = document.getElementById('nr-saida').value.trim();

  if (!nome || !padrao || !saida) {
    alert('Preencha ao menos nome, padrão de nome e CSV de saída.');
    return;
  }
  if (tipo === 'xlsx' && !aba) {
    alert('Preencha a aba principal (Excel) — é onde ficam as linhas de dados.');
    return;
  }

  const colunas_saida = {};
  let erroColuna = null;
  colunasMapeamento.querySelectorAll('.coluna-row').forEach(row => {
    if (erroColuna) return;
    const colNome = row.querySelector('.col-nome').value.trim();
    const colTipo = row.querySelector('.col-tipo').value;
    const colValor = row.querySelector('.col-valor').value.trim();
    if (!colNome || !colValor) return;

    if (colTipo === 'origem') {
      if (tipo === 'xlsx') {
        const colAbaEl = row.querySelector('.col-aba');
        const colOrientacaoEl = row.querySelector('.col-orientacao');
        const colFixoEl = row.querySelector('.col-fixo');
        const colAba = colAbaEl ? colAbaEl.value.trim() : '';
        if (!colAba) {
          erroColuna = `A coluna "${colNome}" precisa de uma aba (obrigatório para Excel).`;
          return;
        }
        colunas_saida[colNome] = {
          aba: colAba,
          orientacao: colOrientacaoEl ? colOrientacaoEl.value : 'horizontal',
          origem: colValor,
          fixo: !!(colFixoEl && colFixoEl.checked),
        };
      } else {
        colunas_saida[colNome] = { origem: colValor };
      }
    } else if (colTipo === 'valor_fixo') {
      colunas_saida[colNome] = { valor_fixo: colValor };
    } else if (colTipo === 'calculo') {
      colunas_saida[colNome] = { calculo: colValor };
    }
  });

  if (erroColuna) {
    alert(erroColuna);
    return;
  }

  const filtroColuna = document.getElementById('nr-filtro-coluna').value.trim();
  const filtroContem = document.getElementById('nr-filtro-contem').value.trim();

  const cabecalhoDeslocado = document.getElementById('nr-cabecalho-deslocado').checked;
  const palavrasCabecalho = document.getElementById('nr-cabecalho-palavras').value
    .split(',').map(s => s.trim()).filter(Boolean);

  if (cabecalhoDeslocado && !palavrasCabecalho.length) {
    alert('Marque ao menos uma palavra-chave do cabeçalho, ou desmarque a opção "cabeçalho não está na primeira linha".');
    return;
  }

  let leitura;
  if (tipo === 'csv') {
    leitura = { separador: ';', encoding: 'utf-8' };
    if (cabecalhoDeslocado) {
      leitura.modo = 'buscar_cabecalho';
      leitura.palavras_chave_cabecalho = palavrasCabecalho;
    }
  } else if (tipo === 'xlsx') {
    leitura = { aba: aba, orientacao: orientacao };
    leitura.modo = cabecalhoDeslocado ? 'buscar_cabecalho' : 'direto';
    if (cabecalhoDeslocado) leitura.palavras_chave_cabecalho = palavrasCabecalho;
  } else {
    leitura = { modo: 'auto', orientacao: orientacao };
  }

  const regra = {
    nome,
    ativo: true,
    tipo_arquivo: tipo,
    padrao_nome: padrao,
    leitura,
    colunas_saida,
    saida_csv: saida,
  };

  if (filtroColuna && filtroContem) {
    regra.filtro_linhas = { coluna: filtroColuna, contem: filtroContem, case_sensitive: false };
  }

  const resp = await fetch('/api/rules/nova', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(regra),
  });
  const data = await resp.json();
  if (data.ok) {
    location.reload();
  } else {
    alert('Não foi possível salvar a regra.');
  }
});

// ---------------- Processar ----------------

const btnProcessar = document.getElementById('btn-processar');
const logConsole = document.getElementById('log-console');
const listaSaidas = document.getElementById('lista-saidas');
const saidasEmpty = document.getElementById('saidas-empty');

btnProcessar.addEventListener('click', async () => {
  const limpar = document.getElementById('chk-limpar').checked;
  btnProcessar.disabled = true;
  btnProcessar.querySelector('.btn-label').textContent = 'Processando...';
  logConsole.textContent = '';

  try {
    const resp = await fetch('/api/processar', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ limpar }),
    });
    const data = await resp.json();

    logConsole.textContent = data.log.join('\n');
    if (data.estado) renderEstado(data.estado);

    if (data.arquivos && data.arquivos.length) {
      montarListaSaidas(data.arquivos);
    }
  } catch (err) {
    logConsole.textContent = 'Erro ao processar: ' + err.message;
  } finally {
    btnProcessar.disabled = false;
    btnProcessar.querySelector('.btn-label').textContent = 'Processar arquivos';
  }
});

function montarListaSaidas(arquivos) {
  saidasEmpty.style.display = 'none';
  listaSaidas.innerHTML = arquivos.map(arquivoParaCardHtml).join('');
}

function arquivoParaCardHtml(arquivo) {
  const thead = `<thead><tr>${arquivo.colunas.map(c => `<th>${c}</th>`).join('')}</tr></thead>`;
  const tbody = `<tbody>${arquivo.linhas.map(linha =>
    `<tr>${linha.map(v => `<td>${v}</td>`).join('')}</tr>`
  ).join('')}</tbody>`;
  const tabela = arquivo.erro
    ? `<p class="empty-msg" style="padding:12px">Não foi possível ler este arquivo para prévia.</p>`
    : `<table class="preview-table">${thead}${tbody}</table>`;

  return `
    <div class="saida-card" data-nome="${arquivo.nome}">
      <div class="saida-card-head">
        <span class="saida-nome">${arquivo.nome}</span>
        <span class="saida-total">${arquivo.total} linha(s) — mostrando as últimas ${arquivo.linhas.length}</span>
        <a class="btn btn-mini btn-ghost" href="/download/saida/${encodeURIComponent(arquivo.nome)}">Baixar</a>
      </div>
      <div class="saida-preview-wrap">${tabela}</div>
    </div>
  `;
}
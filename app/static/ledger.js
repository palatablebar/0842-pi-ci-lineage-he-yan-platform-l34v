(function () {
  var $ = function (id) { return document.getElementById(id); };
  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }
  function setNotice(text) { $('notice').textContent = text || ''; }
  async function request(url, options) {
    var response = await fetch(url, Object.assign({ headers: { 'Content-Type': 'application/json' } }, options || {}));
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
  }
  function parseLines(text) {
    return text.split(';').map(function (edge) { return edge.trim(); }).filter(Boolean).map(function (edge) {
      var parts = edge.split(':');
      if (parts.length !== 2) throw new Error('明细格式应为 父批:数量，例如 L1:200;L5:40');
      var qty = Number(parts[1].trim());
      if (!Number.isInteger(qty) || qty <= 0) throw new Error('数量必须是正整数：' + edge);
      return { lot_id: parts[0].trim(), qty: qty };
    });
  }
  function renderLots(data) {
    $('ledger-shortage').textContent = data.shortage_lots.length
      ? '缺口父批：' + data.shortage_lots.join(', ') : '无缺口';
    $('ledger-lots').innerHTML = data.lots.length ? data.lots.map(function (lot) {
      return '<tr' + (lot.in_shortage ? ' class="row-shortage"' : '') + '>' +
        '<td>' + esc(lot.lot_id) + '</td>' +
        '<td class="num">' + lot.actual_qty + '</td>' +
        '<td class="num">' + lot.signed_qty + '</td>' +
        '<td class="num ' + (lot.available_qty < 0 ? 'check-bad' : '') + '">' + lot.available_qty + '</td>' +
        '<td class="num ' + (lot.shortage ? 'check-bad' : '') + '">' + lot.shortage + '</td>' +
        '<td>v' + lot.version + '</td>' +
        '<td><div class="inline-fix"><input type="number" min="0" step="1" data-correct="' + esc(lot.lot_id) +
          '" placeholder="新实称量"><button type="button" data-correct-btn="' + esc(lot.lot_id) + '">更正</button></div></td>' +
        '</tr>';
    }).join('') : '<tr><td class="empty" colspan="7">尚未登记父批，点击上方按钮从样例登记</td></tr>';
    Array.from(document.querySelectorAll('[data-correct-btn]')).forEach(function (button) {
      button.addEventListener('click', function () { correct(button.getAttribute('data-correct-btn')); });
    });
  }
  function renderDocs(data) {
    $('ledger-docs').innerHTML = data.docs.length ? data.docs.map(function (doc) {
      var lines = doc.lines.map(function (line) { return esc(line.lot_id) + ':' + line.qty; }).join('; ');
      var blocked = doc.blocked_by.length
        ? '<br><small class="check-bad">' + doc.blocked_by.map(function (b) {
            return esc(b.lot_id) + ' 缺 ' + b.missing;
          }).join('；') + '</small>' : '';
      var action = doc.status === 'SIGNED'
        ? '<a class="doc-evidence" href="/api/ledger/docs/' + encodeURIComponent(doc.doc_id) +
          '/evidence" target="_blank" rel="noopener">下载凭证</a>'
        : '<button type="button" data-sign="' + esc(doc.doc_id) + '"' + (doc.signable ? '' : ' disabled') +
          '>签发</button>' + blocked;
      return '<tr><td>' + esc(doc.doc_id) + '</td><td>' + esc(doc.kind) + '</td>' +
        '<td><span class="verdict" data-v="' + (doc.status === 'SIGNED' ? 'PASS' : 'REVIEW') + '">' +
        esc(doc.status) + '</span></td><td>' + lines + '</td><td class="num">' + doc.total_qty +
        '</td><td>' + (doc.signable ? '是' : '否') + '</td><td>' + action + '</td></tr>';
    }).join('') : '<tr><td class="empty" colspan="7">暂无单据</td></tr>';
    Array.from(document.querySelectorAll('[data-sign]')).forEach(function (button) {
      button.addEventListener('click', function () { sign(button.getAttribute('data-sign')); });
    });
  }
  async function refresh() {
    try {
      setNotice('');
      var data = await request('/api/ledger/workbench');
      renderLots(data);
      renderDocs(data);
    } catch (error) { setNotice(error.message); }
  }
  async function correct(lotId) {
    var input = document.querySelector('[data-correct="' + lotId + '"]');
    var value = Number(input.value);
    if (!Number.isInteger(value) || value < 0) { setNotice('更正量必须是非负整数'); return; }
    try {
      setNotice('');
      var result = await request('/api/ledger/lots/' + encodeURIComponent(lotId) + '/corrections', {
        method: 'POST', body: JSON.stringify({ new_qty: value })
      });
      input.value = '';
      renderLots(result.workbench);
      renderDocs(result.workbench);
    } catch (error) { setNotice(error.message); }
  }
  async function sign(docId) {
    try {
      setNotice('');
      await request('/api/ledger/docs/' + encodeURIComponent(docId) + '/sign', { method: 'POST' });
      refresh();
    } catch (error) { setNotice(error.message); await refresh(); }
  }
  async function createDoc() {
    var docId = $('doc-id').value.trim();
    var kind = $('doc-kind').value;
    var lines;
    try {
      if (!docId) throw new Error('请填写单号');
      lines = parseLines($('doc-lines').value);
      if (!lines.length) throw new Error('请填写占用明细');
      setNotice('');
      await request('/api/ledger/docs', { method: 'POST', body: JSON.stringify({
        doc_id: docId, kind: kind, lines: lines }) });
      $('doc-id').value = ''; $('doc-lines').value = '';
      refresh();
    } catch (error) { setNotice(error.message); }
  }
  async function seed() {
    try {
      setNotice('');
      var profile = $('profile') ? $('profile').value : 'clean';
      var result = await request('/api/ledger/seed', { method: 'POST', body: JSON.stringify({ profile: profile }) });
      setNotice('已登记 ' + result.seeded.length + ' 个父批' + (result.seeded.length ? '：' + result.seeded.join(', ') : ''));
      refresh();
    } catch (error) { setNotice(error.message); }
  }
  document.addEventListener('DOMContentLoaded', function () {
    $('ledger-refresh').addEventListener('click', refresh);
    $('ledger-seed').addEventListener('click', seed);
    $('doc-create').addEventListener('click', createDoc);
    refresh();
  });
})();

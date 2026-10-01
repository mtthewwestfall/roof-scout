/* GetVeridataNow frontend */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const GRADE_LABEL = {
    1: '1 · FAILING', 2: '2 · WORN', 3: '3 · AGING',
    4: '4 · HEALTHY', 5: '5 · SOLID', 0: '0 · CAN\u2019T TELL',
  };
  const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;')
    .replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  let pollTimer = null;

  function showErr(msg) {
    const e = $('formErr');
    e.textContent = msg;
    e.classList.remove('hidden');
  }

  async function startScan(ev) {
    ev.preventDefault();
    const zip = $('zipInput').value.replace(/\D/g, '').slice(0, 5);
    if (!/^\d{5}$/.test(zip)) { showErr('Enter a valid 5-digit US zip.'); return; }
    $('formErr').classList.add('hidden');
    $('results').classList.add('hidden');
    $('progress').classList.remove('hidden');
    $('scanBtn').disabled = true;
    setProgress(2, 'Starting…');
    try {
      const r = await fetch('/api/scan', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({zip, count: parseInt($('countSel').value, 10)}),
      });
      const d = await r.json();
      if (!d.ok) { showErr(d.error || 'Scan failed.'); resetBtn(); return; }
      if (d.cached) { renderResults(d.payload); return; }
      poll(d.job_id);
    } catch (e) {
      showErr('Couldn\u2019t reach the server. Try again.');
      resetBtn();
    }
  }

  function resetBtn() {
    $('scanBtn').disabled = false;
    $('progress').classList.add('hidden');
    clearInterval(pollTimer);
  }

  function setProgress(pct, msg) {
    $('pfill').style.width = Math.max(2, Math.min(100, pct)) + '%';
    $('pmsg').textContent = msg;
  }

  function poll(jobId) {
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      try {
        const r = await fetch('/api/scan/' + jobId);
        const d = await r.json();
        if (!d.ok) { showErr('Lost the scan. Try again.'); resetBtn(); return; }
        if (d.status === 'running') {
          const pct = d.total ? (d.done / d.total) * 100 : 5;
          // phase weighting: addresses ~35%, imagery ~30%, grading ~35%
          const w = d.phase === 'addresses' ? pct * 0.35
                  : d.phase === 'imagery' ? 35 + pct * 0.30
                  : d.phase === 'grading' ? 65 + pct * 0.35 : 5;
          setProgress(w, d.msg || 'Working…');
        } else if (d.status === 'done') {
          clearInterval(pollTimer);
          renderResults({zip: '', area: d.area, leads: d.leads});
        } else if (d.status === 'error') {
          clearInterval(pollTimer);
          showErr(d.error || 'Scan failed.');
          resetBtn();
        }
      } catch (e) { /* keep polling */ }
    }, 1500);
  }

  function renderResults(payload) {
    resetBtn();
    $('progress').classList.add('hidden');
    const leads = payload.leads || [];
    $('resTitle').textContent =
      (payload.area ? payload.area.split(',').slice(0, 2).join(',') : 'Results') +
      ` — ${leads.length} roofs graded`;
    const hot = leads.filter((l) => l.grade === 1 || l.grade === 2).length;
    $('resSub').textContent = hot
      ? `${hot} ${hot === 1 ? 'roof needs' : 'roofs need'} work soon — worst first.`
      : 'No urgent roofs in this batch — try more roofs or another zip.';
    const cards = $('cards');
    cards.innerHTML = '';
    leads.forEach((l) => {
      const card = document.createElement('div');
      card.className = 'card';
      const ev = (l.evidence || []).map((e) => `<li>${esc(e)}</li>`).join('');
      const loc = [l.city, l.state, l.postcode].filter(Boolean).join(', ');
      card.innerHTML =
        `<img src="${esc(l.img)}" alt="Aerial view of ${esc(l.address)}" loading="lazy">` +
        `<div class="card-body">` +
        `<div class="card-top"><span class="grade g${l.grade}">${GRADE_LABEL[l.grade] || ''}</span>` +
        `<span class="dim">${esc(l.confidence || '')} confidence</span></div>` +
        `<div class="addr">${esc(l.address)}</div>` +
        `<div class="sub">${esc(loc)}${l.county ? ' · ' + esc(l.county) + ' Co.' : ''}</div>` +
        (ev ? `<ul class="ev">${ev}</ul>` : '') +
        `<div class="links"><a href="${esc(l.maps_url)}" target="_blank" rel="noopener">Google Maps</a>` +
        `<a href="${esc(l.streetview_url)}" target="_blank" rel="noopener">Street View</a></div>` +
        `<button class="copybtn" data-addr="${esc(l.address + ', ' + loc)}">Copy address</button>` +
        `</div>`;
      cards.appendChild(card);
    });
    cards.querySelectorAll('.copybtn').forEach((b) =>
      b.addEventListener('click', () => {
        navigator.clipboard.writeText(b.dataset.addr).catch(() => {});
        b.textContent = 'Copied ✓';
        setTimeout(() => { b.textContent = 'Copy address'; }, 1500);
      }));
    $('results').classList.remove('hidden');
    $('results').scrollIntoView({behavior: 'smooth'});
  }

  $('scanForm').addEventListener('submit', startScan);
})();

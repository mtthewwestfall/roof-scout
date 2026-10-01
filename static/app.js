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
  let lastLeads = [];
  let activeTab = 'repair';

  // Company info for pitch drafts — remembered on this device.
  function company() {
    return {
      name: ($('coName').value || '').trim() || '[Your Company]',
      rep: ($('coRep').value || '').trim() || '[Your Name]',
      phone: ($('coPhone').value || '').trim() || '[Your Phone]',
    };
  }
  function bindCompany() {
    ['coName', 'coRep', 'coPhone'].forEach((id) => {
      const el = $(id);
      try { el.value = localStorage.getItem('gvn_' + id) || ''; } catch (e) {}
      el.addEventListener('input', () => {
        try { localStorage.setItem('gvn_' + id, el.value); } catch (e) {}
        // refresh open pitch drafts with the new details
        document.querySelectorAll('.pitchtext').forEach((t) => {
          if (!t.dataset.edited) t.value = buildPitch(JSON.parse(t.dataset.lead), company());
        });
      });
    });
  }

  // Damage bounding boxes over the aerial thumbnail (0-1000 normalized coords)
  function renderDamageOverlays(damageBoxes) {
    return (damageBoxes || []).map((b) => {
      const box = b.box || b.box_2d;
      if (!box || box.length !== 4) return '';
      const [ymin, xmin, ymax, xmax] = box;
      const top = (ymin / 1000) * 100;
      const left = (xmin / 1000) * 100;
      const height = ((ymax - ymin) / 1000) * 100;
      const width = ((xmax - xmin) / 1000) * 100;
      return `<div class="dmgbox" style="top:${top}%;left:${left}%;width:${width}%;height:${height}%;" title="${esc(b.label || 'damage')}"><span>${esc(b.label || 'damage')}</span></div>`;
    }).join('');
  }

  function exportCSV() {
    const leads = visibleLeads();
    if (!leads.length) return;
    const q = (s) => `"${String(s == null ? '' : s).replace(/"/g, '""')}"`;
    let csv = 'Address,City,State,ZIP,County,Grade,Material,Pitch,Evidence,Obstruction,MapsLink\n';
    leads.forEach((l) => {
      csv += [q(l.address), q(l.city), q(l.state), q(l.postcode), q(l.county),
              l.grade, q(l.material), q(l.pitch),
              q((l.evidence || []).join('; ')), q(l.obstruction),
              q(l.maps_url)].join(',') + '\n';
    });
    const blob = new Blob([csv], {type: 'text/csv'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `roof_leads_${activeTab}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  const GRADE_WORD = {1: 'FAILING', 2: 'WORN'};
  function buildPitch(l, co) {
    const ev = (l.evidence || []).map((e) => '- ' + e).join('\n');
    const loc = [l.address, l.city, l.state, l.postcode].filter(Boolean).join(', ');
    const specs = [l.material, l.pitch ? l.pitch + ' pitch' : ''].filter(Boolean).join(' · ');
    const verdict = l.grade === 1
      ? 'this roof is likely failing and should be replaced before the next major storm turns it into interior damage.'
      : 'this roof is worn and likely needs repair soon, before small problems become expensive ones.';
    return (
`Free roof inspection — ${loc}

Hi, I'm ${co.rep} with ${co.name} (${co.phone}).

We were reviewing recent aerial imagery of the roof at ${loc}${specs ? ` (appears to be ${specs})` : ''} and spotted signs it needs attention:
${ev || '- visible wear (see aerial photo)'}

Our roof grade: ${l.grade}/5 (${GRADE_WORD[l.grade] || ''}) — ${verdict}

I'd like to offer you a free, no-obligation roof inspection. Here's our standard checklist — edit it to fit your job:

[ ] Shingle condition: missing, cracked, lifted, or curling shingles
[ ] Granule loss, bald spots, and discoloration
[ ] Flashing around chimneys, vents, skylights, and valleys
[ ] Gutters, downspouts, and drainage
[ ] Fascia, soffit, and drip edge
[ ] Attic ventilation and daylight through the deck
[ ] Interior ceilings for water stains or active leaks
[ ] Photos of everything we find, plus a straight repair-vs-replace recommendation

No pressure — if the roof turns out fine, we'll tell you that too.

${co.name} · ${co.phone}`);
  }

  function showErr(msg) {
    const e = $('formErr');
    e.textContent = msg;
    e.classList.remove('hidden');
  }

  async function startScan(ev) {
    ev.preventDefault();
    const q = $('zipInput').value.trim();
    if (q.length < 3) { showErr('Enter a zip code or a street address.'); return; }
    $('formErr').classList.add('hidden');
    $('results').classList.add('hidden');
    $('progress').classList.remove('hidden');
    $('scanBtn').disabled = true;
    setProgress(2, 'Starting…');
    try {
      const r = await fetch('/api/scan', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({q, count: parseInt($('countSel').value, 10)}),
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

  function visibleLeads() {
    return activeTab === 'repair'
      ? lastLeads.filter((l) => l.grade === 1 || l.grade === 2)
      : lastLeads;
  }

  function renderCards() {
    const leads = visibleLeads();
    const cards = $('cards');
    cards.innerHTML = '';
    if (!leads.length && activeTab === 'repair') {
      cards.innerHTML = '<p class="dim">No roofs in this batch graded 1–2. Switch to “All roofs” to see the full scan.</p>';
    }
    leads.forEach((l) => {
      const card = document.createElement('div');
      card.className = 'card';
      const ev = (l.evidence || []).map((e) => `<li>${esc(e)}</li>`).join('');
      const loc = [l.city, l.state, l.postcode].filter(Boolean).join(', ');
      const bits = [];
      if (l.area) bits.push(esc(l.area));
      bits.push(esc(loc));
      if (l.county) bits.push(esc(l.county) + ' Co.');
      const isRepair = l.grade === 1 || l.grade === 2;
      const specs = [l.material, l.pitch].filter(Boolean).map(esc).join(' · ');
      const showBoxes = isRepair && (l.damage_boxes || []).length;
      card.innerHTML =
        `<div class="imgwrap"><img src="${esc(l.img)}" alt="Aerial view of ${esc(l.address)}" loading="lazy">` +
        (showBoxes ? renderDamageOverlays(l.damage_boxes) : '') + `</div>` +
        `<div class="card-body">` +
        `<div class="card-top"><span class="grade g${l.grade}">${GRADE_LABEL[l.grade] || ''}</span>` +
        `<span class="dim">${esc(l.confidence || '')} confidence</span></div>` +
        `<div class="addr">${esc(l.address)}</div>` +
        `<div class="sub">${bits.join(' · ')}</div>` +
        (specs ? `<div class="sub">${specs}</div>` : '') +
        (l.obstruction ? `<div class="sub dim">View note: ${esc(l.obstruction)}</div>` : '') +
        `<div class="sub dim">Public record: residential address via OpenStreetMap · ` +
        `Phone not publicly listed in free sources</div>` +
        (ev ? `<ul class="ev">${ev}</ul>` : '') +
        `<div class="links"><a href="${esc(l.maps_url)}" target="_blank" rel="noopener">Google Maps</a>` +
        `<a href="${esc(l.streetview_url)}" target="_blank" rel="noopener">Street View</a></div>` +
        `<button class="copybtn" data-addr="${esc(l.address + ', ' + loc)}">Copy address</button>` +
        (isRepair ? `<button class="pitchbtn">Draft pitch</button>
        <div class="pitch hidden"><textarea class="pitchtext" rows="16"></textarea>
        <div class="pitchrow"><button class="copypitch">Copy pitch</button>
        <span class="dim">Edit freely — drafts update when you change company info.</span></div></div>` : '') +
        `</div>`;
      cards.appendChild(card);
      const pitchBtn = card.querySelector('.pitchbtn');
      if (pitchBtn) {
        const panel = card.querySelector('.pitch');
        const ta = card.querySelector('.pitchtext');
        ta.dataset.lead = JSON.stringify({address: l.address, city: l.city,
          state: l.state, postcode: l.postcode, grade: l.grade, evidence: l.evidence,
          material: l.material, pitch: l.pitch});
        pitchBtn.addEventListener('click', () => {
          const opening = panel.classList.contains('hidden');
          panel.classList.toggle('hidden');
          if (opening && !ta.dataset.filled) {
            ta.value = buildPitch(l, company());
            ta.dataset.filled = '1';
          }
          pitchBtn.textContent = panel.classList.contains('hidden') ? 'Draft pitch' : 'Hide pitch';
        });
        ta.addEventListener('input', () => { ta.dataset.edited = '1'; });
        card.querySelector('.copypitch').addEventListener('click', (e) => {
          navigator.clipboard.writeText(ta.value).catch(() => {});
          e.target.textContent = 'Copied ✓';
          setTimeout(() => { e.target.textContent = 'Copy pitch'; }, 1500);
        });
      }
    });
    cards.querySelectorAll('.copybtn').forEach((b) =>
      b.addEventListener('click', () => {
        navigator.clipboard.writeText(b.dataset.addr).catch(() => {});
        b.textContent = 'Copied ✓';
        setTimeout(() => { b.textContent = 'Copy address'; }, 1500);
      }));
  }

  function setTab(tab) {
    activeTab = tab;
    $('tabRepair').classList.toggle('active', tab === 'repair');
    $('tabAll').classList.toggle('active', tab === 'all');
    renderCards();
  }

  function renderResults(payload) {
    resetBtn();
    $('progress').classList.add('hidden');
    lastLeads = payload.leads || [];
    $('resTitle').textContent =
      (payload.area ? payload.area.split(',').slice(0, 2).join(',') : 'Results') +
      ` — ${lastLeads.length} roofs graded`;
    const hot = lastLeads.filter((l) => l.grade === 1 || l.grade === 2).length;
    $('resSub').textContent = hot
      ? `${hot} ${hot === 1 ? 'roof needs' : 'roofs need'} work soon — worst first.`
      : 'No urgent roofs in this batch — try more roofs or another zip.';
    $('tabRepair').textContent = `🔨 Needs repair${hot ? ' (' + hot + ')' : ''}`;
    setTab(hot ? 'repair' : 'all');
    $('results').classList.remove('hidden');
    $('results').scrollIntoView({behavior: 'smooth'});
  }

  $('tabRepair').addEventListener('click', () => setTab('repair'));
  $('tabAll').addEventListener('click', () => setTab('all'));
  const csvBtn = document.createElement('button');
  csvBtn.id = 'csvBtn';
  csvBtn.className = 'tab';
  csvBtn.textContent = '⬇ Export CSV';
  csvBtn.addEventListener('click', exportCSV);
  $('tabAll').after(csvBtn);
  bindCompany();
  $('scanForm').addEventListener('submit', startScan);
})();

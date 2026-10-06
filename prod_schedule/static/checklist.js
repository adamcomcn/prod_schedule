// Inspection checklist on the inspection page (iPad on site, PC at the desk).
// Answers and photos are kept in a server draft; the report form gets the
// answers as JSON (checklist_json) when it is submitted.
(function () {
  var cfgEl = document.getElementById('checklist-config');
  var app = document.getElementById('checklist-app');
  if (!cfgEl || !app) return;
  var C = JSON.parse(cfgEl.textContent);
  var T = C.texts;
  var ZH = C.lang === 'zh';
  var state = {answers: C.answers || {}, files: C.files || {}, pending: 0};
  var collapsed = {};
  var saveTimer = null;

  // ── helpers ────────────────────────────────────────────────────────────
  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (k.slice(0, 2) === 'on') n[k] = v;
      else if (k === 'checked') n.checked = !!v;
      else if (k === 'value') n.value = v == null ? '' : v;
      else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v);
    });
    (kids || []).forEach(function (c) { if (c) n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
    return n;
  }
  function texts(obj, key) {
    var en = obj[key] || '', zh = obj[key + '_zh'] || '';
    return ZH ? [zh || en, zh ? en : ''] : [en, zh];
  }
  function ans(qid) { return state.answers[qid] || (state.answers[qid] = {}); }
  function num(v) { var n = parseFloat(v); return isNaN(n) ? null : n; }
  function evaluate(q, value) {
    var v = value == null ? '' : String(value).trim().toLowerCase();
    if (!v) return '';
    if (v === 'na') return 'na';
    if (q.type === 'text') return 'ok';
    if (q.type === 'yes_no') return (v === 'yes' || v === 'no') ? (v === (q.fail_on || 'no') ? 'fail' : 'ok') : '';
    if (q.type === 'rating') {
      if (['good', 'fair', 'poor'].indexOf(v) < 0) return '';
      return (q.fail_on === 'fair' ? ['fair', 'poor'] : ['poor']).indexOf(v) >= 0 ? 'fail' : 'ok';
    }
    var n = num(v);
    if (n === null) return '';
    if (q.min != null && n < q.min) return 'fail';
    if (q.max != null && n > q.max) return 'fail';
    return 'ok';
  }
  function eachQuestion(fn) {
    C.data.sections.forEach(function (s) { s.questions.forEach(function (q, i) { fn(s, q, i + 1); }); });
  }
  function photoCount(ref) { return (state.files[ref] || []).length; }
  function needsPhoto(q, result) { return result === 'fail' || (q.photo === 'always' && result !== 'na'); }

  // ── photos / videos ────────────────────────────────────────────────────
  function upload(ref, file) {
    state.pending++;
    renderPhotos(ref);
    var prepare = (window.compressPhoto && /^image\//.test(file.type)) ? window.compressPhoto(file) : Promise.resolve(file);
    prepare.then(function (ready) {
      var data = new FormData();
      data.append('ref', ref);
      data.append('file', ready, ready.name);
      data.append('template_id', C.template_id);
      data.append('version', C.version);
      var xhr = new XMLHttpRequest();
      xhr.open('POST', C.urls.upload);
      xhr.setRequestHeader('X-CSRF-Token', C.csrf);
      xhr.onload = function () {
        var res = null;
        try { res = JSON.parse(xhr.responseText); } catch (e) {}
        if (xhr.status === 200 && res && res.ok) {
          (state.files[ref] = state.files[ref] || []).push(res.file);
          savedLabel(T.saved_now);
        } else {
          alert(T.upload_failed + ' ' + ((res && res.message) || xhr.status));
        }
        done();
      };
      xhr.onerror = function () { alert(T.upload_failed); done(); };
      xhr.send(data);
    });
    function done() { state.pending--; renderPhotos(ref); refresh(); }
  }

  function removeFile(ref, f) {
    if (!confirm(T.delete_photo)) return;
    fetch(C.urls.delete.replace('/0/delete', '/' + f.id + '/delete'),
          {method: 'POST', headers: {'X-CSRF-Token': C.csrf}, credentials: 'same-origin'})
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (!res.ok) return;
        state.files[ref] = (state.files[ref] || []).filter(function (x) { return x.id !== f.id; });
        renderPhotos(ref);
        refresh();
      });
  }

  function picker(ref, accept, capture, label, icon) {
    var input = el('input', {type: 'file', accept: accept, capture: capture, multiple: capture ? null : 'multiple',
                             style: 'display:none', onchange: function () {
      Array.prototype.slice.call(this.files || []).forEach(function (f) { upload(ref, f); });
      this.value = '';
    }});
    var btn = el('button', {type: 'button', 'class': 'ck-pbtn', onclick: function () { input.click(); }}, [icon + ' ' + label]);
    return [btn, input];
  }

  function photoBlock(ref, hint) {
    var box = el('div', {'class': 'ck-photos', id: 'ck-ph-' + ref});
    fillPhotos(box, ref, hint);
    return box;
  }
  function fillPhotos(box, ref, hint) {
    box.innerHTML = '';
    if (hint) box.appendChild(el('div', {'class': 'ck-hint'}, [hint]));
    var thumbs = el('div', {'class': 'ck-thumbs'});
    (state.files[ref] || []).forEach(function (f) {
      var media = f.video ? el('div', {'class': 'ck-video'}, ['🎥']) : el('img', {src: f.url, alt: f.name, loading: 'lazy'});
      thumbs.appendChild(el('div', {'class': 'ck-thumb'}, [
        el('a', {href: f.url, target: '_blank'}, [media]),
        el('button', {type: 'button', 'class': 'ck-del', title: T.delete, onclick: function () { removeFile(ref, f); }}, ['✕'])
      ]));
    });
    box.appendChild(thumbs);
    var row = el('div', {'class': 'ck-prow'});
    picker(ref, 'image/*', 'environment', T.take_photo, '📷').forEach(function (n) { row.appendChild(n); });
    picker(ref, 'video/*', 'environment', T.record_video, '🎥').forEach(function (n) { row.appendChild(n); });
    picker(ref, 'image/*,video/*', null, T.from_gallery, '🖼').forEach(function (n) { row.appendChild(n); });
    if (state.pending) row.appendChild(el('span', {'class': 'ck-uploading'}, ['⏳ ' + T.uploading]));
    box.appendChild(row);
  }
  function renderPhotos(ref) {
    var box = document.getElementById('ck-ph-' + ref);
    if (box) fillPhotos(box, ref, box.firstChild && box.firstChild.className === 'ck-hint' ? box.firstChild.textContent : '');
  }

  // ── questions ──────────────────────────────────────────────────────────
  function choice(s, q, n, value, text, cls) {
    var a = ans(q.id);
    var on = String(a.v || '').toLowerCase() === value;
    return el('button', {type: 'button', 'class': 'ck-btn' + (on ? ' on ' + cls : ''), onclick: function () {
      a.v = on ? '' : value;              // tap again to clear
      rerender(s, q, n);
    }}, [text]);
  }

  function questionNode(s, q, n) {
    var a = ans(q.id);
    var result = evaluate(q, a.v);
    var t = texts(q, 'text');
    var hint = texts(q, 'hint');
    var controls = el('div', {'class': 'ck-ctrl'});
    function cls(v) { return evaluate(q, v) === 'fail' ? 'fail' : 'ok'; }
    if (q.type === 'yes_no') {
      controls.appendChild(choice(s, q, n, 'yes', T.yes, cls('yes')));
      controls.appendChild(choice(s, q, n, 'no', T.no, cls('no')));
    } else if (q.type === 'rating') {
      [['good', T.good], ['fair', T.fair], ['poor', T.poor]].forEach(function (o) {
        controls.appendChild(choice(s, q, n, o[0], o[1], cls(o[0])));
      });
    } else if (q.type === 'text') {
      controls.appendChild(el('input', {type: 'text', 'class': 'ck-textans', value: a.v === 'na' ? '' : (a.v || ''),
                                        placeholder: T.text_placeholder, oninput: function () {
        a.v = this.value;
        refresh();
        changed();
      }}));
    } else {
      var badge = el('span', {'class': 'ck-badge ck-badge-' + (result || 'empty')},
                     [result === 'ok' ? T.ok : result === 'fail' ? T.fail : '']);
      var box = el('input', {type: 'number', step: 'any', inputmode: 'decimal', 'class': 'ck-num',
                             value: a.v === 'na' ? '' : (a.v || ''), placeholder: q.unit || '',
                             oninput: function () {
                               a.v = this.value;
                               var r = evaluate(q, a.v);
                               badge.className = 'ck-badge ck-badge-' + (r || 'empty');
                               badge.textContent = r === 'ok' ? T.ok : r === 'fail' ? T.fail : '';
                               changed();
                             },
                             onchange: function () { rerender(s, q, n); }});
      controls.appendChild(box);
      controls.appendChild(el('span', {'class': 'ck-unit'}, [q.unit || '']));
      controls.appendChild(badge);
    }
    if (q.optional || s.optional) controls.appendChild(choice(s, q, n, 'na', T.na, 'na'));

    var limit = q.type === 'number'
      ? (q.min != null ? '≥ ' + q.min : '') + (q.max != null ? ' ≤ ' + q.max : '') + ' ' + (q.unit || '') : '';
    var node = el('div', {'class': 'ck-q ck-' + (result || 'empty'), id: 'ck-q-' + q.id}, [
      el('div', {'class': 'ck-qhead'}, [
        el('div', {'class': 'ck-text'}, [
          el('span', {'class': 'ck-n'}, [n + '.']), t[0],
          q.photo === 'always' ? el('span', {'class': 'ck-tag'}, ['📷 ' + T.photo_required]) : null,
          limit.trim() ? el('span', {'class': 'ck-tag'}, [limit.trim()]) : null,
          t[1] ? el('div', {'class': 'ck-sub'}, [t[1]]) : null,
          hint[0] ? el('div', {'class': 'ck-hintline'}, ['⏱ ' + hint[0] + (hint[1] ? ' / ' + hint[1] : '')]) : null
        ]),
        controls
      ])
    ]);
    if (result === 'fail') {
      var act = texts(q, 'action');
      node.appendChild(el('div', {'class': 'ck-failbox'}, [
        el('div', {'class': 'ck-action'}, [T.action + ': ' + act[0] + (act[1] ? ' / ' + act[1] : '')]),
        el('div', {'class': 'ck-failrow'}, [
          el('label', {}, [T.occurrences + ' ', el('input', {type: 'number', min: '0', inputmode: 'numeric', 'class': 'ck-occ',
            value: a.occ || '', oninput: function () { a.occ = this.value; changed(); }})]),
          el('label', {'class': 'ck-sup'}, [el('input', {type: 'checkbox', checked: a.sup,
            onchange: function () { a.sup = this.checked; changed(); }}), ' ' + T.supervisor])
        ]),
        el('input', {type: 'text', 'class': 'ck-note', placeholder: T.note, value: a.note || '',
                     oninput: function () { a.note = this.value; changed(); }})
      ]));
    }
    if (needsPhoto(q, result)) node.appendChild(photoBlock(q.id, result === 'fail' ? T.fail_photo_hint : T.photo_hint));
    return node;
  }

  function rerender(s, q, n) {
    var old = document.getElementById('ck-q-' + q.id);
    if (old) old.replaceWith(questionNode(s, q, n));
    refresh();
    changed();
  }

  // ── sections ───────────────────────────────────────────────────────────
  function sectionStats(s) {
    var done = 0, fail = 0;
    s.questions.forEach(function (q) {
      var r = evaluate(q, ans(q.id).v);
      if (r) done++;
      if (r === 'fail') fail++;
    });
    return {done: done, fail: fail, total: s.questions.length};
  }

  function sectionNode(s) {
    var t = texts(s, 'name');
    var st = sectionStats(s);
    if (collapsed[s.id] === undefined) collapsed[s.id] = st.done === st.total && !st.fail;
    var body = el('div', {'class': 'ck-body', style: collapsed[s.id] ? 'display:none' : ''},
                  s.questions.map(function (q, i) { return questionNode(s, q, i + 1); }));
    var head = el('div', {'class': 'ck-shead'}, [
      el('button', {type: 'button', 'class': 'ck-stitle', onclick: function () {
        collapsed[s.id] = !collapsed[s.id];
        body.style.display = collapsed[s.id] ? 'none' : '';
        this.querySelector('.ck-caret').textContent = collapsed[s.id] ? '▸' : '▾';
      }}, [el('span', {'class': 'ck-caret'}, [collapsed[s.id] ? '▸' : '▾']), ' ' + t[0],
           t[1] ? el('span', {'class': 'ck-sub'}, [' ' + t[1]]) : null,
           s.optional ? el('span', {'class': 'ck-tag'}, [T.if_applicable]) : null]),
      el('span', {'class': 'ck-sprog', id: 'ck-sp-' + s.id}),
      el('button', {type: 'button', 'class': 'ck-quick', onclick: function () { quickOk(s); }}, [T.rest_ok]),
      s.optional ? el('button', {type: 'button', 'class': 'ck-quick', onclick: function () { sectionNa(s); }}, [T.section_na]) : null
    ]);
    return el('div', {'class': 'ck-sec', id: 'ck-sec-' + s.id}, [head, body]);
  }

  function quickOk(s) {
    var skipped = 0;
    s.questions.forEach(function (q) {
      var a = ans(q.id);
      if (evaluate(q, a.v)) return;
      if (q.type === 'yes_no') a.v = q.fail_on === 'yes' ? 'no' : 'yes';
      else if (q.type === 'rating') a.v = 'good';
      else skipped++;                      // measurements need a real value
    });
    redrawSection(s);
    if (skipped) alert(T.measure_needed);
  }
  function sectionNa(s) {
    if (!confirm(T.confirm_section_na)) return;
    s.questions.forEach(function (q) { ans(q.id).v = 'na'; });
    redrawSection(s);
  }
  function redrawSection(s) {
    collapsed[s.id] = false;
    document.getElementById('ck-sec-' + s.id).replaceWith(sectionNode(s));
    refresh();
    changed();
  }

  // ── progress, suggestion, saving ───────────────────────────────────────
  function refresh() {
    var done = 0, total = 0, fail = 0;
    C.data.sections.forEach(function (s) {
      var st = sectionStats(s);
      done += st.done; total += st.total; fail += st.fail;
      var sp = document.getElementById('ck-sp-' + s.id);
      if (sp) {
        sp.textContent = st.done + ' / ' + st.total + (st.fail ? ' · ' + st.fail + ' ' + T.fail : '');
        sp.className = 'ck-sprog' + (st.fail ? ' bad' : st.done === st.total ? ' good' : '');
      }
    });
    var prog = document.getElementById('ck-progress');
    if (prog) prog.textContent = done + ' / ' + total + (fail ? ' · ' + fail + ' ' + T.fail : '');
    if (window.ckSuggest) window.ckSuggest(suggestion(done === total));
  }
  function suggestion(complete) {
    var failed = [];
    eachQuestion(function (s, q) { if (evaluate(q, ans(q.id).v) === 'fail') failed.push(q); });
    if (failed.some(function (q) { return /reject/i.test(q.action || ''); })) return 'Fail';
    if (failed.length) return 'Partial Pass';
    return complete ? 'Pass' : '';
  }

  function savedLabel(text) {
    var s = document.getElementById('ck-saved');
    if (s) s.textContent = text;
  }
  function changed() {
    savedLabel(T.unsaved);
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 1500);
  }
  function save() {
    clearTimeout(saveTimer);
    saveTimer = null;
    var body = {template_id: C.template_id, version: C.version, answers: state.answers,
                fields: window.formSnapshot ? window.formSnapshot() : {}};
    return fetch(C.urls.save, {method: 'POST', credentials: 'same-origin',
                               headers: {'Content-Type': 'application/json', 'X-CSRF-Token': C.csrf},
                               body: JSON.stringify(body)})
      .then(function (r) { return r.json(); })
      .then(function (res) {
        var d = res.saved_at ? new Date(res.saved_at) : new Date();
        savedLabel(T.saved + ' ' + String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'));
      })
      .catch(function () { savedLabel(T.save_failed); });
  }
  window.ckSaveNow = save;
  window.ckChanged = changed;
  window.addEventListener('pagehide', function () { if (saveTimer) save(); });

  // ── before submit ──────────────────────────────────────────────────────
  function problems() {
    var list = [];
    if (C.data.product_photo && !photoCount('product')) list.push({ref: 'product', why: T.product_photo_missing});
    eachQuestion(function (s, q, n) {
      var r = evaluate(q, ans(q.id).v);
      var name = texts(s, 'name')[0] + ' ' + n;
      if (!r) list.push({ref: q.id, why: name + ': ' + T.unanswered, section: s});
      else if (needsPhoto(q, r) && !photoCount(q.id)) list.push({ref: q.id, why: name + ': ' + T.photo_needed, section: s});
    });
    return list;
  }
  window.ckBusy = function () { return state.pending > 0; };
  window.ckCheck = function () {
    document.getElementById('checklist-json').value = JSON.stringify(state.answers);
    document.querySelectorAll('.ck-missing').forEach(function (n) { n.classList.remove('ck-missing'); });
    if (state.pending) { alert(T.wait_uploads); return false; }
    var list = problems();
    if (!list.length) return true;
    list.forEach(function (p) {
      if (p.section && collapsed[p.section.id]) {
        collapsed[p.section.id] = false;
        var body = document.querySelector('#ck-sec-' + p.section.id + ' .ck-body');
        if (body) body.style.display = '';
      }
      var node = document.getElementById(p.ref === 'product' ? 'ck-product' : 'ck-q-' + p.ref);
      if (node) node.classList.add('ck-missing');
    });
    var first = document.querySelector('.ck-missing');
    if (first) first.scrollIntoView({behavior: 'smooth', block: 'center'});
    save();
    alert(T.incomplete.replace('{n}', list.length) + '\n\n• ' +
          list.slice(0, 8).map(function (p) { return p.why; }).join('\n• ') + (list.length > 8 ? '\n…' : ''));
    return false;
  };

  // ── first render ───────────────────────────────────────────────────────
  if (C.data.product_photo) {
    app.appendChild(el('div', {'class': 'ck-card', id: 'ck-product'}, [
      el('div', {'class': 'ck-ptitle'}, ['📷 ' + T.product_photo]),
      photoBlock('product', T.product_photo_hint)
    ]));
  }
  C.data.sections.forEach(function (s) { app.appendChild(sectionNode(s)); });
  refresh();
})();

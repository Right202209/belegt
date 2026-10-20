// Spec: 08 §6.2（离线草稿/自动保存）、§6.4（即时校验提示，不阻止提交）。零构建、无第三方依赖；无 JS 时表单照常工作。
(function () {
  "use strict";
  var form = document.getElementById("draft-form");
  if (!form || !window.fetch) return;
  var api = form.getAttribute("data-api");
  var status = document.getElementById("autosave");
  var timer = null;
  var storeKey = "belegt-draft:" + api;

  function collect() {
    var values = {};
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!el.name || el.disabled) return;
      var m = /^(v|u|n)__(.+)$/.exec(el.name);
      if (!m) return;
      var field = { v: "value", u: "unit", n: "note" }[m[1]];
      values[m[2]] = values[m[2]] || {};
      values[m[2]][field] = el.value;
    });
    return values;
  }

  function showHints(hints) {
    Array.prototype.forEach.call(document.querySelectorAll(".js-hints"), function (p) { p.textContent = ""; });
    (hints || []).forEach(function (h) {
      if (!h.field) return;
      var p = document.querySelector('.js-hints[data-for="' + CSS.escape(h.field) + '"]');
      if (p) { p.textContent = "⚠ " + h.message; p.className = "js-hints hint " + h.severity; }
    });
  }

  function save() {
    var versionEl = form.elements["draft_version"];
    var body = { draft_version: parseInt(versionEl.value || "0", 10), values: collect() };
    try { localStorage.setItem(storeKey, JSON.stringify(body.values)); } catch (e) { /* 离线草稿尽力而为 */ }
    fetch(api, { method: "PUT", headers: { "Content-Type": "application/json" }, credentials: "same-origin", body: JSON.stringify(body) })
      .then(function (r) {
        if (r.status === 409) { status.textContent = form.getAttribute("data-conflict"); return null; }
        return r.ok ? r.json() : null;
      })
      .then(function (d) {
        if (!d) return;
        versionEl.value = d.draft_version;
        status.textContent = form.getAttribute("data-saved") + " ✓";
        showHints(d.hints);
      })
      .catch(function () { status.textContent = "offline – saved locally"; });
  }

  form.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(save, 1200);
  });
})();

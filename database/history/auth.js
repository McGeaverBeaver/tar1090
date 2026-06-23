// Report-site auth glue. Asks the backend who we are (/api/me) and adapts the UI:
//  - shows a "name · role" chip + Logout link in the header when OIDC is on
//  - for the "viewer" role, removes everything marked .admin-only (Alerts/Settings tabs,
//    the alert button, ...). This is cosmetic only -- the server independently blocks
//    viewers from the admin APIs and pages.
(function () {
  function applyViewer() {
    document.body.classList.add('role-viewer');
    // Hide via CSS (don't remove) so the page's own code can still reference these elements.
    if (!document.getElementById('rv-style')) {
      var st = document.createElement('style');
      st.id = 'rv-style';
      st.textContent = '.role-viewer .admin-only{display:none!important}';
      (document.head || document.documentElement).appendChild(st);
    }
  }
  // Hide admin-only chrome immediately if a previous answer is cached, to avoid a flash.
  try { if (sessionStorage.getItem('tar1090_role') === 'viewer') applyViewer(); } catch (e) {}

  fetch('api/me', { credentials: 'same-origin' })
    .then(function (r) { return r.json(); })
    .then(function (me) {
      try { sessionStorage.setItem('tar1090_role', me.role || ''); } catch (e) {}
      if (me.role === 'viewer') applyViewer();

      if (me.enabled && me.authenticated) {
        var tabs = document.getElementById('tabs');
        if (tabs && !document.getElementById('whoami')) {
          var chip = document.createElement('span');
          chip.id = 'whoami';
          chip.style.cssText = 'margin-left:auto;display:flex;align-items:center;gap:8px;font-size:12px;color:#9aa3b2;';
          var who = me.name || me.email || 'signed in';
          chip.innerHTML = '<span>' + who + ' · <b style="color:#cdd5e0">' + (me.role || '') + '</b></span>'
            + '<a href="oidc/logout" style="color:#9aa3b2;text-decoration:none;border:1px solid #313845;'
            + 'border-radius:6px;padding:3px 8px">Logout</a>';
          tabs.style.flex = '1';
          tabs.appendChild(chip);
        }
      }
    })
    .catch(function () {});
})();

/* Login state and authenticated requests for the existing workbench. */
(() => {
  'use strict';
  const nativeFetch = window.fetch.bind(window);
  const auth = {canWrite:false, enabled:true, csrfToken:'', ready:null};
  window.EvolvAuth = auth;

  auth.fetch = async (path, options = {}) => {
    const headers = new Headers(options.headers || {});
    const method = (options.method || 'GET').toUpperCase();
    if (!['GET','HEAD','OPTIONS'].includes(method) && auth.csrfToken) {
      headers.set('X-CSRF-Token', auth.csrfToken);
    }
    const response = await nativeFetch(path, {...options, headers, credentials:'same-origin'});
    if (response.status === 401) {
      window.location.replace('/login');
      throw new Error('登录已过期，请重新登录。');
    }
    return response;
  };

  function showIdentity(me) {
    const host = document.querySelector('[data-auth-status]');
    if (!host) return;
    host.replaceChildren();
    if (!me.enabled) { host.hidden = true; return; }
    host.hidden = false;
    const name = document.createElement('span');
    name.textContent = `${me.name} · ${me.can_write ? '可编辑' : '只读'}`;
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = '退出登录';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const response = await auth.fetch('/auth/logout', {method:'POST'});
        if (!response.ok) throw new Error();
        window.location.replace('/login');
      } catch { button.disabled = false; button.textContent = '退出失败，重试'; }
    });
    host.append(name, button);
  }

  auth.ready = (async () => {
    const response = await nativeFetch('/api/auth/me', {cache:'no-store', credentials:'same-origin'});
    if (!response.ok) throw new Error('暂时无法读取登录状态，请刷新重试。');
    const me = await response.json();
    if (me.enabled && !me.authenticated) {
      window.location.replace('/login');
      throw new Error('请先通过飞书登录。');
    }
    auth.canWrite = me.can_write === true;
    auth.enabled = me.enabled;
    auth.csrfToken = me.csrf_token || '';
    showIdentity(me);
    return me;
  })();
  // A page restored from browser history must recheck its session and role.
  window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
})();

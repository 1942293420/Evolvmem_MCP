/* Login state and authenticated requests for the existing workbench. */
(() => {
  'use strict';
  const nativeFetch = window.fetch.bind(window);
  const auth = {canWrite:false, enabled:true, csrfToken:'', ready:null};
  window.EvolvAuth = auth;
  auth.fetch = async (path, options = {}) => {
    const headers = new Headers(options.headers || {});
    if (!['GET','HEAD','OPTIONS'].includes((options.method || 'GET').toUpperCase()) && auth.csrfToken) headers.set('X-CSRF-Token', auth.csrfToken);
    const response = await nativeFetch(path, {...options, headers, credentials:'same-origin'});
    if (response.status === 401) { window.location.replace('/login'); throw new Error('登录已过期，请重新登录。'); }
    return response;
  };
  auth.ready = nativeFetch('/api/auth/me', {cache:'no-store', credentials:'same-origin'}).then(async response => {
    if (!response.ok) throw new Error('暂时无法读取登录状态，请刷新重试。');
    const me = await response.json();
    if (me.enabled && !me.authenticated) { window.location.replace('/login'); throw new Error('请先通过飞书登录。'); }
    auth.canWrite = me.can_write === true; auth.enabled = me.enabled; auth.csrfToken = me.csrf_token || '';
    return me;
  });
  window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
})();

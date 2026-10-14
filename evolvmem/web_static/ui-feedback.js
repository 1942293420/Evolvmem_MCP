/* Feedback belongs to a user action, never to background polling requests. */
window.EvolvUI = {
  busy(button, label) {
    const children=[...button.childNodes], disabled=button.disabled;
    const title=button.textContent;
    label ||= /保存|入库|归档|归属|更改/.test(title)?'正在保存…':/预览|比较|提取|整理/.test(title)?'正在处理…':/创建|新增/.test(title)?'正在创建…':/删除/.test(title)?'正在删除…':'正在读取…';
    const status=document.createElement('span');status.textContent=label;status.setAttribute('role','status');
    button.replaceChildren(status);button.setAttribute('aria-busy','true');button.disabled=true;
    return ()=>{button.replaceChildren(...children);button.removeAttribute('aria-busy');button.disabled=disabled;};
  },
  async saved(message, refresh, notify, onRefreshFailed=()=>{}) {
    notify(message);
    try { await refresh(); }
    catch { onRefreshFailed();notify(message+' 列表暂未刷新，请点击刷新重试，无需重复保存。'); }
  }
};
